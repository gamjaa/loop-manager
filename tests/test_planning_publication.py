from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import json
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.notifications import notifications
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.maintenance import maintenance_file_review
from loop_manager_internal.maintenance import maintenance_findings
from loop_manager_internal.maintenance import maintenance_reconciliation
from loop_manager_internal.maintenance.maintenance_file_inventory import RootPlacement, TrackedSource
from tests.support.maintenance import result_for
from loop_manager_internal.planning.planning_catalog import MutableReadySnapshot, mutable_snapshot_token
from loop_manager_internal.storage.planning_publication_state import (
    GeneralPlanningPublication, general_planning_publication,
    record_general_planning_publication,
)
from loop_manager_internal.storage.database import GENERAL_PLANNING_PUBLICATIONS_SCHEMA
from loop_manager_internal.maintenance.maintenance_orchestration import maintenance_planning_fingerprint
from loop_manager_internal.planning.planning_runtime import (
    PlanningAgentResult,
    PlanningAgentResultKind,
    PlanningContext,
    PlanningLifecycleCoordinator,
    PlanningOutcomeKind,
    PlanningResultInvalid,
    PlanningScopeResult,
    PlanningWorkspaceOwnershipError,
    PlanningDecisionKind,
    choose_planning_decision,
)
from loop_manager_internal.storage.runtime_state import PlanningSnapshot


from tests.support.planning_case import (
    PlanningCase, FakePorts, FakeStatePort, FakeGitPort, FakeAgentPort,
    FakeValidationPort, FakeEventPort, planning_snapshot, maintenance_cycle,
)

