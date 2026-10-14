"""Perl file discovery and native TAP::Parser results; core owns reservations."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback

from loop_manager_internal.verification.contract import ContractError, inside, validate_manifest
from loop_manager_internal.verification.manifest import Module
from loop_manager_internal.verification.timing import EventWriter, atomic_json, phase
from loop_manager_internal.verification import unittest_adapter
from loop_manager_internal.verification.core import execute_manifest, stop_worker, failure_output

REPORTER = Path(__file__).with_name('perl_reporter.pl')


def command_argv(connection, argv):
    # Compile-time BEGIN blocks need the same guard as tests.
    return [str(connection.tool), '-I' + str(REPORTER.parent), '-MLoopManagerVerifyGuard', *argv[1:]] if argv[0] == '{perl}' else argv


def inventory(connection, stage):
    directory = inside(connection.root, stage['collection']['directory'])
    units = []
    for path in sorted(directory.rglob(stage['collection']['pattern'])):
        if path.is_file():
            inside(connection.root, str(path.relative_to(connection.root)))
            name = path.relative_to(connection.root).as_posix()
            units.append(Module(name, (name,), pattern=stage['collection']['pattern']))
    validate_manifest(units)
    return units


def collect_project(connection, stage, directory, run_id, entrypoint, timeout, execute_command, writer, client):
    path = directory / 'collection-request.json'
    atomic_json(path, {'version': 1, 'run_id': run_id, 'stage_id': stage['id'],
        'tool': str(connection.tool), 'root': str(connection.root), 'stage': stage,
        'result': str(directory / 'collection.json')})
    if not execute_command([sys.executable, str(entrypoint), '--perl-collect', str(path)],
            connection.root / stage['cwd'], timeout, writer, client, run_id, stage['id'] + ':collect'):
        raise ContractError('Perl collection/tool probe failed')
    report = json.loads((directory / 'collection.json').read_text())
    if type(report.get('version')) is not int or any(report.get(key) != value for key, value in
            {'version': 1, 'run_id': run_id, 'stage_id': stage['id']}.items()):
        raise ContractError('Perl collection identity mismatch')
    units = [Module(item['name'], tuple(item['ids']), pattern=item['pattern']) for item in report['modules']]
    validate_manifest(units)
    if units != inventory(connection, stage):
        raise ContractError('Perl collection scope changed')
    return units, report['tool_version']


def collect(request):
    from types import SimpleNamespace
    connection = SimpleNamespace(root=Path(request['root']), tool=Path(request['tool']))
    probe = subprocess.run([str(connection.tool), str(REPORTER), 'version'], capture_output=True, text=True, check=True)
    version = json.loads(probe.stdout)
    atomic_json(Path(request['result']), {**{key: request[key] for key in ('version', 'run_id', 'stage_id')},
        'tool_version': version, 'modules': [asdict(unit) for unit in inventory(connection, request['stage'])]})


def native_report(records, returncode):
    """A file ID owns dynamic TAP assertions; native plan proves complete coverage."""
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records) or not records or records[-1].get('type') != 'summary' or sum(item.get('type') == 'summary' for item in records) != 1:
        raise ContractError('Perl partial/missing native summary')
    summary = records[-1]
    tests = [item for item in records if item.get('type') == 'test']
    if (type(returncode) is not int or any(type(summary.get(key)) is not int for key in ('exit', 'wait'))
            or any(summary.get(key) is not None and (type(summary[key]) is not int or summary[key] < 0)
                for key in ('planned', 'run'))
            or any(type(summary.get(key)) is not bool for key in ('good_plan', 'problems'))
            or not isinstance(summary.get('errors'), list)
            or any(type(item.get('number')) is not int or any(type(item.get(key)) is not bool
                for key in ('ok', 'actual_ok', 'skip', 'todo')) for item in tests)):
        raise ContractError('Perl invalid native result fields')
    numbers = [item['number'] for item in tests]
    complete = (not any(item.get('type') == 'bailout' for item in records) and
        summary['good_plan'] is True and not summary['errors'] and
        type(summary['planned']) is int and type(summary['run']) is int and
        numbers == list(range(1, summary['run'] + 1)) and
        (summary['planned'] == summary['run'] and summary['run'] > 0 or bool(summary['skip_all']) and not tests))
    passed = (complete and returncode == 0 and summary['exit'] == 0 and summary['wait'] == 0
              and summary['problems'] is False and all(item['ok'] is True for item in tests))
    return complete, passed


def validate_report(unit, report, identity):
    if type(report.get('version')) is not int or any(report.get(key) != value for key, value in
            {**identity, 'version': 1, 'unit_id': unit.name, 'module': unit.name}.items()):
        raise ContractError('Perl result identity mismatch')
    if any(any(record.get(key) != value for key, value in {**identity, 'unit_id': unit.name}.items())
           for record in report['native']):
        raise ContractError('Perl native result identity mismatch')
    complete, passed = native_report(report['native'], report['exit_code'])
    if report['completed'] != (list(unit.ids) if complete else []) or report['passed'] is not passed:
        raise ContractError('Perl result coverage mismatch')
    return passed


def worker_main():
    for line in sys.stdin:
        request = json.loads(line)
        unit = Module(request['module'], tuple(request['ids']), pattern=request['pattern'])
        report = {key: request[key] for key in ('version', 'run_id', 'stage_id', 'unit_id')}
        report.update(module=unit.name, passed=False, completed=[])
        writer = EventWriter(Path(request['directory']) / f"worker-{request['worker']}.jsonl", identity=report.copy())
        try:
            # *.jsonl belongs to the common EventWriter protocol, not native TAP records.
            raw = Path(request['result']).with_suffix('.native.ndjson')
            with writer.activate(), phase('module:' + unit.name), raw.open('w') as output:
                process = subprocess.run([os.environ['LOOP_MANAGER_VERIFY_PERL'], str(REPORTER), 'run',
                    str(inside(Path(os.environ['LOOP_MANAGER_VERIFY_PROJECT_ROOT']), unit.name)),
                    str(raw.with_suffix('.tap')), json.dumps({key: request[key] for key in
                        ('version', 'run_id', 'stage_id', 'unit_id')})], stdout=output, check=False)
                records = [json.loads(line) for line in raw.read_text().splitlines()]
                for record in records:
                    writer.emit('native_result', test_id=f"{unit.name}#{record['number']}" if record['type'] == 'test' else unit.name, native=record)
                complete, passed = native_report(records, process.returncode)
                report.update(native=records, exit_code=process.returncode, passed=passed,
                    completed=list(unit.ids) if complete else [])
        except Exception:
            traceback.print_exc()
            report['runner_error'] = True
        finally:
            writer.close()
        atomic_json(Path(request['result']), report)
    return 0


def execute(connection, stage, modules, directory, writer, client, run_id, entrypoint):
    if modules != inventory(connection, stage):
        raise ContractError('Perl execution scope changed after collection')
    identity = {'version': 1, 'run_id': run_id, 'stage_id': stage['id']}
    # Reuse the unittest worker's transport only; no unittest discovery/reporter semantics.
    def spawn(token, number, logs):
        return unittest_adapter.start_worker(token, number, logs,
            command=[sys.executable, '-u', str(entrypoint), '--perl-worker'], cwd=connection.root / stage['cwd'],
            environment=dict(os.environ, LOOP_MANAGER_VERIFY_PROJECT_ROOT=str(connection.root),
                LOOP_MANAGER_VERIFY_PERL=str(connection.tool)), identity=identity)
    parent = client
    if stage['execution'] == 'serial':
        class SerialClient:
            def request(self, operation, **data):
                if operation == 'acquire' and 'token' not in data and active[0]:
                    return {'granted': False, 'reason': 'serial stage'}
                reply = parent.request(operation, **data)
                if operation == 'acquire' and reply['granted']:
                    active[0] = True
                return reply
        active = [False]
        client = SerialClient()
    return execute_manifest(modules, directory, writer, client, spawn=spawn,
        validate_report=lambda unit, report: validate_report(unit, report, identity),
        stop_worker=stop_worker, failure_output=failure_output, coverage=lambda unit, report: report['completed'])
