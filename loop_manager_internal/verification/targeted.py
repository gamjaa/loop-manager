"""개별 targeted test 실행을 기록하는 unittest/JUnit 진입점."""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
from pathlib import Path
import subprocess
import sys
import stat
import unittest
from uuid import uuid4
import xml.etree.ElementTree as ET

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from loop_manager_internal.verification.manifest import test_ids
from loop_manager_internal.verification.timing import EventWriter, TimedResult, fixture_timing
from loop_manager_internal.verification.unittest_adapter import project_imports


RESULTS = frozenset({'success', 'failure', 'error', 'skip', 'expected_failure', 'unexpected_success',
                     'fixture_error', 'indeterminate'})
RUNNER_INSTRUCTION = (
    '프로젝트 Python으로 환경변수 LOOP_MANAGER_TARGETED_TEST_RUNNER의 스크립트를 실행하라. '
    'unittest는 --unittest <test ID·class·module>, JUnit 도구는 --junit -- <명령과 인자>를 사용하고 '
    'XML 출력 인자에 {junit}을 넣어라. 새 실행 결과를 보고하되 전체 verify는 실행하지 마라.'
)


def collect(directory):
    """완료된 개별 결과는 프로세스 중단 때도 보존한다. 불완전한 마지막 줄은 제외한다."""
    events = []
    root = Path(directory).absolute()
    if root.resolve() != root or not root.is_dir():
        raise ValueError('targeted test 기록 폴더가 없거나 교체되었습니다.')
    with os.scandir(root) as entries:
        paths = sorted(root / entry.name for entry in entries
                       if fnmatch.fnmatch(entry.name, 'targeted-*.jsonl'))
    for path in paths:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.resolve().parent != root or info.st_size > 16 * 1024 * 1024:
            raise ValueError('targeted test 기록 경로·크기가 유효하지 않습니다.')
        run_id = path.stem.removeprefix('targeted-')
        if not run_id:
            raise ValueError('targeted test 실행 ID가 비었습니다.')
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(descriptor, encoding='utf-8') as stream:
            opened = os.fstat(stream.fileno())
            if (not stat.S_ISREG(opened.st_mode)
                    or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)):
                raise ValueError('targeted test 기록 파일이 교체되었습니다.')
            content = stream.read(16 * 1024 * 1024 + 1)
            after = os.fstat(stream.fileno())
        current = path.lstat()
        if (len(content.encode('utf-8')) > 16 * 1024 * 1024
                or (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                != (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
                or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns)
                != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)):
            raise ValueError('targeted test 수집 중 기록 파일이 변경되었습니다.')
        seen = set()
        run_results, fixture_errors, suite_error = [], [], False
        for line in content.splitlines(keepends=True):
            if not line.endswith('\n'):
                break
            event = json.loads(line)
            # Timing and Git cost diagnostics carry no test outcome or run ownership.
            if isinstance(event, dict) and event.get('kind') in {
                    'phase_start', 'phase_end', 'git_cost', 'git_cost_incomplete'}:
                continue
            if not isinstance(event, dict) or event.get('run_id') != run_id or event.get('version') != 1:
                raise ValueError('targeted test 실행 identity가 유효하지 않습니다.')
            if event.get('kind') != 'test_end':
                if event.get('kind') == 'fixture_result' and event.get('result') == 'error':
                    fixture_errors.append(event.get('id'))
                elif event.get('kind') == 'execution_error' and event.get('scope') == 'junit_suite':
                    suite_error = True
                continue
            identity = event.get('id')
            if (not isinstance(identity, str) or not identity.strip() or identity in seen
                    or event.get('result') not in RESULTS or not isinstance(event.get('utc'), str)):
                raise ValueError('targeted test 개별 결과가 유효하지 않습니다.')
            seen.add(identity)
            run_results.append(event)
        for event in run_results:
            if event['result'] in {'success', 'skip', 'expected_failure'} and (
                    suite_error or any(_fixture_affects(event['id'], fixture_id)
                                       for fixture_id in fixture_errors)):
                event = {**event, 'native_result': event['result'], 'result': 'indeterminate'}
            events.append(event)
    return tuple(sorted(events, key=lambda event: (event['utc'], event['run_id'], event['id'])))


def _fixture_affects(test_id, fixture_id):
    if not isinstance(fixture_id, str):
        return False
    for fixture in ('setUpModule', 'tearDownModule', 'setUpClass', 'tearDownClass'):
        prefix = f'{fixture} ('
        if fixture_id.startswith(prefix) and fixture_id.endswith(')'):
            owner = fixture_id[len(prefix):-1]
            return test_id == owner or test_id.startswith(owner + '.')
    return test_id == fixture_id


