"""Real Git/SQLite rebase lifecycle coverage; run by Manager full verify only.

Git, journal, stage validation, correction turns, checkpoint recovery and phase
handlers are real. Agent CLI and catalog services use deterministic fixtures.
"""

from dataclasses import replace
import os
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

import blocked_checkpoint
import execution_handlers as handlers
from execution_checkpoint_state import ExecutionCheckpointRepository
from execution_runtime import ExecutionOutcome, ExecutionPhase
from execution_state import ExecutionStatePort
import loop_manager as lm
import manager_git_journal as journal
import runtime_state
from worker_session import SessionBinding
from tests import test_execution_handlers as policy_fixture


class SimulatedProcessExit(BaseException):
    pass


@unittest.skipIf(os.name == "nt", "Linux mode-bit and bash verification fixture")
class ManagedRebaseLifecycleTests(unittest.TestCase):
    def test_checkpoint_multiconflict_skip_restart_verify_review(self):
        self._lifecycle(with_pr=False)

    def test_pr_multiconflict_skip_restart_verify_review(self):
        self._lifecycle(with_pr=True)

    def test_checkpoint_restart_after_journal_seal_before_sqlite(self):
        self._lifecycle(with_pr=False, sealed_restart=True)

    def test_pr_restart_after_journal_seal_before_sqlite(self):
        self._lifecycle(with_pr=True, sealed_restart=True)

    def test_legacy_same_file_replay_restart_before_journal_seal(self):
        self._lifecycle(with_pr=True, legacy=True)

    def test_legacy_same_file_replay_restart_after_journal_seal(self):
        self._lifecycle(with_pr=True, legacy=True, sealed_restart=True)

    def _lifecycle(self, *, with_pr, legacy=False, sealed_restart=False):
        # Reuse only the deterministic service bundle, not the policy fixture's
        # substituted Manager rebase methods or its substituted journal writer.
        real_begin = journal.begin_rebase
        fixture = policy_fixture.ExecutionHandlerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with TemporaryDirectory() as directory, mock.patch.object(journal, 'begin_rebase', real_begin):
            root = Path(directory)
            repo, remote, state_dir = root / 'repo', root / 'remote.git', root / 'state'
            state_dir.mkdir()
            for args in (('init', '--bare', str(remote)), ('init', '-b', 'main', str(repo))):
                subprocess.run(['git', *args], check=True, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
            git = lm.git
            git(repo, 'config', 'user.name', 'Lifecycle Test')
            git(repo, 'config', 'user.email', 'lifecycle@example.test')
            git(repo, 'config', 'core.fileMode', 'true')
            git(repo, 'remote', 'add', 'origin', str(remote))
            lines = ['value=initial\n'] + [f'line-{n}\n' for n in range(1, 25)]
            (repo / 'app.txt').write_text(''.join(lines))
            (repo / 'skip.txt').write_text('initial\n')
            (repo / 'scripts').mkdir()
            # This fixture verification runs only when Manager executes this
            # integration test, never in the Worker's targeted unit command.
            (repo / 'scripts/verify.sh').write_text(
                "#!/usr/bin/env bash\nset -eu\npython3 - <<'CHECK'\n"
                "from pathlib import Path\n"
                "p = Path('app.txt')\n"
                "assert p.read_text().splitlines()[0] == 'value=resolved'\n"
                "assert p.read_text().splitlines()[20] == 'later-content'\n"
                "assert p.stat().st_mode & 0o111\n"
                "assert Path('skip.txt').read_text() == 'base\\n'\n"
                "print('phase-bound candidate verified')\nCHECK\n")
            git(repo, 'add', '.')
            git(repo, 'commit', '-m', 'Initial fixture')
            base = git(repo, 'rev-parse', 'HEAD').stdout.strip()
            git(repo, 'push', '-u', 'origin', 'main')
            branch, worktree = lm.expected_execplan_workspace(state_dir, 'repo', 'G-047')
            worktree.parent.mkdir(parents=True, exist_ok=True)
            git(repo, 'worktree', 'add', '-b', branch, str(worktree), base)

            def commit(path, content, title, *, executable=False):
                target = worktree / path
                target.write_text(content)
                if executable:
                    target.chmod(0o755)
                git(worktree, 'add', '--', path)
                git(worktree, 'commit', '-m', title)

            commit('notes.txt', 'independent\n', 'Independent first commit')
            changed = lines.copy()
            changed[0] = 'value=worker\n'
            commit('app.txt', ''.join(changed), 'Middle conflicting commit')
            if not legacy:
                commit('skip.txt', 'worker\n', 'Second conflict to resolve as empty')
            changed[20] = 'later-content\n'
            commit('app.txt', ''.join(changed), 'Later content and mode', executable=True)
            changed[12] = 'another-later-change\n'
            commit('app.txt', ''.join(changed), 'Another later same-file change')
            original = git(worktree, 'rev-parse', 'HEAD').stdout.strip()
            git(worktree, 'push', 'origin', branch)
            upstream = lines.copy()
            upstream[0] = 'value=base\n'
            (repo / 'app.txt').write_text(''.join(upstream))
            (repo / 'skip.txt').write_text('base\n')
            git(repo, 'add', '.')
            git(repo, 'commit', '-m', 'Conflicting latest base')
            target = git(repo, 'rev-parse', 'HEAD').stdout.strip()
            git(repo, 'push', 'origin', 'main')
            git(repo, 'fetch', 'origin')
            fixture.remote = target

            database = state_dir / 'execution.db'
            connection = sqlite3.connect(database)
            fixture.connection.commit()
            fixture.connection.backup(connection)
            connection.row_factory = sqlite3.Row
            self.addCleanup(lambda: connection.close())
            repository = runtime_state.RuntimeStateRepository(connection)
            state = ExecutionStatePort(connection, repository, 'repo')
            state.update_project(
                'repo', worktree=str(worktree), branch=branch, active_sha=base,
                phase='VERIFYING', publication_remote_oid=original,
                worker_session_id='worker-session',
                worker_session_binding=SessionBinding('codex', 'model', 'high').serialize(),
                worker_result=policy_fixture.READY_WORKER_RESULT,
                pr_url='https://example.test/pr/47' if with_pr else None,
                pr_head_oid=original if with_pr else None,
                pr_conflict_recovery=int(with_pr),
                pr_conflict_head_oid=original if with_pr else None,
                pr_conflict_base_oid=target if with_pr else None,
            )
            if not with_pr:
                # A clean checkpoint can reuse its existing local commit. Bind
                # its legacy identity exactly as the checkpoint publisher does.
                tree = git(worktree, 'rev-parse', 'HEAD^{tree}').stdout.strip()
                identity = blocked_checkpoint.checkpoint_identity(
                    plan_id='G-047', reason_code='verify_retry_exhausted',
                    evidence='fixture checkpoint', parent_oid=original,
                    tree_oid=tree, included_paths=())
                connection.execute(
                    "INSERT INTO blocked_checkpoints "
                    "(repo, plan_id, request_identity, phase, reason_code, evidence, "
                    "branch, worktree, commit_identity, tree_oid, local_oid, "
                    "published_oid, accepted_remote_oid, result) "
                    "VALUES ('repo', 'G-047', 'checkpoint', 'published', "
                    "'verify_retry_exhausted', 'fixture checkpoint', ?, ?, ?, ?, ?, ?, ?, 'published')",
                    (branch, str(worktree), identity, tree, original, original, original))

            effects = replace(fixture.effects, state=state,
                              checkpoint=ExecutionCheckpointRepository(connection))
            effects.workspace.git.side_effect = git
            effects.workspace.is_directory.side_effect = lambda path: path.is_dir()
            effects.workspace.remote_branch_oid.side_effect = lm.remote_branch_oid
            effects.workspace._rebase_in_progress_for_branch.side_effect = lm._rebase_in_progress_for_branch
            effects.workspace.git_operation_entries.side_effect = lm._git_operation_entries
            effects.workspace.validate_managed_worktree.side_effect = lm.validate_managed_worktree
            effects.workspace.validate_execplan_workspace_identity.side_effect = lm.validate_execplan_workspace_identity
            effects.workspace.branch_has_changes.side_effect = lm.branch_has_changes
            effects.agent.capture_git_snapshot.side_effect = lm.capture_git_snapshot
            effects.agent.path_signature.side_effect = lm._path_signature
            effects.agent.begin_agent_scope.side_effect = lambda _repo, path, *_a, **_k: lm.capture_git_snapshot(path)
            effects.agent.validate_agent_scope.side_effect = lm.validate_agent_scope
            effects.agent._guidance_worker_targets.return_value = ()
            effects.publication.pr_for_branch.return_value = (
                {'state': 'OPEN', 'headRefOid': original, 'url': 'https://example.test/pr/47'}
                if with_pr else None)
            events = []
            resolutions = []

            def validate_worker(_repo, path, _branch, _base, plan_path, _catalog, before, **kwargs):
                lm.validate_agent_scope('worker', path, before, lm.capture_git_snapshot(path),
                                        selected_plan_path=plan_path)
                return fixture.plan

            effects.agent.validate_worker_workspace_scope.side_effect = validate_worker

            def agent(_provider, path, _state, _config, role, *_args, **kwargs):
                events.append(role)
                kwargs['on_session_started'](role + '-session')
                if role == 'worker' and lm._rebase_in_progress_for_branch(path, branch):
                    source = git(path, 'rev-parse', 'REBASE_HEAD').stdout.strip()
                    self.assertNotIn(source, resolutions, 'completed correction repeated')
                    resolutions.append(source)
                    conflicts = git(path, 'diff', '--name-only', '--diff-filter=U').stdout.splitlines()
                    if conflicts == ['app.txt']:
                        resolved = upstream.copy()
                        resolved[0] = 'value=resolved\n'
                        (path / 'app.txt').write_text(''.join(resolved))
                    else:
                        self.assertEqual(conflicts, ['skip.txt'])
                        (path / 'skip.txt').write_text('base\n')
                return lm.AgentResult(0, 'VERDICT: PASS' if role == 'reviewer'
                                      else policy_fixture.READY_WORKER_RESULT,
                                      session_id=role + '-session')

            effects.agent.run_agent.side_effect = agent

            def verify(path):
                events.append('verify')
                return lm.verify(path, timeout_seconds=30)

            effects.validation.verify.side_effect = verify

            def new_handler():
                instance = handlers.ExecutionHandlers(
                    effects, repo='repo', repo_path=repo, state_dir=state_dir,
                    base='main', base_sha=target, provider='codex',
                    config_path=root / 'config', newly_activated=False)
                instance.plan = fixture.plan
                instance.catalog_plan = effects.state.active_execplan('repo')
                return instance

            def restart():
                nonlocal connection, effects
                connection.commit()
                connection.close()
                connection = sqlite3.connect(database)
                connection.row_factory = sqlite3.Row
                effects = replace(effects, state=ExecutionStatePort(
                    connection, runtime_state.RuntimeStateRepository(connection), 'repo'),
                    checkpoint=ExecutionCheckpointRepository(connection))
                return new_handler()

            if legacy:
                # Emit the pre-v2 journal format at creation time. Keep actual
                # Git observations, approved tree and reflog invocation IDs;
                # do not downgrade or rewrite any sealed modern receipt.
                def begin_legacy(state_path, **kwargs):
                    expected = kwargs['expected']
                    kwargs['expected'] = {
                        key: expected[key] for key in (
                            'branch', 'before_head', 'target_base_oid',
                            'remote_oid', 'affected_paths', 'agent_session_id',
                            'action', 'accepted_files', 'staged_tree_oid',
                        ) if key in expected
                    }
                    current = effects.checkpoint.get('repo', 'G-047')
                    if (current.snapshot or {}).get('rebase_evidence_version') == 2:
                        effects.checkpoint.record(
                            'repo', 'G-047', expected_generation=current.generation,
                            operation=current.operation, expected_head=current.expected_head,
                            target_base_oid=current.target_base_oid,
                            observed_remote_oid=current.observed_remote_oid,
                            snapshot={key: value for key, value in current.snapshot.items()
                                      if not key.startswith('rebase_')},
                        )
                    return real_begin(state_path, **kwargs)

                journal.begin_rebase = begin_legacy

            handler = new_handler()
            handler._rebase_latest(handler._context(), handler._row(), target, publishing=with_pr)
            self.assertTrue(lm._rebase_in_progress_for_branch(worktree, branch))
            handler = restart()
            conflict_context = handler.recover(handler._context())
            self.assertNotIsInstance(conflict_context, ExecutionOutcome)
            self.assertTrue(conflict_context.resolving_rebase)
            self.assertEqual(events, [])
            if not legacy:
                handler.run(conflict_context)
                self.assertEqual(len(resolutions), 1)
                self.assertEqual(git(worktree, 'diff', '--name-only', '--diff-filter=U').stdout.strip(), 'skip.txt')
            real_post = handler._post_rebase_observation

            def crash_after_git(context, target_oid, output, **kwargs):
                if not lm._rebase_in_progress_for_branch(worktree, branch):
                    raise SimulatedProcessExit()
                return real_post(context, target_oid, output, **kwargs)

            crash = (mock.patch.object(effects.state, 'complete_rebase',
                                       side_effect=SimulatedProcessExit)
                     if sealed_restart else mock.patch.object(
                         handler, '_post_rebase_observation', side_effect=crash_after_git))
            with crash:
                with self.assertRaises(SimulatedProcessExit):
                    handler.run(handler._context())
            self.assertEqual(len(resolutions), 1 if legacy else 2)
            final_head = git(worktree, 'rev-parse', 'HEAD').stdout.strip()
            slot = journal._slot(state_dir, str(repo.resolve()), 'managed_rebase', str(worktree.resolve()))
            self.assertEqual((slot / 'active.json').exists(), not sealed_restart)
            sealed_before = {path.name: path.read_bytes()
                             for path in slot.glob('*.result.json')}
            intents_before = {path.name: path.read_bytes()
                              for path in slot.glob('*.intent.json')}
            if sealed_restart:
                final_results = [journal._read(path) for path in slot.glob('*.result.json')
                                 if journal._read(path)['observed']['metadata'] is None]
                self.assertEqual(len(final_results), 1)
                sealed_id = final_results[0]['id']
                self.assertIsNone(effects.checkpoint.get('repo', 'G-047').completed_rebase())
            self.assertEqual(effects.checkpoint.get('repo', 'G-047').operation, 'rebase')
            audit_before = handler.rebase_corrections.get('repo', 'G-047').audit
            handler = restart()

            def no_repeated_rebase(path, *args, **kwargs):
                if 'rebase' in args:
                    self.fail('restart repeated completed Git rebase')
                return git(path, *args, **kwargs)

            effects.workspace.git.side_effect = no_repeated_rebase
            recovered = handler.recover(handler._context())
            self.assertNotIsInstance(recovered, ExecutionOutcome)
            completed_checkpoint = effects.checkpoint.get('repo', 'G-047')
            self.assertIsNone(completed_checkpoint.operation)
            for name, content in {**sealed_before, **intents_before}.items():
                self.assertEqual((slot / name).read_bytes(), content)
            if sealed_restart:
                self.assertEqual(completed_checkpoint.snapshot['rebase_completion']['id'], sealed_id)
            handler._complete_rebase_intent(handler._context())
            self.assertEqual(effects.checkpoint.get('repo', 'G-047'), completed_checkpoint)
            self.assertEqual(effects.state.row()['worker_session_id'], 'worker-session')
            self.assertEqual(handler.rebase_corrections.get('repo', 'G-047').audit, audit_before)
            self.assertEqual(len(resolutions), 1 if legacy else 2)
            transitions = journal.verified_managed_transitions(
                state_dir, str(repo.resolve()), 'plan:G-047',
                agent_session_id='worker-session', git=git)
            completed = [item for item in transitions
                         if item['result']['observed']['metadata'] is None]
            self.assertEqual(len(completed), 1)
            proof = completed[0]['result']['replay_proof']
            self.assertEqual([item['outcome'] for item in proof],
                             (['patch', 'resolved', 'patch', 'patch'] if legacy else
                              ['patch', 'resolved', 'skip', 'patch', 'patch']))
            self.assertNotEqual(proof[1]['tree'], proof[-1]['tree'])
            context = handler.guard(recovered)
            self.assertNotIsInstance(context, ExecutionOutcome)
            if context.phase is ExecutionPhase.IMPLEMENTING:
                context = handler.run(context).context
            self.assertEqual(context.phase, ExecutionPhase.VERIFYING)
            verified = handler.verify(context, handler)
            self.assertEqual(verified.context.phase, ExecutionPhase.REVIEWING, verified.detail)
            reviewed = handler.review(verified.context)
            self.assertEqual(reviewed.context.phase, ExecutionPhase.PUBLISHING, reviewed.detail)
            self.assertEqual(events.count('verify'), 1)
            self.assertEqual(events[-2:], ['verify', 'reviewer'])
            self.assertEqual(len(resolutions), 1 if legacy else 2)
            self.assertEqual(git(worktree, 'rev-parse', 'HEAD').stdout.strip(), final_head)
            self.assertIn('phase-bound candidate verified', effects.state.row()['verify_result'])
            effects.publication.push_managed_publication_branch.assert_not_called()
