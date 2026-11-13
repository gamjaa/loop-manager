"""Trusted orchestration of the fixed runner integration suite."""
import json
import os
from pathlib import Path
import subprocess
import sys
import shutil
import math
import unittest
from collections import Counter

from loop_manager_internal.verification.contract import ContractError, inside, validate_manifest, load
from loop_manager_internal.verification.manifest import Module, validate_report
from loop_manager_internal.verification import perl_adapter

from loop_manager_internal.execution.verify_process import (
    run_verify_process, VerifyTerminationUncertain, _LINUX_SUBREAPER, _WINDOWS_VERIFY_BOOTSTRAP,
)
from loop_manager_internal.execution.validation_feedback import redact_secrets
from loop_manager_internal.verification.guard import MARKER, _COORDINATION_ENV, _state
from loop_manager_internal.verification.timing import atomic_json


def _require(condition, message):
    if not condition:
        raise ContractError('runner integration: ' + message)


def case_ids():
    return [f'{language}-{mode}-{outcome}' for language in ('python', 'perl')
            for mode in ('standalone', 'coordinated') for outcome in ('pass', 'native-failure')]


def validate_stage(result, run_id, stage_id, revision):
    _require(type(result.get('version')) is int and result['version'] == 1
        and result.get('kind') == 'runner_integration' and result.get('run_id') == run_id
        and result.get('stage_id') == stage_id and result.get('revision') == revision
        and result.get('status') == 'PASS' and result.get('platform') == sys.platform,
        'stage identity/result mismatch')
    cases = result.get('cases', [])
    _require([item.get('id') for item in cases] == case_ids(), 'missing/duplicate integration cases')
    seen = {run_id}
    for case in cases:
        expected = 'SKIP' if case['id'].startswith('perl-') and not sys.platform.startswith('linux') else 'PASS'
        _require(case.get('status') == expected, 'incomplete/failed integration case: ' + case['id'])
        if expected == 'SKIP':
            _require(case.get('reason') == 'Perl native examples support Linux', 'unsupported skip reason')
            continue
        evidence = case.get('evidence', {})
        identities = [evidence.get('run_id')]
        _require(evidence.get('revision') == revision and evidence.get('adapter') ==
            ('perl-tap' if case['id'].startswith('perl-') else 'unittest'), 'case revision/adapter mismatch')
        for identity in identities:
            _require(isinstance(identity, str) and identity and identity not in seen, 'missing/duplicate child run identity')
            seen.add(identity)
    return True


