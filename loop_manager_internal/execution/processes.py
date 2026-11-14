"""Agent와 프로젝트 명령의 제한 시간·하위 프로세스 종료 경계."""
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from loop_manager_internal.execution.verify_process import (
    _LINUX_SUBREAPER, _confirm_posix_group_stopped, terminate_verify_process,
    VerifyTerminationUncertain, _linux_process_info,
)


@dataclass(frozen=True)
class ProcessResult:
    code: int
    timed_out: bool
    output: str


_WINDOWS_START = """\
import os, subprocess, sys
if os.read(0,1) != b'\\0':
    raise SystemExit(2)
raise SystemExit(subprocess.call(sys.argv[1:], stdin=sys.stdin))
"""


def process_identity(pid):
    if os.name=='nt':
        from loop_manager_internal.execution.windows_job import process_identity as windows_identity
        value = windows_identity(pid)
        return str(value) if value is not None else None
    if sys.platform.startswith('linux'):
        info = _linux_process_info(pid)
        if info is None or info[2]=='Z':
            return None
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()+':'+str(info[3])
    result = subprocess.run(['ps','-o','lstart=','-p',str(pid)],capture_output=True,text=True,check=False)
    return result.stdout.strip() or None


def still_running(pid,identity):
    if not pid:
        return False
    current = process_identity(pid)
    return current is not None and current==identity


def run(arguments,cwd,timeout,*,input_text='',environment=None,output_path=None,on_started=None,on_stopped=None):
    if timeout <= 0:
        raise ValueError('실행 timeout은 양수여야 합니다.')
    started = time.monotonic()
    job,process = None,None
    argv = list(arguments)
    if sys.platform.startswith('linux'):
        supervisor = _LINUX_SUBREAPER.replace('verify = subprocess.Popen',
            "if os.read(0,1) != b'\\0':\n    raise SystemExit(2)\nverify = subprocess.Popen")
        argv = [sys.executable,'-I','-c',supervisor.replace('stdin=subprocess.DEVNULL','stdin=sys.stdin'),*argv]
    elif os.name=='nt':
        from loop_manager_internal.execution.windows_job import WindowsJob
        job = WindowsJob()
        argv = [sys.executable,'-I','-c',_WINDOWS_START,*argv]
    else:
        argv = [sys.executable,'-I','-c',_WINDOWS_START,*argv]
    with tempfile.TemporaryFile() as output:
        try:
            process = subprocess.Popen(argv,cwd=cwd,env=environment,stdin=subprocess.PIPE,
                                       stdout=output,stderr=subprocess.STDOUT,
                                       **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt'
                                          else {'start_new_session':True}))
            if job:
                job.assign(process)
            if on_started:
                on_started(process.pid,process_identity(process.pid))
            data = b'\0'+input_text.encode('utf-8')
            timed_out = False
            try:
                process.communicate(data,timeout=timeout)
                remaining = max(0.001,timeout-(time.monotonic()-started))
                if job:
                    job.wait(remaining)
                elif not sys.platform.startswith('linux'):
                    _confirm_posix_group_stopped(process.pid)
            except (subprocess.TimeoutExpired,TimeoutError):
                timed_out = True
                if job:
                    job.terminate()
                    process.wait(timeout=10)
                    job.wait(10)
                else:
                    terminate_verify_process(process)
            if on_stopped:
                on_stopped()
            output.seek(0)
            if output_path:
                destination = Path(output_path)
                with destination.open('wb') as target:
                    while chunk := output.read(1024*1024):
                        target.write(chunk)
            output.seek(0,os.SEEK_END)
            output.seek(max(0,output.tell()-64000))
            return ProcessResult(process.returncode,timed_out,output.read().decode('utf-8',errors='replace'))
        except BaseException:
            if process is not None and process.poll() is None:
                try:
                    if job:
                        job.terminate()
                        process.wait(timeout=10)
                        job.wait(10)
                    else:
                        terminate_verify_process(process)
                except BaseException as cleanup:
                    raise VerifyTerminationUncertain(f'하위 프로세스 종료 확인 실패: {cleanup}') from cleanup
            raise
        finally:
            if job:
                job.close()
