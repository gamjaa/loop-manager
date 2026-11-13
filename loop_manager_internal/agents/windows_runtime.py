"""Windows Provider 홈의 소유자와 ACL 확인."""
import base64
import os
import subprocess


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