def _json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def validate_example(directory, language, coordinated, failing, revision):
    """A native assertion, complete scope and actual reservations prove a case."""
    proof = _json(directory / 'coordination.json')
    process = _json(directory / 'process.json')
    start = _json(directory / 'runner-start.json')
    manifest = _json(directory / 'manifest.json')
    summary = _json(directory / 'summary.json')
    run_id = proof['session']
    _require(all(type(item.get('version')) is int and item['version'] == 1
                 for item in (proof, process, start, manifest)), 'unsupported child evidence version')
    _require(proof.get('stopped') is True and not proof.get('reservations')
        and proof.get('observer_only') is (not coordinated) and type(proof.get('pid')) is int
        and proof['pid'] > 0 and proof.get('identity') is not None, 'missing child ownership/stop evidence')
    _require(start.get('session') == run_id and start.get('state') == 'bound'
        and start.get('pid') == proof['pid'] and start.get('identity') == proof['identity'], 'child start mismatch')
    _require(process.get('session') == run_id and process.get('timed_out') is False
        and process.get('passed') is (not failing) and type(process.get('exit_code')) is int
        and process['exit_code'] == int(failing), 'infrastructure failure is not an expected native failure')
    _require(manifest.get('run_id') == run_id and manifest.get('adapter') == ('unittest' if language == 'python' else 'perl-tap')
        and type(manifest.get('adapter_version')) is int and manifest['adapter_version'] == 1, 'child manifest identity mismatch')
    _require([(stage['id'], stage['kind']) for stage in manifest['stages']] == [('compile', 'command'), ('tests', 'tests')],
             'required compile/tests order changed')
    compile_argv = manifest['stages'][0]['argv']
    if language == 'python':
        _require(compile_argv == [str(Path(sys.executable).absolute()), str(Path(__file__).with_name('entrypoint.py').resolve()),
                                '--compile', 'tests/test_example.py'], 'Python compile target changed')
    else:
        _require(len(compile_argv) == 5 and Path(compile_argv[0]).is_absolute()
            and compile_argv[1:] == ['-I' + str(perl_adapter.REPORTER.parent), '-MLoopManagerVerifyGuard', '-c', 'tests/example.t'],
            'Perl compile target/guard changed')
    metadata, result = summary['metadata'], summary['result']
    _require(metadata.get('run_id') == run_id and metadata.get('revision') == revision
        and metadata.get('mode') == 'full' and metadata.get('coordinated') is coordinated
        and result.get('run_id') == run_id and result.get('passed') is (not failing)
        and not summary['active'], 'incomplete child run/revision/result')
    unit, = manifest['stages'][1]['units']
    payload = unit['payload']
    module = Module(payload['name'], tuple(payload['ids']), tuple(tuple(owner) for owner in payload.get('owners', ())), payload.get('pattern'))
    validate_manifest([module])
    _require(unit['id'] == module.name and unit['ids'] == list(module.ids), 'unit payload mismatch')
    reports = list((directory / 'stage-1').glob('module-*.json'))
    _require(len(reports) == 1, 'missing/duplicate native reports')
    report = _json(reports[0])
    _require(type(report.get('version')) is int and report['version'] == 1
        and report.get('run_id') == run_id and report.get('stage_id') == 'tests'
        and report.get('unit_id') == module.name and not report.get('runner_error'), 'native report identity/error')
    if language == 'python':
        expected = {f'test_example.ExampleTests.test_{name}': outcome for name, outcome in
                    (('expected_failure', 'expected_failure'), ('optional', 'skip'), ('sum', 'failure' if failing else 'success'))}
        _require(set(module.ids) == set(expected) and module.name == 'test_example', 'Python example collection changed')
        _require(validate_report(module, report) is (not failing) and Counter(report['completed']) == Counter(expected.keys())
            and not report.get('fixture_skipped') and not report.get('fixture_skip_sources'), 'incomplete Python native coverage')
        events = summary['tests']
        _require(len(events) == len(expected) and {event['id']: event['result'] for event in events} == expected
            and all(event.get('run_id') == run_id for event in events)
            and not summary['fixture_results'], 'body/skip/expected failure or fixture result mismatch')
    else:
        _require(module.name == 'tests/example.t' and module.ids == ('tests/example.t',), 'Perl example collection changed')
        _require(perl_adapter.validate_report(module, report, {'run_id': run_id, 'stage_id': 'tests'}) is (not failing),
                 'Perl native coverage/result mismatch')
        tests = [item for item in report['native'] if item['type'] == 'test']
        _require(len(tests) == 4 and [item['number'] for item in tests] == [1, 2, 3, 4]
            and tests[0]['ok'] is (not failing) and tests[0]['actual_ok'] is (not failing)
            and tests[1]['skip'] is True and tests[1]['ok'] is True
            and tests[2]['todo'] is True and tests[2]['ok'] is True and tests[2]['actual_ok'] is False
            and tests[3]['ok'] is True, 'Perl sum/skip/TODO/subtest meaning changed')
    events = [json.loads(line) for line in (directory / 'runner.jsonl').read_text(encoding='utf-8').splitlines()]
    commands = [event for event in events if event['kind'] == 'unit_result' and event.get('kind_of_unit') == 'command']
    _require([event['unit_id'] for event in commands] == ['compile', 'tests:collect']
        and all(event.get('run_id') == run_id and event.get('passed') is True
                and event.get('timed_out') is False and event.get('exit_code') == 0 for event in commands),
        'required compile/collection did not actually succeed')
    if coordinated:
        traces = {}
        acquired = []
        for event in proof['events']:
            token = event['token']
            traces.setdefault(token, []).append(event)
            if event['operation'] == 'acquire':
                _require(event.get('run_id') == run_id, 'reservation run mismatch')
                acquired.append(event['unit_id'])
        _require(Counter(acquired) == Counter(['compile', 'tests:collect', module.name]), 'coordinated acquire scope mismatch')
        for trace in traces.values():
            operations = [event['operation'] for event in trace]
            _require(operations in (['acquire', 'bind', 'release'], ['acquire', 'bind', 'idle', 'release']),
                     'missing/duplicate/incomplete reservation lifecycle')
            bound, released = trace[1], trace[-1]
            _require(type(bound.get('pid')) is int and bound['pid'] > 0 and bound.get('identity') is not None
                and released.get('pid') == bound['pid'] and released.get('identity') == bound['identity'],
                'reservation PID/start identity mismatch')
    else:
        _require(not proof['events'], 'standalone observation unexpectedly reserved parent resources')
    _validate_budget(process['budget'], result['budget'])
    return {'run_id': run_id, 'revision': revision, 'adapter': manifest['adapter']}


