"""Windows Agent runtime의 ACL 확인과 자식 프로세스 소유권."""
import base64
import os
import subprocess
import sys
import threading

from loop_manager_internal.execution.windows_job import WindowsJob
from loop_manager_internal.verification.guard import check_command


def assert_private_directory(path):
    # 사용자 설정·프로필을 실행하지 않고 SID와 유효 allow ACE를 검사한다.
    script = r"""
$ErrorActionPreference = 'Stop'
$sidType = [System.Security.Principal.SecurityIdentifier]
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$acl = Get-Acl -LiteralPath $env:LOOP_MANAGER_RUNTIME_PATH
if ($acl.GetOwner($sidType).Value -notin @($user, 'S-1-5-32-544')) { throw 'owner' }
foreach ($rule in $acl.GetAccessRules($true, $true, $sidType)) {
    if ($rule.AccessControlType -eq 'Allow' -and
        $rule.IdentityReference.Value -notin @($user, 'S-1-3-4', 'S-1-5-18', 'S-1-5-32-544') -and
        ([int]$rule.FileSystemRights -band 2032127)) { throw 'shared runtime ACL' }
}
"""
    environment = {key: value for key, value in os.environ.items() if key.upper() != 'PSMODULEPATH'}
    environment['LOOP_MANAGER_RUNTIME_PATH'] = str(path)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
         base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
        env=environment,
        capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError("Codex runtime 경로의 Windows 소유권·ACL을 확인할 수 없습니다.")


_BOOTSTRAP = """\
import os, subprocess, sys
if os.read(0, 1) != b'\\0':
    raise SystemExit(2)
raise SystemExit(subprocess.call(sys.argv[1:]))
"""


def start(arguments, launch_process, **kwargs):
    """Job에 등록되기 전에는 Agent 또는 도구를 시작하지 않는다."""
    check_command(arguments)
    job = WindowsJob()
    process = None
    try:
        process = launch_process([sys.executable, "-I", "-c", _BOOTSTRAP, *arguments],
                                 stdin=subprocess.PIPE, **kwargs)
        job.assign(process)
        process._loop_manager_job = job
        process._loop_manager_stop_lock = threading.Lock()
        process._loop_manager_stop_error = None
        process.stdin.write("\0" if kwargs.get("text") else b"\0")
        process.stdin.flush()
        return process
    except BaseException:
        try:
            if process is not None:
                process.kill()
                process.wait(timeout=10)
        finally:
            job.close()
        raise


def stop(process):
    with process._loop_manager_stop_lock:
        job = process._loop_manager_job
        if job is None:
            if process._loop_manager_stop_error is not None:
                raise process._loop_manager_stop_error
            return
        try:
            job.terminate()
            process.wait(timeout=10)
            job.wait(10)
        except BaseException as error:
            process._loop_manager_stop_error = error
            raise
        finally:
            job.close()
            process._loop_manager_job = None
