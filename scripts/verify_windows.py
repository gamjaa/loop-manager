"""Loop Manager 자체 검증만 WSL 전체와 Windows 핵심 회귀로 나눈다."""
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
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
WINDOWS_TEMP_VARIABLES = ('TEMP', 'TMP', 'TMPDIR')


def windows_temporary_directory():
    selected = os.environ.get('LOOP_MANAGER_VERIFY_TEMP_DIR')
    if not selected:
        raise RuntimeError('자체 Windows verify 임시 경로가 지정되지 않았습니다.')
    try:
        if not Path(selected).is_absolute():
            raise RuntimeError(f'자체 Windows verify 임시 경로가 절대 경로가 아닙니다: {selected}')
        expected = Path(selected).resolve(strict=True)
        actual = Path(tempfile.gettempdir()).resolve(strict=True)
        supplied = []
        for name in WINDOWS_TEMP_VARIABLES:
            value = Path(os.environ[name])
            if not value.is_absolute():
                raise RuntimeError(f'{name} 임시 경로가 절대 경로가 아닙니다: {value}')
            supplied.append(value.resolve(strict=True))
    except (KeyError, OSError) as error:
        raise RuntimeError(f'자체 Windows verify 임시 경로를 사용할 수 없습니다: {selected}: {error}') from error
    if not expected.is_dir() or any(not path.is_dir() for path in supplied):
        raise RuntimeError(f'자체 Windows verify 임시 경로가 디렉터리가 아닙니다: {selected}')
    normalized = os.path.normcase(os.fspath(expected))
    if (os.path.normcase(os.fspath(actual)) != normalized
            or any(os.path.normcase(os.fspath(path)) != normalized for path in supplied)):
        raise RuntimeError(
            '자체 Windows verify의 실제 Python 임시 경로와 TEMP·TMP·TMPDIR 선택이 다릅니다: '
            f'선택={expected}, Python={actual}')
    return str(actual)


def wsl_temporary_directory(directory):
    try:
        summary = json.loads((Path(directory) / 'summary.json').read_text(encoding='utf-8'))
        value = summary['metadata']['execution_context']['temporary_directory']
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RuntimeError(f'WSL 임시 경로를 진단에서 확인할 수 없습니다: {error}') from error
    path = PurePosixPath(value) if isinstance(value, str) else PurePosixPath()
    if not path.is_absolute() or not path.is_relative_to(PurePosixPath('/tmp')):
        raise RuntimeError(f'WSL verify 임시 경로가 전용 /tmp 아래가 아닙니다: {value}')
    return str(path)


def native_temporary_directory(directory):
    try:
        summary = json.loads((Path(directory) / 'summary.json').read_text(encoding='utf-8'))
        value = summary['temporary_directory']
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RuntimeError(f'Windows 핵심 회귀 임시 경로를 진단에서 확인할 수 없습니다: {error}') from error
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise RuntimeError(f'Windows 핵심 회귀 임시 경로 진단이 절대 경로가 아닙니다: {value}')
    selected = windows_temporary_directory()
    if os.path.normcase(os.path.realpath(value)) != os.path.normcase(os.path.realpath(selected)):
        raise RuntimeError(f'Windows 핵심 회귀의 실제 임시 경로가 launcher 선택과 다릅니다: {value}')
    return selected


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


def check_budget(budget, timeout):
    if budget.remaining(timeout) <= 0:
        raise TimeoutError('자체 verify 준비 중 전체 실행 시간이 만료됐습니다.')