def _validate_budget(parent, child):
    for record in (parent, child):
        limit = record.get('timeout')
        _require(limit is None or type(limit) in (int, float) and math.isfinite(limit) and limit > 0,
                 'invalid budget timeout')
        for key in ('execution_seconds', 'wall_seconds', 'waiting_seconds'):
            value = record[key]
            _require(type(value) in (int, float) and math.isfinite(value) and value >= 0, 'invalid budget observation')
    _require(math.isclose(parent['waiting_seconds'], child['waiting_seconds'], abs_tol=1e-6, rel_tol=0)
        and parent['waiting_seconds'] <= parent['wall_seconds'] + 1e-6
        and child['execution_seconds'] <= parent['execution_seconds'] + 1e-6
        and child['wall_seconds'] <= parent['wall_seconds'] + 1e-6
        and child.get('timeout') == parent.get('timeout'), 'parent/child budget evidence mismatch')
    _require(type(child.get('elapsed')) in (int, float) and math.isfinite(child['elapsed']) and child['elapsed'] >= 0,
             'invalid child consumed budget')
    if child['timeout'] is not None:
        _require(child['elapsed'] < child['timeout'], 'child exceeded actual execution timeout')


def _fixture_environment(directory):
    template = directory / 'git-template'
    template.mkdir(parents=True, exist_ok=True)
    removed = {*_COORDINATION_ENV, MARKER, 'PYTHONPATH', 'LOOP_MANAGER_VERIFY_RUNNER',
               'LOOP_MANAGER_VERIFY_PARTIAL_BASE', 'LOOP_MANAGER_VERIFY_TIMEOUT',
               'LOOP_MANAGER_VERIFY_PARENT_RUN_ID'}
    environment = {key: value for key, value in os.environ.items()
                   if key not in removed and not key.upper().startswith('GIT_')}
    environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_NOSYSTEM='1',
        GIT_ATTR_NOSYSTEM='1', GIT_TEMPLATE_DIR=str(template), XDG_CONFIG_HOME=str(template))
    return environment


def _require_orchestrator():
    if (_state.audit is not None or getattr(_state, 'wrapper', None) is not None
            and subprocess.Popen.__init__ is _state.wrapper):
        raise RuntimeError("guard 적용 자식은 신뢰된 runner 실행 port를 사용할 수 없습니다.")


