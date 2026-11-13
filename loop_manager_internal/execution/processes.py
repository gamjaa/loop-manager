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
    VerifyTerminationUncertain,
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


def run(arguments,cwd,timeout,*,input_text='',environment=None,output_path=None):
    if timeout <= 0:
        raise ValueError('실행 timeout은 양수여야 합니다.')
    started = time.monotonic()
    job,process = None,None
    argv = list(arguments)
    if sys.platform.startswith('linux'):
        argv = [sys.executable,'-I','-c',_LINUX_SUBREAPER.replace('stdin=subprocess.DEVNULL','stdin=sys.stdin'),*argv]
    elif os.name=='nt':
        from loop_manager_internal.execution.windows_job import WindowsJob
        job = WindowsJob()
        argv = [sys.executable,'-I','-c',_WINDOWS_START,*argv]
    with tempfile.TemporaryFile() as output:
        try:
            process = subprocess.Popen(argv,cwd=cwd,env=environment,stdin=subprocess.PIPE,
                                       stdout=output,stderr=subprocess.STDOUT,
                                       **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt'
                                          else {'start_new_session':True}))
            if job:
                job.assign(process)
            data = (b'\0' if job else b'')+input_text.encode('utf-8')
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
