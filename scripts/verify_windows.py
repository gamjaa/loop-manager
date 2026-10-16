"""Loop Manager 자체 검증만 WSL 전체와 Windows 핵심 회귀로 나눈다."""
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid


CLEANUP_SECONDS = 10
HEARTBEAT_SECONDS = 5


def lifecycle(directory, state, **values):
    if directory is None:
        return
    directory=Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    temporary=directory/'wsl-lifecycle.tmp'
    temporary.write_text(json.dumps({'state':state,**values}),encoding='utf-8')
    temporary.replace(directory/'wsl-lifecycle.json')


def load_tests(loader, _suite, _pattern):
    names = json.loads(Path(__file__).with_name('verify-windows-tests.json').read_text(encoding='utf-8'))
    if (not isinstance(names, list) or not names or not all(
            isinstance(name, str) and name.startswith('tests.') for name in names)
            or len(names) != len(set(names))):
        raise ValueError('Windows 핵심 회귀 목록이 올바르지 않습니다.')
    return loader.loadTestsFromNames(names)


def runtime(root):
    supplied = os.environ.get('LOOP_MANAGER_VERIFY_RUNNER')
    entrypoint = Path(supplied) if supplied else root / 'loop_manager_internal/verification/entrypoint.py'
    python = os.environ.get('LOOP_MANAGER_VERIFY_RUNTIME_PYTHON', sys.executable)
    if not entrypoint.is_file() or (supplied and (
            os.environ.get('LOOP_MANAGER_VERIFY_VERSION') != '1'
            or not os.environ.get('LOOP_MANAGER_VERIFY_RUNTIME_PYTHON') or not Path(python).is_file())):
        raise RuntimeError('Manager verify runner, runtime Python 또는 version이 올바르지 않습니다.')
    coordination = [os.environ.get('LOOP_MANAGER_VERIFY_' + key) for key in ('ENDPOINT', 'SESSION', 'SECRET')]
    if any(coordination) and (not all(coordination) or not supplied):
        raise RuntimeError('Manager verify 조율 정보가 불완전합니다.')
    sys.path.insert(0, str(entrypoint.resolve().parents[2]))
    return entrypoint.resolve(), python


def git(root, *arguments):
    return subprocess.check_output(['git', '-C', str(root), *arguments],
                                   env=dict(os.environ, GIT_OPTIONAL_LOCKS='0'))


def snapshot(root, bundle, entrypoint):
    revision = git(root, 'rev-parse', 'HEAD').decode().strip()
    names = git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z')
    paths = [root / os.fsdecode(name) for name in names.split(b'\0') if name]
    paths = [path for path in paths if path.exists() or path.is_symlink()]
    before = {path: path.lstat() for path in paths}
    with tarfile.open(bundle / 'project.tar', 'w') as archive:
        for path in paths:
            if path.is_dir() and not path.is_symlink():
                raise RuntimeError(f'자체 검증에서 지원하지 않는 Git 경로입니다: {path}')
            archive.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
    for path, old in before.items():
        new = path.lstat()
        if any(getattr(old, key) != getattr(new, key) for key in ('st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns')):
            raise RuntimeError(f'검증 복사 중 파일이 변경됐습니다: {path}')
    if (git(root, 'rev-parse', 'HEAD').decode().strip() != revision
            or git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z') != names):
        raise RuntimeError('검증 복사 중 Git 입력이 변경됐습니다.')
    runtime_root = entrypoint.parents[2]
    with tarfile.open(bundle / 'runtime.tar', 'w') as archive:
        for path in sorted((runtime_root / 'loop_manager_internal').rglob('*')):
            if path.is_file() and path.suffix in {'.py', '.pl', '.pm'} and '__pycache__' not in path.parts:
                archive.add(path, arcname=path.relative_to(runtime_root).as_posix(), recursive=False)
    shutil.copyfile(__file__, bundle / 'driver.py')
    with (bundle / 'project.tar').open('rb') as stream:
        fingerprint = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'revision': revision, 'snapshot_sha256': fingerprint}