class TrustedRunnerPort:
    """Launch only the runtime entrypoint; ordinary commands never use this port."""
    def __init__(self, client, budget, timeout, *, parent_run_id=None):
        _require_orchestrator()
        self.client, self.budget, self.timeout = client, budget, timeout
        self.parent_run_id = parent_run_id

    def run(self, project, directory, *, coordinated, git_directory=None):
        _require_orchestrator()
        remaining = self.budget.remaining(self.timeout)
        if remaining is not None and remaining <= 0:
            raise TimeoutError("verify total timeout before integration runner")
        directory.mkdir(parents=True, exist_ok=True)
        before = self.budget.snapshot(self.timeout)
        environment = _fixture_environment(directory)
        environment.update(LOOP_MANAGER_VERIFY_LOG_DIR=str(directory),
                           LOOP_MANAGER_VERIFY_PROJECT_ROOT=str(project))
        if self.parent_run_id is not None:
            environment['LOOP_MANAGER_VERIFY_PARENT_RUN_ID'] = self.parent_run_id
        if git_directory is not None:
            environment.update(GIT_DIR=str(git_directory), GIT_WORK_TREE=str(project))
        entrypoint = Path(__file__).with_name('entrypoint.py').resolve()
        arguments = [sys.executable, '-I', '-X', 'utf8', '-u', str(entrypoint), '--execute',
                     '--protocol-version', '1', '--project-python', sys.executable, '--parallelism', '1']
        if remaining is not None:
            arguments += ['--timeout', str(remaining)]
        expected = tuple(arguments)
        def launch(command, **options):
            _require_orchestrator()
            command = tuple(command)
            if command == expected:
                isolated = command
            elif (command[:2] == (sys.executable, '-c') and len(command) > 2
                  and command[2] in (_LINUX_SUBREAPER, _WINDOWS_VERIFY_BOOTSTRAP)
                  and command[3:] == expected):
                isolated = (sys.executable, '-I', '-X', 'utf8', *command[1:])
            else:
                raise RuntimeError('신뢰된 runner의 고정 bootstrap/명령과 일치하지 않습니다.')
            return subprocess.Popen(isolated, **options)
        lease = self.client.request('open_child', observer_only=not coordinated)
        if coordinated:
            environment.update(LOOP_MANAGER_VERIFY_ENDPOINT=json.dumps(lease['endpoint']),
                               LOOP_MANAGER_VERIFY_SESSION=lease['session'],
                               LOOP_MANAGER_VERIFY_SECRET=lease['secret'])
        else:
            environment['LOOP_MANAGER_VERIFY_OBSERVER'] = json.dumps(lease)
        stopped = False
        def confirmed_stop():
            nonlocal stopped
            stopped = True
        def preserve_output(output):
            (directory / 'console.log').write_text(redact_secrets(output), encoding='utf-8')
        def bind(process, _job):
            start = {'version': 1, 'session': lease['session'], 'pid': process.pid}
            atomic_json(directory / 'runner-start.json', {**start, 'state': 'starting'})
            proof = self.client.request('bind_child', child=lease['session'], pid=process.pid)
            atomic_json(directory / 'runner-start.json', {**start, 'state': 'bound', 'identity': proof['identity']})
        try:
            try:
                result = run_verify_process(arguments, project, self.timeout, environment=environment,
                    job_scope=True, execution_clock=self.budget, on_started=bind,
                    launch_process=launch, on_stopped=confirmed_stop,
                    on_output=preserve_output,
                    on_wait=lambda: self.client.request('children_wait'))
            except VerifyTerminationUncertain:
                raise

            except BaseException as error:
                if not stopped:
                    raise VerifyTerminationUncertain(f"하위 runner의 종료/미시작 확인이 없습니다: {error}") from error
                raise
            stopped = True
            passed, timed_out, output = result
            preserve_output(str(output))
            after = self.budget.snapshot(self.timeout)
            wall = after['wall_seconds'] - before['wall_seconds']
            waiting = after['waiting_seconds'] - before['waiting_seconds']
            local = after['local_wait_seconds'] - before['local_wait_seconds']
            atomic_json(directory / 'process.json', {'version': 1, 'passed': passed, 'timed_out': timed_out,
                'exit_code': getattr(output, 'exit_code', None), 'session': lease['session'],
                'budget': {'wall_seconds': wall, 'waiting_seconds': waiting,
                           'execution_seconds': max(0.0, wall - local - waiting), 'timeout': remaining}})
            return result
        finally:
            primary = sys.exc_info()[1]
            try:
                try:
                    status = self.client.request('child_status', child=lease['session'])
                    proof = {'version': 1, **status, 'stopped': False}
                    atomic_json(directory / 'coordination.json', proof)
                finally:
                    try:
                        finished = self.client.request('finish_child', child=lease['session'], stopped=stopped)
                        if finished.get('ok') is not True:
                            raise RuntimeError('하위 runner 종료 확인 응답이 유효하지 않습니다.')
                    except Exception as error:
                        raise VerifyTerminationUncertain(f'하위 runner 최종 종료 확인 실패: {error}') from error
                if stopped:
                    atomic_json(directory / 'coordination.json', {**proof, 'stopped': True})
            except Exception as error:
                if isinstance(primary, VerifyTerminationUncertain):
                    raise VerifyTerminationUncertain(f"{primary}; 하위 runner 진단/조율 정리 실패: {error}") from primary
                raise


