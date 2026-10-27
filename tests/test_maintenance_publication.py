from tests.support.catalog import plan_text
from contextlib import ExitStack
import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from loop_manager_internal.documents import document_drafts
from loop_manager_internal.documents import document_publication
from loop_manager_internal.catalog import execplan_catalog
from loop_manager_internal.storage import execplan_catalog_state
import loop_manager
from loop_manager_internal.maintenance import maintenance_planning
from loop_manager_internal.maintenance import maintenance_findings
from loop_manager_internal.maintenance import maintenance_file_inventory
from loop_manager_internal.storage import maintenance_file_ledger
from loop_manager_internal.maintenance import maintenance_file_publication
from loop_manager_internal.maintenance import maintenance_reconciliation
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.maintenance.maintenance_file_inventory import SourceInventory, TrackedSource
from loop_manager_internal.planning.planning_catalog import MutableReadySnapshot, mutable_snapshot_token


from tests.support.maintenance_planning_case import MaintenancePlanningCase, git
from tests.support.git import isolated_git_test

@isolated_git_test
class MaintenancePublicationTest(MaintenancePlanningCase):
    def test_applied_maintenance_batch_retains_planning_until_cleanup_release(self):
        from loop_manager_internal.storage import runtime_state
        from loop_manager_internal.storage.database import GENERAL_PLANNING_PUBLICATIONS_SCHEMA
        context = self._begin_barrier()
        self._write_barrier_batch()
        result = self._validate_barrier(context)
        with self.connection:
            self.connection.execute(GENERAL_PLANNING_PUBLICATIONS_SCHEMA)
            self.connection.execute("UPDATE projects SET phase='PLANNING', active_sha=?, worktree=?, "
                "planning_workspace_owned=1, planning_attempt_fingerprint='attempt', planner_session_id='saved-session'",
                (self.base_sha, str(self.repo)))
        git(self.repo, 'add', 'docs/plans')
        git(self.repo, 'commit', '-m', 'Prepare maintenance cleanup release fixture')
        commit = git(self.repo, 'rev-parse', 'HEAD')
        loop_manager.record_maintenance_publication_intent(
            self.connection, str(self.repo), self.repo, 'main', result, commit)
        git(self.repo, 'push', 'origin', 'HEAD:main')
        git(self.repo, 'fetch', 'origin')
        state = loop_manager._PlanningStatePorts(self.connection, runtime_state.RuntimeStateRepository(self.connection),
            maintenance_state.MaintenanceRepository(self.connection), self.repo / 'state')
        cycle = state.publish_maintenance_batch(str(self.repo), self.repo, 'main', result, commit)
        release = state.pending_publication_release(str(self.repo))
        self.assertIsNotNone(release)
        self.assertEqual(release.revision, commit)
        self.assertEqual(cycle.publication_stage, 'remote_confirmed')
        self.assertEqual(state.planning(str(self.repo)).phase, 'PLANNING')
        self.assertEqual(state.planning(str(self.repo)).planner_session_id, 'saved-session')
        state.complete_publication_release(release)
        self.assertIsNone(state.planning(str(self.repo)).phase)
        self.assertIsNone(state.pending_publication_release(str(self.repo)))
        self.assertEqual(state.maintenance_cycle(str(self.repo)), cycle)

    def test_batch_observes_git_files_and_success_fingerprints_before_transaction(self):
        from loop_manager_internal.agents import common_guidance
        from loop_manager_internal.catalog import execplan_drafts
        context = self._begin_barrier()
        with self.connection:
            self.connection.execute('UPDATE maintenance_cycles SET include_general_planning=1')
        self._write_barrier_batch()
        result = self._validate_barrier(context)
        git(self.repo, 'add', 'docs/plans')
        git(self.repo, 'commit', '-m', 'Prepare publication boundary fixture')
        commit = git(self.repo, 'rev-parse', 'HEAD')
        def outside_transaction(function):
            def observe(*args, **kwargs):
                self.assertFalse(self.connection.in_transaction)
                return function(*args, **kwargs)
            return observe
        with ExitStack() as stack:
            for name in ('git', 'git_bytes', 'file_hash', 'plan_declarations', 'is_loop_manager_repository',
                         '_planning_input_fingerprint', '_specification_input_fingerprint'):
                stack.enter_context(mock.patch.object(loop_manager, name,
                    side_effect=outside_transaction(getattr(loop_manager, name))))
            stack.enter_context(mock.patch.object(common_guidance, '_read',
                side_effect=outside_transaction(common_guidance._read)))
            stack.enter_context(mock.patch.object(execplan_drafts, 'input_digests',
                side_effect=outside_transaction(execplan_drafts.input_digests)))
            loop_manager.record_maintenance_publication_intent(
                self.connection, str(self.repo), self.repo, 'main', result, commit)
            git(self.repo, 'push', 'origin', 'HEAD:main')
            git(self.repo, 'fetch', 'origin')
            first = loop_manager.publish_maintenance_planning_batch(
                self.connection, str(self.repo), self.repo, 'main', result, commit)
            second = loop_manager.publish_maintenance_planning_batch(
                self.connection, str(self.repo), self.repo, 'main', result, commit)
        self.assertEqual(first, second)
        self.assertEqual(first.publication_stage, 'remote_confirmed')
        self.assertEqual(self.connection.execute('SELECT processed_sha FROM projects').fetchone()[0], commit)

    def test_batch_rejects_cycle_owner_and_catalog_races_during_observation(self):
        from loop_manager_internal.maintenance import maintenance_publication
        from loop_manager_internal.storage import manual_work_state
        manual_work_state.ensure_schema(self.connection)
        self.connection.commit()
        context = self._begin_barrier()
        self._write_barrier_batch()
        result = self._validate_barrier(context)
        git(self.repo, 'add', 'docs/plans')
        git(self.repo, 'commit', '-m', 'Prepare publication CAS fixture')
        commit = git(self.repo, 'rev-parse', 'HEAD')
        saved = loop_manager.record_maintenance_publication_intent(
            self.connection, str(self.repo), self.repo, 'main', result, commit)
        self.connection.commit()
        declarations = loop_manager.plan_declarations
        for mutation, restore in (
            ('UPDATE maintenance_cycles SET revision=revision+1', 'UPDATE maintenance_cycles SET revision=revision-1'),
            ("UPDATE projects SET planner_session_id='competitor'", 'UPDATE projects SET planner_session_id=NULL'),
            ('UPDATE exec_plans SET catalog_generation=catalog_generation+1',
             'UPDATE exec_plans SET catalog_generation=catalog_generation-1'),
            ("INSERT INTO manual_work_members SELECT repo,'G-001','orphan' FROM projects",
             "DELETE FROM manual_work_members WHERE group_id='orphan'"),
        ):
            def observe(*args):
                self.assertFalse(self.connection.in_transaction)
                value = declarations(*args)
                if args[-1] == commit:
                    with self.connection:
                        self.connection.execute(mutation)
                return value
            with self.subTest(mutation=mutation), \
                    mock.patch.object(loop_manager, 'plan_declarations', side_effect=observe), \
                    self.assertRaisesRegex(loop_manager.MaintenancePlanningError, '게시 준비 이후|수동 소유권'):
                loop_manager._apply_maintenance_planning_batch(
                    self.connection, str(self.repo), self.repo, 'main', result, commit)
            self.assertEqual([row[0] for row in self.connection.execute(
                'SELECT plan_id FROM exec_plans ORDER BY plan_id')], ['G-000', 'G-001'])
            current = maintenance_state.MaintenanceRepository(self.connection).get(str(self.repo))
            self.assertEqual(current.publication_commit_sha, saved.publication_commit_sha)
            self.assertEqual(current.publication_stage, 'prepared')
            with self.connection:
                self.connection.execute(restore)
        self.assertEqual(maintenance_publication._maintenance_publication_db_identity(
            self.connection, str(self.repo), result)[0], saved)

    def test_intent_rejects_owner_changed_during_commit_observation(self):
        context = self._begin_barrier()
        self._write_barrier_batch()
        result = self._validate_barrier(context)
        git(self.repo, 'add', 'docs/plans')
        git(self.repo, 'commit', '-m', 'Prepare intent CAS fixture')
        commit = git(self.repo, 'rev-parse', 'HEAD')
        self.connection.commit()
        validate = loop_manager._validate_maintenance_publication_commit
        def observe(*args, **kwargs):
            self.assertFalse(self.connection.in_transaction)
            validate(*args, **kwargs)
            with self.connection:
                self.connection.execute("UPDATE projects SET planner_session_id='competitor'")
        with mock.patch.object(loop_manager, '_validate_maintenance_publication_commit', side_effect=observe), \
                self.assertRaisesRegex(loop_manager.MaintenancePlanningError, 'intent 준비 이후'):
            loop_manager.record_maintenance_publication_intent(
                self.connection, str(self.repo), self.repo, 'main', result, commit)
        cycle = maintenance_state.MaintenanceRepository(self.connection).get(str(self.repo))
        self.assertIsNone(cycle.publication_stage)
        self.assertEqual(self.connection.execute('SELECT planner_session_id FROM projects').fetchone()[0], 'competitor')

    def test_legacy_manager_plan_recovers_from_published_maintenance_intent(self):
        from loop_manager_internal.catalog import manager_sources
        from loop_manager_internal.storage import verify_input_requests

        self._begin_legacy_assessment(self.base_sha)
        self.connection.execute(
            "UPDATE maintenance_cycles SET include_general_planning = 1 WHERE repo = ?",
            (str(self.repo),),
        )
        context = loop_manager.prepare_maintenance_planning_context(
            self.connection, str(self.repo), self.repo, self.base_sha,
        )
        request = verify_input_requests.register(
            self.connection, str(self.repo), context.active_sha,
        )
        path = self.repo / "docs/plans/[G-002] Verify Inputs.md"
        path.write_text(
            "---\nid: G-002\ntitle: Verify Inputs\nsources:\n  - "
            + manager_sources.SOURCE
            + "\ndepends_on: []\ncomplexity: low\nrisk: medium\npriority: normal\n"
            + "---\n\n## Purpose\n\n"
            + manager_sources.RULE
            + "\n\n## Acceptance Criteria\n\n- Keep scripts/verify-inputs.json current.\n"
            + "\n## Implementation\n\n- Record the declared verify inputs.\n"
            + "\n## Validation\n\n- Keep full verify required.\n"
            + "\n## Decision Log\n\n"
            + manager_sources.PREFIX
            + json.dumps(request, sort_keys=True)
            + "\n",
            encoding="utf-8", newline="\n",
        )
        # Model a durable batch accepted before the critical-only policy existed.
        with mock.patch.object(verify_input_requests, "validate_new_priority", return_value=None):
            result = loop_manager.validate_maintenance_planner_result(
                self.connection, context, self.repo, loop_manager.changed_files(self.repo),
                refactoring_plan_ids=(), allow_general_plans=True,
            )
        self.assertIsNotNone(result)

        git(self.repo, "add", "docs/plans")
        git(self.repo, "commit", "-m", "Prepare legacy verify-input maintenance plan")
        plan_commit = git(self.repo, "rev-parse", "HEAD")
        intent = loop_manager.record_maintenance_publication_intent(
            self.connection, str(self.repo), self.repo, "main", result, plan_commit,
        )
        self.assertEqual(intent.publication_stage, "prepared")
        git(self.repo, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM exec_plans WHERE repo = ? AND plan_id = 'G-002'",
            (str(self.repo),),
        ).fetchone())

        self.connection.commit()
        restarted = sqlite3.connect(":memory:")
        self.addCleanup(restarted.close)
        self.connection.backup(restarted)
        restarted.row_factory = sqlite3.Row
        recovered = loop_manager.recover_maintenance_planning_batch(
            restarted, str(self.repo), self.repo, "main",
        )

        row = restarted.execute(
            "SELECT priority, status FROM exec_plans WHERE repo = ? AND plan_id = 'G-002'",
            (str(self.repo),),
        ).fetchone()
        self.assertEqual(tuple(row), ("normal", "READY"))
        self.assertEqual(recovered.phase, maintenance_state.MaintenancePhase.IDLE)
        self.assertEqual(recovered.last_cycle_commit, plan_commit)
        self.assertIsNone(recovered.publication_stage)
        self.assertEqual(restarted.execute(
            "SELECT COUNT(*) FROM exec_plans WHERE repo = ? AND plan_id = 'G-002'",
            (str(self.repo),),
        ).fetchone()[0], 1)

    def test_publication_requires_latest_single_commit_and_is_atomic_and_idempotent(self):
        context = self._begin_barrier()
        self._write_barrier_batch()
        result = self._validate_barrier(context)
        loop_manager.validate_maintenance_publication_base(
            self.connection, str(self.repo), self.repo, "main", context
        )
        git(self.repo, "add", "docs/plans")
        git(self.repo, "commit", "-m", "Publish maintenance batch")
        plan_commit = git(self.repo, "rev-parse", "HEAD")
        intent = loop_manager.record_maintenance_publication_intent(
            self.connection,
            str(self.repo),
            self.repo,
            "main",
            result,
            plan_commit,
        )
        self.assertEqual(intent.publication_commit_sha, plan_commit)

        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "fast-forward push"
        ):
            loop_manager.publish_maintenance_planning_batch(
                self.connection,
                str(self.repo),
                self.repo,
                "main",
                result,
                plan_commit,
            )

        git(self.repo, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")
        self.connection.execute(
            "UPDATE exec_plans SET workspace_owned = 2 "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        )
        self.connection.commit()
        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "snapshot"
        ):
            loop_manager.publish_maintenance_planning_batch(
                self.connection,
                str(self.repo),
                self.repo,
                "main",
                result,
                plan_commit,
            )
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM exec_plans WHERE repo = ? AND plan_id = 'G-002'",
                (str(self.repo),),
            ).fetchone()
        )
        self.connection.execute(
            "UPDATE exec_plans SET workspace_owned = 0 "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        )
        self.connection.commit()

        self.connection.execute(
            "UPDATE exec_plans SET pr_head_oid = ? "
            "WHERE repo = ? AND plan_id = 'G-001'",
            ("b" * 40, str(self.repo)),
        )
        self.connection.commit()
        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "snapshot"
        ):
            loop_manager.publish_maintenance_planning_batch(
                self.connection,
                str(self.repo),
                self.repo,
                "main",
                result,
                plan_commit,
            )
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM exec_plans WHERE repo = ? AND plan_id = 'G-002'",
                (str(self.repo),),
            ).fetchone()
        )
        self.connection.execute(
            "UPDATE exec_plans SET pr_head_oid = NULL "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        )
        self.connection.commit()

        self.connection.execute(
            "UPDATE exec_plans SET pr_conflict_recovery = 1, "
            "pr_conflict_head_oid = ?, pr_conflict_base_oid = ? "
            "WHERE repo = ? AND plan_id = 'G-001'",
            ("c" * 40, "d" * 40, str(self.repo)),
        )
        self.connection.commit()
        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "snapshot"
        ):
            loop_manager.publish_maintenance_planning_batch(
                self.connection,
                str(self.repo),
                self.repo,
                "main",
                result,
                plan_commit,
            )
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM exec_plans WHERE repo = ? AND plan_id = 'G-002'",
                (str(self.repo),),
            ).fetchone()
        )
        self.connection.execute(
            "UPDATE exec_plans SET pr_conflict_recovery = 0, "
            "pr_conflict_head_oid = NULL, pr_conflict_base_oid = NULL "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        )
        self.connection.commit()

        cycle = loop_manager.publish_maintenance_planning_batch(
            self.connection,
            str(self.repo),
            self.repo,
            "main",
            result,
            plan_commit,
        )
        repeated = loop_manager.publish_maintenance_planning_batch(
            self.connection,
            str(self.repo),
            self.repo,
            "main",
            result,
            plan_commit,
        )
        rows = self.connection.execute(
            "SELECT plan_id, depends_on, plan_sha, current_revision_sha, "
            "publication_sha FROM exec_plans "
            "WHERE repo = ? ORDER BY plan_id",
            (str(self.repo),),
        ).fetchall()

        self.assertEqual(cycle.phase, maintenance_state.MaintenancePhase.REFACTORING)
        self.assertEqual(repeated, cycle)
        self.assertEqual(cycle.refactoring_plan_ids, ("G-002", "G-003"))
        self.assertEqual(cycle.revalidation_plan_ids, ("G-001",))
        self.assertEqual(
            json.loads(rows[1]["depends_on"]), ["G-000", "G-002", "G-003"],
        )
        self.assertTrue(all(row["plan_sha"] == plan_commit for row in rows[1:]))
        self.assertTrue(
            all(row["current_revision_sha"] == plan_commit for row in rows[1:])
        )
        self.assertEqual(rows[1]["publication_sha"], self.base_sha)
        self.assertTrue(
            all(row["publication_sha"] == plan_commit for row in rows[2:])
        )

    def test_publication_rejects_body_drift_after_result_validation(self):
        context = self._begin_barrier()
        self._write_barrier_batch()
        result = self._validate_barrier(context)
        validated = self.ready_path.read_text(encoding="utf-8")
        self.ready_path.write_text(
            validated.replace(
                "A concrete structural responsibility problem",
                "A tampered product goal",
            ),
            encoding="utf-8",
        )
        git(self.repo, "add", "docs/plans")
        git(self.repo, "commit", "-m", "Tamper with validated plan body")
        plan_commit = git(self.repo, "rev-parse", "HEAD")

        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "결과 검증 후 변경"
        ):
            loop_manager.record_maintenance_publication_intent(
                self.connection,
                str(self.repo),
                self.repo,
                "main",
                result,
                plan_commit,
            )
        cycle = maintenance_state.MaintenanceRepository(self.connection).get(
            str(self.repo)
        )
        self.assertIsNone(cycle.publication_stage)

    def test_publication_rolls_back_new_and_revised_rows_if_cycle_update_fails(self):
        context = self._begin_barrier()
        self._write_barrier_batch()
        result = self._validate_barrier(context)
        git(self.repo, "add", "docs/plans")
        git(self.repo, "commit", "-m", "Prepare atomic maintenance batch")
        plan_commit = git(self.repo, "rev-parse", "HEAD")
        intent = loop_manager.record_maintenance_publication_intent(
            self.connection,
            str(self.repo),
            self.repo,
            "main",
            result,
            plan_commit,
        )
        git(self.repo, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")
        before = tuple(
            self.connection.execute(
                "SELECT depends_on, content_hash, plan_sha FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(self.repo),),
            ).fetchone()
        )

        with mock.patch.object(
            maintenance_state.MaintenanceRepository,
            "transition",
            side_effect=maintenance_state.MaintenanceStateError(
                "injected cycle update failure"
            ),
        ):
            with self.assertRaisesRegex(
                maintenance_state.MaintenanceStateError, "injected"
            ):
                loop_manager.publish_maintenance_planning_batch(
                    self.connection,
                    str(self.repo),
                    self.repo,
                    "main",
                    result,
                    plan_commit,
                )

        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-002'",
                (str(self.repo),),
            ).fetchone()
        )
        after = tuple(
            self.connection.execute(
                "SELECT depends_on, content_hash, plan_sha FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(self.repo),),
            ).fetchone()
        )
        self.assertEqual(after, before)
        self.assertEqual(
            maintenance_state.MaintenanceRepository(self.connection).get(
                str(self.repo)
            ),
            intent,
        )

        completed = loop_manager.publish_maintenance_planning_batch(
            self.connection,
            str(self.repo),
            self.repo,
            "main",
            result,
            plan_commit,
        )
        self.assertEqual(
            completed.phase, maintenance_state.MaintenancePhase.REFACTORING
        )

    def test_publication_rejects_clean_filter_content_changes(self):
        context = self._begin_barrier()
        self._write_barrier_batch()
        result = self._validate_barrier(context)
        git(self.repo, "add", "docs/plans")
        git(self.repo, "commit", "-m", "Prepare validated batch")
        plan_commit = git(self.repo, "rev-parse", "HEAD")
        path = result.plans[0][0]
        original_git_bytes = loop_manager.git_bytes
        # Mock the committed clean-filter output: only byte preservation is
        # under test, so running an external filter adds no useful evidence.
        for before, after in ((b"priority: normal", b"priority: high"),
                              (b"Preserve", b"Discard")):
            with self.subTest(change=before):
                def filtered(root, *args):
                    contents = original_git_bytes(root, *args)
                    if args == ("show", f"{plan_commit}:{path}"):
                        self.assertIn(before, contents)
                        return contents.replace(before, after)
                    return contents
                with mock.patch("loop_manager.git_bytes", side_effect=filtered):
                    with self.assertRaisesRegex(loop_manager.MaintenancePlanningError, "검증 후"):
                        loop_manager._validate_maintenance_planning_commit(
                            self.repo, result, plan_commit)

    def test_crlf_batch_recovers_from_durable_intent_after_restart(self):
        git(self.repo, "config", "core.autocrlf", "true")
        self.ready_path.write_bytes(
            self.ready_path.read_bytes().replace(b"\n", b"\r\n")
        )
        context = self._begin_barrier()
        self._write_barrier_batch()
        # Actual Git CRLF cleaning must cover newly registered plans too.
        for path in (self.repo / "docs/plans").glob("[[]G-00[23]]*.md"):
            path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        result = self._validate_barrier(context)
        self.assertTrue(all(b"\r\n" in (self.repo / path).read_bytes()
                            for path, _plan in result.plans))
        self.assertIn(b"\r\n", self.ready_path.read_bytes())
        git(self.repo, "add", "docs/plans")
        git(self.repo, "commit", "-m", "Prepare recoverable maintenance batch")
        plan_commit = git(self.repo, "rev-parse", "HEAD")
        committed_ready = subprocess.run(
            [
                "git", "-C", str(self.repo), "show",
                f"{plan_commit}:docs/plans/[G-001] Ready Feature.md",
            ],
            check=True,
            stdout=subprocess.PIPE,
        ).stdout
        self.assertNotIn(b"\r\n", committed_ready)
        self.assertIn(b"\n", committed_ready)
        loop_manager.record_maintenance_publication_intent(
            self.connection,
            str(self.repo),
            self.repo,
            "main",
            result,
            plan_commit,
        )
        git(self.repo, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")

        self.connection.execute(
            "ALTER TABLE maintenance_cycles DROP COLUMN publication_id_allocations"
        )
        maintenance_state.ensure_schema(self.connection)
        migrated_intent = maintenance_state.MaintenanceRepository(self.connection).get(
            str(self.repo)
        )
        self.assertEqual(migrated_intent.publication_id_allocations, ())

        row = self.connection.execute(
            "SELECT content_hash FROM exec_plans "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        ).fetchone()
        expected_hash = str(row["content_hash"])
        self.connection.execute(
            "UPDATE exec_plans SET content_hash = 'catalog-drift' "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        )
        self.connection.commit()
        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "content hash"
        ):
            loop_manager.recover_maintenance_planning_batch(
                self.connection, str(self.repo), self.repo, "main"
            )
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-002'",
                (str(self.repo),),
            ).fetchone()
        )

        self.connection.execute(
            "UPDATE exec_plans SET content_hash = ? "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (expected_hash, str(self.repo)),
        )
        self.connection.commit()
        recovered = loop_manager.recover_maintenance_planning_batch(
            self.connection, str(self.repo), self.repo, "main"
        )
        repeated = loop_manager.recover_maintenance_planning_batch(
            self.connection, str(self.repo), self.repo, "main"
        )
        rows = self.connection.execute(
            "SELECT plan_id, depends_on FROM exec_plans "
            "WHERE repo = ? ORDER BY plan_id",
            (str(self.repo),),
        ).fetchall()
        self.assertEqual(
            recovered.phase, maintenance_state.MaintenancePhase.REFACTORING
        )
        self.assertEqual(repeated, recovered)
        snapshot = execplan_catalog.load_execplan_snapshot(self.repo)
        for path, plan in result.plans:
            row = self.connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
                (str(self.repo), plan["id"]),
            ).fetchone()
            published = loop_manager.git_bytes(self.repo, "show", f"{plan_commit}:{path}")
            self.assertNotIn(b"\r\n", published)
            self.assertEqual(row["publication_content_hash"], hashlib.sha256(published).hexdigest())
            self.assertEqual(row["content_hash"], loop_manager.file_hash(self.repo / path))
            self.assertNotEqual(row["content_hash"], row["publication_content_hash"])
            identity = replace(execplan_catalog_state.publication_identity(row), status="MERGED")
            proof = loop_manager._LegacyCatalogMergeProof(self.repo, plan_commit, {})
            self.assertTrue(proof.verify_stored(snapshot.active[str(plan["id"])], identity).merged)
        self.assertEqual(
            [row["plan_id"] for row in rows],
            ["G-000", "G-001", "G-002", "G-003"],
        )
        self.assertEqual(
            json.loads(rows[1]["depends_on"]), ["G-000", "G-002", "G-003"],
        )
        self.assertIn(b"\r\n", self.ready_path.read_bytes())
        self.assertNotIn(b"\n", self.ready_path.read_bytes().replace(b"\r\n", b""))

    def test_published_batch_recovers_after_remote_tip_advances(self):
        context = self._begin_barrier()
        self._write_barrier_batch()
        result = self._validate_barrier(context)
        git(self.repo, "add", "docs/plans")
        git(self.repo, "commit", "-m", "Prepare batch")
        plan_commit = git(self.repo, "rev-parse", "HEAD")
        intent = loop_manager.record_maintenance_publication_intent(
            self.connection, str(self.repo), self.repo, "main",
            result, plan_commit,
        )
        git(self.repo, "push", "origin", "HEAD:main")
        other = Path(self.temporary.name) / "published-descendant"
        git(self.repo, "worktree", "add", "--detach", str(other), plan_commit)
        git(other, "commit", "--allow-empty", "-m", "Advance after publication")
        git(other, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")

        recovered = loop_manager.recover_maintenance_planning_batch(
            self.connection, str(self.repo), self.repo, "main"
        )
        repeated = loop_manager.recover_maintenance_planning_batch(
            self.connection, str(self.repo), self.repo, "main"
        )
        rows = self.connection.execute(
            "SELECT plan_id FROM exec_plans WHERE repo = ? ORDER BY plan_id",
            (str(self.repo),),
        ).fetchall()

        self.assertEqual(
            recovered.phase, maintenance_state.MaintenancePhase.REFACTORING
        )
        self.assertEqual(repeated, recovered)
        self.assertEqual(recovered.publication_commit_sha, plan_commit)
        self.assertEqual(recovered.publication_remote_oid, plan_commit)
        self.assertEqual(
            [row["plan_id"] for row in rows],
            ["G-000", "G-001", "G-002", "G-003"],
        )
        self.assertNotEqual(recovered, intent)

    def test_legacy_pushed_intent_recovers_after_restart_without_claiming_root_review(self):
        plan_commit, _intent, inventory = self._prepare_pushed_file_assessment_batch(legacy=True)
        self.connection.commit()
        restarted_connection = sqlite3.connect(":memory:")
        self.addCleanup(restarted_connection.close)
        self.connection.backup(restarted_connection)
        restarted_connection.row_factory = sqlite3.Row
        # A new manager instance consumes only durable pre-policy evidence.
        recovered = loop_manager.recover_maintenance_planning_batch(
            restarted_connection, str(self.repo), self.repo, "main",
        )
        self.assertEqual(recovered.phase, maintenance_state.MaintenancePhase.REFACTORING)
        self.assertEqual(recovered.last_success_file_assessment_merged_count, 1)
        ledger = maintenance_file_ledger.FileReviewRepository(restarted_connection)
        self.assertEqual(ledger.last_success(str(self.repo)).source_revision, self.base_sha)
        self.assertEqual(len(ledger.reviews(str(self.repo))), len(inventory.sources))
        self.assertEqual(ledger.root_placements(str(self.repo)), ())
        self.assertIsNone(ledger.reusable_root_reviews(str(self.repo), inventory))
        recovered_again = loop_manager.recover_maintenance_planning_batch(
            restarted_connection, str(self.repo), self.repo, "main",
        )
        self.assertEqual(recovered_again, recovered)
        latest = maintenance_file_inventory.tracked_source_inventory(self.repo, "main", plan_commit)
        progress = loop_manager.maintenance_file_review.ProvisionalReviewRepository(restarted_connection).progress(
            str(self.repo), self.repo, "main", recovered.revision, latest,
        )
        self.assertEqual({source.path for source in progress.selected},
                         {source.path for source in latest.sources})
        self.assertIsNotNone(progress.next_unit)
        # Imported legacy judgments remain audit rows; no common version was recorded.
        self.assertEqual(len(ledger.reviews(str(self.repo))), len(inventory.sources))

    def test_published_batch_recovery_rejects_changed_batch_path(self):
        context = self._begin_barrier()
        self._write_barrier_batch()
        result = self._validate_barrier(context)
        git(self.repo, "add", "docs/plans")
        git(self.repo, "commit", "-m", "Prepare batch")
        plan_commit = git(self.repo, "rev-parse", "HEAD")
        intent = loop_manager.record_maintenance_publication_intent(
            self.connection, str(self.repo), self.repo, "main",
            result, plan_commit,
        )
        git(self.repo, "push", "origin", "HEAD:main")
        other = Path(self.temporary.name) / "changed-published-batch"
        git(self.repo, "worktree", "add", "--detach", str(other), plan_commit)
        changed_plan = other / self.ready_path.relative_to(self.repo)
        changed_plan.write_text(
            changed_plan.read_text(encoding="utf-8") + "\nRemote edit.\n",
            encoding="utf-8",
        )
        git(other, "add", changed_plan.relative_to(other).as_posix())
        git(other, "commit", "-m", "Change a published batch path")
        git(other, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")

        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "batch 경로"
        ):
            loop_manager.recover_maintenance_planning_batch(
                self.connection, str(self.repo), self.repo, "main"
            )
        self.assertEqual(
            maintenance_state.MaintenanceRepository(self.connection).get(str(self.repo)),
            intent,
        )
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-002'",
                (str(self.repo),),
            ).fetchone()
        )

    def test_published_batch_recovery_keeps_added_user_plan_unobserved(self):
        self._assert_published_batch_recovery_preserves_catalog_drift("addition")

    def test_published_batch_recovery_keeps_modified_user_plan_unobserved(self):
        self._assert_published_batch_recovery_preserves_catalog_drift("modification")

    def test_publication_preflight_restarts_when_origin_base_moves(self):
        context = self._begin_barrier()
        before = maintenance_state.MaintenanceRepository(self.connection).get(
            str(self.repo)
        )
        git(self.repo, "commit", "--allow-empty", "-m", "External base move")
        git(self.repo, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")
        latest = git(self.repo, "rev-parse", "HEAD")

        with self.assertRaises(loop_manager.MaintenancePlanningRestart) as caught:
            loop_manager.validate_maintenance_publication_base(
                self.connection, str(self.repo), self.repo, "main", context
            )
        restarted = maintenance_state.MaintenanceRepository(self.connection).get(
            str(self.repo)
        )
        self.assertEqual(caught.exception.new_base_sha, latest)
        self.assertEqual(caught.exception.cycle_revision, restarted.revision)
        self.assertEqual(restarted.phase, before.phase)
        self.assertEqual(restarted.revision, before.revision + 1)
        self.assertEqual(restarted.cycle_base_sha, latest)
        self.assertEqual(restarted.refactoring_plan_ids, before.refactoring_plan_ids)
        self.assertEqual(restarted.revalidation_plan_ids, before.revalidation_plan_ids)
        self.assertIsNone(restarted.publication_stage)
        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "단계 또는 revision"
        ):
            loop_manager.validate_maintenance_publication_base(
                self.connection, str(self.repo), self.repo, "main", context
            )
        fresh = loop_manager.prepare_maintenance_planning_context(
            self.connection, str(self.repo), self.repo, latest
        )
        self.assertEqual(fresh.active_sha, latest)
        self.assertEqual(fresh.cycle_revision, restarted.revision)
        loop_manager.validate_maintenance_publication_base(
            self.connection, str(self.repo), self.repo, "main", fresh
        )

    def test_refactoring_publication_promotes_file_evidence_once_after_remote_push(self):
        context = self._begin_barrier()
        self.connection.execute(
            "UPDATE maintenance_cycles SET file_assessment_start_merged_count = 1 "
            "WHERE repo = ?", (str(self.repo),),
        )
        self.connection.execute(
            "INSERT INTO execplan_catalog_revisions "
            "(repo, revision_sha, catalog_fingerprint) VALUES (?, ?, 'catalog')",
            (str(self.repo), self.base_sha),
        )
        self._write_barrier_batch()
        result = self._validate_barrier(context)
        inventory = maintenance_file_inventory.tracked_source_inventory(
            self.repo, "main", self.base_sha,
        )
        reviewed = tuple(maintenance_file_ledger.ReviewedSource(
            source.path, source.content_hash, source.mode, source.revision,
            "Reviewed implementation, dependencies, and related tests.",
        ) for source in inventory.sources)
        maps = tuple((source.path, json.dumps({
            "responsibility": "Review the module responsibility",
            "entry_points": [], "public_symbols": [],
            "direct_references": [], "related_tests": [],
        })) for source in inventory.sources)
        from tests.support.maintenance import root_results_for
        evidence = maintenance_file_publication.AssessmentEvidence(
            inventory, reviewed, maps, 1,
            root_results_for(self.repo, inventory),
        )
        git(self.repo, "add", "docs/plans")
        git(self.repo, "commit", "-m", "Publish refactoring plans")
        plan_commit = git(self.repo, "rev-parse", "HEAD")
        with mock.patch.object(
            maintenance_file_publication, "collect", return_value=evidence,
        ):
            loop_manager.record_maintenance_publication_intent(
                self.connection, str(self.repo), self.repo, "main", result,
                plan_commit,
            )
        self.assertIsNone(maintenance_file_ledger.FileReviewRepository(
            self.connection,
        ).last_success(str(self.repo)))
        git(self.repo, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")
        with mock.patch.object(
            maintenance_state.MaintenanceRepository, "transition",
            side_effect=maintenance_state.MaintenanceStateError("injected"),
        ):
            with self.assertRaisesRegex(maintenance_state.MaintenanceStateError, "injected"):
                loop_manager.publish_maintenance_planning_batch(
                    self.connection, str(self.repo), self.repo, "main", result,
                    plan_commit,
                )
        self.assertIsNone(maintenance_file_ledger.FileReviewRepository(
            self.connection,
        ).last_success(str(self.repo)))
        self.assertIsNone(maintenance_state.MaintenanceRepository(
            self.connection,
        ).get(str(self.repo)).last_success_file_assessment_merged_count)
        published = loop_manager.recover_maintenance_planning_batch(
            self.connection, str(self.repo), self.repo, "main",
        )
        self.assertEqual(published.phase, maintenance_state.MaintenancePhase.REFACTORING)
        self.assertEqual(published.last_success_file_assessment_merged_count, 1)
        self.assertEqual(len(maintenance_file_ledger.FileReviewRepository(
            self.connection,
        ).reviews(str(self.repo))), len(inventory.sources))
        repeated = loop_manager.publish_maintenance_planning_batch(
            self.connection, str(self.repo), self.repo, "main", result,
            plan_commit,
        )
        self.assertEqual(repeated, published)

    def test_legacy_publication_compatibility_rejects_tampered_and_new_incomplete_evidence(self):
        from dataclasses import replace
        plan_commit, intent, _inventory = self._prepare_pushed_file_assessment_batch(legacy=True)
        row = self.connection.execute("SELECT evidence_json FROM file_assessment_publications WHERE repo = ?",
                                      (str(self.repo),)).fetchone()
        payload = json.loads(row[0])
        evidence = maintenance_file_publication.saved_intent(
            self.connection, str(self.repo), intent.revision, self.base_sha, plan_commit)
        self.assertIsNone(evidence.root_reviews)
        with self.assertRaises(maintenance_file_publication.FileAssessmentPublicationError):
            maintenance_file_publication.promote(self.connection, str(self.repo), self.repo, "main", plan_commit,
                                                replace(evidence, merged_count=999))
        with self.assertRaises(maintenance_file_publication.FileAssessmentPublicationError):
            maintenance_file_publication.save_intent(self.connection, str(self.repo), intent.revision,
                                                    self.base_sha, plan_commit, evidence)
        # An explicit empty array belongs to the new contract, never the legacy exemption.
        payload["root_reviews"] = []
        self.connection.execute("UPDATE file_assessment_publications SET evidence_json = ? WHERE repo = ?",
                                (json.dumps(payload), str(self.repo)))
        modern = maintenance_file_publication.saved_intent(self.connection, str(self.repo), intent.revision,
                                                         self.base_sha, plan_commit)
        with self.assertRaises(maintenance_file_ledger.FileReviewLedgerError):
            maintenance_file_publication.promote(self.connection, str(self.repo), self.repo, "main", plan_commit, modern)
        self.assertIsNone(maintenance_file_ledger.FileReviewRepository(self.connection).last_success(str(self.repo)))
