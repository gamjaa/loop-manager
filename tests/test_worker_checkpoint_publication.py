import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from loop_manager_internal.git import github_observation, publication_service
from loop_manager_internal.storage import database
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from tests.support.git import git_command, git_test_environment, local_github_origin, make_remote_repository


class RemoteWorkAdoptionDatabaseTests(unittest.TestCase):
    def test_only_a_database_created_from_absence_can_adopt_first_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            connection = database.connect_database(
                state_dir, create=True, mark_created_from_absence=True,
            )
            try:
                self.assertTrue(database.remote_work_adoption_allowed(connection, 'repo', 'G-001'))
                database.record_execplan_workspace_seen(connection, 'repo', 'G-001')
                self.assertFalse(database.remote_work_adoption_allowed(connection, 'repo', 'G-001'))
            finally:
                connection.close()

            existing_dir = state_dir / 'existing'
            existing_dir.mkdir()
            existing_path = existing_dir / 'loop-manager.db'
            existing = sqlite3.connect(existing_path)
            existing.executescript(database.SCHEMA)
            existing.close()
            existing = database.connect_database(existing_dir)
            try:
                self.assertFalse(database.remote_work_adoption_allowed(existing, 'repo', 'G-001'))
            finally:
                existing.close()


class PullRequestHistoryTests(unittest.TestCase):
    def test_merged_history_wins_over_an_open_pr_for_the_same_branch(self):
        effects = github_observation.PrForBranchEffects(command=mock.Mock(return_value=subprocess.CompletedProcess(
            [], 0, json.dumps([
                {'url': 'open', 'state': 'OPEN', 'mergedAt': None, 'headRefOid': 'a' * 40,
                 'headRefName': 'loop-manager/G-001', 'baseRefName': 'main', 'mergeable': 'MERGEABLE'},
                {'url': 'merged-old', 'state': 'MERGED', 'mergedAt': '2026-01-01T00:00:00Z',
                 'headRefOid': 'b' * 40, 'headRefName': 'loop-manager/G-001',
                 'baseRefName': 'develop', 'mergeable': None,
                 'mergeCommit': {'oid': 'c' * 40}},
            ]), '')))

        result = github_observation.pr_for_branch(
            effects, Path('/repo'), 'loop-manager/G-001', history=True,
        )

        self.assertEqual(result['url'], 'merged-old')
        arguments = effects.command.call_args.args[0]
        self.assertEqual(arguments[:5], ['gh', 'pr', 'list', '--head', 'loop-manager/G-001'])
        self.assertIn('--state', arguments)
        self.assertEqual(arguments[arguments.index('--state') + 1], 'all')

    def test_incomplete_history_and_multiple_open_prs_are_not_guessed(self):
        saturated = github_observation.PrForBranchEffects(command=mock.Mock(return_value=
            subprocess.CompletedProcess([], 0, '[]', '')))
        saturated.command.return_value.stdout = json.dumps([{}] * 1000)
        with self.assertRaisesRegex(RuntimeError, '목록 응답'):
            github_observation.pr_for_branch(
                saturated, Path('/repo'), 'loop-manager/G-001', history=True,
            )

        opened = [{'url': f'open-{i}', 'state': 'OPEN', 'mergedAt': None, 'headRefOid': 'a' * 40,
                   'headRefName': 'loop-manager/G-001', 'baseRefName': 'main', 'mergeable': 'MERGEABLE'}
                  for i in range(2)]
        ambiguous = github_observation.PrForBranchEffects(command=mock.Mock(return_value=
            subprocess.CompletedProcess([], 0, json.dumps(opened), '')))
        with self.assertRaisesRegex(RuntimeError, '열린 PR이 여러 개'):
            github_observation.pr_for_branch(
                ambiguous, Path('/repo'), 'loop-manager/G-001', history=True,
            )