def language_case(root, directory, port, language, coordinated, failing, revision):
    project, logs = directory / 'project', directory / 'logs'
    shutil.copytree(inside(root, 'tests/support/verify_examples/' + language), project)
    if language == 'perl':
        tool = shutil.which('perl')
        _require(tool is not None, 'Perl is required on the supported platform')
        declaration = project / 'scripts/verify.json'
        data = _json(declaration)
        data['environment']['perl'] = str(Path(tool).absolute())
        atomic_json(declaration, data)
    connection = load(project, project_python=sys.executable)
    expected_command = (['{python}', '{runner}', '--compile', 'tests/test_example.py'] if language == 'python'
                        else ['{perl}', '-c', 'tests/example.t'])
    expected_stages = ({'id': 'compile', 'kind': 'command', 'cwd': '.', 'argv': expected_command},
        {'id': 'tests', 'kind': 'tests', 'cwd': '.', 'execution': 'independent',
         'collection': {'directory': 'tests', 'pattern': 'test*.py' if language == 'python' else '*.t'}})
    _require(connection is not None and connection.adapter == ('unittest' if language == 'python' else 'perl-tap')
        and connection.python == Path(sys.executable).absolute()
        and connection.stages == expected_stages, 'fixed example declaration changed')
    if failing:
        test = project / ('tests/example.t' if language == 'perl' else 'tests/test_example.py')
        original = ', 5,' if language == 'perl' else 'sum(self.fixture), 5'
        replacement = ', 6,' if language == 'perl' else 'sum(self.fixture), 6'
        text = test.read_text(encoding='utf-8')
        _require(text.count(original) == 1, 'native failure fixture changed')
        test.write_text(text.replace(original, replacement), encoding='utf-8')
    git_file = root / '.git'
    git_directory = (root / git_file.read_text(encoding='utf-8').strip().removeprefix('gitdir: ')).resolve() if git_file.is_file() else git_file
    try:
        port.run(project, logs, coordinated=coordinated, git_directory=git_directory)
        return validate_example(logs, language, coordinated, failing, revision)
    finally:
        receipt = logs / 'coordination.json'
        if receipt.is_file() and _json(receipt).get('stopped') is True:
            shutil.rmtree(project)


def execute(root, directory, client, budget, timeout, run_id, stage_id, revision):
    port = TrustedRunnerPort(client, budget, timeout, parent_run_id=run_id)
    result = {'version': 1, 'kind': 'runner_integration', 'run_id': run_id, 'stage_id': stage_id,
              'revision': revision, 'platform': sys.platform, 'status': 'ERROR', 'cases': []}
    path = directory / 'integration-result.json'
    atomic_json(path, result)
    for identity in case_ids():
        case_directory = directory / 'cases' / identity
        case_directory.mkdir(parents=True)
        case = {'id': identity, 'status': 'RUNNING', 'diagnostic': str(case_directory)}
        result['cases'].append(case)
        atomic_json(path, result)
        try:
            language, mode, outcome = identity.split('-', 2)
            if language == 'perl' and not sys.platform.startswith('linux'):
                case.update(status='SKIP', reason='Perl native examples support Linux')
                atomic_json(path, result)
                continue
            case['evidence'] = language_case(root, case_directory, port, language,
                mode == 'coordinated', outcome == 'native-failure', revision)
            case['status'] = 'PASS'
        except BaseException as error:
            case.update(status='FAIL', error=redact_secrets(f'{type(error).__name__}: {error}'))
            result['status'] = 'FAIL'
            atomic_json(path, result)
            if isinstance(error, (VerifyTerminationUncertain, KeyboardInterrupt, SystemExit)):
                raise
            return result
        atomic_json(path, result)
    result['status'] = 'PASS'
    validate_stage(result, run_id, stage_id, revision)
    atomic_json(path, result)
    return result