@contextmanager
def heartbeat(path):
    stopped = threading.Event()
    def update():
        while not stopped.is_set():
            path.write_text(uuid.uuid4().hex, encoding='ascii')
            stopped.wait(0.5)
    thread = threading.Thread(target=update, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join()


def wsl_environment():
    # Windows의 경로·조율 인증값을 Linux interpreter에 상속하지 않는다.
    return {key: value for key, value in os.environ.items()
            if not key.upper().startswith(('LOOP_MANAGER_', 'PYTHON', 'GIT_'))
            and key.upper() not in {'WSLENV', 'VIRTUAL_ENV'}}


def run_wsl(root, bundle, directory, timeout, cap):
    from loop_manager_internal.execution.verify_process import run_verify_process, VerifyTerminationUncertain
    started = time.monotonic()
    environment = wsl_environment()
    translated = subprocess.run(['wsl.exe', '--exec', 'wslpath', '-a', '-u', str(bundle)],
        capture_output=True, text=True, encoding='utf-8', timeout=min(10, timeout), env=environment, check=True).stdout.strip()
    if not translated.startswith('/') or '\n' in translated:
        raise RuntimeError('WSL 검증 경로 변환에 실패했습니다.')
    budget_arguments = []
    if os.environ.get('LOOP_MANAGER_VERIFY_BUDGET_DIR'):
        budget_path = subprocess.run(['wsl.exe', '--exec', 'wslpath', '-a', '-u',
            os.environ['LOOP_MANAGER_VERIFY_BUDGET_DIR']], capture_output=True, text=True,
            encoding='utf-8', timeout=min(10, timeout), env=environment, check=True).stdout.strip()
        if not budget_path.startswith('/') or '\n' in budget_path:
            raise RuntimeError('WSL 실행 시간 기록 경로 변환에 실패했습니다.')
        budget_arguments = [budget_path]
        lifecycle(os.environ['LOOP_MANAGER_VERIFY_BUDGET_DIR'],'starting')
    timeout -= time.monotonic() - started
    if timeout <= CLEANUP_SECONDS:
        raise TimeoutError('전체 verify 제한 안에 WSL 실행·정리 시간을 확보하지 못했습니다.')
    primary = None
    try:
        with heartbeat(bundle / 'heartbeat'):
            from loop_manager_internal.verification.execution_clock import ExecutionClock
            with ExecutionClock() as budget:
                passed, timed_out, output = run_verify_process(
                    ['wsl.exe', '--exec', 'python3', translated + '/driver.py', '--wsl', translated,
                     str(timeout - CLEANUP_SECONDS), str(cap), *budget_arguments], root, timeout - CLEANUP_SECONDS,
                    environment=environment, job_scope=True, execution_clock=budget)
    except BaseException as error:
        primary = error
    # Windows Job 종료만으로 Linux 자식의 종료를 확정하지 않는다.
    receipt = bundle / 'cleanup.json'
    deadline = time.monotonic() + CLEANUP_SECONDS
    while not receipt.is_file() and time.monotonic() < deadline:
        time.sleep(0.1)
    if not receipt.is_file():
        raise VerifyTerminationUncertain('WSL 자식 종료·전용 /tmp 정리 근거가 없습니다.')
    cleanup = json.loads(receipt.read_text(encoding='utf-8'))
    if not cleanup.get('stopped') or not cleanup.get('removed'):
        raise VerifyTerminationUncertain('WSL 자식 또는 전용 /tmp 정리를 확인하지 못했습니다.')
    if primary:
        raise primary
    for path in (bundle / 'logs').glob('*'):
        destination = directory / path.name
        shutil.copytree(path, destination) if path.is_dir() else shutil.copy2(path, destination)
    shutil.copy2(receipt, directory / 'cleanup.json')
    (directory / 'console.log').write_text(str(output), encoding='utf-8')
    print(output, flush=True)
    return passed and not timed_out


def wsl_main(bundle, timeout, cap, budget_directory=None):
    started = time.monotonic()
    bundle = Path(bundle)
    if budget_directory is not None:
        os.environ['LOOP_MANAGER_VERIFY_BUDGET_DIR'] = budget_directory
    stopped = threading.Event()
    def cancel(_signum, _frame):
        raise InterruptedError('Windows 검증 모체가 종료됐습니다.')
    previous = {sig: signal.signal(sig, cancel) for sig in (signal.SIGTERM, signal.SIGHUP)}
    def watch():
        observed, changed = None, time.monotonic()
        while not stopped.wait(0.5):
            try:
                current = (bundle / 'heartbeat').read_text(encoding='ascii')
            except OSError:
                current = None
            if current != observed:
                observed, changed = current, time.monotonic()
            elif time.monotonic() - changed > HEARTBEAT_SECONDS:
                os.kill(os.getpid(), signal.SIGTERM)
                return
    watcher = threading.Thread(target=watch, daemon=True)
    run_root = Path(tempfile.mkdtemp(prefix='loop-manager-verify-', dir='/tmp'))
    receipt = {'root': str(run_root), 'stopped': True, 'removed': False}
    passed = False
    try:
        lifecycle(budget_directory,'running',root=str(run_root),pid=os.getpid())
        (bundle / 'started.json').write_text(json.dumps(receipt), encoding='utf-8')
        watcher.start()
        project, runtime_root, temporary = (run_root / name for name in ('project', 'runtime', 'tmp'))
        for path in (project, runtime_root, temporary):
            path.mkdir()
        for filename, destination in (('project.tar', project), ('runtime.tar', runtime_root)):
            with tarfile.open(bundle / filename) as archive:
                archive.extractall(destination, filter='data')
        os.environ.update(TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary))
        tempfile.tempdir = str(temporary)
        sys.path.insert(0, str(runtime_root))
        from loop_manager_internal.execution.verify_process import run_verify_process, VerifyTerminationUncertain
        environment = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
            GIT_CONFIG_SYSTEM='/dev/null', PYTHONIOENCODING='utf-8',
            LOOP_MANAGER_VERIFY_LOG_DIR=str(run_root / 'logs'), LOOP_MANAGER_VERIFY_PARALLELISM=str(cap))
        from loop_manager_internal.verification.execution_clock import ExecutionClock
        budget = ExecutionClock(environment)
        budget.started = started
        for arguments in (['git', 'init', '-q', '--initial-branch=main'], ['git', 'add', '-A'],
                          ['git', '-c', 'user.name=Verify', '-c', 'user.email=verify@example.invalid',
                           '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Windows verify snapshot']):
            subprocess.run(arguments, cwd=project, env=environment, check=True, timeout=10)
        remaining = budget.remaining(timeout)
        if remaining <= 0:
            raise TimeoutError('WSL 복사 준비 중 전체 verify 시간이 만료됐습니다.')
        receipt['stopped'] = False
        try:
            passed, timed_out, output = run_verify_process(
                [sys.executable, '-u', str(runtime_root / 'loop_manager_internal/verification/entrypoint.py'),
                 '--project-python', sys.executable, '--timeout', str(remaining), '--parallelism', str(cap)],
                project, remaining, environment=environment)
        except VerifyTerminationUncertain:
            raise
        except BaseException:
            receipt['stopped'] = True
            raise
        receipt['stopped'] = True
        print(output, flush=True)
        passed = passed and not timed_out
    finally:
        stopped.set()
        if watcher.ident is not None:
            watcher.join()
        try:
            if (run_root / 'logs').is_dir():
                shutil.copytree(run_root / 'logs', bundle / 'logs')
        finally:
            if receipt['stopped']:
                shutil.rmtree(run_root)
            receipt['removed'] = not run_root.exists()
            lifecycle(budget_directory,'stopped' if receipt['stopped'] and receipt['removed'] else 'uncertain',
                      root=str(run_root),pid=os.getpid())
            (bundle / 'cleanup.tmp').write_text(json.dumps(receipt), encoding='utf-8')
            (bundle / 'cleanup.tmp').replace(bundle / 'cleanup.json')
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    return 0 if passed else 1