class WorkerCheckpointPublicationTests(unittest.TestCase):
    def _repository(self, directory):
        repo = make_remote_repository(directory)
        origin = local_github_origin(self, repo)
        branch = 'loop-manager/G-001'
        git_command(repo, 'switch', '-c', branch)
        git_command(repo, 'config', 'user.email', 'worker@example.com')
        git_command(repo, 'config', 'user.name', 'Worker')
        base = git_command(repo, 'rev-parse', 'HEAD')
        for index in range(2):
            (repo / 'src/app.py').write_text(f'VALUE = {index + 2}\n', encoding='utf-8')
            git_command(repo, 'add', 'src/app.py')
            git_command(repo, 'commit', '-m', f'Checkpoint {index + 1}')

        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        connection.execute(database.SCHEMA)
        database._ensure_execplans_schema(connection)
        connection.execute(
            "INSERT INTO projects(repo, base_branch, origin, active_sha, phase, branch, worktree, plan_path) "
            "VALUES (?, 'main', ?, ?, 'WORKING', ?, ?, ?)",
            (str(repo), origin, base, branch, str(repo), 'docs/plans/[G-001] Resume.md'),
        )
        connection.execute(
            "INSERT INTO exec_plans(repo, plan_id, path, title, sources, depends_on, priority, content_hash, "
            "status, plan_sha, active_sha, branch, worktree, workspace_owned) "
            "VALUES (?, 'G-001', 'docs/plans/[G-001] Resume.md', 'Resume', '[]', '[]', 'normal', 'hash', "
            "'WORKING', ?, ?, ?, ?, 1)",
            (str(repo), base, base, branch, str(repo)),
        )
        connection.commit()
        return repo, origin, branch, base, connection

    @staticmethod
    def _git(path, *arguments, check=True):
        result = subprocess.run(
            ['git', '-C', str(path), *arguments], capture_output=True, text=True,
            encoding='utf-8', errors='replace', env=git_test_environment(), check=False,
        )
        if check and result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        return result

    def test_pushes_each_linear_worker_commit_with_successive_exact_leases(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, origin, branch, base, connection = self._repository(directory)
            self.addCleanup(connection.close)
            state_dir = Path(directory) / 'state'
            state_dir.mkdir()
            effects = publication_service.WorkerCheckpointPublicationEffects(
                active_execplan=lambda _connection, selected_repo: connection.execute(
                    'SELECT * FROM exec_plans WHERE repo=?', (selected_repo,)).fetchone(),
                git=self._git,
                remote_branch_oid=lambda *_: None,
                pr_for_branch=lambda *_: None,
                github_repository=lambda _url: origin,
                validate_execplan_workspace_identity=mock.Mock(),
                validate_managed_worktree=mock.Mock(),
                update_project=lambda _connection, selected_repo, **values: connection.execute(
                    'UPDATE exec_plans SET ' + ', '.join(f'{key}=?' for key in values) + ' WHERE repo=?',
                    (*values.values(), selected_repo),
                ),
            )
            pushes = []
            original_git = effects.git

            def record_push(path, *arguments, **options):
                if arguments and arguments[0] == 'push':
                    pushes.append(arguments)
                return original_git(path, *arguments, **options)

            effects = publication_service.WorkerCheckpointPublicationEffects(
                **{**effects.__dict__, 'git': record_push},
            )

            head, pushed = publication_service.push_worker_checkpoint_commits(
                effects, connection, repo, state_dir, str(repo), 'G-001', branch,
                repo, 'main', None, None,
            )

            self.assertEqual(pushed, 2)
            self.assertEqual(git_command(repo.parent / 'remote.git', 'rev-parse', f'refs/heads/{branch}'), head)
            self.assertEqual(len(pushes), 2)
            self.assertEqual(pushes[0][1], f'--force-with-lease=refs/heads/{branch}:')
            self.assertEqual(pushes[1][1], f'--force-with-lease=refs/heads/{branch}:{pushes[0][3].split(":")[0]}')
            self.assertEqual(connection.execute(
                'SELECT publication_remote_oid FROM exec_plans WHERE repo=?', (str(repo),)
            ).fetchone()[0], head)
            checkpoint = ExecutionCheckpointRepository(connection).get(str(repo), 'G-001')
            self.assertEqual(checkpoint.snapshot['accepted_work_branch'], head)
            self.assertNotIn('worker_checkpoint_pending', checkpoint.snapshot)

    def test_a_push_confirmed_before_sqlite_interruption_is_not_repeated(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, origin, branch, base, connection = self._repository(directory)
            self.addCleanup(connection.close)
            state_dir = Path(directory) / 'state'
            state_dir.mkdir()
            first = git_command(repo, 'rev-parse', 'HEAD~1')
            git_command(repo, 'push', 'origin', f'{first}:refs/heads/{branch}')
            checkpoint = ExecutionCheckpointRepository(connection)
            checkpoint.record(
                str(repo), 'G-001', expected_generation=0, operation=None,
                expected_head=first, target_base_oid=base, observed_remote_oid=base,
                snapshot={'worker_checkpoint_pending': {
                    'branch': branch, 'from': base, 'target': first,
                }},
            )
            effects = publication_service.WorkerCheckpointPublicationEffects(
                active_execplan=lambda _connection, selected_repo: connection.execute(
                    'SELECT * FROM exec_plans WHERE repo=?', (selected_repo,)).fetchone(),
                git=self._git,
                remote_branch_oid=lambda *_: first,
                pr_for_branch=lambda *_: None,
                github_repository=lambda _url: origin,
                validate_execplan_workspace_identity=mock.Mock(),
                validate_managed_worktree=mock.Mock(),
                update_project=lambda _connection, selected_repo, **values: connection.execute(
                    'UPDATE exec_plans SET ' + ', '.join(f'{key}=?' for key in values) + ' WHERE repo=?',
                    (*values.values(), selected_repo),
                ),
            )

            head, pushed = publication_service.push_worker_checkpoint_commits(
                effects, connection, repo, state_dir, str(repo), 'G-001', branch,
                repo, 'main', first, None,
            )

            self.assertEqual(pushed, 1)
            self.assertEqual(git_command(repo.parent / 'remote.git', 'rev-parse', f'refs/heads/{branch}'), head)
            self.assertNotIn('worker_checkpoint_pending', checkpoint.get(str(repo), 'G-001').snapshot)

    def test_remote_advance_is_never_overwritten_by_a_stale_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, origin, branch, _base, connection = self._repository(directory)
            self.addCleanup(connection.close)
            state_dir = Path(directory) / 'state'
            state_dir.mkdir()
            upstream = Path(directory) / 'upstream'
            subprocess.run(['git', 'clone', str(repo.parent / 'remote.git'), str(upstream)], check=True,
                           capture_output=True, env=git_test_environment())
            git_command(upstream, 'switch', '-c', branch)
            git_command(upstream, 'config', 'user.email', 'other@example.com')
            git_command(upstream, 'config', 'user.name', 'Other')
            (upstream / 'src/app.py').write_text('VALUE = 99\n', encoding='utf-8')
            git_command(upstream, 'add', 'src/app.py')
            git_command(upstream, 'commit', '-m', 'Concurrent remote change')
            git_command(upstream, 'push', 'origin', f'HEAD:refs/heads/{branch}')
            remote_head = git_command(upstream, 'rev-parse', 'HEAD')
            local_head = git_command(repo, 'rev-parse', 'HEAD')
            effects = publication_service.WorkerCheckpointPublicationEffects(
                active_execplan=lambda _connection, selected_repo: connection.execute(
                    'SELECT * FROM exec_plans WHERE repo=?', (selected_repo,)).fetchone(),
                git=self._git,
                remote_branch_oid=lambda *_: None,
                pr_for_branch=lambda *_: None,
                github_repository=lambda _url: origin,
                validate_execplan_workspace_identity=mock.Mock(),
                validate_managed_worktree=mock.Mock(),
                update_project=mock.Mock(),
            )
            with self.assertRaisesRegex(RuntimeError, '게시 전 원격 branch가 전진'):
                publication_service.push_worker_checkpoint_commits(
                    effects, connection, repo, state_dir, str(repo), 'G-001', branch,
                    repo, 'main', None, None,
                )
            self.assertEqual(git_command(upstream, 'rev-parse', 'HEAD'), remote_head)
            self.assertEqual(git_command(repo, 'rev-parse', 'HEAD'), local_head)
