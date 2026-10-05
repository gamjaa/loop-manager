"""복구 준비 자료의 실제 파일·Git·DB 격리와 손상 거부를 검증한다."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

from tests.support.git import make_remote_repository, git_command, _remote_repository_seed
from tests.support.recovery_boundary import RecoveryBoundary
from loop_manager_internal.verification.git_timing import git_command_timing
from loop_manager_internal.verification.timing import EventWriter, phase, summarize
import loop_manager as manager


class RecoveryBoundaryTests(unittest.TestCase):
    def test_reused_preparation_reduces_commands_including_copy_and_validation(self):
        # 두 비교 모두 기존 초기 저장소 seed가 준비된 동일 조건이다.
        _remote_repository_seed()
        costs = {}
        for reuse in (False, True):
            with RecoveryBoundary() as logs:
                writer = EventWriter(Path(logs.directory) / 'worker.jsonl')
                def prepare(boundary):
                    repo = make_remote_repository(boundary.directory)
                    connection = sqlite3.connect(':memory:')
                    connection.execute('CREATE TABLE checkpoint (repo TEXT)')
                    connection.execute('INSERT INTO checkpoint VALUES (?)', (str(repo),))
                    before = manager.capture_git_snapshot(repo)
                    return repo, connection, before
                try:
                    with writer.activate(), git_command_timing(), phase('representative_preparation'):
                        with RecoveryBoundary() as first:
                            repo, connection, before = prepare(first)
                            if reuse:
                                first.save(connection)
                                (repo / 'src/app.py').write_text('failed variant')
                                first.restore(connection)
                                self.assertEqual(manager.capture_git_snapshot(repo), before)
                            else:
                                connection.close()
                                with RecoveryBoundary() as second:
                                    other_repo, other_connection, other_before = prepare(second)
                                    self.assertEqual(manager.capture_git_snapshot(other_repo), other_before)
                                    other_connection.close()
                finally:
                    writer.close()
                summary = summarize(Path(logs.directory))
                preparation_phases = {}
                for item in summary['phases']:
                    if item['name'].startswith('git_fixture:'):
                        preparation_phases[item['name']] = preparation_phases.get(item['name'], 0) + item['duration']
                costs['reuse' if reuse else 'independent'] = {
                    'commands': sum(item['count'] for item in summary['git_commands']),
                    'snapshots': sum(item['count'] for item in summary['git_snapshots'].values()),
                    'seconds': next(item['duration'] for item in summary['phases']
                                    if item['name'] == 'representative_preparation'),
                    'preparation_phases': preparation_phases,
                }
        self.assertLess(costs['reuse']['commands'], costs['independent']['commands'])
        self.assertEqual(costs['reuse']['snapshots'], 2)
        self.assertEqual(costs['independent']['snapshots'], 3)
        print('R070_PREPARATION_COST ' + json.dumps(costs, sort_keys=True))

    def test_real_git_and_sqlite_restore_without_rewriting_identity(self):
        for disk in (False, True):
            with self.subTest(disk=disk), RecoveryBoundary() as boundary:
                root = Path(boundary.directory)
                repo = make_remote_repository(boundary.directory)
                worktree = root / 'worktree'
                git_command(repo, 'worktree', 'add', '-b', 'owned', str(worktree))
                git_command(worktree, 'push', 'origin', 'owned')
                database = str(root / 'state.db') if disk else ':memory:'
                connection = sqlite3.connect(database)
                connection.execute('CREATE TABLE checkpoint (identity TEXT, generation INTEGER)')
                connection.execute('INSERT INTO checkpoint VALUES (?, 7)', (str(worktree),))
                journal = root / 'journal.json'
                journal.write_text(str(worktree), encoding='utf-8')
                (worktree / 'src/app.py').write_text('staged\n', encoding='utf-8')
                git_command(worktree, 'add', 'src/app.py')
                index = (repo / '.git/worktrees/worktree/index').read_bytes()
                head = git_command(worktree, 'rev-parse', 'HEAD')
                boundary.save(connection)
                (worktree / 'src/app.py').write_text('other variant\n', encoding='utf-8')
                git_command(worktree, 'add', '.')
                git_command(worktree, 'commit', '-m', 'Other variant')
                git_command(worktree, 'push', 'origin', 'owned')
                journal.write_text('invalid', encoding='utf-8')
                connection.execute('UPDATE checkpoint SET generation=99')
                connection.commit()
                with mock.patch('subprocess.Popen', side_effect=AssertionError('restore must not rerun Git')):
                    connection = boundary.restore(connection)
                self.assertEqual(tuple(connection.execute('SELECT * FROM checkpoint').fetchone()), (str(worktree), 7))
                self.assertEqual(journal.read_text(encoding='utf-8'), str(worktree))
                self.assertEqual(git_command(worktree, 'rev-parse', 'HEAD'), head)
                self.assertEqual(git_command(repo, 'ls-remote', '--heads', 'origin', 'owned').split()[0], head)
                self.assertEqual((repo / '.git/worktrees/worktree/index').read_bytes(), index)
                self.assertEqual((worktree / 'src/app.py').read_text(), 'staged\n')
                # 준비 자료 손상은 실패이며 현재 작업을 먼저 삭제하지 않는다.
                (boundary.archive / 'files/journal.json').write_text('corrupt')
                with self.assertRaisesRegex(RuntimeError, 'digest'):
                    boundary.restore(connection)
                self.assertEqual(journal.read_text(), str(worktree))

    def test_independent_parallel_variants_and_failed_variant_restore(self):
        def variant(value):
            with RecoveryBoundary() as boundary:
                root = Path(boundary.directory)
                (root / 'content').write_text(value)
                connection = sqlite3.connect(':memory:')
                connection.execute('CREATE TABLE state (value TEXT)')
                connection.execute('INSERT INTO state VALUES (?)', (value,))
                boundary.save(connection)
                try:
                    (root / 'content').write_text('interrupted')
                    connection.execute("UPDATE state SET value='interrupted'")
                    raise KeyboardInterrupt()
                except KeyboardInterrupt:
                    boundary.restore(connection)
                self.assertEqual((root / 'content').read_text(), value)
                self.assertEqual(connection.execute('SELECT value FROM state').fetchone()[0], value)
                return boundary.directory
        with ThreadPoolExecutor(max_workers=2) as pool:
            paths = list(pool.map(variant, ('first', 'second')))
        self.assertNotEqual(*paths)
        self.assertTrue(all(not Path(path).exists() for path in paths))

    def test_missing_boundary_and_changed_database_are_not_success(self):
        with RecoveryBoundary() as boundary:
            connection = sqlite3.connect(':memory:')
            self.addCleanup(connection.close)
            with self.assertRaisesRegex(RuntimeError, '없습니다'):
                boundary.restore(connection)
            boundary.save(connection)
            boundary.database.execute('CREATE TABLE corrupted (value TEXT)')
            with self.assertRaisesRegex(RuntimeError, 'identity'):
                boundary.restore(connection)