def native_main(root, entrypoint, directory, python):
    root, entrypoint, directory = Path(root), Path(entrypoint), Path(directory)
    sys.path.insert(0, str(entrypoint.parents[2]))
    from loop_manager_internal.verification import unittest_adapter as adapter
    from loop_manager_internal.verification.guard import install_guard
    from loop_manager_internal.verification.contract import Connection
    from loop_manager_internal.verification.core import execute_command
    from loop_manager_internal.verification.coordination import CoordinatorClient, VerifyCoordinator
    from loop_manager_internal.verification.timing import EventWriter, atomic_json, diagnostic, phase
    os.environ['LOOP_MANAGER_VERIFY_PROJECT_ROOT'] = str(root)
    install_guard()
    session = os.environ.get('LOOP_MANAGER_VERIFY_SESSION', uuid.uuid4().hex)
    coordinator = None
    if 'LOOP_MANAGER_VERIFY_ENDPOINT' in os.environ:
        client = CoordinatorClient(json.loads(os.environ['LOOP_MANAGER_VERIFY_ENDPOINT']),
                                   session, os.environ['LOOP_MANAGER_VERIFY_SECRET'])
    else:
        coordinator = VerifyCoordinator()
        session, secret = coordinator.open(1)
        class LocalClient:
            def request(self, operation, **data):
                return coordinator.request(session, secret, operation, **data)
        client = LocalClient()
    stage = {'id': 'windows-core', 'cwd': '.', 'execution': 'serial',
             'collection': {'directory': 'scripts', 'pattern': 'verify_windows.py'}}
    connection = Connection(root, 'unittest', Path(python), (stage,))
    writer = EventWriter(directory / 'runner.jsonl', identity={'run_id': session, 'stage_id': stage['id']})
    passed = False
    try:
        writer.emit('run_start', mode='windows-core', coordinated=coordinator is None)
        with writer.activate(), phase('verify'):
            modules, _python = adapter.collect_project(connection, stage, directory, session,
                entrypoint, None, execute_command, writer, client)
            atomic_json(directory / 'manifest.json', [asdict(module) for module in modules])
            passed = adapter.execute(connection, stage, modules, directory, writer, client, session, entrypoint)
        writer.emit('run_end', passed=passed)
        return 0 if passed else 1
    finally:
        writer.close()
        if coordinator:
            coordinator.finish(session, stopped=not coordinator.reservations)
        print(diagnostic(directory), flush=True)