def git(root, bundle, budget, timeout, *arguments):
    from loop_manager_internal.execution.verify_process import run_verify_process
    check_budget(budget, timeout)
    output_path = bundle / 'git-output'
    # Git의 NUL 구분 출력은 임의의 파일명 바이트를 담으므로 텍스트 출력으로 변환하지 않는다.
    command = [sys.executable, '-c',
        'import subprocess,sys; f=open(sys.argv[1],"wb"); '
        'sys.exit(subprocess.call(sys.argv[2:],stdout=f))',
        str(output_path), 'git', '-C', str(root), *arguments]
    passed, timed_out, output = run_verify_process(
        command, root, timeout, environment=dict(os.environ, GIT_OPTIONAL_LOCKS='0'),
        job_scope=True, execution_clock=budget)
    if timed_out:
        raise TimeoutError('snapshot Git 실행 중 전체 verify 시간이 만료됐습니다.')
    if not passed:
        raise RuntimeError(f'snapshot Git 실행에 실패했습니다: {output}')
    check_budget(budget, timeout)
    data = output_path.read_bytes()
    output_path.unlink()
    return data


def snapshot(root, bundle, entrypoint, budget, timeout):
    revision = git(root, bundle, budget, timeout, 'rev-parse', 'HEAD').decode().strip()
    names = git(root, bundle, budget, timeout, 'ls-files', '--cached', '--others', '--exclude-standard', '-z')
    paths = [root / os.fsdecode(name) for name in names.split(b'\0') if name]
    paths = [path for path in paths if path.exists() or path.is_symlink()]
    before = {path: path.lstat() for path in paths}
    with tarfile.open(bundle / 'project.tar', 'w') as archive:
        for path in paths:
            check_budget(budget, timeout)
            if path.is_dir() and not path.is_symlink():
                raise RuntimeError(f'자체 검증에서 지원하지 않는 Git 경로입니다: {path}')
            archive.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
    for path, old in before.items():
        check_budget(budget, timeout)
        new = path.lstat()
        if any(getattr(old, key) != getattr(new, key) for key in ('st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns')):
            raise RuntimeError(f'검증 복사 중 파일이 변경됐습니다: {path}')
    if (git(root, bundle, budget, timeout, 'rev-parse', 'HEAD').decode().strip() != revision
            or git(root, bundle, budget, timeout, 'ls-files', '--cached', '--others', '--exclude-standard', '-z') != names):
        raise RuntimeError('검증 복사 중 Git 입력이 변경됐습니다.')
    runtime_root = entrypoint.parents[2]
    with tarfile.open(bundle / 'runtime.tar', 'w') as archive:
        for path in sorted((runtime_root / 'loop_manager_internal').rglob('*')):
            check_budget(budget, timeout)
            if path.is_file() and path.suffix in {'.py', '.pl', '.pm'} and '__pycache__' not in path.parts:
                archive.add(path, arcname=path.relative_to(runtime_root).as_posix(), recursive=False)
    shutil.copyfile(__file__, bundle / 'driver.py')
    with (bundle / 'project.tar').open('rb') as stream:
        fingerprint = hashlib.file_digest(stream, 'sha256').hexdigest()
    check_budget(budget, timeout)
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


@contextmanager
def owned_bundle():
    from loop_manager_internal.execution.verify_process import VerifyTerminationUncertain
    directory = Path(tempfile.mkdtemp(prefix='loop-manager-verify-'))
    try:
        yield directory
    except VerifyTerminationUncertain as error:
        error.add_note(f'종료 불명 자료를 보존합니다: {directory}')
        raise
    except BaseException as error:
        try:
            shutil.rmtree(directory)
        except OSError as cleanup_error:
            error.add_note(f'bundle 정리 실패: {cleanup_error}')
        raise
    else:
        shutil.rmtree(directory)

def wsl_environment():
    # Windows의 경로·조율 인증값을 Linux interpreter에 상속하지 않는다.
    return {key: value for key, value in os.environ.items()
            if not key.upper().startswith(('LOOP_MANAGER_', 'PYTHON', 'GIT_'))
            and key.upper() not in {'WSLENV', 'VIRTUAL_ENV'}}


