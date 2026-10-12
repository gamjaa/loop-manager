"""수동 준비와 자동 실행·게시가 경합할 때의 소유권 및 작업물 보존."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, ExitStack
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

import loop_manager as manager
from loop_manager_internal.application.manual_work import ManualWorkService, ManualWorkError
from loop_manager_internal.application.manual_concurrency import group_resources
from loop_manager_internal.maintenance.maintenance_planning import PlanRevision
from loop_manager_internal.runtime.process_lock import process_lock, _held_process_lock_identity
from loop_manager_internal.runtime.resource_lock import resource_locks
from loop_manager_internal.storage import maintenance_state, runtime_state, work_claims
from loop_manager_internal.agents import agent_write_isolation
from loop_manager_internal.planning import manual_plan_generation
from tests.support import manual_work as manual_tests
from tests.support.catalog import write_execplan
from tests.support.git import git_command as git, commit_and_push, isolated_git_test
from tests.support.origin import remote_fixture, OriginScopeFixture
from tests.support.workspace import WorkspaceFixture


@isolated_git_test
class ManualConcurrencyTests(WorkspaceFixture, unittest.TestCase):
    fixture = manual_tests.ManualWorkFixture.fixture

    def plans(self, service):
        paths = [write_execplan(service.repo_path, key, title) for key, title in
                 [('G-169', 'Verifying'), ('G-188', 'Independent')]]
        commit_and_push(service.repo_path, 'Add independent plans')
        head = git(service.repo_path, 'rev-parse', 'origin/main')
        manager.register_execplans(service.connection, service.repo, service.repo_path,
            [(path.relative_to(service.repo_path).as_posix(), manager._parse_execplan(service.repo_path, path))
             for path in paths], head, manager.planning_input_fingerprint(service.repo_path, 'main'))
        service.connection.commit()
        return head

    def activate(self, service, key, head):
        branch, path = manager.expected_execplan_workspace(service.state_dir, service.repo, key)
        service.runtime.activate(service.repo, key, activation_sha=head, branch=branch, worktree=str(path))
        service.runtime.transition(service.repo, key, plan_values={'status': 'VERIFYING'},
                                   project_values={'phase': 'VERIFYING'}, enqueue=False)

    def test_g188_prepares_and_pushes_during_g169_verify_without_changing_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _, _ = self.fixture(directory)
            self.activate(service, 'G-169', self.plans(service))
            before = dict(service.connection.execute('SELECT * FROM projects').fetchone())
            with process_lock(service.state_dir, identity='automatic-verify'):
                self.assertEqual(service.start('G-188'), 0)
                with self.assertRaisesRegex(ManualWorkError, 'G-169.*VERIFYING.*소유권'):
                    service.start('G-169')
                self.assertEqual(service.complete(manual_tests.completion('G-188')), 1)
            branch, path = manager.expected_execplan_workspace(service.state_dir, service.repo, 'G-188')
            self.assertTrue(path.is_dir())
            self.assertEqual(git(path, 'rev-parse', 'HEAD'), git(service.repo_path, 'ls-remote', '--heads', 'origin', branch).split()[0])
            self.assertEqual(dict(service.connection.execute('SELECT * FROM projects').fetchone()), before)
            self.assertEqual(service.runtime.get(service.repo, 'G-188').status, 'MANUAL_PENDING')
            self.assertIsNone(service.store.for_plan(service.repo, 'G-169'))
            service.effects.run_project.assert_not_called()

    def test_same_g_parallel_start_reserves_and_creates_only_once(self):
        with tempfile.TemporaryDirectory() as directory:
            service, branch, path = self.fixture(directory)
            database = service.state_dir / 'test.db'
            service.state_dir.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(database)) as target, target:
                service.connection.backup(target)
            entered, release = threading.Event(), threading.Event()
            real_make = service.effects.make_worktree
            def paused(*args, **kwargs):
                entered.set()
                if not release.wait(10):
                    raise RuntimeError('test synchronization timeout')
                return real_make(*args, **kwargs)
            make = mock.Mock(side_effect=paused)
            def command(effects):
                with closing(sqlite3.connect(database)) as connection, connection:
                    connection.row_factory = sqlite3.Row
                    return ManualWorkService(effects, connection, service.state_dir, service.repo_path, service.config_path).start('G-012')
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(command, replace(service.effects, make_worktree=make))
                try:
                    self.assertTrue(entered.wait(10))
                    second = executor.submit(command, service.effects)
                    with self.assertRaisesRegex(ManualWorkError, '자원 충돌.*소유자'):
                        second.result(timeout=10)
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=10), 0)
            self.assertEqual(make.call_count, 1)
            with closing(sqlite3.connect(database)) as connection, connection:
                self.assertEqual(connection.execute('SELECT count(*) FROM manual_work_groups').fetchone()[0], 1)
                self.assertEqual(connection.execute('SELECT count(*) FROM manual_work_members').fetchone()[0], 1)
            self.assertTrue(path.is_dir())
            self.assertEqual(git(path, 'rev-parse', 'HEAD'), git(service.repo_path, 'ls-remote', '--heads', 'origin', branch).split()[0])

    def test_worker_wins_before_manual_atomic_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _, path = self.fixture(directory)
            original = service.effects.fetch_base
            def activate_after_fetch(*args, **kwargs):
                head = original(*args, **kwargs)
                self.activate(service, 'G-012', head)
                return head
            service.effects = replace(service.effects, fetch_base=activate_after_fetch)
            with self.assertRaisesRegex(ManualWorkError, 'VERIFYING.*소유권'):
                service.start('G-012')
            self.assertIsNone(service.store.for_plan(service.repo, 'G-012'))
            self.assertFalse(path.exists())
            self.assertEqual(service.runtime.get(service.repo, 'G-012').status, 'VERIFYING')

    def test_manual_reservation_wins_before_worker_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            service, branch, path = self.fixture(directory)
            original = service.effects.make_worktree
            def worker_attempt(*args, **kwargs):
                with self.assertRaises(runtime_state.RuntimeStateError):
                    service.runtime.activate(service.repo, 'G-012', activation_sha=git(service.repo_path, 'rev-parse', 'HEAD'),
                                             branch=branch, worktree=str(path))
                return original(*args, **kwargs)
            service.effects = replace(service.effects, make_worktree=worker_attempt)
            self.assertEqual(service.start('G-012'), 0)
            self.assertIsNone(service.runtime.projected(service.repo))

    def publication(self, service, key='G-012', *, new=False):
        base = git(service.repo_path, 'rev-parse', 'origin/main')
        snapshot = manager.mutable_ready_snapshot(service.connection, service.repo, service.repo_path, base)
        original = snapshot.by_id[key] if not new else None
        path = Path(service.repo_path).parent / 'planner-candidate'
        git(service.repo_path, 'worktree', 'add', '--detach', str(path), base)
        if new:
            document = write_execplan(path, key, 'Planner addition')
            relative = document.relative_to(path).as_posix()
        else:
            relative = original.path
            document = path / relative
            document.write_text(document.read_text() + '\n## Validation\n\nKeep the current goal and verify concurrency.\n', newline='\n')
        plan = manager._parse_execplan(path, document)
        git(path, 'add', '--', relative)
        git(path, 'commit', '-m', 'Publish selected plan')
        head = git(path, 'rev-parse', 'HEAD')
        content = manager.git_bytes(path, 'show', f'{head}:{relative}')
        result = manager.ValidatedPlanningResult(plans=((relative, plan),) if new else (), publication_paths=(relative,),
            publication_hashes=((relative, hashlib.sha256(content).hexdigest()),),
            revisions=() if new else (PlanRevision(relative, plan, original),), mutable_snapshot=snapshot)
        return path, result, head

    def record(self, service, candidate):
        path, result, head = candidate
        return manager.record_general_planning_publication_intent(service.connection, service.repo,
                                                                  path, result, head, 'spec', 'planning')

    def publish(self, service, candidate):
        path, result, head = candidate
        git(path, 'push', manager.manager_git_journal.exact_branch_lease('main', result.mutable_snapshot.active_sha),
            'origin', f'{head}:refs/heads/main')
        manager._recorded_base_fetch(service.repo_path, service.state_dir, 'main', owner='project')
        manager.publish_general_planning_batch(service.connection, service.repo, path, 'main', result, head, 'spec', 'planning')

    def test_manual_first_rejects_stale_planner_snapshot_before_push(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _, _ = self.fixture(directory)
            candidate = self.publication(service)
            before = git(service.repo_path, 'ls-remote', '--heads', 'origin', 'main')
            service.start('G-012')
            with self.assertRaisesRegex(manager.PlannerValidationError, '실행 상태가 변경'):
                self.record(service, candidate)
            self.assertEqual(git(service.repo_path, 'ls-remote', '--heads', 'origin', 'main'), before)
            self.assertFalse(work_claims.publication_claims(service.connection, service.repo)[0])

    def test_planner_first_blocks_start_through_push_until_db_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _, _ = self.fixture(directory)
            candidate = self.publication(service)
            self.record(service, candidate)
            for pushed in (False, True):
                with self.subTest(pushed=pushed):
                    if pushed:
                        git(candidate[0], 'push', 'origin', f'{candidate[2]}:refs/heads/main')
                    with self.assertRaisesRegex(ManualWorkError, 'Planner 게시 소유 예약.*G-012'):
                        service.start('G-012')
                    with self.assertRaisesRegex(runtime_state.RuntimeStateError, 'Planner 게시'):
                        self.activate(service, 'G-012', candidate[2])
                    self.assertIsNone(service.store.for_plan(service.repo, 'G-012'))
            self.publish(service, candidate)
            self.assertEqual(service.start('G-012'), 0)
            group = service.store.for_plan(service.repo, 'G-012')
            self.assertEqual(group['base_sha'], candidate[2])
            self.assertIn('verify concurrency', json.loads(group['documents'])[0]['text'])

    def test_unrelated_start_does_not_invalidate_prepared_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _, _ = self.fixture(directory)
            self.plans(service)
            candidate = self.publication(service, 'G-169')
            self.record(service, candidate)
            self.assertEqual(service.start('G-188'), 0)
            self.publish(service, candidate)
            self.assertEqual(service.runtime.get(service.repo, 'G-188').status, 'MANUAL_WORKING')
            self.assertEqual(service.runtime.get(service.repo, 'G-169').status, 'READY')

    def test_new_planner_goal_sharing_product_source_does_not_block_existing_start(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _, _ = self.fixture(directory)
            candidate = self.publication(service, 'G-013', new=True)
            self.record(service, candidate)
            service.start('G-012')
            self.publish(service, candidate)
            self.assertEqual(service.runtime.get(service.repo, 'G-012').status, 'MANUAL_WORKING')
            self.assertEqual(service.runtime.get(service.repo, 'G-013').status, 'READY')

    def test_publication_validation_and_intent_are_atomic_against_manual_start(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _, path = self.fixture(directory)
            candidate = self.publication(service)
            database = Path(directory) / 'state.db'
            with closing(sqlite3.connect(database)) as target, target:
                service.connection.backup(target)
            checked, fetched, release = threading.Event(), threading.Event(), threading.Event()
            real_check = manager._assert_mutable_ready_snapshot_current
            def pause(connection, snapshot):
                real_check(connection, snapshot)
                checked.set()
                if not release.wait(10):
                    raise RuntimeError('test synchronization timeout')
            def publish():
                with closing(sqlite3.connect(database)) as connection, connection:
                    connection.row_factory = sqlite3.Row
                    return manager.record_general_planning_publication_intent(connection, service.repo,
                        candidate[0], candidate[1], candidate[2], 'spec', 'planning')
            real_fetch = service.effects.fetch_base
            def after_fetch(*args, **kwargs):
                result = real_fetch(*args, **kwargs)
                fetched.set()
                return result
            def start():
                with closing(sqlite3.connect(database)) as connection, connection:
                    connection.row_factory = sqlite3.Row
                    command = ManualWorkService(replace(service.effects, fetch_base=after_fetch), connection,
                        service.state_dir, service.repo_path, service.config_path)
                    return command.start('G-012')
            with mock.patch.object(manager, '_assert_mutable_ready_snapshot_current', side_effect=pause), \
                 ThreadPoolExecutor(max_workers=2) as executor:
                publication = executor.submit(publish)
                try:
                    self.assertTrue(checked.wait(10))
                    manual = executor.submit(start)
                    self.assertTrue(fetched.wait(10))
                finally:
                    release.set()
                publication.result(timeout=10)
                with self.assertRaisesRegex(ManualWorkError, 'Planner 게시 소유 예약'):
                    manual.result(timeout=10)
            self.assertFalse(path.exists())
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.row_factory = sqlite3.Row
                self.assertEqual(work_claims.publication_claims(connection, service.repo)[0], {'G-012'})
                self.assertEqual(connection.execute('SELECT count(*) FROM manual_work_members').fetchone()[0], 0)

    def test_uncertain_agent_ownership_is_preserved_without_a_live_manager(self):
        from loop_manager_internal.agents.agent_invocation import InvocationContext, ResolvedProfile, Usage
        from loop_manager_internal.storage import agent_invocation_state
        with tempfile.TemporaryDirectory() as directory:
            service, _, path = self.fixture(directory)
            agent_invocation_state.ensure_schema(service.connection)
            ledger = agent_invocation_state.InvocationRepository(service.connection)
            identity = ledger.start(InvocationContext(service.repo, 'G-012'), 'worker', 'codex',
                                    ResolvedProfile('model', 'high'), None)
            for finished in (False, True):
                with self.subTest(finished=finished):
                    if finished:
                        ledger.finish(identity, 'interrupted', None, 0, None, Usage())
                    with self.assertRaisesRegex(ManualWorkError, '소유권과 충돌'):
                        service.start('G-012')
            self.assertFalse(path.exists())
            self.assertIsNone(service.store.for_plan(service.repo, 'G-012'))

    def test_other_plan_resource_claim_is_rejected_without_modifying_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            service, branch, path = self.fixture(directory)
            self.plans(service)
            with service.connection:
                service.connection.execute('UPDATE exec_plans SET branch=?,worktree=?,workspace_owned=2 WHERE plan_id=?',
                                           (branch, str(path), 'G-169'))
            with self.assertRaisesRegex(ManualWorkError, 'G-169.*충돌'):
                service.start('G-012')
            self.assertIsNone(service.store.for_plan(service.repo, 'G-012'))
            self.assertFalse(path.exists())
            git(service.repo_path, 'worktree', 'add', '-b', branch, str(path), 'origin/main')
            (path / 'user-data').write_text('preserve')
            before = git(path, 'status', '--porcelain')
            with self.assertRaises(ManualWorkError):
                service.start('G-012')
            self.assertEqual((path / 'user-data').read_text(), 'preserve')
            self.assertEqual(git(path, 'status', '--porcelain'), before)

    def test_active_group_complete_is_rejected_without_replacing_request(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _, _ = self.fixture(directory)
            service.start('G-012')
            service.complete(manual_tests.completion())
            group = service.store.for_plan(service.repo, 'G-012')
            before = dict(service.store.request(group))
            service.runtime.transition(service.repo, 'G-012', plan_values={'status':'VERIFYING'},
                project_values={'phase':'VERIFYING'}, enqueue=False)
            with self.assertRaisesRegex(ManualWorkError, 'G-012.*VERIFYING.*소유권'):
                service.complete(manual_tests.completion())
            with process_lock(service.state_dir, identity='automatic-verify'):
                with self.assertRaisesRegex(ManualWorkError, 'G-012.*VERIFYING.*소유권'):
                    service.complete(manual_tests.completion())
            self.assertEqual(dict(service.store.request(group)), before)

    def test_submit_prepares_and_pushes_during_independent_verify(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _, _ = self.fixture(directory)
            head = self.plans(service)
            git(service.repo_path, 'branch', 'source')
            git(service.repo_path, 'push', 'origin', 'source')
            self.activate(service, 'G-169', head)
            before = dict(service.connection.execute('SELECT * FROM projects').fetchone())
            service.effects.generate_plans.return_value = json.dumps({'plans': [manual_tests.generated('Submitted')]})
            args = manual_tests.completion(branch='source', requirement=['docs/requirements/Active.md'])
            with process_lock(service.state_dir, identity='automatic-verify'):
                self.assertEqual(service.submit(args), 1)
            group = service.store.for_source(service.repo, 'source', args.requirement)
            self.assertEqual(group['state'], 'AUTOMATIC')
            self.assertIsNotNone(group['request_id'])
            self.assertTrue(Path(group['worktree']).is_dir())
            self.assertEqual(git(service.repo_path, 'ls-remote', '--heads', 'origin', group['branch']).split()[0], group['prepared_head'])
            self.assertEqual(dict(service.connection.execute('SELECT * FROM projects').fetchone()), before)
            service.effects.run_project.assert_not_called()

    def test_submit_id_allocation_observes_reservations_made_during_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _, _ = self.fixture(directory)
            git(service.repo_path, 'branch', 'source')
            git(service.repo_path, 'push', 'origin', 'source')
            def reserve(*args, **kwargs):
                with service.connection:
                    group = service.store.create(service.repo, 'submit', 'other', ['docs/requirements/Other.md'],
                                                 git(service.repo_path, 'rev-parse', 'HEAD'), git(service.repo_path, 'rev-parse', 'HEAD'))
                    service.connection.execute('INSERT INTO manual_work_members VALUES (?,?,?)', (service.repo, 'G-013', group['group_id']))
                return json.dumps({'plans': [manual_tests.generated('Submitted')]})
            service.effects.generate_plans.side_effect = reserve
            args = manual_tests.completion(branch='source', requirement=['docs/requirements/Active.md'])
            service.submit(args)
            self.assertEqual(json.loads(service.store.for_source(service.repo, 'source', args.requirement)['members']), ['G-014'])

    def test_automatic_reconciliation_preserves_live_manual_planner_invocation(self):
        from loop_manager_internal.agents.agent_invocation import InvocationContext, ResolvedProfile, Usage
        from loop_manager_internal.agents.common_guidance import task_identity
        from loop_manager_internal.storage import agent_invocation_state
        with tempfile.TemporaryDirectory() as directory:
            service, _, _ = self.fixture(directory)
            git(service.repo_path, 'branch', 'source')
            git(service.repo_path, 'push', 'origin', 'source')
            agent_invocation_state.ensure_schema(service.connection)
            ledger = agent_invocation_state.InvocationRepository(service.connection)
            orphan = ledger.start(InvocationContext(service.repo, 'G-012'), 'worker', 'codex', ResolvedProfile('model','high'), None)
            def generate(_connection, _repo, _state, _config, _provider, group, _requirements):
                context = InvocationContext(service.repo, None, task_id='manual-submit:' + group['group_id'])
                identity = ledger.start(context, 'planner', 'codex', ResolvedProfile('model','high'), None)
                ledger.bind_guidance(identity, task_identity(context, 'planner', service.repo_path), 'a' * 64)
                self.assertEqual(ledger.reconcile_interrupted(state_dir=service.state_dir), 1)
                self.assertIsNone(service.connection.execute('SELECT finished_at FROM agent_invocations WHERE invocation_id=?', (identity,)).fetchone()[0])
                self.assertEqual(service.connection.execute('SELECT result_kind FROM agent_invocations WHERE invocation_id=?', (orphan,)).fetchone()[0], 'interrupted')
                ledger.finish(identity, 'success', 0, 1, None, Usage())
                return json.dumps({'plans': [manual_tests.generated('Submitted')]})
            service.effects.generate_plans.side_effect = generate
            args = manual_tests.completion(branch='source', requirement=['docs/requirements/Active.md'])
            self.assertEqual(service.submit(args), 1)
            self.assertEqual(service.store.for_source(service.repo, 'source', args.requirement)['state'], 'AUTOMATIC')

    def test_manual_entry_does_not_reconcile_other_invocations(self):
        from loop_manager_internal.application import commands
        effects = commands.ExecuteRuntimeCommandEffects(
            mock.Mock(), mock.Mock(), mock.Mock(side_effect=AssertionError('manual entry must not reconcile')),
            mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock(return_value=0))
        args = manual_tests.completion(command='manual')
        self.assertEqual(commands.execute_runtime_command(effects,args,Path('/state'),Path('/config')), 0)
        effects.connect_database.assert_not_called()
        effects.manual_command.assert_called_once()

    def test_submit_workspace_collision_is_not_an_accepted_pending_request(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _, _ = self.fixture(directory)
            git(service.repo_path, 'branch', 'source')
            git(service.repo_path, 'push', 'origin', 'source')
            branch, path = manager.expected_execplan_workspace(service.state_dir, service.repo, 'G-013')
            git(service.repo_path, 'branch', branch)
            service.effects.generate_plans.return_value = json.dumps({'plans': [manual_tests.generated('Submitted')]})
            args = manual_tests.completion(branch='source', requirement=['docs/requirements/Active.md'])
            with self.assertRaisesRegex(ManualWorkError, 'G-013.*branch='):
                service.submit(args)
            group = service.store.for_source(service.repo, 'source', args.requirement)
            self.assertEqual(group['state'], 'PREPARATION_CONFLICT')
            self.assertIsNone(group['request_id'])
            self.assertFalse(service.store.pending(service.repo))
            self.assertFalse(path.exists())

    def test_submit_foreign_remote_after_reservation_is_rejected_and_artifacts_survive(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _, _ = self.fixture(directory)
            git(service.repo_path, 'branch', 'source')
            git(service.repo_path, 'push', 'origin', 'source')
            original = service.effects.make_worktree
            def foreign_remote(*args, **kwargs):
                result = original(*args, **kwargs)
                git(service.repo_path, 'push', 'origin', f'source:refs/heads/{args[2]}')
                return result
            service.effects = replace(service.effects, make_worktree=foreign_remote)
            service.effects.generate_plans.return_value=json.dumps({'plans':[manual_tests.generated('Submitted')]})
            args=manual_tests.completion(branch='source', requirement=['docs/requirements/Active.md'])
            with self.assertRaisesRegex(ManualWorkError, '원격 게시 대상.*충돌'):
                service.submit(args)
            group=service.store.for_source(service.repo,'source',args.requirement)
            self.assertIsNone(group['request_id'])
            self.assertEqual(group['state'],'PREPARATION_CONFLICT')
            self.assertTrue(Path(group['worktree']).is_dir())
            self.assertFalse(service.store.pending(service.repo))
            self.assertEqual(git(service.repo_path,'ls-remote','--heads','origin',group['branch']).split()[0],group['source_head'])
            # An explicit retry uses the same saved plans and workspace once the
            # foreign ref is removed by its owner; generation is never duplicated.
            git(service.repo_path,'push','origin',f":refs/heads/{group['branch']}")
            service.effects=replace(service.effects,make_worktree=original)
            self.assertEqual(service.submit(args),1)
            current=service.store.for_source(service.repo,'source',args.requirement)
            self.assertEqual(current['members'],group['members'])
            self.assertEqual(current['worktree'],group['worktree'])
            self.assertEqual(service.effects.generate_plans.call_count,1)

    def test_resume_keeps_registration_order_and_skips_a_group_being_prepared(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _, _ = self.fixture(directory)
            self.plans(service)
            service.start('G-188')
            service.start('G-012')
            with process_lock(service.state_dir, identity='automatic-run'):
                service.complete(manual_tests.completion('G-188'))
                service.complete(manual_tests.completion('G-012'))
                first, second = service.store.pending(service.repo)
                self.assertEqual(json.loads(first['members']), ['G-188'])
                with resource_locks(service.state_dir, service.repo, group_resources(first), owner='manual-complete'):
                    service.resume()
                    self.assertEqual(service.runtime.active_row(service.repo)['plan_id'], 'G-012')
                service.runtime.transition_and_clear(service.repo, 'G-012', plan_values={'status': 'PR_OPEN'}, enqueue=False)
                service.resume()
                self.assertEqual(service.runtime.active_row(service.repo)['plan_id'], 'G-188')

    def test_active_maintenance_gate_is_not_relaxed_by_manual_concurrency(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _, path = self.fixture(directory)
            maintenance_state.MaintenanceRepository(service.connection).get(service.repo)
            with service.connection:
                service.connection.execute("INSERT INTO maintenance_cycles(repo,phase) VALUES (?,'assessment')", (service.repo,))
            with self.assertRaisesRegex(ManualWorkError, '유지보수 gate'):
                service.start('G-012')
            self.assertFalse(path.exists())
            self.assertIsNone(service.store.for_plan(service.repo, 'G-012'))

    def test_unfinished_git_operation_is_preserved_on_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _, path = self.fixture(directory)
            service.start('G-012')
            marker = Path(git(path, 'rev-parse', '--path-format=absolute', '--git-path', 'MERGE_HEAD'))
            marker.write_text(git(path, 'rev-parse', 'HEAD'))
            with self.assertRaisesRegex(ManualWorkError, '미완료 Git operation'):
                service.complete(manual_tests.completion())
            self.assertTrue(marker.is_file())
            self.assertIsNone(service.store.for_plan(service.repo, 'G-012')['request_id'])


@isolated_git_test
class ManualBootstrapConcurrencyTests(OriginScopeFixture, unittest.TestCase):
    def test_concurrent_entries_reach_child_while_automatic_lease_is_held(self):
        for command in ("manual", "recover"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                _, source, _writer = remote_fixture(directory)
                state = Path(directory) / 'state'
                command_args = (['manual', 'start', str(source), 'G-188'] if command == 'manual'
                                else ['recover', str(source), '--plan', 'G-188'])
                def child(_source, _state, _config, runtime, _arguments):
                    self.assertTrue(runtime.concurrent)
                    self.assertIn('/commands/', runtime.path.as_posix())
                    self.assertEqual(_held_process_lock_identity(state), 'automatic-run')
                    self.assertEqual(_held_process_lock_identity(state, name=f'manual-{runtime.mutation_id}.lock'), 'runtime:' + runtime.mutation_id)
                    service = manager._bootstrap_service()
                    service.runtime_root = runtime.path
                    service.validate_runtime_child(source, state, runtime.commit_sha, command_id=runtime.mutation_id)
                    service.validate_runtime_mutation(source, state, runtime.commit_sha, runtime.mutation_id, concurrent=True)
                    service.remove_runtime_worktree(source, state, runtime)
                    return 0
                with process_lock(state, identity='automatic-run'), mock.patch.object(manager, 'ROOT', source), \
                     mock.patch.object(manager, 'github_repository', return_value='owner/repo'), \
                     mock.patch.object(manager.sys, 'argv', ['loop_manager.py', '--state-dir', str(state), *command_args]), \
                     mock.patch.object(manager, 'run_runtime_child', side_effect=child), \
                     mock.patch.object(manager, 'synchronize_base_worktree') as sync:
                    self.assertEqual(manager.main(), 0)
                    sync.assert_not_called()

    def test_same_revision_manual_runtimes_are_isolated_and_cleanup_is_owned(self):
        with tempfile.TemporaryDirectory() as directory:
            _, source, _writer = remote_fixture(directory)
            state = Path(directory) / 'state'
            with mock.patch.object(manager, 'github_repository', return_value='owner/repo'):
                bootstrap = manager._bootstrap_service()
                runtimes = [bootstrap.prepare_bootstrap_runtime(source, state, token * 32, concurrent=True) for token in ('a', 'b')]
                self.assertEqual(runtimes[0].commit_sha, runtimes[1].commit_sha)
                self.assertNotEqual(runtimes[0].path, runtimes[1].path)
                for runtime in runtimes:
                    inspected = agent_write_isolation.inspect_worktree(runtime.path, state, read_only=True)
                    self.assertEqual(inspected.source, source)
                    with self.assertRaises(agent_write_isolation.IsolationError):
                        agent_write_isolation.inspect_worktree(runtime.path, state, read_only=False)
                bootstrap.remove_runtime_worktree(source, state, runtimes[0])
                self.assertTrue(runtimes[1].path.is_dir())
                bootstrap.validate_runtime_worktree(source, state, runtimes[1].path, runtimes[1].commit_sha, command_id=runtimes[1].mutation_id)
                bootstrap.remove_runtime_worktree(source, state, runtimes[1])

    def test_manual_runtime_scope_uses_its_parent_lease_during_automatic_followup(self):
        with tempfile.TemporaryDirectory() as directory:
            _, source, _writer = remote_fixture(directory)
            state = Path(directory) / 'state'
            connection = sqlite3.connect(':memory:')
            self.addCleanup(connection.close)
            connection.row_factory = sqlite3.Row
            self._persist_planner_scope(connection, source)
            token = 'a' * 32
            with mock.patch.object(manager, 'github_repository', return_value='owner/repo'), \
                 process_lock(state, identity='runtime:' + token, name=f'manual-{token}.lock'):
                bootstrap = manager._bootstrap_service()
                runtime = bootstrap.prepare_bootstrap_runtime(source, state, token, concurrent=True)
                bootstrap.runtime_root = runtime.path
                mutation = bootstrap.validate_runtime_mutation(source, state, runtime.commit_sha, token, concurrent=True)
                with process_lock(state, identity='manual-followup'):
                    manager.validate_persisted_agent_scope(connection, source, state, str(source), runtime_mutation=mutation)
                bootstrap.remove_runtime_worktree(source, state, runtime)

    def test_fetch_and_journal_conflicts_do_not_take_over_live_operations(self):
        with tempfile.TemporaryDirectory() as directory:
            _, source, _writer = remote_fixture(directory)
            state = Path(directory) / 'state'
            with resource_locks(state, str(source), ['git:fetch'], owner='automatic-fetch'):
                with self.assertRaisesRegex(manager.ProcessLockBusyError, 'git:fetch.*automatic-fetch'):
                    manager._recorded_base_fetch(source, state, 'main', owner='project')
            target = 'refs/heads/shared'
            apply = mock.Mock()
            with resource_locks(state, str(source), ['git:' + target], owner='automatic-push'):
                with self.assertRaisesRegex(manager.ProcessLockBusyError, 'automatic-push'):
                    manager.manager_git_journal.execute(state, repo=str(source), owner='manual-push', kind='manual_branch_push',
                        target=target, before={'oid': None}, expected={'oid': 'a' * 40},
                        observe=lambda: {'oid': None}, apply=apply, marker=lambda _: True)
            apply.assert_not_called()

    def test_generation_uses_a_pinned_read_only_runtime_without_project_projection_changes(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            _, source, _writer = remote_fixture(directory)
            state = Path(directory) / 'state'
            state.mkdir()
            connection = manager.connect_database(state, create=True)
            cleanup.callback(connection.close)
            head = git(source, 'rev-parse', 'HEAD')
            group = {'base_sha': head, 'source_head': head, 'group_id': 'a' * 32}
            def agent(_provider, path, *_args, **kwargs):
                self.assertNotEqual(path, source)
                self.assertEqual(git(path, 'rev-parse', 'HEAD'), head)
                agent_write_isolation.inspect_worktree(path, state, read_only=True)
                self.assertTrue(kwargs['read_only'])
                self.assertEqual(kwargs['invocation'].repo, str(source))
                return 0, '{"plans": []}'
            effects = manual_plan_generation.ManualGenerationEffects(manager.git, mock.Mock(side_effect=agent),
                manager.prepare_runtime_worktree, manager.remove_runtime_worktree)
            self.assertEqual(manual_plan_generation.generate(effects, connection, source, state, source / 'unused.ini',
                'codex', group, {}), '{"plans": []}')
            self.assertEqual(len(manager._worktree_catalog(source)), 1)
            self.assertEqual(connection.execute('SELECT count(*) FROM projects').fetchone()[0], 0)