def windows_main(root):
    entrypoint, runtime_python = runtime(root)
    from loop_manager_internal.execution.verify_process import run_verify_process
    from loop_manager_internal.verification.timing import EventWriter, atomic_json, phase
    from loop_manager_internal.config.managed_config import load_managed_config
    from loop_manager_internal.verification.execution_clock import ExecutionClock, BUDGET_DIRECTORY
    timeout = float(os.environ.get('LOOP_MANAGER_VERIFY_TIMEOUT', '600'))
    cap = os.environ.get('LOOP_MANAGER_VERIFY_PARALLELISM', 'auto')
    if (root / 'config/config.ini').is_file():
        config = load_managed_config(root / 'config/config.ini')
        if 'LOOP_MANAGER_VERIFY_TIMEOUT' not in os.environ:
            timeout = config.verify_timeout_seconds
        if 'LOOP_MANAGER_VERIFY_PARALLELISM' not in os.environ:
            cap = config.verify_parallelism
    if not math.isfinite(timeout) or timeout <= CLEANUP_SECONDS:
        raise ValueError(f'전체 verify 시간은 {CLEANUP_SECONDS}초보다 커야 합니다.')
    directory = Path(os.environ.get('LOOP_MANAGER_VERIFY_LOG_DIR', root / '.cache/verify' / uuid.uuid4().hex)).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    session = os.environ.get('LOOP_MANAGER_VERIFY_SESSION', uuid.uuid4().hex)
    if os.environ.get('LOOP_MANAGER_VERIFY_PARTIAL_BASE'):
        atomic_json(directory / 'partial-result.json', {'version': 1, 'kind': 'partial_verify',
            'run_id': session, 'status': 'FALLBACK', 'selection': {'fallback': ['self_windows_requires_full']}})
        return 0
    writer = EventWriter(directory / 'launcher.jsonl', identity={'run_id': session})
    report = {'kind': 'loop_manager_windows_verify', 'passed': False,
              'wsl': 'NOT_RUN', 'windows': 'NOT_RUN'}
    started = time.monotonic()
    budget = ExecutionClock()
    previous_budget = os.environ.get(BUDGET_DIRECTORY)
    os.environ[BUDGET_DIRECTORY] = budget.environment[BUDGET_DIRECTORY]
    try:
        writer.emit('run_start', mode='windows+wsl', cap=cap)
        with writer.activate(), phase('verify'), tempfile.TemporaryDirectory(prefix='loop-manager-verify-') as temporary:
            bundle = Path(temporary)
            report.update(snapshot(root, bundle, entrypoint))
            atomic_json(directory / 'snapshot.json', {
                'revision': report['revision'], 'bundle': str(bundle),
                'snapshot_sha256': report.get('snapshot_sha256')})
            wsl_logs = Path(str(directory) + '-wsl')
            wsl_logs.mkdir()
            print(f'WSL 전체 verify 시작: {wsl_logs}', flush=True)
            report['wsl'] = 'FAIL'
            with phase('wsl-full'):
                passed = run_wsl(root, bundle, wsl_logs,
                                 budget.remaining(timeout), cap)
            report['wsl'] = 'PASS' if passed else 'FAIL'
            report['wsl_diagnostic'] = str(wsl_logs / 'summary.json')
            if not passed:
                return 1
            native_logs = Path(str(directory) + '-windows')
            native_logs.mkdir()
            print(f'Windows 핵심 회귀 시작: {native_logs}', flush=True)
            report['windows'] = 'FAIL'
            with phase('windows-core'):
                # 자체 실행기는 표준 라이브러리만 사용하며 venv redirector를 worker로 남기지 않는다.
                passed, timed_out, output = run_verify_process(
                    [runtime_python, '-u', str(Path(__file__).resolve()), '--native', str(root),
                     str(entrypoint), str(native_logs), str(getattr(sys, '_base_executable', sys.executable))],
                    root, max(0.001, budget.remaining(timeout)), job_scope=True)
                print(output, flush=True)
            report['windows'] = 'PASS' if passed and not timed_out else 'FAIL'
            report['windows_diagnostic'] = str(native_logs / 'summary.json')
        report['passed'] = report['windows'] == 'PASS'
        return 0 if report['passed'] else 1
    finally:
        report['seconds'] = time.monotonic() - started
        atomic_json(directory / 'result.json', report)
        writer.emit('run_end', passed=report['passed'], wsl=report['wsl'], windows=report['windows'])
        writer.close()
        if previous_budget is None:
            os.environ.pop(BUDGET_DIRECTORY, None)
        else:
            os.environ[BUDGET_DIRECTORY] = previous_budget
        budget.__exit__(*sys.exc_info())
        print(f'Loop Manager 자체 verify: {directory / "result.json"}', flush=True)


if __name__ == '__main__':
    try:
        if len(sys.argv) > 1 and sys.argv[1] == '--wsl':
            raise SystemExit(wsl_main(sys.argv[2], float(sys.argv[3]), sys.argv[4],
                                     sys.argv[5] if len(sys.argv) > 5 else None))
        if len(sys.argv) > 1 and sys.argv[1] == '--native':
            raise SystemExit(native_main(*sys.argv[2:]))
        if os.name != 'nt':
            raise RuntimeError('이 진입점은 Loop Manager의 Windows 자체 검증 전용입니다.')
        raise SystemExit(windows_main(Path(__file__).resolve().parents[1]))
    except Exception:
        import traceback
        traceback.print_exc()
        raise SystemExit(1)