def run_wsl(root, bundle, directory, timeout, cap, parent_run_id=None):
    from loop_manager_internal.execution.verify_process import run_verify_process, VerifyTerminationUncertain
    from loop_manager_internal.verification.execution_clock import ExecutionClock, VerifyBudgetError
    environment = wsl_environment()
    with ExecutionClock() as budget:
        def translate(path):
            check_budget(budget, timeout - CLEANUP_SECONDS)
            result = subprocess.run(['wsl.exe', '--exec', 'wslpath', '-a', '-u', str(path)],
                capture_output=True, text=True, encoding='utf-8',
                timeout=budget.remaining(timeout - CLEANUP_SECONDS), env=environment, check=True)
            check_budget(budget, timeout - CLEANUP_SECONDS)
            value = result.stdout.strip()
            if not value.startswith('/') or '\n' in value:
                raise RuntimeError('WSL 검증 경로 변환에 실패했습니다.')
            return value

        translated = translate(bundle)
        budget_arguments = []
        budget_directory = os.environ.get('LOOP_MANAGER_VERIFY_BUDGET_DIR')
        if budget_directory:
            budget_arguments = [translate(budget_directory)]
        if parent_run_id:
            budget_arguments.extend(['--parent-run-id', parent_run_id])
        remaining = budget.remaining(timeout)
        if remaining <= CLEANUP_SECONDS:
            raise TimeoutError('전체 verify 제한 안에 WSL 실행·정리 시간을 확보하지 못했습니다.')
        if budget_directory:
            lifecycle(budget_directory, 'starting')
        primary = None
        output = None
        launched = False
        def on_started(_process, _job):
            nonlocal launched
            launched = True
        try:
            with heartbeat(bundle / 'heartbeat'):
                passed, timed_out, output = run_verify_process(
                    ['wsl.exe', '--exec', 'python3', translated + '/driver.py', '--wsl', translated,
                     str(remaining), str(cap), *budget_arguments], root, timeout - CLEANUP_SECONDS,
                    environment=environment, job_scope=True, execution_clock=budget, on_started=on_started)
        except BaseException as error:
            primary = error
        if primary is not None and not launched and not isinstance(primary, VerifyTerminationUncertain):
            if budget_directory:
                lifecycle(budget_directory, 'stopped')
            raise primary
        # Windows Job 종료만으로 Linux 자식의 종료를 확정하지 않는다.
        receipt = bundle / 'cleanup.json'
        cleanup_deadline = time.monotonic() + min(CLEANUP_SECONDS, max(0, timeout - budget.last_elapsed))
        while not receipt.is_file():
            try:
                remaining_cleanup = budget.remaining(timeout)
            except VerifyBudgetError as error:
                if primary is None:
                    primary = error
                remaining_cleanup = cleanup_deadline - time.monotonic()
            if remaining_cleanup <= 0:
                break
            time.sleep(min(0.1, remaining_cleanup))
        if not receipt.is_file():
            raise VerifyTerminationUncertain('WSL 자식 종료·전용 /tmp 정리 근거가 없습니다.') from primary
        try:
            cleanup = json.loads(receipt.read_text(encoding='utf-8'))
            if not isinstance(cleanup, dict) or not cleanup.get('stopped') or not cleanup.get('removed'):
                raise ValueError('incomplete cleanup receipt')
        except (OSError, ValueError, TypeError) as error:
            raise VerifyTerminationUncertain('WSL 자식 또는 전용 /tmp 정리를 확인하지 못했습니다.') from error
        try:
            for path in (bundle / 'logs').glob('*'):
                if primary is None:
                    try:
                        check_budget(budget, timeout)
                    except (TimeoutError, VerifyBudgetError) as error:
                        primary = error
                destination = directory / path.name
                shutil.copytree(path, destination) if path.is_dir() else shutil.copy2(path, destination)
            shutil.copy2(receipt, directory / 'cleanup.json')
            if output is not None:
                (directory / 'console.log').write_text(str(output), encoding='utf-8')
                print(output, flush=True)
        except BaseException as error:
            if primary is None:
                raise
            primary.add_note(f'WSL 진단 복사 실패: {error}')
        if primary:
            raise primary
        check_budget(budget, timeout)
        return passed and not timed_out