def _junit_report(path):
    if Path(path).stat().st_size > 16 * 1024 * 1024:
        raise ValueError('JUnit 보고서 크기가 한도를 초과했습니다.')
    root = ET.parse(path).getroot()
    if root.tag.rsplit('}', 1)[-1] not in {'testsuite', 'testsuites'}:
        raise ValueError('JUnit testsuite가 필요합니다.')
    results = []
    for case in root.iter():
        if case.tag.rsplit('}', 1)[-1] != 'testcase':
            continue
        name, owner = case.get('name'), case.get('classname')
        if not name or not name.strip() or not owner or not owner.strip():
            raise ValueError('JUnit testcase의 classname과 name이 필요합니다.')
        child_tags = [child.tag.rsplit('}', 1)[-1] for child in case]
        outcomes = [tag for tag in child_tags if tag in {'error', 'failure', 'skipped'}]
        if len(outcomes) > 1:
            raise ValueError('JUnit testcase 결과가 중복되거나 상충합니다.')
        unknown = [tag for tag in child_tags if tag not in {
            'error', 'failure', 'skipped', 'system-out', 'system-err', 'properties'}]
        if outcomes and unknown:
            raise ValueError('JUnit testcase 결과 요소가 상충하거나 지원되지 않습니다.')
        native_result = outcomes[0] if outcomes else ('unknown' if unknown else 'success')
        result = {'failure': 'failure', 'skipped': 'skip', 'error': 'indeterminate',
                  'unknown': 'indeterminate'}.get(native_result, 'success')
        results.append((f'{owner}.{name}', result, native_result))
    if len({identity for identity, _, _ in results}) != len(results):
        raise ValueError('JUnit testcase가 비었거나 ID가 중복됐습니다.')
    suite_error = False
    for suite in root.iter():
        if suite.tag.rsplit('}', 1)[-1] not in {'testsuite', 'testsuites'}:
            continue
        cases = [case for case in suite.iter() if case.tag.rsplit('}', 1)[-1] == 'testcase']
        counts = {
            'errors': sum(any(child.tag.rsplit('}', 1)[-1] == 'error' for child in case) for case in cases),
            'failures': sum(any(child.tag.rsplit('}', 1)[-1] == 'failure' for child in case) for case in cases),
        }
        for kind, count in counts.items():
            raw = suite.get(kind)
            if raw is not None:
                if not raw.isdecimal():
                    raise ValueError('JUnit testsuite 결과 수가 유효하지 않습니다.')
                suite_error = suite_error or int(raw) > count
    parents = {child: parent for parent in root.iter() for child in parent}
    for node in root.iter():
        if node.tag.rsplit('}', 1)[-1] not in {'error', 'failure'}:
            continue
        parent = parents.get(node)
        while parent is not None and parent.tag.rsplit('}', 1)[-1] != 'testcase':
            parent = parents.get(parent)
        suite_error = suite_error or parent is None
    if not results and not suite_error:
        raise ValueError('JUnit testcase가 비었습니다.')
    return results, suite_error


def junit_results(path):
    results, _suite_error = _junit_report(path)
    return [(identity, result) for identity, result, _native_result in results]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--unittest', nargs='+', metavar='TEST_ID')
    mode.add_argument('--junit', action='store_true', help='-- 뒤 명령의 {junit}을 새 보고서 경로로 치환')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    directory = os.environ.get('LOOP_MANAGER_TARGETED_TEST_DIRECTORY')
    if not directory or not Path(directory).is_dir():
        parser.error('Manager가 지정한 targeted test 기록 폴더가 필요합니다.')
    run_id = uuid4().hex
    directory = Path(directory)
    writer = EventWriter(directory / f'targeted-{run_id}.jsonl', identity={'run_id': run_id})
    try:
        if args.unittest:
            if args.command:
                parser.error('--unittest 뒤에는 테스트 ID만 지정합니다.')
            project_imports()
            sys.path.insert(0, str(Path.cwd() / 'tests'))
            loader = unittest.TestLoader()
            suite = loader.loadTestsFromNames(args.unittest)
            identities = tuple(test_ids(suite))
            if loader.errors or not identities or len(identities) != len(set(identities)):
                raise ValueError('targeted unittest 수집 실패·빈 범위·중복 ID입니다.')
            with writer.activate():
                # shortcut: subTest는 상위 test.id()로 집계하며, 파라미터별 차단이 필요하면 reporter를 확장한다.
                with fixture_timing():
                    result = unittest.TextTestRunner(verbosity=2, resultclass=lambda *a, **kw:
                        TimedResult(*a, writer=writer, **kw)).run(suite)
            return 0 if result.wasSuccessful() else 1
        command = args.command[1:] if args.command[:1] == ['--'] else args.command
        if not command or not any('{junit}' in arg for arg in command):
            parser.error('--junit -- 뒤에는 {junit} 보고서 경로를 포함한 명령이 필요합니다.')
        report = directory / f'targeted-{run_id}.xml'
        code = subprocess.run([arg.replace('{junit}', str(report)) for arg in command], check=False).returncode
        if report.is_symlink() or report.resolve().parent != directory.resolve():
            raise ValueError('JUnit 보고서가 기록 폴더를 벗어났습니다.')
        results, suite_error = _junit_report(report)
        for identity, result, native_result in results:
            writer.emit('test_end', id=identity, result=result, native_result=native_result)
        if suite_error:
            writer.emit('execution_error', source='junit', scope='junit_suite')
        if code:
            writer.emit('execution_error', source='junit', returncode=code)
        return code or int(suite_error or any(result in {'failure', 'error', 'indeterminate'}
                                              for _, result, _ in results))
    except (OSError, ValueError, ET.ParseError) as error:
        print(f'targeted test 실행 근거 수집 실패: {error}', file=sys.stderr)
        return 2
    finally:
        writer.close()


if __name__ == '__main__':
    sys.exit(main())