class PlanningPublicationTest(PlanningCase):
    def test_publication_release_scopes_pending_rebase_on_first_publish_and_recovery(self):
        from loop_manager_internal.git import manager_git_journal as journal
        from loop_manager_internal.planning.git_ports import _PlanningGitPorts
        for recovery in (False, True):
            for scenario in ('independent', 'same-worktree', 'same-branch', 'unknown-kind',
                             'missing-registration', 'changed-intent', 'damaged-intent', 'unknown-owner'):
                with self.subTest(recovery=recovery, scenario=scenario), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    worktree, other = root / 'planning', root / 'G-297'
                    worktree.mkdir()
                    other.mkdir()
                    target = worktree if scenario == 'same-worktree' else other
                    state = root / 'state'
                    identity = journal.begin_rebase(state, repo='/repo', owner='plan:G-297',
                        target=str(target), before={'head': 'old', 'branch': 'loop-manager/G-297'},
                        expected={'branch': 'loop-manager/G-297'})
                    slot = journal._slot(state, '/repo', 'managed_rebase', str(target))
                    if scenario in ('unknown-kind', 'unknown-owner'):
                        active = json.loads((slot / 'active.json').read_text())
                        active['kind' if scenario == 'unknown-kind' else 'owner'] = 'unknown'
                        (slot / 'active.json').write_text(json.dumps(active))
                    if scenario == 'changed-intent':
                        saved = json.loads((slot / f'{identity}.intent.json').read_text())
                        saved['before']['head'] = 'changed'
                        (slot / f'{identity}.intent.json').write_text(json.dumps(saved))
                    if scenario == 'damaged-intent':
                        (slot / f'{identity}.intent.json').write_text('{')
                    preserved = {path.name: path.read_bytes() for path in slot.iterdir()}
                    removed = []
                    from tests.support.database import runtime_connection
                    connection = runtime_connection('/repo')
                    self.addCleanup(connection.close)
                    connection.execute("UPDATE exec_plans SET plan_id='G-297',branch='loop-manager/G-297',worktree=?", (str(other),))
                    connection.commit()
                    def git(repo, *args, **kwargs):
                        output = ''
                        if args[:2] == ('worktree', 'list'):
                            if scenario != 'missing-registration':
                                output = f'worktree {other}\0HEAD old\0detached\0\0'
                            if worktree.exists():
                                branch = 'branch refs/heads/loop-manager/G-297' if scenario == 'same-branch' else 'detached'
                                output += f'worktree {worktree}\0HEAD commit\0{branch}\0\0'
                        elif args[:2] == ('worktree', 'remove'):
                            worktree.rmdir()
                            removed.append(args[2])
                        elif args[:1] == ('symbolic-ref',):
                            return SimpleNamespace(stdout='', returncode=1)
                        elif args[:1] == ('show-ref',):
                            output = 'a' * 40
                        elif args[:1] == ('config',):
                            output = 'core.bare\0false\0'
                        elif args[:1] == ('rev-parse',):
                            output = '/shared' if '--git-common-dir' in args else 'commit'
                        return SimpleNamespace(stdout=output, returncode=0)
                    port = _PlanningGitPorts(SimpleNamespace(git=git, _git_operation_entries=lambda _: {},
                                                            _nul_git_paths=mock.Mock()),
                                             connection, Path('/repo'), state)
                    ports = FakePorts(planning_snapshot(phase='PLANNING', active_sha='base',
                        worktree=str(worktree), planning_workspace_owned=1))
                    ports.state.retain_planning_after_publication('/repo', 'commit')
                    ports.git.run = git
                    ports.git.validate_planning_release = port.validate_planning_release
                    ports.git.validate_publication_release = port.validate_publication_release
                    coordinator = PlanningLifecycleCoordinator(ports)
                    run = coordinator._new_run(self.context)
                    if recovery:
                        outcome = coordinator._publication._recover_publication(run)
                        self.assertEqual(outcome.kind, PlanningOutcomeKind.PUBLICATION_RECOVERY
                            if scenario == 'independent' else PlanningOutcomeKind.OPERATIONAL_ERROR)
                    elif scenario == 'independent':
                        coordinator._publication._complete_publication_release(run)
                    else:
                        with self.assertRaises(RuntimeError):
                            coordinator._publication._complete_publication_release(run)
                    self.assertEqual(ports.state.pending_publication_release('/repo') is None,
                                     scenario == 'independent')
                    self.assertEqual(len(removed), int(scenario == 'independent'))
                    self.assertEqual({path.name: path.read_bytes() for path in slot.iterdir()}, preserved)
                    self.assertTrue(other.exists())
                    self.assertEqual(ports.agent.run_count, 0)

    def test_applied_publication_recovers_after_cleanup_before_db_release(self):
        from loop_manager_internal.storage import database, runtime_state
        from loop_manager_internal.storage.db_access import open_connection
        database_directory = tempfile.TemporaryDirectory()
        self.addCleanup(database_directory.cleanup)
        database_path = Path(database_directory.name) / 'state.db'
        connection = open_connection(database_path)
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.execute(loop_manager.SCHEMA)
        database._ensure_execplans_schema(connection)
        maintenance_state.ensure_schema(connection)
        connection.execute(GENERAL_PLANNING_PUBLICATIONS_SCHEMA)
        with connection:
            connection.execute('CREATE TABLE independent(value INTEGER)')
            connection.execute('INSERT INTO independent VALUES (0)')
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / 'planning'
            worktree.mkdir()
            connection.execute(
                "INSERT INTO projects(repo,base_branch,phase,active_sha,worktree,planning_workspace_owned,"
                "planning_attempt_fingerprint,planner_session_id,planning_result,planning_result_completed) "
                "VALUES ('/repo','main','PLANNING','base',?,1,'attempt','saved-session','saved result',1)",
                (str(worktree),))
            snapshot = MutableReadySnapshot('/repo', 'base', (), mutable_snapshot_token('/repo', 'base', ()))
            result = loop_manager.ValidatedPlanningResult((), (), mutable_snapshot=snapshot)
            manifest = loop_manager._planning_result_manifest(result)
            record_general_planning_publication(connection, GeneralPlanningPublication(
                '/repo', 'prepared', 'base', 'commit', snapshot.token, 'spec', 'input', manifest,
                hashlib.sha256(manifest.encode()).hexdigest()))
            connection.commit()
            state = loop_manager._PlanningStatePorts(connection, runtime_state.RuntimeStateRepository(connection),
                maintenance_state.MaintenanceRepository(connection), Path(directory))
            with state.transaction():
                state.retain_planning_after_publication('/repo', 'commit', processed_sha='commit',
                    specification_fingerprint='spec', planning_fingerprint='input')
            ports = FakePorts(state.planning('/repo'))
            ports.state = state
            coordinator = PlanningLifecycleCoordinator(ports)
            git = ports.git.run
            removed = []
            entered, updated = threading.Event(), threading.Event()
            errors = []
            def independent_request():
                try:
                    self.assertTrue(entered.wait(5))
                    other = open_connection(database_path)
                    try:
                        with other:
                            other.execute('UPDATE independent SET value=1')
                    finally:
                        other.close()
                except BaseException as error:
                    errors.append(error)
                finally:
                    updated.set()
            worker = threading.Thread(target=independent_request)
            worker.start()
            self.addCleanup(worker.join, 6)
            self.addCleanup(entered.set)
            def remove(repository, *args, **kwargs):
                self.assertFalse(connection.in_transaction)
                self.assertFalse(connection._held)
                if args[:2] == ('worktree', 'remove'):
                    entered.set()
                    self.assertTrue(updated.wait(5))
                    removed.append(args)
                    worktree.rmdir()
                return git(repository, *args, **kwargs)
            ports.git.run = remove
            with mock.patch.object(state, 'complete_publication_release', side_effect=RuntimeError('release interrupted')):
                failed = coordinator._publication._recover_publication(coordinator._new_run(self.context))
            self.assertEqual(failed.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            self.assertFalse(worktree.exists())
            self.assertEqual(state.planning('/repo').planner_session_id, 'saved-session')
            self.assertIsNotNone(state.pending_publication_release('/repo'))
            self.assertIsNotNone(state.general_publication('/repo'))
            request = connection.run_request
            def retry_release(operation):
                with self.assertRaisesRegex(RuntimeError, 'DB retry'), connection:
                    operation(connection)
                    raise RuntimeError('DB retry')
                self.assertIsNotNone(state.pending_publication_release('/repo'))
                return request(operation)
            with mock.patch.object(loop_manager, 'publish_general_planning_batch', side_effect=AssertionError('republish')), \
                    mock.patch.object(loop_manager, 'recover_general_planning_batch', side_effect=AssertionError('reapply')), \
                    mock.patch.object(connection, 'run_request', side_effect=retry_release) as retried:
                recovered = coordinator._publication._recover_publication(coordinator._new_run(self.context))
                retried.assert_called_once()
            self.assertEqual(recovered.kind, PlanningOutcomeKind.PUBLICATION_RECOVERY)
            self.assertEqual(len(removed), 1)
            worker.join(6)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(connection.execute('SELECT value FROM independent').fetchone()[0], 1)
            self.assertEqual(ports.agent.run_count, 0)
            self.assertIsNone(state.pending_publication_release('/repo'))
            self.assertIsNone(state.general_publication('/repo'))
            self.assertEqual(state.planning('/repo').processed_sha, 'commit')

    def test_new_manager_source_publication_requires_critical_before_db_confirmation(self):
        from loop_manager_internal.catalog import execplan_catalog, manager_sources
        from loop_manager_internal.planning import planner_publication
        from loop_manager_internal.storage import database, verify_input_requests

        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        database._ensure_execplans_schema(connection)
        repo = "/repo"
        connection.execute("INSERT INTO projects (repo, base_branch) VALUES (?, 'main')", (repo,))
        request = verify_input_requests.register(connection, repo, "a" * 40)
        connection.commit()
        path = "docs/plans/[G-900] Verify Inputs.md"
        worktree = Path("/publication")

        def make_text(priority):
            return (
                "---\nid: G-900\ntitle: Verify Inputs\nsources:\n  - "
                + manager_sources.SOURCE
                + "\ndepends_on: []\ncomplexity: low\nrisk: medium\npriority: "
                + priority
                + "\n---\n\n## Purpose\n\n"
                + manager_sources.RULE
                + "\n\n## Acceptance Criteria\n\n- Keep scripts/verify-inputs.json current.\n\n"
                + "## Decision Log\n\n"
                + manager_sources.PREFIX
                + json.dumps(request, sort_keys=True)
                + "\n"
            )

        def publish(text):
            plan = execplan_catalog.parse_execplan_text(path, text)
            verify_input_requests.validate(connection, repo, text, plan["sources"])
            row_effects = planner_publication.RegisterExecplanRowsEffects(
                _ensure_execplans_schema=database._ensure_execplans_schema,
                file_hash=lambda _path: execplan_catalog.plan_content_hash(text),
                git=mock.Mock(),
                git_bytes=mock.Mock(return_value=text.encode("utf-8")),
            )
            def prepare_rows(*args):
                self.assertFalse(connection.in_transaction)
                return planner_publication._prepare_execplan_rows(row_effects, *args)
            effects = planner_publication.RegisterExecplansEffects(
                _prepare_execplan_rows=prepare_rows,
                _register_execplan_rows=lambda db, target_repo, root, plans, commit, **kwargs:
                    planner_publication._register_execplan_rows(
                        row_effects, db, target_repo, root, plans, commit, **kwargs,
                    ),
            )
            planner_publication.register_execplans(
                effects, connection, repo, worktree, [(path, plan)], "published", "fingerprint",
            )

        for priority in ("high", "normal", "low"):
            with self.subTest(priority=priority):
                with self.assertRaisesRegex(RuntimeError, "priority: critical"):
                    publish(make_text(priority))
                self.assertIsNone(connection.execute(
                    "SELECT 1 FROM exec_plans WHERE repo=? AND plan_id='G-900'", (repo,),
                ).fetchone())
                self.assertIsNone(connection.execute(
                    "SELECT 1 FROM execplan_drafts WHERE repo=? AND plan_id='G-900'", (repo,),
                ).fetchone())
                project = connection.execute(
                    "SELECT processed_sha FROM projects WHERE repo=?", (repo,),
                ).fetchone()
                self.assertIsNotNone(project)
                self.assertIsNone(project[0])

        publish(make_text("critical"))
        row = connection.execute(
            "SELECT priority,status FROM exec_plans WHERE repo=? AND plan_id='G-900'", (repo,),
        ).fetchone()
        self.assertEqual(tuple(row), ("critical", "READY"))
        self.assertEqual(connection.execute(
            "SELECT processed_sha FROM projects WHERE repo=?", (repo,),
        ).fetchone()[0], "published")

    def test_recovery_applies_legacy_manager_plan_from_published_intent(self):
        from loop_manager_internal.catalog import execplan_catalog, manager_sources
        from loop_manager_internal.planning import planner_publication
        from loop_manager_internal.planning.planning_catalog import (
            mutable_snapshot_token, snapshot_from_dict, snapshot_to_dict,
        )
        from loop_manager_internal.storage import database, planning_publication_state, verify_input_requests
        from loop_manager_internal.storage.database import GENERAL_PLANNING_PUBLICATIONS_SCHEMA
        from loop_manager_internal.storage.planning_publication_state import GeneralPlanningPublication

        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        connection.execute(GENERAL_PLANNING_PUBLICATIONS_SCHEMA)
        database._ensure_execplans_schema(connection)
        repo = "/repo"
        connection.execute("INSERT INTO projects (repo, base_branch) VALUES (?, 'main')", (repo,))
        request = verify_input_requests.register(connection, repo, "a" * 40)
        path = "docs/plans/[G-900] Legacy Verify Inputs.md"
        text = (
            "---\nid: G-900\ntitle: Legacy Verify Inputs\nsources:\n  - "
            + manager_sources.SOURCE
            + "\ndepends_on: []\ncomplexity: low\nrisk: medium\npriority: normal\n"
            + "---\n\n## Purpose\n\n"
            + manager_sources.RULE
            + "\n\n## Decision Log\n\n"
            + manager_sources.PREFIX
            + json.dumps(request, sort_keys=True)
            + "\n"
        )
        plan = execplan_catalog.parse_execplan_text(path, text)
        verify_input_requests.validate(connection, repo, text, plan["sources"])
        content_hash = execplan_catalog.plan_content_hash(text)
        snapshot = MutableReadySnapshot(
            repo, "base", (), mutable_snapshot_token(repo, "base", ()),
        )
        result = loop_manager.ValidatedPlanningResult(
            plans=((path, plan),), publication_paths=(path,),
            publication_hashes=((path, content_hash),), mutable_snapshot=snapshot,
        )
        manifest = planner_publication._planning_result_manifest(
            planner_publication.PlanningResultManifestEffects(
                snapshot_to_dict=snapshot_to_dict,
            ),
            result,
        )
        commit = "published"
        publication = GeneralPlanningPublication(
            repo, "prepared", "base", commit, snapshot.token, "spec", "planning",
            manifest, hashlib.sha256(manifest.encode("utf-8")).hexdigest(),
        )
        planning_publication_state.record_general_planning_publication(connection, publication)
        connection.commit()

        worktree = Path("/published")
        row_effects = planner_publication.RegisterExecplanRowsEffects(
            _ensure_execplans_schema=database._ensure_execplans_schema,
            file_hash=lambda _path: content_hash,
            git=mock.Mock(),
            git_bytes=lambda *_args: text.encode("utf-8"),
        )
        def prepare_rows(*args):
            self.assertFalse(connection.in_transaction)
            return planner_publication._prepare_execplan_rows(row_effects, *args)
        apply_effects = planner_publication.ApplyGeneralPlanningBatchEffects(
            _prepare_execplan_rows=prepare_rows,
            _observe_mutable_ready_prs=mock.Mock(),
            _assert_mutable_ready_snapshot_current=mock.Mock(),
            _clear_active_in_current_transaction=mock.Mock(),
            _general_planning_batch_already_applied=mock.Mock(return_value=False),
            _register_execplan_rows=lambda db, *args, **kwargs:
                planner_publication._register_execplan_rows(row_effects, db, *args, **kwargs),
            file_hash=lambda _path: content_hash,
            general_planning_publication=planning_publication_state.general_planning_publication,
            is_loop_manager_repository=mock.Mock(return_value=False),
            plan_declarations=mock.Mock(return_value={}),
        )
        manifest_effects = planner_publication.PlanningResultFromManifestEffects(
            mutable_snapshot_token=mutable_snapshot_token,
            snapshot_from_dict=snapshot_from_dict,
        )
        recovery_effects = planner_publication.RecoverGeneralPlanningBatchEffects(
            _apply_general_planning_batch=lambda db, *args, **kwargs:
                planner_publication._apply_general_planning_batch(apply_effects, db, *args, **kwargs),
            _assert_remote_preserves_maintenance_batch=mock.Mock(),
            _clear_active_in_current_transaction=mock.Mock(),
            _git_commit_is_ancestor=lambda _root, ancestor, descendant:
                (ancestor, descendant) == (commit, "remote"),
            _planning_result_from_manifest=lambda value:
                planner_publication._planning_result_from_manifest(manifest_effects, value),
            _validate_general_planning_commit=mock.Mock(),
            general_planning_publication=planning_publication_state.general_planning_publication,
            git=lambda *_args: SimpleNamespace(stdout="remote"),
        )

        with mock.patch.object(planner_publication.planning_publication_state,
                               "apply_mutable_catalog_changes"), \
             mock.patch("loop_manager_internal.notifications.notifications.record_planning_publication"), \
             mock.patch.object(planner_publication.planning_input_state,
                               "record_general"), \
             mock.patch("loop_manager_internal.catalog.remote_deletions.resolve_recovery"):
            outcome = planner_publication.recover_general_planning_batch(
                recovery_effects, connection, repo, worktree, "main",
            )

        row = connection.execute(
            "SELECT priority, status FROM exec_plans WHERE repo=? AND plan_id='G-900'", (repo,),
        ).fetchone()
        self.assertEqual(outcome, "applied")
        self.assertEqual(tuple(row), ("normal", "READY"))
        self.assertIsNone(planning_publication_state.general_planning_publication(connection, repo))

    def test_publication_uses_current_changes_after_manager_validation(self):
        path = "docs/plans/[G-001] Plan.md"
        for manager_changed in (True, False):
            with self.subTest(manager_changed=manager_changed), tempfile.TemporaryDirectory() as directory:
                worktree = Path(directory)
                target = worktree / path
                target.parent.mkdir(parents=True)
                target.write_text("validated decision log", encoding="utf-8")
                ports = FakePorts(planning_snapshot(phase="PLANNING", active_sha="base",
                    worktree=directory, planning_workspace_owned=1))
                ports.git.dirty_files = [path] if manager_changed else []
                ports.git.head = "base" if manager_changed else "plan-commit"
                ports.git.remote = "concurrent-base"
                coordinator = PlanningLifecycleCoordinator(ports)
                run = coordinator._new_run(self.context)
                run.planning_worktree = worktree
                run.validated_result = ports.validation.result
                run.validated_result.plans = ()
                run.validated_result.revisions = (object(),)
                run.dirty_files = [] if manager_changed else [path]
                run.current_fingerprint = run.general_fingerprint = "fingerprint"
                run.specification_fingerprint = "specification"
                original_intent = ports.state.record_general_intent
                def record_intent(*args):
                    self.assertEqual(args[3], "plan-commit")
                    original_intent(*args)
                ports.state.record_general_intent = record_intent

                outcome = coordinator._publication._publish_planner_result(run)

                self.assertEqual(outcome.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED)
                commands = [args for _repo, args, _check in ports.git.calls]
                self.assertEqual(sum(args[0] == "commit" for args in commands), int(manager_changed))
                if manager_changed:
                    self.assertIn(("add", "--", f":(literal){path}"), commands)
                self.assertEqual(ports.state.general_intent_record_count, 1)

    def _reviewed_publication_connection(self):
        from loop_manager_internal.storage import planning_review_state as reviews
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        connection.execute(GENERAL_PLANNING_PUBLICATIONS_SCHEMA)
        reviews.begin(connection, '/repo', purpose='general', revision='base',
            input_fingerprint='input', specification_fingerprint='spec',
            work_cycle='cycle', comparison_revision=None)
        reviews.validate(connection, '/repo', 'base', 'validated review', 'complete')
        connection.commit()
        return connection

    def test_requirement_publication_preserves_general_baseline_and_recovers_without_model(self):
        from loop_manager_internal.planning import planner_publication
        from loop_manager_internal.storage import planning_review_state as reviews, planning_input_state
        connection = self._reviewed_publication_connection()
        connection.execute("INSERT INTO projects (repo, base_branch, processed_sha, specification_fingerprint, planning_fingerprint) VALUES ('/repo', 'main', 'old', 'old-spec', 'old-input')")
        old = {'docs/plans/Decision.md': ('blob', 'old-answer')}
        planning_input_state.record_general(connection, '/repo', 'old', old)
        connection.execute("UPDATE planning_reviews SET purpose='requirement' WHERE slot='attempt'")
        connection.commit()
        snapshot = MutableReadySnapshot('/repo', 'base', (), 'token')
        result = loop_manager.ValidatedPlanningResult(plans=(), publication_paths=(), mutable_snapshot=snapshot)
        intent = SimpleNamespace(stage='prepared', base_sha='base', commit_sha='commit',
            snapshot_token='token', specification_fingerprint='spec', planning_fingerprint='input')
        clear = mock.Mock()
        effects = planner_publication.ApplyGeneralPlanningBatchEffects(
            mock.Mock(), clear, mock.Mock(return_value=False), mock.Mock(), mock.Mock(),
            mock.Mock(return_value=intent), mock.Mock(return_value=False), mock.Mock(), mock.Mock(), mock.Mock())
        with mock.patch.object(planner_publication.planning_publication_state, 'apply_mutable_catalog_changes'), \
             mock.patch('loop_manager_internal.catalog.remote_deletions.resolve_recovery'):
            planner_publication._apply_general_planning_batch(effects, connection, '/repo',
                Path('/worktree'), result, 'commit', 'spec', 'input')
        self.assertEqual(clear.call_args.kwargs['processed_sha'], 'old')
        self.assertEqual(clear.call_args.kwargs['planning_fingerprint'], 'old-input')
        self.assertTrue(planning_input_state.matches(connection, '/repo', old))
        self.assertEqual(reviews.get(connection, '/repo', 'requirement')['confirmed_revision'], 'commit')
        self.assertIsNone(reviews.get(connection, '/repo', 'general'))
        applied = planner_publication.GeneralPlanningBatchAlreadyAppliedEffects(mock.Mock(), mock.Mock(), mock.Mock())
        self.assertTrue(planner_publication._general_planning_batch_already_applied(applied,
            connection, '/repo', Path('/worktree'), result, 'commit', 'spec', 'input'))

    def test_general_batch_confirms_review_and_pinned_guidance_before_clear_atomically(self):
        from loop_manager_internal.planning import planner_publication
        from loop_manager_internal.storage import planning_input_state, planning_review_state as reviews
        from loop_manager_internal.agents import common_guidance
        from loop_manager_internal.storage import database
        for fail_clear in (False, True):
            with self.subTest(fail_clear=fail_clear):
                connection = self._reviewed_publication_connection()
                database._ensure_execplans_schema(connection)
                connection.execute("INSERT INTO projects (repo, base_branch) VALUES ('/repo', 'main')")
                for plan_id in ('G-001', 'G-002'):
                    connection.execute("INSERT INTO exec_plans "
                        "(repo, plan_id, path, title, sources, depends_on, content_hash, status, priority) "
                        "VALUES ('/repo', ?, ?, 'Work', '[]', '[]', 'unchanged', 'PLAN_BLOCKED', 'normal')",
                        (plan_id, f'docs/plans/[{plan_id}] Work.md'))
                relevance = dict(revision='base', plans=[dict(plan_id='G-001', requirements=[dict(
                    source='docs/requirements/[R-002] Policy.md', reason='resolves the original choice',
                    quote='Use ordinary calls.')])])
                reviews.validate(connection, '/repo', 'base', 'BLOCKED_PLAN_INPUTS: ' + json.dumps(relevance), 'reviewed')
                connection.commit()
                self.assertEqual(reviews.reassessment(connection, '/repo', 'G-001'), {})
                snapshot = MutableReadySnapshot('/repo', 'base', (), 'token')
                result = loop_manager.ValidatedPlanningResult(plans=(), publication_paths=(), mutable_snapshot=snapshot)
                intent = SimpleNamespace(stage='prepared', base_sha='base', commit_sha='commit',
                    snapshot_token='token', specification_fingerprint='spec', planning_fingerprint='input')
                entries = {'docs/plans/G-001.md': ('G-001', 'plan-hash')}
                def clear(*_args, **_kwargs):
                    self.assertEqual(reviews.get(connection, '/repo', 'general')['confirmed_revision'], 'commit')
                    baseline = connection.execute('SELECT guidance_hash FROM planning_input_baselines').fetchone()
                    self.assertEqual(baseline['guidance_hash'], 'pinned-guidance')
                    self.assertTrue(planning_input_state.matches(connection, '/repo', entries))
                    queued = reviews.reassessment(connection, '/repo', 'G-001')
                    self.assertEqual(queued['revision'], 'base')
                    self.assertEqual(queued['confirmed_revision'], 'commit')
                    self.assertEqual(queued['requirements'][0]['source'], relevance['plans'][0]['requirements'][0]['source'])
                    self.assertEqual(reviews.reassessment(connection, '/repo', 'G-002'), {})
                    if fail_clear:
                        raise RuntimeError('clear interrupted')
                effects = planner_publication.ApplyGeneralPlanningBatchEffects(
                    mock.Mock(), clear, mock.Mock(return_value=False), mock.Mock(), mock.Mock(),
                    mock.Mock(return_value=intent), mock.Mock(return_value=False), mock.Mock(return_value=entries), mock.Mock(), mock.Mock())
                with mock.patch.object(planner_publication.planning_publication_state, 'apply_mutable_catalog_changes'), \
                     mock.patch('loop_manager_internal.catalog.remote_deletions.resolve_recovery'), \
                     mock.patch.object(common_guidance, 'planning_content_hash', return_value='pinned-guidance'), \
                     mock.patch.object(common_guidance, 'runtime_content_hash', side_effect=AssertionError('reload')), \
                     mock.patch('loop_manager_internal.notifications.notifications.flush_project_events',
                         side_effect=lambda db, repo: self.assertFalse(db.in_transaction)) as flush:
                    if fail_clear:
                        with self.assertRaisesRegex(RuntimeError, 'clear interrupted'):
                            planner_publication._apply_general_planning_batch(effects, connection, '/repo',
                                Path('/worktree'), result, 'commit', 'spec', 'input')
                    else:
                        planner_publication._apply_general_planning_batch(effects, connection, '/repo',
                            Path('/worktree'), result, 'commit', 'spec', 'input')
                self.assertEqual(flush.call_count, 0 if fail_clear else 1)
                self.assertEqual(reviews.get(connection, '/repo', 'general') is None, fail_clear)
                self.assertEqual(reviews.get(connection, '/repo', 'attempt') is not None, fail_clear)
                self.assertEqual(planning_input_state.has_baseline(connection, '/repo'), not fail_clear)
                self.assertEqual(bool(reviews.reassessment(connection, '/repo', 'G-001')), not fail_clear)
                self.assertEqual(connection.execute("SELECT status FROM exec_plans WHERE plan_id='G-001'").fetchone()[0], 'PLAN_BLOCKED')

    def test_planner_relevance_requires_pinned_remote_text_and_current_blocked_target(self):
        from loop_manager_internal.planning.planning_document_relevance import validate_blocked_plan_inputs
        source = 'docs/requirements/[R-002] Policy.md'
        requirement = '# [R-002] Policy\n\nUse ordinary calls.\n'
        inputs = dict(revision='remote', plans=[dict(plan_id='G-001', requirements=[dict(
            source=source, reason='changes this task validation contract', quote='Use ordinary calls.')])])
        rows = [dict(plan_id='G-001', status='PLAN_BLOCKED')]
        for case in ('valid', 'revision', 'unknown_target', 'archive', 'quote', 'duplicate', 'duplicate_key'):
            with self.subTest(case=case):
                value = json.loads(json.dumps(inputs))
                item = value['plans'][0]['requirements'][0]
                if case == 'revision':
                    value['revision'] = 'local'
                elif case == 'unknown_target':
                    value['plans'][0]['plan_id'] = 'G-002'
                elif case == 'archive':
                    item['source'] = source.replace('requirements/', 'requirements/archive/')
                elif case == 'quote':
                    item['quote'] = 'Invented approval.'
                output = 'BLOCKED_PLAN_INPUTS: ' + json.dumps(value)
                if case == 'duplicate':
                    output += '\n' + output
                elif case == 'duplicate_key':
                    output = output.replace('"revision": "remote"', '"revision": "remote", "revision": "remote"')
                git = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout=requirement))
                if case == 'valid':
                    validate_blocked_plan_inputs(output, 'remote', rows, git, Path('/repo'))
                    git.assert_called_once_with(Path('/repo'), 'show', f'remote:{source}')
                else:
                    with self.assertRaises(ValueError):
                        validate_blocked_plan_inputs(output, 'remote', rows, git, Path('/repo'))

    def test_confirmed_direct_revision_queues_reassessment_without_inferred_requirements(self):
        from loop_manager_internal.storage import database, planning_review_state as reviews
        connection = self._reviewed_publication_connection()
        database._ensure_execplans_schema(connection)
        connection.execute("INSERT INTO exec_plans "
            "(repo, plan_id, path, title, sources, depends_on, content_hash, status, priority) "
            "VALUES ('/repo', 'G-001', 'docs/plans/[G-001] Work.md', 'Work', '[]', '[]', 'revised', 'PLAN_BLOCKED', 'normal')")
        connection.commit()
        with connection:
            reviews.confirm(connection, '/repo', 'base', 'published', revised_plan_ids=('G-001',))
        self.assertEqual(reviews.reassessment(connection, '/repo', 'G-001'),
                         dict(revision='base', confirmed_revision='published', requirements=[]))
        self.assertEqual(connection.execute('SELECT status FROM exec_plans').fetchone()[0], 'PLAN_BLOCKED')

    def test_published_planning_recovery_queues_saved_related_input_once(self):
        from loop_manager_internal.planning import planner_publication
        from loop_manager_internal.storage import database, planning_review_state as reviews
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        connection = self._reviewed_publication_connection()
        database._ensure_execplans_schema(connection)
        connection.execute("INSERT INTO projects (repo, base_branch) VALUES ('/repo', 'main')")
        connection.execute("INSERT INTO exec_plans "
            "(repo, plan_id, path, title, sources, depends_on, content_hash, status, priority) "
            "VALUES ('/repo', 'G-001', 'docs/plans/[G-001] Work.md', 'Work', '[]', '[]', 'unchanged', 'PLAN_BLOCKED', 'normal')")
        reviews.validate(connection, '/repo', 'base', 'BLOCKED_PLAN_INPUTS: ' + json.dumps(dict(
            revision='base', plans=[dict(plan_id='G-001', requirements=[dict(
                source='docs/requirements/[R-002] Policy.md', reason='changes the task contract', quote='Current policy.')])])), 'reviewed')
        connection.commit()
        snapshot = MutableReadySnapshot('/repo', 'base', (), 'token')
        result = loop_manager.ValidatedPlanningResult(plans=(), publication_paths=(), mutable_snapshot=snapshot)
        intent = SimpleNamespace(stage='prepared', base_sha='base', commit_sha='published', manifest='saved',
            snapshot_token='token', specification_fingerprint='spec', planning_fingerprint='input')
        apply = planner_publication.ApplyGeneralPlanningBatchEffects(
            mock.Mock(), mock.Mock(), mock.Mock(return_value=False), mock.Mock(), mock.Mock(),
            mock.Mock(return_value=intent), mock.Mock(return_value=False), mock.Mock(return_value={}), mock.Mock(), mock.Mock())
        recovery = planner_publication.RecoverGeneralPlanningBatchEffects(
            _apply_general_planning_batch=lambda *args, **kwargs: planner_publication._apply_general_planning_batch(apply, *args, **kwargs),
            _assert_remote_preserves_maintenance_batch=mock.Mock(), _clear_active_in_current_transaction=mock.Mock(),
            _git_commit_is_ancestor=mock.Mock(return_value=True), _planning_result_from_manifest=mock.Mock(return_value=result),
            _validate_general_planning_commit=mock.Mock(), general_planning_publication=mock.Mock(return_value=intent),
            git=mock.Mock(return_value=SimpleNamespace(stdout='published')))
        self.assertTrue(reviews.general_pending(connection, '/repo'))
        self.assertEqual(reviews.reassessment(connection, '/repo', 'G-001'), {})
        with mock.patch.object(planner_publication.planning_publication_state, 'apply_mutable_catalog_changes'), \
             mock.patch('loop_manager_internal.catalog.remote_deletions.resolve_recovery'):
            for _ in range(2):
                self.assertEqual(planner_publication.recover_general_planning_batch(
                    recovery, connection, '/repo', Path('/worktree'), 'main'), 'applied')
                self.assertEqual(ExecutionCheckpointRepository(connection).get('/repo', 'G-001').generation, 1)
        self.assertEqual(reviews.reassessment(connection, '/repo', 'G-001')['confirmed_revision'], 'published')
        self.assertFalse(reviews.general_pending(connection, '/repo'))

    def test_no_change_confirms_review_and_guidance_before_cleanup_release(self):
        from loop_manager_internal.storage import planning_input_state, planning_review_state as reviews
        from loop_manager_internal.agents import common_guidance
        for fail_cleanup in (False, True):
            with self.subTest(fail_cleanup=fail_cleanup), tempfile.TemporaryDirectory() as directory:
                connection = self._reviewed_publication_connection()
                worktree = Path(directory)
                ports = FakePorts(planning_snapshot(phase='PLANNING', active_sha='base',
                    worktree=directory, planning_attempt_fingerprint='input', planning_workspace_owned=1))
                ports.state.confirm_planning_review = lambda *args: reviews.confirm(connection, *args)
                ports.state.record_general_plan_declarations = lambda *args, **kwargs: planning_input_state.record_general(connection, *args, **kwargs)
                ports.state.publication_guidance_hash = lambda repo: common_guidance.planning_content_hash(connection, repo)
                original_transaction = ports.state.transaction
                @contextmanager
                def transaction():
                    with original_transaction(), connection:
                        yield
                ports.state.transaction = transaction
                original_clear = ports.state.retain_planning_after_publication
                def clear(*args, **kwargs):
                    self.assertEqual(reviews.get(connection, '/repo', 'general')['confirmed_revision'], 'base')
                    self.assertEqual(connection.execute('SELECT guidance_hash FROM planning_input_baselines')
                                     .fetchone()[0], 'pinned-guidance')
                    original_clear(*args, **kwargs)
                ports.state.retain_planning_after_publication = clear
                if fail_cleanup:
                    ports.git.remove_error = RuntimeError('cleanup interrupted')
                coordinator = PlanningLifecycleCoordinator(ports)
                run = coordinator._new_run(self.context)
                run.planning_worktree = worktree
                run.current_fingerprint = 'input'
                with mock.patch.object(common_guidance, 'planning_content_hash', return_value='pinned-guidance'), \
                     mock.patch.object(common_guidance, 'runtime_content_hash', side_effect=AssertionError('reload')):
                    outcome = coordinator._publication._publish_planner_result(run)
                if fail_cleanup:
                    self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
                else:
                    self.assertIsNone(outcome)
                self.assertIsNotNone(reviews.get(connection, '/repo', 'general'))
                self.assertIsNone(reviews.get(connection, '/repo', 'attempt'))
                self.assertTrue(planning_input_state.has_baseline(connection, '/repo'))
                self.assertEqual(ports.state.pending_publication_release('/repo') is not None, fail_cleanup)
                if fail_cleanup:
                    self.assertEqual(ports.state.snapshot.worktree, directory)
                    self.assertEqual(ports.state.snapshot.phase, 'PLANNING')

    def test_publication_intent_rejects_bad_hash_before_insert_and_recovers(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        database = Path(directory.name) / "state.db"
        connection = sqlite3.connect(database)
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        connection.execute(GENERAL_PLANNING_PUBLICATIONS_SCHEMA)
        connection.execute(
            "INSERT INTO projects (repo, base_branch, planner_session_id) "
            "VALUES ('/repo', 'main', 'planner-session')"
        )
        connection.commit()
        snapshot = MutableReadySnapshot(
            "/repo", "base", (), mutable_snapshot_token("/repo", "base", ())
        )
        manifest = loop_manager._planning_result_manifest(
            loop_manager.ValidatedPlanningResult(
                plans=(), publication_paths=(), mutable_snapshot=snapshot,
            )
        )
        valid = GeneralPlanningPublication(
            "/repo", "prepared", "base", "commit", snapshot.token, "spec", "input",
            manifest, hashlib.sha256(manifest.encode()).hexdigest(),
        )
        with self.assertRaises(PlanningResultInvalid):
            record_general_planning_publication(
                connection, replace(valid, manifest_hash="invalid")
            )
        for invalid_manifest in ("", "[]", "invalid", "{}"):
            with self.subTest(manifest=invalid_manifest):
                with self.assertRaises(PlanningResultInvalid):
                    record_general_planning_publication(
                        connection, replace(
                            valid, manifest=invalid_manifest,
                            manifest_hash=hashlib.sha256(invalid_manifest.encode()).hexdigest(),
                        ),
                    )
        for field, value in (("base_sha", "wrong-base"), ("snapshot_token", "wrong-token")):
            with self.subTest(field=field):
                with self.assertRaises(PlanningResultInvalid):
                    record_general_planning_publication(
                        connection, replace(valid, **{field: value})
                    )
        self.assertIsNone(general_planning_publication(connection, "/repo"))
        self.assertEqual(record_general_planning_publication(connection, valid), valid)
        self.assertEqual(record_general_planning_publication(connection, valid), valid)
        self.assertEqual(general_planning_publication(connection, "/repo"), valid)
        connection.close()
        resumed = sqlite3.connect(database)
        self.addCleanup(resumed.close)
        resumed.row_factory = sqlite3.Row
        restored = general_planning_publication(resumed, "/repo")
        self.assertEqual(restored, valid)
        self.assertEqual(
            loop_manager._planning_result_from_manifest(restored.manifest).mutable_snapshot,
            snapshot,
        )
        self.assertEqual(
            resumed.execute(
                "SELECT planner_session_id FROM projects WHERE repo = '/repo'"
            ).fetchone()["planner_session_id"],
            "planner-session",
        )

    def test_unpublished_assessment_keeps_owned_worktree_for_every_ready_guard(self):
        for guard in ("clean", "dirty", "committed", "ownership", "publication", "project_blocked", "active"):
            with self.subTest(guard=guard), tempfile.TemporaryDirectory() as directory:
                snapshot = planning_snapshot(
                    phase="PLANNING", active_sha="base", worktree=directory,
                    planning_attempt_fingerprint="attempt", planning_workspace_owned=1,
                    processed_sha="old", planning_fingerprint="successful-input",
                    specification_fingerprint="successful-specification",
                )
                cycle = maintenance_cycle(
                    phase=maintenance_state.MaintenancePhase.ASSESSMENT,
                    cycle_base_sha="base", last_success_merged_count=5,
                    last_success_file_assessment_merged_count=5,
                )
                ports = FakePorts(snapshot, cycle)
                ports.state.schedule_result.runnable_ready = {"plan_id": "G-058"}
                if guard == "dirty":
                    original = ports.git.run
                    ports.git.run = lambda path, *args: (
                        SimpleNamespace(stdout="?? user.txt", returncode=0)
                        if args == ("status", "--porcelain") else original(path, *args)
                    )
                elif guard == "committed":
                    ports.git.head = "new-commit"
                elif guard == "ownership":
                    ports.git.ownership_error = PlanningWorkspaceOwnershipError("foreign workspace")
                elif guard == "publication":
                    ports.state.general_intent = object()
                elif guard == "project_blocked":
                    ports.state.schedule_result.project_blocked = True
                elif guard == "active":
                    ports.state.schedule_result.active = {"plan_id": "G-059"}
                coordinator = PlanningLifecycleCoordinator(ports)
                run = coordinator._new_run(self.context)
                outcome = coordinator._selection._select_planning_work(run)
                self.assertIsNone(outcome)
                self.assertEqual(ports.state.snapshot, snapshot)
                self.assertEqual(ports.state.diagnostics, [])
                self.assertEqual(ports.state.cycle, cycle)

    def test_remote_base_race_preserves_commit_before_push_or_registration(self):
        ports = FakePorts(
            planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree="/planning",
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
                planning_result_pending_validation=True,
                planning_result_completed=True,
                planning_result="completed",
            )
        )
        ports.git.dirty_files = ["docs/plans/[G-001] Plan.md"]
        ports.git.remote = "concurrent-base"
        ports.git.ancestor = False

        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(self.context)
        run.planning_worktree = Path("/planning")
        run.validated_result = ports.validation.result
        run.dirty_files = list(ports.git.dirty_files)
        run.current_fingerprint = "fingerprint"
        run.general_fingerprint = "fingerprint"
        run.specification_fingerprint = "specification"
        outcome = coordinator._publication._publish_planner_result(run)

        self.assertEqual(
            outcome.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED
        )
        commands = [arguments for _repo, arguments, _check in ports.git.calls]
        self.assertIn(("commit", "-m", "Add ExecPlan G-001: Plan"), commands)
        self.assertNotIn(
            ("push", "origin", "HEAD:refs/heads/main"), commands
        )
        self.assertEqual(ports.state.clear_calls, [])
        self.assertEqual(ports.state.snapshot.planner_session_id, None)
        self.assertIsNotNone(ports.state.general_intent)

    def test_mutable_ready_race_after_fetch_stops_before_push(self):
        ports = FakePorts(
            planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree="/planning",
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
                planning_result_pending_validation=True,
                planning_result_completed=True,
                planning_result="completed",
            )
        )
        ports.git.dirty_files = ["docs/plans/[G-001] Plan.md"]
        ports.state.general_intent_error_call = 2

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
        self.assertEqual(
            ports.state.snapshot.planning_error,
            "mutable READY changed before push",
        )
        commands = [arguments for _repo, arguments, _check in ports.git.calls]
        self.assertNotIn(
            ("push", "origin", "HEAD:refs/heads/main"), commands
        )

    def test_live_pr_race_defers_before_push_and_preserves_planning_candidate(self):
        from loop_manager_internal.planning.planner_validation import MutableReadyPRConflict

        ports = FakePorts(
            planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree="/planning",
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
                planning_result_pending_validation=True,
                planning_result_completed=True,
                planning_result="completed",
            )
        )
        ports.git.dirty_files = ["docs/plans/[G-001] Plan.md"]
        ports.state.record_general_intent = mock.Mock(
            side_effect=MutableReadyPRConflict("mutable READY snapshot 이후 PR이 등록됐습니다: G-001")
        )
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(self.context)
        run.planning_worktree = Path("/planning")
        run.validated_result = ports.validation.result
        run.dirty_files = list(ports.git.dirty_files)
        run.current_fingerprint = "fingerprint"
        run.general_fingerprint = "fingerprint"
        run.specification_fingerprint = "specification"

        outcome = coordinator._publication._publish_planner_result(run)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED)
        self.assertEqual(ports.state.snapshot.phase, "PLANNING")
        self.assertEqual(ports.state.snapshot.worktree, "/planning")
        self.assertEqual(ports.state.snapshot.planning_result, "completed")
        self.assertIsNone(ports.state.snapshot.planning_error)
        ports.state.record_general_intent.assert_called_once()
        commands = [arguments for _repo, arguments, _check in ports.git.calls]
        self.assertNotIn(("push", "origin", "HEAD:refs/heads/main"), commands)
        self.assertEqual(ports.state.clear_calls, [])

    def test_live_pr_race_after_fetch_supersedes_only_unpublished_intent(self):
        from loop_manager_internal.planning.planner_validation import MutableReadyPRConflict

        ports = FakePorts(
            planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree="/planning",
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
                planning_result_pending_validation=True,
                planning_result_completed=True,
                planning_result="completed",
            )
        )
        ports.git.dirty_files = ["docs/plans/[G-001] Plan.md"]
        record_intent = ports.state.record_general_intent
        calls = 0

        def record_or_race(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise MutableReadyPRConflict("mutable READY snapshot 이후 PR이 등록됐습니다: G-001")
            return record_intent(*args)

        ports.state.record_general_intent = mock.Mock(side_effect=record_or_race)
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(self.context)
        run.planning_worktree = Path("/planning")
        run.validated_result = ports.validation.result
        run.dirty_files = list(ports.git.dirty_files)
        run.current_fingerprint = "fingerprint"
        run.general_fingerprint = "fingerprint"
        run.specification_fingerprint = "specification"

        outcome = coordinator._publication._publish_planner_result(run)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED)
        self.assertIsNone(ports.state.general_intent)
        self.assertEqual(ports.state.snapshot.worktree, "/planning")
        self.assertEqual(ports.state.snapshot.planning_result, "completed")
        self.assertIsNone(ports.state.snapshot.planning_error)
        self.assertEqual(ports.state.record_general_intent.call_count, 2)
        commands = [arguments for _repo, arguments, _check in ports.git.calls]
        self.assertNotIn(("push", "origin", "HEAD:refs/heads/main"), commands)
        self.assertEqual(ports.state.clear_calls, [])

    def test_prepared_publication_recovers_before_agent_or_validation(self):
        cycle = maintenance_cycle(
            phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            cycle_base_sha="base",
            publication_stage="prepared",
            publication_base_sha="old-base",
            publication_commit_sha="plan-commit",
            revision=3,
        )
        ports = FakePorts(
            planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree="/planning",
                planning_attempt_fingerprint="maintenance-fingerprint",
                planning_workspace_owned=1,
            ),
            cycle,
        )
        ports.git.remote = "published-base"
        ports.state.recovered_cycle = replace(
            cycle,
            publication_stage="remote_confirmed",
            publication_remote_oid="published-base",
        )

        ports.state.transaction = mock.Mock(side_effect=AssertionError('external recovery inside outer transaction'))
        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(
            outcome.kind, PlanningOutcomeKind.PUBLICATION_RECOVERY
        )
        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(
            ports.state.recover_calls,
            [("/repo", Path("/planning"), "main")],
        )

    def test_general_publication_cleanup_failure_keeps_saved_planning_state(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "planning"
            worktree.mkdir()
            before = planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree=str(worktree),
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
                planning_error="saved publication failure",
                planning_error_fingerprint="fingerprint",
                planner_session_id="saved-session",
                planning_result="saved result",
                planning_result_completed=True,
            )
            ports = FakePorts(before)
            ports.state.general_intent = SimpleNamespace(
                base_sha="base", commit_sha="plan-commit", stage="prepared",
            )
            ports.git.remove_error = RuntimeError("cleanup failed")

            outcome = PlanningLifecycleCoordinator(ports).run(self.context)

            self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            self.assertEqual(ports.state.snapshot.planning_error,
                             "saved publication failure")
            self.assertEqual(ports.state.snapshot.planning_error_fingerprint,
                             "fingerprint")
            self.assertEqual(ports.state.snapshot.worktree, str(worktree))
            self.assertEqual(ports.state.snapshot.planner_session_id,
                             "saved-session")
            self.assertTrue(worktree.exists())
            self.assertEqual(ports.agent.run_count, 0)

    def test_verified_pending_general_publication_resumes_saved_result(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING",
            active_sha="base",
            worktree="/planning",
            planning_attempt_fingerprint="fingerprint",
            planning_workspace_owned=1,
            planning_error="saved push failure",
            planning_error_fingerprint="fingerprint",
            planning_result="saved result",
            planning_result_completed=True,
        ))
        ports.state.general_intent = SimpleNamespace(
            base_sha="base", commit_sha="plan-commit", stage="prepared",
        )
        ports.state.general_recovery_result = "pending"
        ports.git.dirty_files = ["docs/plans/[G-001] Plan.md"]
        ports.git.advance_remote_on_push = True

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertNotEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR,
                            ports.events.messages)
        self.assertEqual(ports.agent.run_count, 0)
        self.assertTrue(any(
            arguments[:1] == ("push",)
            for _repo, arguments, _check in ports.git.calls
        ))

    def test_pending_general_publication_cleanup_failure_restores_saved_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "planning"
            worktree.mkdir()
            before = planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree=str(worktree),
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
                planning_error="saved push failure",
                planning_error_fingerprint="fingerprint",
                planning_validation_error="saved diagnostics",
                planner_session_id="saved-session",
                planning_result="saved result",
                planning_result_pending_validation=True,
                planning_result_completed=True,
            )
            ports = FakePorts(before)
            ports.state.general_intent = SimpleNamespace(
                base_sha="base", commit_sha="plan-commit", stage="prepared",
            )
            ports.state.general_recovery_result = "pending"
            def apply_batch(*_args):
                ports.state.retain_planning_after_publication('/repo', 'plan-commit')
            ports.state.publish_general_batch = mock.Mock(side_effect=apply_batch)
            ports.git.dirty_files = ["docs/plans/[G-001] Plan.md"]
            ports.git.advance_remote_on_push = True
            ports.git.remove_error = RuntimeError("cleanup failed")

            outcome = PlanningLifecycleCoordinator(ports).run(self.context)

            self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            self.assertEqual(ports.state.snapshot.phase, "PLANNING")
            self.assertEqual(ports.state.snapshot.active_sha, "base")
            self.assertEqual(ports.state.snapshot.worktree, str(worktree))
            self.assertEqual(ports.state.snapshot.planning_attempt_fingerprint,
                             "fingerprint")
            self.assertEqual(ports.state.snapshot.planning_workspace_owned, 1)
            self.assertEqual(ports.state.snapshot.planning_error,
                             "saved push failure")
            self.assertEqual(ports.state.snapshot.planning_error_fingerprint,
                             "fingerprint")
            self.assertEqual(ports.state.snapshot.planning_validation_error,
                             "saved diagnostics")
            self.assertEqual(ports.state.snapshot.planner_session_id,
                             "saved-session")
            self.assertEqual(ports.state.snapshot.planning_result,
                             "saved result")
            self.assertTrue(ports.state.snapshot.planning_result_pending_validation)
            self.assertTrue(ports.state.snapshot.planning_result_completed)
            self.assertIsNotNone(ports.state.general_intent)
            self.assertTrue(worktree.exists())
            self.assertEqual(ports.agent.run_count, 0)
            self.assertFalse(ports.state.validation_cleared)
            ports.state.publish_general_batch.assert_called_once()

    def test_remote_publication_retry_keeps_saved_result_and_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "planning"
            worktree.mkdir()
            ports = FakePorts(planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree=str(worktree),
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
                planning_result="saved result",
                planning_result_completed=True,
            ))
            ports.git.dirty_files = ["docs/plans/[G-001] Plan.md"]
            ports.git.remote = "concurrent-base"
            ports.git.remove_error = RuntimeError("cleanup failed")

            outcome = PlanningLifecycleCoordinator(ports).run(self.context)

            self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            self.assertEqual(ports.state.snapshot.phase, "PLANNING")
            self.assertEqual(ports.state.snapshot.worktree, str(worktree))
            self.assertEqual(ports.state.snapshot.planning_result,
                             "saved result")
            self.assertTrue(ports.state.snapshot.planning_result_completed)
            self.assertIsNone(ports.state.snapshot.planning_error_fingerprint)
            self.assertIsNotNone(ports.state.general_intent)
            self.assertTrue(worktree.exists())
            self.assertEqual(ports.agent.run_count, 0)
            self.assertNotIn(("worktree", "remove", str(worktree)),
                             [args for _repo, args, _check in ports.git.calls])

    def test_pending_maintenance_publication_cleanup_failure_restores_state(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "planning"
            worktree.mkdir()
            cycle = maintenance_cycle(
                phase=maintenance_state.MaintenancePhase.ASSESSMENT,
                cycle_base_sha="base",
                publication_stage="prepared",
                publication_base_sha="base",
                revision=4,
            )
            fingerprint = maintenance_planning_fingerprint(cycle, "fingerprint")
            before = planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree=str(worktree),
                planning_attempt_fingerprint=fingerprint,
                planning_workspace_owned=1,
                planning_error="saved publication failure",
                planning_error_fingerprint=fingerprint,
                planning_validation_error="saved diagnostics",
                planner_session_id="saved-session",
                planning_result="saved result",
                planning_result_completed=True,
            )
            ports = FakePorts(before, cycle)
            ports.git.head = "plan-commit"
            ports.git.advance_remote_on_push = True
            ports.git.remove_error = RuntimeError("cleanup failed")
            def apply_batch(*_args):
                ports.state.retain_planning_after_publication('/repo', 'plan-commit')
            ports.state.publish_maintenance_batch = mock.Mock(side_effect=apply_batch)
            coordinator = PlanningLifecycleCoordinator(ports)
            run = coordinator._new_run(self.context)
            run.current_fingerprint = fingerprint
            run.planning_worktree = worktree
            run.maintenance_context = object()
            run.validated_result = ports.validation.result

            outcome = coordinator._publication._publish_planner_result(run)

            self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            self.assertEqual(ports.state.snapshot.phase, "PLANNING")
            self.assertEqual(ports.state.snapshot.active_sha, "base")
            self.assertEqual(ports.state.snapshot.worktree, str(worktree))
            self.assertEqual(ports.state.snapshot.planning_attempt_fingerprint,
                             fingerprint)
            self.assertEqual(ports.state.snapshot.planning_workspace_owned, 1)
            self.assertEqual(ports.state.snapshot.planning_error,
                             "saved publication failure")
            self.assertEqual(ports.state.snapshot.planning_error_fingerprint,
                             fingerprint)
            self.assertEqual(ports.state.snapshot.planning_validation_error,
                             "saved diagnostics")
            self.assertEqual(ports.state.snapshot.planner_session_id,
                             "saved-session")
            self.assertEqual(ports.state.snapshot.planning_result,
                             "saved result")
            self.assertTrue(ports.state.snapshot.planning_result_completed)
            self.assertTrue(worktree.exists())
            ports.state.publish_maintenance_batch.assert_called_once()

    def test_no_change_cleanup_failure_keeps_saved_planning_result(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "planning"
            worktree.mkdir()
            ports = FakePorts(planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree=str(worktree),
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
                planning_error="saved validation failure",
                planning_error_fingerprint="fingerprint",
                planner_session_id="saved-session",
                planning_result="saved result",
                planning_result_completed=True,
            ))
            ports.git.remove_error = RuntimeError("cleanup failed")
            coordinator = PlanningLifecycleCoordinator(ports)
            run = coordinator._new_run(self.context)
            run.planning_worktree = worktree
            run.current_fingerprint = "fingerprint"

            outcome = coordinator._publication._publish_planner_result(run)

            self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            self.assertEqual(ports.state.snapshot.phase, "PLANNING")
            self.assertEqual(ports.state.snapshot.worktree, str(worktree))
            self.assertEqual(ports.state.snapshot.planning_error,
                             "saved validation failure")
            self.assertEqual(ports.state.snapshot.planning_error_fingerprint,
                             "fingerprint")
            self.assertEqual(ports.state.snapshot.planner_session_id,
                             "saved-session")
            self.assertEqual(ports.state.snapshot.planning_result,
                             "saved result")
            self.assertTrue(ports.state.snapshot.planning_result_completed)
            self.assertTrue(worktree.exists())

    def test_prerequisite_publication_cleanup_failure_preserves_planning_and_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "planning"
            worktree.mkdir()
            cycle = maintenance_cycle(
                phase=maintenance_state.MaintenancePhase.ASSESSMENT,
                cycle_base_sha="base",
                revision=4,
            )
            ports = FakePorts(planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree=str(worktree),
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
                planner_session_id="saved-session",
                planning_result=(
                    'MAINTENANCE_RESULT: '
                    '{"outcome":"prerequisites","plan_ids":["G-001"]}'
                ),
                planning_result_completed=True,
            ), cycle)
            ports.git.remove_error = RuntimeError("cleanup failed")
            ports.state.transition_maintenance = mock.Mock(
                side_effect=lambda *_args, **_kwargs: setattr(
                    ports.state, "cycle", replace(
                        ports.state.cycle,
                        phase=maintenance_state.MaintenancePhase.PREREQUISITE_WAIT,
                    )
                )
            )
            coordinator = PlanningLifecycleCoordinator(ports)
            run = coordinator._new_run(self.context)
            run.planning_worktree = worktree
            run.current_fingerprint = "fingerprint"
            run.maintenance_context = object()

            with mock.patch(
                "loop_manager_internal.planning.assessment_service.validate_prerequisite_plan_ids",
                return_value=("G-001",),
            ):
                self.assertIsNone(coordinator._assessment._validate_planner_result(run))

            ports.state.transition_maintenance.assert_not_called()
            cycle = ports.state.cycle
            self.assertEqual(cycle.prerequisite_plan_ids, ("G-001",))
            run.row = ports.state.snapshot
            run.maintenance_cycle = cycle
            outcome = coordinator._publication._publish_planner_result(run)

            self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            self.assertEqual(ports.state.snapshot.phase, "PLANNING")
            self.assertEqual(ports.state.snapshot.worktree, str(worktree))
            self.assertEqual(ports.state.snapshot.planning_error,
                             "cleanup failed")
            self.assertEqual(ports.state.snapshot.planning_error_fingerprint,
                             "fingerprint")
            self.assertEqual(ports.state.snapshot.planner_session_id,
                             "saved-session")
            self.assertTrue(ports.state.snapshot.planning_result_completed)
            self.assertEqual(ports.state.cycle, replace(cycle, phase=maintenance_state.MaintenancePhase.PREREQUISITE_WAIT))
            self.assertIsNotNone(ports.state.pending_publication_release('/repo'))
            self.assertTrue(worktree.exists())
            ports.state.transition_maintenance.assert_called_once()


    def test_unpublished_planner_does_not_activate_stale_ready(self):
        ports = FakePorts(planning_snapshot(
            processed_sha="old-base",
            specification_fingerprint="old-spec",
            planning_fingerprint="old-full",
        ))
        ports.git.specification_fingerprints["old-base"] = "old-spec"
        ports.git.fingerprint = "new-full"
        ports.git.fingerprints["old-base"] = "old-full"
        ports.git.dirty_files = ["docs/plans/[G-001] Plan.md"]
        ports.state.general_intent_error_call = 1
        ports.state.schedule_result = SimpleNamespace(
            active=None, has_ready=True, has_waiting=False,
            runnable_ready=object(), project_blocked=False,
        )
        ports.state.activate_result = object()

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
        self.assertNotEqual(ports.state.snapshot.phase, "WORKING")
        self.assertEqual(ports.state.snapshot.planning_fingerprint, "old-full")