def wsl_main(bundle, timeout, cap, budget_directory=None, parent_run_id=None):
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
    budget = None
    try:
        lifecycle(budget_directory,'running',root=str(run_root),pid=os.getpid())
        (bundle / 'started.json').write_text(json.dumps(receipt), encoding='utf-8')
        watcher.start()
        project, runtime_root, temporary = (run_root / name for name in ('project', 'runtime', 'tmp'))
        for path in (project, runtime_root, temporary):
            path.mkdir()
        for filename, destination in (('project.tar', project), ('runtime.tar', runtime_root)):
            if time.monotonic() - started >= timeout - CLEANUP_SECONDS:
                raise TimeoutError('WSL 복사 준비 중 전체 verify 시간이 만료됐습니다.')
            with tarfile.open(bundle / filename) as archive:
                archive.extractall(destination, filter='data')
        sys.path.insert(0, str(runtime_root))
        from loop_manager_internal.execution.verify_process import run_verify_process, VerifyTerminationUncertain
        logs = bundle / 'logs'
        logs.mkdir(exist_ok=True)
        environment = dict(os.environ, TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary),
            GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
            GIT_CONFIG_SYSTEM='/dev/null', PYTHONIOENCODING='utf-8',
            LOOP_MANAGER_VERIFY_LOG_DIR=str(logs), LOOP_MANAGER_VERIFY_PARALLELISM=str(cap))
        if parent_run_id:
            environment['LOOP_MANAGER_VERIFY_PARENT_RUN_ID'] = parent_run_id
        from loop_manager_internal.verification.execution_clock import ExecutionClock
        budget = ExecutionClock(environment)
        budget.started = started
        os.environ.update(TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary))
        tempfile.tempdir = str(temporary)
        for arguments in (['git', 'init', '-q', '--initial-branch=main'], ['git', 'add', '-A'],
                          ['git', '-c', 'user.name=Verify', '-c', 'user.email=verify@example.invalid',
                           '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Windows verify snapshot']):
            if budget.remaining(timeout - CLEANUP_SECONDS) <= 0:
                raise TimeoutError('WSL Git 준비 중 전체 verify 시간이 만료됐습니다.')
            receipt['stopped'] = False
            try:
                success, timed_out, output = run_verify_process(
                    arguments, project, timeout - CLEANUP_SECONDS,
                    environment=environment, execution_clock=budget)
            except VerifyTerminationUncertain:
                raise
            except BaseException:
                receipt['stopped'] = True
                raise
            receipt['stopped'] = True
            if timed_out:
                raise TimeoutError('WSL Git 준비 중 전체 verify 시간이 만료됐습니다.')
            if not success:
                raise RuntimeError(f'WSL Git 준비에 실패했습니다: {output}')
        remaining = budget.remaining(timeout - CLEANUP_SECONDS)
        if remaining <= 0:
            raise TimeoutError('WSL 복사 준비 중 전체 verify 시간이 만료됐습니다.')
        receipt['stopped'] = False
        try:
            passed, timed_out, output = run_verify_process(
                [sys.executable, '-u', str(runtime_root / 'loop_manager_internal/verification/entrypoint.py'),
                 '--project-python', sys.executable, '--timeout', str(remaining), '--parallelism', str(cap)],
                project, timeout - CLEANUP_SECONDS, environment=environment, execution_clock=budget)
        except VerifyTerminationUncertain:
            raise
        except BaseException:
            receipt['stopped'] = True
            raise
        receipt['stopped'] = True
        print(output, flush=True)
        passed = passed and not timed_out
    except BaseException as error:
        evidence = getattr(error, 'diagnostic_directory', None)
        if evidence is not None:
            try:
                shutil.copytree(evidence, bundle / 'logs' / 'budget-error', symlinks=True)
            except OSError as copy_error:
                error.add_note(f'WSL 예산 오류 진단 복사 실패: {copy_error}')
        raise
    finally:
        primary_error = sys.exc_info()[1]
        try:
            stopped.set()
            if watcher.ident is not None:
                watcher.join()
            try:
                if receipt['stopped']:
                    shutil.rmtree(run_root)
            finally:
                receipt['removed'] = not run_root.exists()
                lifecycle(budget_directory,'stopped' if receipt['stopped'] and receipt['removed'] else 'uncertain',
                          root=str(run_root),pid=os.getpid())
                (bundle / 'cleanup.tmp').write_text(json.dumps(receipt), encoding='utf-8')
                (bundle / 'cleanup.tmp').replace(bundle / 'cleanup.json')
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
                if budget is not None:
                    from loop_manager_internal.verification.execution_clock import VerifyBudgetError
                    primary = primary_error or sys.exc_info()[1]
                    try:
                        if primary is None:
                            check_budget(budget, timeout)
                    except BaseException as error:
                        primary = error
                        if isinstance(error, VerifyBudgetError) and error.diagnostic_directory is not None:
                            try:
                                shutil.copytree(error.diagnostic_directory, bundle / 'logs' / 'budget-error', symlinks=True)
                            except OSError as copy_error:
                                error.add_note(f'WSL 예산 오류 진단 복사 실패: {copy_error}')
                        raise
                    finally:
                        if not isinstance(primary, (VerifyBudgetError, VerifyTerminationUncertain)):
                            budget.close()
        except BaseException as cleanup_error:
            if primary_error is None:
                raise
            primary_error.add_note(f'WSL 정리 실패: {cleanup_error}')
    return 0 if passed else 1


def native_main(root, entrypoint, directory, python):
    root, entrypoint, directory = Path(root), Path(entrypoint), Path(directory)
    temporary_directory = windows_temporary_directory()
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
    identity = {'run_id': session, 'stage_id': stage['id']}
    parent_run_id = os.environ.get('LOOP_MANAGER_VERIFY_PARENT_RUN_ID')
    if parent_run_id:
        identity['parent_run_id'] = parent_run_id
    writer = EventWriter(directory / 'runner.jsonl', identity=identity)
    passed = False
    try:
        writer.emit('run_start', mode='windows-core', coordinated=coordinator is None,
                    temporary_directory=temporary_directory)
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
        details = diagnostic(directory)
        summary_path = directory / 'summary.json'
        if summary_path.is_file():
            summary = json.loads(summary_path.read_text(encoding='utf-8'))
            summary['temporary_directory'] = temporary_directory
            atomic_json(summary_path, summary)
        print(details, flush=True)


def windows_main(root):
    temporary_directory = windows_temporary_directory()
    entrypoint, runtime_python = runtime(root)
    from loop_manager_internal.execution.verify_process import run_verify_process
    from loop_manager_internal.verification.timing import EventWriter, atomic_json, diagnostic, phase
    from loop_manager_internal.config.managed_config import load_managed_config
    from loop_manager_internal.verification.execution_clock import ExecutionClock, BUDGET_DIRECTORY, VerifyBudgetError
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
            'run_id': session, 'status': 'FALLBACK', 'selection': {'fallback': ['self_windows_requires_full']},
            'temporary_directories': {'windows_launcher': temporary_directory, 'wsl': None,
                                      'windows_native': None}})
        from loop_manager_internal.verification.timing import diagnostic
        print(diagnostic(directory), flush=True)
        return 0
    writer = EventWriter(directory / 'launcher.jsonl', identity={'run_id': session})
    report = {'kind': 'loop_manager_windows_verify', 'run_id': session, 'passed': False,
              'wsl': 'NOT_RUN', 'windows': 'NOT_RUN',
              'temporary_directories': {'windows_launcher': temporary_directory,
                                        'wsl': None, 'windows_native': None},
              'wsl_run_id': None, 'windows_run_id': None}
    started = time.monotonic()
    budget = ExecutionClock()
    budget.__enter__()
    previous_budget = os.environ.get(BUDGET_DIRECTORY)
    os.environ[BUDGET_DIRECTORY] = budget.environment[BUDGET_DIRECTORY]
    try:
        writer.emit('run_start', mode='windows+wsl', cap=cap)
        with writer.activate(), phase('verify'), owned_bundle() as bundle:
            report.update(snapshot(root, bundle, entrypoint, budget, timeout - CLEANUP_SECONDS))
            atomic_json(directory / 'snapshot.json', {
                'run_id': session, 'revision': report['revision'], 'bundle': str(bundle),
                'snapshot_sha256': report.get('snapshot_sha256')})
            wsl_logs = Path(str(directory) + '-wsl')
            wsl_logs.mkdir()
            print(f'WSL 전체 verify 시작: {wsl_logs}', flush=True)
            report['wsl'] = 'FAIL'
            with phase('wsl-full'):
                passed = run_wsl(root, bundle, wsl_logs,
                                 budget.remaining(timeout), cap, session)
            report['temporary_directories']['wsl'] = wsl_temporary_directory(wsl_logs)
            try:
                wsl_summary = json.loads((wsl_logs / 'summary.json').read_text(encoding='utf-8'))
                wsl_metadata = wsl_summary.get('metadata', {}) if isinstance(wsl_summary, dict) else {}
                if isinstance(wsl_metadata, dict) and wsl_metadata.get('parent_run_id') == session:
                    report['wsl_run_id'] = wsl_metadata.get('run_id')
            except (OSError, UnicodeError, ValueError):
                pass
            report['wsl'] = 'PASS' if passed else 'FAIL'
            report['wsl_diagnostic'] = str(wsl_logs / 'summary.json')
            if not passed:
                return 1
            if budget.remaining(timeout) <= 0:
                raise TimeoutError('WSL 종료 후 전체 verify 시간이 만료됐습니다.')
            native_logs = Path(str(directory) + '-windows')
            native_logs.mkdir()
            print(f'Windows 핵심 회귀 시작: {native_logs}', flush=True)
            report['windows'] = 'FAIL'
            with phase('windows-core'):
                # 자체 실행기는 표준 라이브러리만 사용하며 venv redirector를 worker로 남기지 않는다.
                native_environment = dict(os.environ, LOOP_MANAGER_VERIFY_PARENT_RUN_ID=session)
                check_budget(budget, timeout)
                passed, timed_out, output = run_verify_process(
                    [runtime_python, '-u', str(Path(__file__).resolve()), '--native', str(root),
                     str(entrypoint), str(native_logs), str(getattr(sys, '_base_executable', sys.executable))],
                    root, timeout, environment=native_environment, job_scope=True, execution_clock=budget)
                print(output, flush=True)
            report['temporary_directories']['windows_native'] = native_temporary_directory(native_logs)
            try:
                native_summary = json.loads((native_logs / 'summary.json').read_text(encoding='utf-8'))
                native_metadata = native_summary.get('metadata', {})
                if (isinstance(native_metadata, dict)
                        and native_metadata.get('parent_run_id') == session
                        and isinstance(native_metadata.get('run_id'), str)):
                    report['windows_run_id'] = native_metadata['run_id']
            except (OSError, UnicodeError, ValueError):
                pass
            report['windows'] = 'PASS' if passed and not timed_out else 'FAIL'
            report['windows_diagnostic'] = str(native_logs / 'summary.json')
        if budget.remaining(timeout) <= 0:
            raise TimeoutError('전체 verify 정리 중 시간이 만료됐습니다.')
        report['passed'] = report['windows'] == 'PASS'
        return 0 if report['passed'] else 1
    finally:
        primary = sys.exc_info()[1]
        candidate = report['passed'] and primary is None
        report['passed'] = False
        budget_error = primary
        while budget_error is not None and not isinstance(budget_error, VerifyBudgetError):
            budget_error = budget_error.__cause__
        if budget_error is not None and budget_error.diagnostic_directory is not None:
            try:
                shutil.copytree(budget_error.diagnostic_directory, directory / 'budget-error', symlinks=True)
            except OSError as error:
                budget_error.add_note(f'예산 오류 진단 복사 실패: {error}')
        try:
            report['seconds'] = time.monotonic() - started
            atomic_json(directory / 'result.json', report)
            print(f'Loop Manager 자체 verify: {directory / "result.json"}', flush=True)
            print(diagnostic(directory), flush=True)
            if primary is None:
                check_budget(budget, timeout)
            report['passed'] = candidate
            atomic_json(directory / 'result.json', report)
            if candidate:
                print(diagnostic(directory), flush=True)
                check_budget(budget, timeout)
            writer.emit('run_end', passed=report['passed'], wsl=report['wsl'], windows=report['windows'])
        except BaseException as error:
            report['passed'] = False
            if isinstance(error, VerifyBudgetError) and error.diagnostic_directory is not None:
                try:
                    shutil.copytree(error.diagnostic_directory, directory / 'budget-error', symlinks=True)
                except OSError as copy_error:
                    error.add_note(f'예산 오류 진단 복사 실패: {copy_error}')
            try:
                atomic_json(directory / 'result.json', report)
            except OSError as write_error:
                error.add_note(f'실패 결과 기록 실패: {write_error}')
            if primary is None:
                primary = error
                raise
            primary.add_note(f'최종 진단 실패: {error}')
        finally:
            try:
                try:
                    writer.close()
                except OSError as error:
                    report['passed'] = False
                    try:
                        atomic_json(directory / 'result.json', report)
                    except OSError as write_error:
                        error.add_note(f'실패 결과 기록 실패: {write_error}')
                    if primary is None:
                        primary = error
                        raise
                    primary.add_note(f'이벤트 로그 닫기 실패: {error}')
            finally:
                if previous_budget is None:
                    os.environ.pop(BUDGET_DIRECTORY, None)
                else:
                    os.environ[BUDGET_DIRECTORY] = previous_budget
                try:
                    budget.__exit__(type(primary) if primary is not None else None, primary,
                                    primary.__traceback__ if primary is not None else None)
                except BaseException as error:
                    report['passed'] = False
                    try:
                        atomic_json(directory / 'result.json', report)
                    except OSError as write_error:
                        error.add_note(f'실패 결과 기록 실패: {write_error}')
                    if primary is None:
                        raise
                    primary.add_note(f'실행 시간 기록 정리 실패: {error}')


if __name__ == '__main__':
    try:
        if len(sys.argv) > 1 and sys.argv[1] == '--wsl':
            arguments = sys.argv[2:]
            parent_run_id = None
            if '--parent-run-id' in arguments:
                index = arguments.index('--parent-run-id')
                if index + 1 >= len(arguments):
                    raise ValueError('WSL parent run identity is missing.')
                parent_run_id = arguments[index + 1]
                del arguments[index:index + 2]
            raise SystemExit(wsl_main(arguments[0], float(arguments[1]), arguments[2],
                                     arguments[3] if len(arguments) > 3 else None,
                                     parent_run_id=parent_run_id))
        if len(sys.argv) > 1 and sys.argv[1] == '--native':
            raise SystemExit(native_main(*sys.argv[2:]))
        if os.name != 'nt':
            raise RuntimeError('이 진입점은 Loop Manager의 Windows 자체 검증 전용입니다.')
        raise SystemExit(windows_main(Path(__file__).resolve().parents[1]))
    except Exception:
        import traceback
        traceback.print_exc()
        raise SystemExit(1)
