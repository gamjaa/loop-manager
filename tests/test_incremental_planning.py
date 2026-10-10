from tests.support.fixed_samples import use_fixed_guidance
import hashlib
import json
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.storage import maintenance_state, planning_publication_state, planning_input_state
from loop_manager_internal.maintenance.maintenance_planning import PlanRevision, ReadyPlanSnapshot
from loop_manager_internal.planning.planning_catalog import PlanDeletion, MutableReadySnapshot, mutable_snapshot_token


def git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ).stdout.strip()


def plan_text(
    plan_id: str,
    title: str,
    *,
    priority: str = "normal",
) -> str:
    return f"""---
id: {plan_id}
title: {title}
sources:
  - docs/PRODUCT.md
depends_on: []
complexity: low
risk: low
priority: {priority}
---

## Purpose

Implement {title}.

## Acceptance Criteria

- The behavior is covered.

## Implementation

- Keep the boundary explicit.

## Validation

- Run the targeted unit test.

## Progress

- [ ] Complete

## Decision Log

- Initial plan.
"""


class IncrementalPlanningCatalogTest(unittest.TestCase):
    def setUp(self):
        use_fixed_guidance(self)
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "core.autocrlf", "false")
        (self.repo / "docs/plans").mkdir(parents=True)
        (self.repo / "docs/requirements").mkdir(parents=True)
        (self.repo / "docs/PRODUCT.md").write_text("# Product\n", encoding="utf-8")
        (self.repo / "docs/ARCHITECTURE.md").write_text(
            "# Architecture\n", encoding="utf-8"
        )
        (self.repo / "docs/requirements/R.md").write_text(
            "# Requirement\n", encoding="utf-8"
        )
        self.paths = {
            "G-001": "docs/plans/[G-001] First.md",
            "G-002": "docs/plans/[G-002] Second.md",
        }
        for plan_id, relative in self.paths.items():
            title = "First" if plan_id == "G-001" else "Second"
            (self.repo / relative).write_text(
                plan_text(plan_id, title), encoding="utf-8", newline="\n"
            )
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.base)

        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.connection)
        maintenance_state.ensure_schema(self.connection)
        self.connection.execute(
            """
            INSERT INTO projects (
                repo, base_branch, provider, phase, active_sha,
                planning_attempt_fingerprint, planning_workspace_owned
            ) VALUES (?, 'main', 'codex', 'PLANNING', ?, 'attempt', 1)
            """,
            (str(self.repo), self.base),
        )
        for plan_id, relative in self.paths.items():
            title = "First" if plan_id == "G-001" else "Second"
            self.connection.execute(
                """
                INSERT INTO exec_plans (
                    repo, plan_id, path, title, sources, depends_on,
                    priority, content_hash, status, plan_sha,
                    initial_worker_level, effective_worker_level
                ) VALUES (?, ?, ?, ?, '["docs/PRODUCT.md"]', '[]',
                          'normal', ?, 'READY', ?, 1, 1)
                """,
                (
                    str(self.repo),
                    plan_id,
                    relative,
                    title,
                    hashlib.sha256((self.repo / relative).read_bytes()).hexdigest(),
                    self.base,
                ),
            )
        self.connection.commit()

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def test_existing_plan_revision_accepts_old_requirement_source_after_rename(self):
        original = "docs/requirements/[R-001] Original.md"
        renamed = "docs/requirements/[R-001] Renamed.md"
        (self.repo / "docs/requirements/R.md").unlink()
        (self.repo / original).write_text(
            "# [R-001] Original\n\n## Acceptance Criteria\n\n- Works\n",
            encoding="utf-8", newline="\n",
        )
        plan_path = self.repo / self.paths["G-001"]
        initial_plan = plan_text("G-001", "First").replace(
            "  - docs/PRODUCT.md", f"  - {original}",
        )
        plan_path.write_text(initial_plan, encoding="utf-8", newline="\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "link plan to requirement")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.base)
        self.connection.execute("UPDATE projects SET active_sha=?", (self.base,))
        self.connection.commit()
        self.assertTrue(loop_manager.reconcile_execplan_catalog(
            self.connection, self.repo, str(self.repo), "main",
        ).usable)

        (self.repo / original).rename(self.repo / renamed)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "rename requirement")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.base)
        self.connection.execute("UPDATE projects SET active_sha=?", (self.base,))
        self.connection.commit()
        self.assertTrue(loop_manager.reconcile_execplan_catalog(
            self.connection, self.repo, str(self.repo), "main",
        ).usable)

        snapshot = loop_manager.mutable_ready_snapshot(
            self.connection, str(self.repo), self.repo, self.base,
        )
        plan_path.write_text(
            initial_plan.replace("- Keep the boundary explicit.", "- Preserve the source relation."),
            encoding="utf-8", newline="\n",
        )
        result = loop_manager.validate_planner_result(
            self.repo, self.base, loop_manager.changed_files(self.repo),
            connection=self.connection, repo=str(self.repo), mutable_snapshot=snapshot,
        )
        self.assertEqual([revision.plan["id"] for revision in result.revisions], ["G-001"])
        self.assertEqual(result.revisions[0].plan["sources"], [original])

    def _remote_deletion(self, *, dependent=True, started=False, new_plan=False, archive=False, target_text=None, new_plan_text=None):
        from loop_manager_internal.catalog import remote_deletions
        if dependent:
            target = self.repo / self.paths["G-002"]
            target.write_text(plan_text("G-002", "Second").replace("depends_on: []", "depends_on:\n  - G-001"), encoding="utf-8")
            git(self.repo, "add", ".")
            git(self.repo, "commit", "-m", "dependency")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.base)
        result = loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
        self.assertTrue(result.usable)
        if started:
            self.connection.execute("UPDATE exec_plans SET ever_started=1, worker_session_id='saved' WHERE plan_id='G-001'")
            self.connection.commit()
        deleted = self.repo / self.paths["G-001"]
        if archive:
            archive_root = self.repo / "docs/plans/archive"
            archive_root.mkdir()
            deleted.rename(archive_root / deleted.name)
        else:
            deleted.unlink()
        if target_text is not None:
            (self.repo / self.paths["G-002"]).write_text(target_text, encoding="utf-8", newline="\n")
        if new_plan or new_plan_text is not None:
            (self.repo / "docs/plans/[G-007] Independent.md").write_text(
                new_plan_text if new_plan_text is not None else plan_text("G-007", "Independent"), encoding="utf-8", newline="\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "user declaration")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.base)
        self.connection.execute("UPDATE projects SET active_sha=?", (self.base,))
        self.connection.commit()
        result = loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
        self.assertTrue(result.usable)
        return result, remote_deletions

    def _recovery_result(self, *, outcome="replaced", judgment=True):
        snapshot = loop_manager.mutable_ready_snapshot(self.connection, str(self.repo), self.repo, self.base)
        self.assertIn("G-002", snapshot.by_id)
        self.assertEqual(json.loads(snapshot.recovery_goals[0])["id"], "G-001")
        decision = {"deleted_id": "G-001", "outcome": outcome,
                    "replacement_ids": ["G-003"] if outcome == "replaced" else [],
                    "sources": ["docs/PRODUCT.md"], "reason": "Preserve the prerequisite goal in the replacement."}
        body = plan_text("G-002", "Second")
        if outcome == "replaced":
            body = body.replace("depends_on: []", "depends_on:\n  - G-003")
            (self.repo / "docs/plans/[G-003] Replacement.md").write_text(plan_text("G-003", "Replacement"), encoding="utf-8")
        if judgment:
            body += "\nDEPENDENCY_RECOVERY: " + json.dumps(decision) + "\n"
        (self.repo / self.paths["G-002"]).write_text(body, encoding="utf-8")
        return snapshot, lambda: loop_manager.validate_planner_result(
            self.repo, self.base, loop_manager.changed_files(self.repo),
            connection=self.connection, repo=str(self.repo), mutable_snapshot=snapshot)

    def test_remote_deletion_cancels_unstarted_and_registers_independent_gap_once(self):
        result, _ = self._remote_deletion(dependent=False, new_plan=True)
        self.assertEqual(result.withheld_plan_ids, ())
        audit = self.connection.execute("SELECT * FROM execplan_cancellations").fetchone()
        self.assertEqual(audit["plan_id"], "G-001")
        evidence = json.loads(audit["recovery_evidence"])
        self.assertIn("Implement First", evidence["text"])
        self.assertEqual(evidence["row"]["plan_sha"], audit["plan_sha"])
        self.assertIsNone(evidence["row"]["publication_sha"])
        loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM execplan_cancellations").fetchone()[0], 1)
        self.assertEqual([row[0] for row in self.connection.execute("SELECT plan_id FROM exec_plans ORDER BY plan_id")], ["G-002", "G-007"])

    def test_remote_deletion_archive_move_preserves_identity_without_cancellation(self):
        result, _ = self._remote_deletion(archive=True)
        self.assertFalse(result.withheld_plan_ids)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM execplan_cancellations").fetchone()[0], 0)
        row = self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-001'").fetchone()
        self.assertEqual(row["status"], "READY")
        self.assertTrue(row["path"].startswith("docs/plans/archive/"))

    def test_remote_deletion_started_history_and_dependents_are_scoped(self):
        result, _ = self._remote_deletion(started=True, new_plan=True)
        self.assertEqual(set(result.withheld_plan_ids), {"G-001", "G-002"})
        self.assertEqual(self.connection.execute("SELECT worker_session_id FROM exec_plans WHERE plan_id='G-001'").fetchone()[0], "saved")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM execplan_cancellations").fetchone()[0], 0)
        self.assertIsNone(loop_manager.scoped_blocking.project_block(self.connection, str(self.repo)))
        self.assertIsNotNone(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-007'").fetchone())
        repeated = loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
        self.assertEqual(set(repeated.withheld_plan_ids), {"G-001", "G-002"})

    def test_missing_started_plan_reference_recovery_requires_published_judgment(self):
        (self.repo / 'docs/requirements/R.md').unlink()
        body = plan_text('G-002', 'Second').replace('docs/PRODUCT.md', 'docs/requirements/R.md')
        self._remote_deletion(started=True, target_text=body)
        self.connection.execute("UPDATE exec_plans SET status='IMPLEMENTING', ever_started=1, "
                                "worker_session_id='second-session' WHERE plan_id='G-002'")
        self.connection.commit()
        loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), 'main')
        snapshot, validate = self._recovery_result()
        held = self.connection.execute("SELECT evidence FROM execplan_catalog_holds WHERE plan_id='G-002'").fetchone()
        self.assertIn('missing-source', {cause['code'] for cause in json.loads(held[0])['causes']})
        payload = json.loads(snapshot.prompt_payload())
        self.assertEqual(payload['deleted_goals'][0]['kind'], 'missing-plan')
        self.assertTrue(payload['deleted_goals'][0]['plan_revision'])
        self.assertNotIn('G-001', snapshot.by_id)
        result = validate()
        self.assertEqual(loop_manager.scoped_blocking.catalog_held_plan_ids(self.connection, str(self.repo)),
                         {'G-001', 'G-002'})
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'Recover missing prerequisite reference')
        commit = git(self.repo, 'rev-parse', 'HEAD')
        spec = loop_manager._specification_input_fingerprint(self.repo, commit)
        planning = loop_manager._planning_input_fingerprint(self.repo, commit)
        loop_manager.record_general_planning_publication_intent(
            self.connection, str(self.repo), self.repo, result, commit, spec, planning)
        with self.assertRaisesRegex(loop_manager.PlannerValidationError, 'push'):
            loop_manager.publish_general_planning_batch(
                self.connection, str(self.repo), self.repo, 'main', result, commit, spec, planning)
        self.assertEqual(loop_manager.scoped_blocking.catalog_held_plan_ids(self.connection, str(self.repo)),
                         {'G-001', 'G-002'})
        git(self.repo, 'update-ref', 'refs/remotes/origin/main', commit)
        self.assertEqual(loop_manager.recover_general_planning_batch(
            self.connection, str(self.repo), self.repo, 'main'), 'applied')
        self.assertEqual(self.connection.execute("SELECT status FROM exec_plans WHERE plan_id='G-002'").fetchone()[0],
                         'PLAN_BLOCKED')
        self.connection.execute("UPDATE projects SET phase='READY', planning_workspace_owned=0, "
                                "planning_attempt_fingerprint=NULL WHERE repo=?", (str(self.repo),))
        self.connection.commit()
        for _ in range(2):
            reconciled = loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), 'main')
            self.assertEqual(set(reconciled.withheld_plan_ids), {'G-001'})
        repaired = self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-002'").fetchone()
        self.assertEqual((repaired['status'], repaired['worker_session_id']), ('IMPLEMENTING', 'second-session'))
        missing = self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-001'").fetchone()
        self.assertEqual(missing['worker_session_id'], 'saved')
        self.assertFalse((self.repo / self.paths['G-001']).exists())
        target = self.repo / self.paths['G-002']
        target.write_text(target.read_text() + '\nUnreviewed G-001 reference.\n', encoding='utf-8')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'Change approved reference inputs')
        git(self.repo, 'update-ref', 'refs/remotes/origin/main', git(self.repo, 'rev-parse', 'HEAD'))
        reconciled = loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), 'main')
        self.assertEqual(set(reconciled.withheld_plan_ids), {'G-001', 'G-002'})

    def test_missing_plan_reference_review_checks_ownership_and_retains_execution_hold(self):
        self._remote_deletion(started=True)
        self.connection.execute("UPDATE exec_plans SET status='IMPLEMENTING', ever_started=1, "
                                "worker_session_id='second-session' WHERE plan_id='G-002'")
        self.connection.commit()
        loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), 'main')
        snapshot, validate = self._recovery_result(outcome='obsolete')
        row = self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-002'").fetchone()
        self.assertEqual(row['status'], 'PLAN_BLOCKED')
        self.assertEqual(row['worker_session_id'], 'second-session')
        validate()
        self.assertEqual(self.connection.execute("SELECT status FROM exec_plans WHERE plan_id='G-002'").fetchone()[0],
                         'PLAN_BLOCKED')
        self.connection.execute("UPDATE exec_plans SET pr_url='https://example.test/pr/2' WHERE plan_id='G-002'")
        with self.assertRaisesRegex(loop_manager.PlannerValidationError, '실행 상태'):
            loop_manager._assert_mutable_ready_snapshot_current(self.connection, snapshot)

    def test_missing_plan_reference_cannot_be_removed_without_goal_judgment(self):
        self._remote_deletion(started=True)
        _, validate = self._recovery_result(outcome='obsolete', judgment=False)
        with self.assertRaisesRegex(loop_manager.PlannerValidationError, '목표의 판단'):
            validate()

    def test_remote_deletion_started_implementation_reference_is_held(self):
        body = plan_text("G-002", "Second").replace(
            "- Keep the boundary explicit.", "- Build on the G-001 implementation.")
        (self.repo / self.paths["G-002"]).write_text(body, encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "adopt implementation reference")
        self._assert_started_deletion_reference(target_text=plan_text("G-002", "Second"))

    def test_remote_deletion_started_simultaneous_validation_reference_is_held(self):
        body = plan_text("G-002", "Second").replace(
            "- Run the targeted unit test.", f"- Validate against `{self.paths['G-001']}`.")
        self._assert_started_deletion_reference(target_text=body)

    def _assert_started_deletion_reference(self, *, target_text):
        result, _ = self._remote_deletion(dependent=False, started=True, new_plan=True, target_text=target_text)
        self.assertEqual(set(result.withheld_plan_ids), {"G-001", "G-002"})
        root = dict(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-001'").fetchone())
        self.assertEqual(root["ever_started"], 1)
        self.assertEqual(root["worker_session_id"], "saved")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM execplan_cancellations").fetchone()[0], 0)
        snapshot = loop_manager.mutable_ready_snapshot(self.connection, str(self.repo), self.repo, self.base)
        second = self.connection.execute("SELECT pr_url FROM exec_plans WHERE plan_id='G-002'").fetchone()
        self.assertEqual(set(snapshot.by_id), {"G-007"} if second[0] else {"G-002", "G-007"})
        self.assertEqual(json.loads(snapshot.recovery_goals[0])["kind"], "missing-plan")
        self.assertNotIn("G-001", snapshot.by_id)
        self.assertEqual(loop_manager.select_ready_execplan(self.connection, str(self.repo))["plan_id"], "G-007")
        for _ in range(2):
            result = loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
            self.assertEqual(set(result.withheld_plan_ids), {"G-001", "G-002"})
            current = self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-001'").fetchone()
            for field in ("plan_id", "path", "sources", "depends_on", "content_hash", "plan_sha",
                          "ever_started", "worker_session_id", "status", "branch", "worktree", "pr_url", "workspace_owned"):
                self.assertEqual(current[field], root[field])

    def test_remote_deletion_started_reference_preserves_owned_pr(self):
        body = plan_text("G-002", "Second") + "\nUse G-001 as the validation foundation.\n"
        (self.repo / self.paths["G-002"]).write_text(body, encoding="utf-8", newline="\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "owned validation reference")
        self.connection.execute("""UPDATE exec_plans SET status='PR_OPEN', ever_started=1,
            branch='owned-second', worktree='/owned-second', workspace_owned=1,
            worker_session_id='second-session', pr_url='https://example.test/pr/2' WHERE plan_id='G-002'""")
        self.connection.commit()
        self._assert_started_deletion_reference(target_text=plan_text("G-002", "Second"))
        row = self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-002'").fetchone()
        self.assertEqual((row["status"], row["branch"], row["worktree"], row["workspace_owned"],
                          row["worker_session_id"], row["pr_url"]),
                         ("PR_OPEN", "owned-second", "/owned-second", 1, "second-session", "https://example.test/pr/2"))
        self.assertEqual(row["content_hash"], hashlib.sha256(body.encode("utf-8")).hexdigest())

    def test_remote_deletion_started_new_reference_hold_survives_removal_until_restored(self):
        body = plan_text("G-007", "Independent") + "\nValidate against G-001.\n"
        (self.repo / "docs/plans/[G-008] Downstream.md").write_text(
            plan_text("G-008", "Downstream").replace("depends_on: []", "depends_on:\n  - G-007"), encoding="utf-8")
        result, _ = self._remote_deletion(dependent=False, started=True, new_plan_text=body)
        self.assertEqual(set(result.withheld_plan_ids), {"G-001", "G-007", "G-008"})
        (self.repo / "docs/plans/[G-007] Independent.md").write_text(plan_text("G-007", "Independent"), encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "remove reference without recovering goal")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.base)
        for _ in range(2):
            result = loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
            self.assertEqual(set(result.withheld_plan_ids), {"G-001", "G-007", "G-008"})
            self.assertIsNotNone(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-007'").fetchone())
            snapshot = loop_manager.mutable_ready_snapshot(self.connection, str(self.repo), self.repo, self.base)
            self.assertIn('G-007', snapshot.by_id)
            self.assertIsNone(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-008'").fetchone())
            self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM execplan_cancellations").fetchone()[0], 0)
            self.assertEqual(loop_manager.select_ready_execplan(self.connection, str(self.repo))["plan_id"], "G-002")
        root = self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-001'").fetchone()
        (self.repo / self.paths["G-001"]).write_bytes(loop_manager.git_bytes(
            self.repo, "show", f"{root['current_revision_sha'] or root['plan_sha']}:{root['path']}"))
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "restore prerequisite declaration")
        git(self.repo, "update-ref", "refs/remotes/origin/main", git(self.repo, "rev-parse", "HEAD"))
        result = loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
        self.assertEqual(result.withheld_plan_ids, ())
        self.assertEqual(self.connection.execute("SELECT worker_session_id FROM exec_plans WHERE plan_id='G-001'").fetchone()[0], "saved")
        self.assertIsNotNone(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-007'").fetchone())
        self.assertIsNotNone(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-008'").fetchone())

    def test_remote_deletion_started_hold_allows_independent_planner_result(self):
        self._remote_deletion(started=True, new_plan=True)
        snapshot = loop_manager.mutable_ready_snapshot(self.connection, str(self.repo), self.repo, self.base)
        self.assertEqual(set(snapshot.by_id), {"G-002", "G-007"})
        (self.repo / "docs/plans/[G-008] Another.md").write_text(plan_text("G-008", "Another"), encoding="utf-8")
        result = loop_manager.validate_planner_result(self.repo, self.base, loop_manager.changed_files(self.repo),
            connection=self.connection, repo=str(self.repo), mutable_snapshot=snapshot)
        self.assertEqual([plan["id"] for _path, plan in result.plans], ["G-008"])

    def test_remote_deletion_dependency_removal_requires_goal_judgment(self):
        self._remote_deletion()
        _, validate = self._recovery_result(outcome="obsolete", judgment=False)
        with self.assertRaisesRegex(loop_manager.PlannerValidationError, "목표의 판단"):
            validate()
        self.assertEqual(loop_manager.scoped_blocking.catalog_held_plan_ids(self.connection, str(self.repo)), {"G-002"})

    def test_remote_deletion_replacement_without_dependency_gate_is_rejected(self):
        self._remote_deletion()
        _, validate = self._recovery_result()
        target = self.repo / self.paths["G-002"]
        target.write_text(target.read_text().replace("depends_on:\n  - G-003", "depends_on: []"), encoding="utf-8")
        with self.assertRaisesRegex(loop_manager.PlannerValidationError, "대체 dependency"):
            validate()

    def test_remote_deletion_replacement_cycle_is_rejected_before_publication(self):
        self._remote_deletion()
        _, validate = self._recovery_result()
        target = self.repo / "docs/plans/[G-003] Replacement.md"
        target.write_text(plan_text("G-003", "Replacement").replace("depends_on: []", "depends_on:\n  - G-002"), encoding="utf-8")
        with self.assertRaisesRegex(loop_manager.PlannerValidationError, "새 오류|순환"):
            validate()
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM general_planning_publications").fetchone()[0], 0)

    def test_remote_deletion_goal_recovery_publication_is_atomic_and_idempotent(self):
        self._remote_deletion()
        snapshot, validate = self._recovery_result()
        result = validate()
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "replacement batch")
        commit = git(self.repo, "rev-parse", "HEAD")
        spec = loop_manager._specification_input_fingerprint(self.repo, commit)
        planning = loop_manager._planning_input_fingerprint(self.repo, commit)
        loop_manager.record_general_planning_publication_intent(self.connection, str(self.repo), self.repo, result, commit, spec, planning)
        with self.assertRaisesRegex(loop_manager.PlannerValidationError, "push"):
            loop_manager.publish_general_planning_batch(self.connection, str(self.repo), self.repo, "main", result, commit, spec, planning)
        self.assertEqual(loop_manager.scoped_blocking.catalog_held_plan_ids(self.connection, str(self.repo)), {"G-002"})
        git(self.repo, "update-ref", "refs/remotes/origin/main", commit)
        self.assertEqual(loop_manager.recover_general_planning_batch(
            self.connection, str(self.repo), self.repo, "main"), "applied")
        for _ in range(2):
            loop_manager.publish_general_planning_batch(self.connection, str(self.repo), self.repo, "main", result, commit, spec, planning)
        self.assertFalse(loop_manager.scoped_blocking.catalog_held_plan_ids(self.connection, str(self.repo)))
        row = self.connection.execute("SELECT * FROM execplan_cancellations").fetchone()
        self.assertEqual(json.loads(row["recovery_evidence"])["resolved"], {"G-002": commit})
        self.assertEqual(self.connection.execute("SELECT depends_on FROM exec_plans WHERE plan_id='G-002'").fetchone()[0], '["G-003"]')
        self.assertFalse(loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main").withheld_plan_ids)
        self.assertEqual(snapshot.recovery_goals, loop_manager._planning_result_from_manifest(loop_manager._planning_result_manifest(result)).mutable_snapshot.recovery_goals)

    def test_remote_deletion_snapshot_race_preserves_audit_and_held_graph(self):
        self._remote_deletion()
        snapshot, validate = self._recovery_result()
        validate()
        self.connection.execute("UPDATE execplan_cancellations SET recovery_evidence='{}'")
        with self.assertRaisesRegex(loop_manager.PlannerValidationError, "삭제 목표 감사"):
            loop_manager._assert_mutable_ready_snapshot_current(self.connection, snapshot)

    def test_remote_deletion_obsolete_goal_accepts_explicit_source_judgment(self):
        self._remote_deletion()
        _, validate = self._recovery_result(outcome="obsolete")
        self.assertIsNotNone(validate())

    def test_remote_deletion_obsolete_goal_can_use_superseding_product_source(self):
        original = self.repo / self.paths["G-001"]
        original.write_text(plan_text("G-001", "First").replace("  - docs/PRODUCT.md", "  - docs/requirements/R.md"), encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "original requirement goal")
        self._remote_deletion()
        (self.repo / "docs/requirements/R.md").unlink()
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "user removes requirement")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.base)
        loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
        _, validate = self._recovery_result(outcome="obsolete")
        self.assertIsNotNone(validate())

    def test_remote_deletion_uncertain_goal_allows_independent_batch(self):
        self._remote_deletion()
        snapshot = loop_manager.mutable_ready_snapshot(self.connection, str(self.repo), self.repo, self.base)
        (self.repo / "docs/plans/[G-003] Independent.md").write_text(plan_text("G-003", "Independent"), encoding="utf-8")
        result = loop_manager.validate_planner_result(self.repo, self.base, loop_manager.changed_files(self.repo),
            connection=self.connection, repo=str(self.repo), mutable_snapshot=snapshot)
        self.assertEqual([plan["id"] for _path, plan in result.plans], ["G-003"])
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "independent batch")
        commit = git(self.repo, "rev-parse", "HEAD")
        spec = loop_manager._specification_input_fingerprint(self.repo, commit)
        planning = loop_manager._planning_input_fingerprint(self.repo, commit)
        loop_manager.record_general_planning_publication_intent(self.connection, str(self.repo), self.repo, result, commit, spec, planning)
        git(self.repo, "update-ref", "refs/remotes/origin/main", commit)
        loop_manager.publish_general_planning_batch(self.connection, str(self.repo), self.repo, "main", result, commit, spec, planning)
        self.assertEqual(loop_manager.scoped_blocking.catalog_held_plan_ids(self.connection, str(self.repo)), {"G-002"})
        audit = self.connection.execute("SELECT recovery_evidence FROM execplan_cancellations").fetchone()[0]
        self.assertEqual(json.loads(audit)["resolved"], {})

    def test_remote_deletion_uncertain_no_change_preserves_the_scoped_hold(self):
        self._remote_deletion()
        snapshot = loop_manager.mutable_ready_snapshot(self.connection, str(self.repo), self.repo, self.base)
        result = loop_manager.validate_planner_result(self.repo, self.base, [], connection=self.connection,
            repo=str(self.repo), mutable_snapshot=snapshot)
        self.assertIsNone(result)
        self.assertEqual(loop_manager.scoped_blocking.catalog_held_plan_ids(self.connection, str(self.repo)), {"G-002"})

    def test_remote_deletion_user_dependency_removal_stays_held_and_mutable(self):
        self._remote_deletion()
        target = self.repo / self.paths["G-002"]
        target.write_text(plan_text("G-002", "Second"), encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "remove dependency without judgment")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.base)
        result = loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
        self.assertEqual(set(result.withheld_plan_ids), {"G-002"})
        snapshot = loop_manager.mutable_ready_snapshot(self.connection, str(self.repo), self.repo, self.base)
        self.assertEqual(set(snapshot.by_id), {"G-002"})
        self.assertTrue(snapshot.recovery_goals)

    def test_remote_deletion_implementation_reference_is_also_held(self):
        target = self.repo / self.paths["G-002"]
        target.write_text(plan_text("G-002", "Second") + "\nUse G-001 as the validation foundation.\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "implementation reference")
        result, _ = self._remote_deletion(dependent=False, target_text=plan_text("G-002", "Second"))
        self.assertEqual(set(result.withheld_plan_ids), {"G-002"})

    def test_remote_deletion_simultaneous_implementation_reference_is_held(self):
        body = plan_text("G-002", "Second").replace(
            "- Keep the boundary explicit.", "- Build on the G-001 implementation.")
        self._assert_simultaneous_deletion_reference(body)

    def test_remote_deletion_simultaneous_validation_path_reference_is_held(self):
        body = plan_text("G-002", "Second").replace(
            "- Run the targeted unit test.", f"- Validate against `{self.paths['G-001']}`.")
        self._assert_simultaneous_deletion_reference(body)

    def test_remote_deletion_simultaneous_dependency_reference_is_held(self):
        body = plan_text("G-002", "Second").replace("depends_on: []", "depends_on:\n  - G-001")
        self._assert_simultaneous_deletion_reference(body)

    def _assert_simultaneous_deletion_reference(self, body):
        result, remote_deletions = self._remote_deletion(dependent=False, new_plan=True, target_text=body)
        self.assertEqual(set(result.withheld_plan_ids), {"G-002"})
        audit = self.connection.execute("SELECT recovery_evidence FROM execplan_cancellations").fetchone()
        self.assertEqual(json.loads(audit[0])["affected"], ["G-002"])
        snapshot = loop_manager.mutable_ready_snapshot(self.connection, str(self.repo), self.repo, self.base)
        self.assertEqual(snapshot.by_id["G-002"].text, body)
        self.assertEqual(json.loads(remote_deletions.recovery_goals(self.connection, str(self.repo))[0])["affected_ids"], ["G-002"])
        self.assertEqual(loop_manager.select_ready_execplan(self.connection, str(self.repo))["plan_id"], "G-007")
        again = loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
        self.assertEqual(set(again.withheld_plan_ids), {"G-002"})
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM execplan_cancellations").fetchone()[0], 1)

    def test_remote_deletion_new_implementation_reference_is_held(self):
        body = plan_text("G-007", "Independent").replace(
            "- Keep the boundary explicit.", "- Build on the G-001 implementation.")
        self._assert_new_plan_deletion_reference(body)

    def test_remote_deletion_new_validation_reference_is_held(self):
        body = plan_text("G-007", "Independent").replace(
            "- Run the targeted unit test.", f"- Validate against `{self.paths['G-001']}`.")
        self._assert_new_plan_deletion_reference(body)

    def test_remote_deletion_new_dependency_reference_is_held(self):
        body = plan_text("G-007", "Independent").replace("depends_on: []", "depends_on:\n  - G-001")
        self._assert_new_plan_deletion_reference(body)

    def _assert_new_plan_deletion_reference(self, body):
        result, remote_deletions = self._remote_deletion(dependent=False, new_plan_text=body)
        self.assertEqual(set(result.withheld_plan_ids), {"G-007"})
        row = self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-007'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "READY")
        audit = self.connection.execute("SELECT recovery_evidence FROM execplan_cancellations").fetchone()[0]
        self.assertEqual(json.loads(audit)["affected"], ["G-007"])
        snapshot = loop_manager.mutable_ready_snapshot(self.connection, str(self.repo), self.repo, self.base)
        self.assertEqual(snapshot.by_id["G-007"].text, body)
        self.assertEqual(json.loads(snapshot.recovery_goals[0])["affected_ids"], ["G-007"])
        self.assertEqual(loop_manager.select_ready_execplan(self.connection, str(self.repo))["plan_id"], "G-002")
        for _ in range(2):
            again = loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
            self.assertEqual(set(again.withheld_plan_ids), {"G-007"})
            self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM execplan_cancellations").fetchone()[0], 1)
            self.assertEqual(remote_deletions.recovery_goals(self.connection, str(self.repo)), snapshot.recovery_goals)
        path = self.repo / "docs/plans/[G-007] Independent.md"
        path.write_text(plan_text("G-007", "Independent"), encoding="utf-8")
        with self.assertRaisesRegex(loop_manager.PlannerValidationError, "판단"):
            loop_manager.validate_planner_result(self.repo, self.base, loop_manager.changed_files(self.repo),
                connection=self.connection, repo=str(self.repo), mutable_snapshot=snapshot)

    def test_remote_deletion_new_missing_priority_preserves_pending_goal(self):
        body = plan_text("G-007", "Independent").replace("priority: normal\n", "") + "\nUse G-001.\n"
        self._assert_pending_deletion_goal_survives_correction(body)

    def test_remote_deletion_new_missing_source_preserves_pending_goal(self):
        (self.repo / "docs/requirements/R.md").unlink()
        body = plan_text("G-007", "Independent").replace("docs/PRODUCT.md", "docs/requirements/R.md") + "\nUse G-001.\n"
        self._assert_pending_deletion_goal_survives_correction(body)

    def test_remote_deletion_new_invalid_dependency_preserves_pending_goal(self):
        body = plan_text("G-007", "Independent").replace("depends_on: []", "depends_on:\n  - G-008") + "\nUse G-001.\n"
        (self.repo / "docs/plans/[G-008] Invalid.md").write_text(
            plan_text("G-008", "Invalid").replace("priority: normal\n", ""), encoding="utf-8")
        self._assert_pending_deletion_goal_survives_correction(body, extra_held={"G-008"})

    def test_remote_deletion_legacy_registration_preserves_invalid_dependency_hold(self):
        body = plan_text("G-007", "Independent").replace("depends_on: []", "depends_on:\n  - G-008") + "\nUse G-001.\n"
        (self.repo / "docs/plans/[G-007] Independent.md").write_text(body, encoding="utf-8")
        (self.repo / "docs/plans/[G-008] Invalid.md").write_text(
            plan_text("G-008", "Invalid").replace("priority: normal\n", ""), encoding="utf-8")
        (self.repo / self.paths["G-001"]).unlink()
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "delete and add before first refresh")
        git(self.repo, "update-ref", "refs/remotes/origin/main", git(self.repo, "rev-parse", "HEAD"))
        for _ in range(2):
            result = loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
            self.assertTrue(result.usable)
            self.assertEqual(set(result.withheld_plan_ids), {"G-007", "G-008"})
            self.assertIsNone(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-007'").fetchone())
            self.assertIsNone(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-008'").fetchone())
            self.assertEqual(loop_manager.select_ready_execplan(self.connection, str(self.repo))["plan_id"], "G-002")

    def _assert_pending_deletion_goal_survives_correction(self, body, *, extra_held=frozenset()):
        result, remote_deletions = self._remote_deletion(dependent=False, new_plan_text=body)
        expected_held = {"G-007"} | extra_held
        self.assertEqual(set(result.withheld_plan_ids), expected_held)
        audit = self.connection.execute("SELECT recovery_evidence FROM execplan_cancellations").fetchone()[0]
        self.assertEqual(json.loads(audit)["affected"], ["G-007"])
        for _ in range(2):
            again = loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
            self.assertEqual(set(again.withheld_plan_ids), expected_held)
            self.assertIsNone(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-007'").fetchone())
            self.assertEqual(json.loads(remote_deletions.recovery_goals(self.connection, str(self.repo))[0])["affected_ids"], ["G-007"])
            self.assertEqual(loop_manager.select_ready_execplan(self.connection, str(self.repo))["plan_id"], "G-002")
        path = self.repo / "docs/plans/[G-007] Independent.md"
        path.write_text(plan_text("G-007", "Independent"), encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "correct declaration without goal judgment")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.base)
        result = loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
        self.assertEqual(set(result.withheld_plan_ids), expected_held)
        self.assertIsNotNone(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-007'").fetchone())
        snapshot = loop_manager.mutable_ready_snapshot(self.connection, str(self.repo), self.repo, self.base)
        self.assertIn("G-007", snapshot.by_id)
        decision = {"deleted_id": "G-001", "outcome": "obsolete", "replacement_ids": [],
                    "sources": ["docs/PRODUCT.md"], "reason": "The current product no longer requires this foundation."}
        path.write_text(plan_text("G-007", "Independent") + "\nDEPENDENCY_RECOVERY: " + json.dumps(decision) + "\n", encoding="utf-8")
        result = loop_manager.validate_planner_result(self.repo, self.base, loop_manager.changed_files(self.repo),
            connection=self.connection, repo=str(self.repo), mutable_snapshot=snapshot)
        self.assertIsNotNone(result)

    def test_remote_deletion_unknown_adopted_hash_is_preserved(self):
        loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
        self.connection.execute("UPDATE exec_plans SET content_hash='unverified' WHERE plan_id='G-001'")
        self.connection.commit()
        (self.repo / self.paths["G-001"]).unlink()
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "delete unverified")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.base)
        result = loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
        self.assertEqual(set(result.withheld_plan_ids), {"G-001"})
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM execplan_cancellations").fetchone()[0], 0)

    def test_remote_deletion_unreconciled_project_owner_is_preserved(self):
        loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
        self.connection.execute("UPDATE projects SET phase='WORKING', plan_path=?, branch='saved-owner', planning_workspace_owned=0",
                                (self.paths["G-001"],))
        self.connection.commit()
        (self.repo / self.paths["G-001"]).unlink()
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "delete projected owner")
        revision = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", revision)
        result = loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main", record_invalid=False)
        self.assertEqual(result.diagnostic.code, "shared-ownership")
        self.assertEqual(self.connection.execute("SELECT branch FROM projects").fetchone()[0], "saved-owner")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM execplan_cancellations").fetchone()[0], 0)

    def test_remote_deletion_catalog_interruption_and_remote_race_roll_back(self):
        from loop_manager_internal.catalog import remote_deletions
        from loop_manager_internal.storage import execplan_catalog_state as state
        from tests.support.catalog import FakeDefinitionPort
        # Capture real adopted evidence, then simulate a new observed deletion.
        loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
        (self.repo / self.paths["G-001"]).unlink()
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "delete")
        revision = git(self.repo, "rev-parse", "HEAD")
        pinned = state.PinnedCatalog(revision, loop_manager.revision_execplan_snapshot(self.repo, revision, isolate_errors=True), frozenset({"docs/PRODUCT.md"}))
        from dataclasses import replace
        pinned = replace(pinned, deletions=remote_deletions.prove_deletions(self.connection, str(self.repo), self.repo, pinned, git=loop_manager.git, git_bytes=loop_manager.git_bytes))
        self.assertEqual(len(pinned.deletions), 1)
        before = state.DefinitionStateRepository(self.connection).capture(str(self.repo))
        def interrupt(stage):
            if stage.startswith("after-cancellation:"):
                raise RuntimeError("interrupted cancellation")
        repository = state.DefinitionStateRepository(self.connection, fault_hook=interrupt)
        with self.assertRaisesRegex(RuntimeError, "interrupted cancellation"):
            state.CatalogReconciliationService(repository, FakeDefinitionPort(pinned)).reconcile(str(self.repo))
        self.assertEqual(repository.capture(str(self.repo)), before)
        repository._fault_hook = None
        stale = state.CatalogReconciliationService(repository, FakeDefinitionPort(pinned, [revision, revision, 'f'*40])).reconcile(str(self.repo))
        self.assertEqual(stale.kind, state.ReconciliationKind.STALE)
        self.assertEqual(repository.capture(str(self.repo)), before)
        self.assertTrue(state.CatalogReconciliationService(repository, FakeDefinitionPort(pinned)).reconcile(str(self.repo)).usable)

    def prepare_batch(self, *, record_intent: bool = True):
        snapshot = loop_manager.mutable_ready_snapshot(
            self.connection, str(self.repo), self.repo, self.base
        )
        first = self.repo / self.paths["G-001"]
        renamed = self.repo / "docs/plans/[G-001] Urgent First.md"
        first.unlink()
        renamed.write_text(
            plan_text("G-001", "Urgent First", priority="critical"),
            encoding="utf-8",
        )
        (self.repo / self.paths["G-002"]).unlink()
        third = self.repo / "docs/plans/[G-003] Third.md"
        third.write_text(plan_text("G-003", "Third"), encoding="utf-8")
        result = loop_manager.validate_planner_result(
            self.repo,
            self.base,
            loop_manager.changed_files(self.repo),
            connection=self.connection,
            repo=str(self.repo),
            mutable_snapshot=snapshot,
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual([plan[1]["id"] for plan in result.plans], ["G-003"])
        self.assertEqual(
            [revision.snapshot.plan_id for revision in result.revisions],
            ["G-001"],
        )
        self.assertEqual(
            [deletion.snapshot.plan_id for deletion in result.deletions],
            ["G-002"],
        )
        git(self.repo, "add", "--", *result.publication_paths)
        git(self.repo, "commit", "-m", "planning batch")
        commit = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", commit)
        specification = loop_manager._specification_input_fingerprint(
            self.repo, commit
        )
        planning = loop_manager._planning_input_fingerprint(self.repo, commit)
        if record_intent:
            loop_manager.record_general_planning_publication_intent(
                self.connection,
                str(self.repo),
                self.repo,
                result,
                commit,
                specification,
                planning,
            )
        return result, commit, specification, planning

    def test_mixed_batch_updates_catalog_and_cancellation_audit_atomically(self):
        result, commit, specification, planning = self.prepare_batch()

        loop_manager.publish_general_planning_batch(
            self.connection,
            str(self.repo),
            self.repo,
            "main",
            result,
            commit,
            specification,
            planning,
        )

        rows = self.connection.execute(
            "SELECT plan_id, path, status, ever_started FROM exec_plans "
            "WHERE repo = ? ORDER BY plan_id",
            (str(self.repo),),
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                (
                    "G-001",
                    "docs/plans/[G-001] Urgent First.md",
                    "READY",
                    0,
                ),
                ("G-003", "docs/plans/[G-003] Third.md", "READY", 0),
            ],
        )
        cancelled = self.connection.execute(
            "SELECT plan_id, path, deletion_commit "
            "FROM execplan_cancellations WHERE repo = ?",
            (str(self.repo),),
        ).fetchone()
        self.assertEqual(
            tuple(cancelled),
            ("G-002", self.paths["G-002"], commit),
        )
        project = self.connection.execute(
            "SELECT phase, processed_sha, specification_fingerprint, "
            "planning_fingerprint FROM projects WHERE repo = ?",
            (str(self.repo),),
        ).fetchone()
        self.assertEqual(
            tuple(project), (None, commit, specification, planning)
        )
        self.assertTrue(planning_input_state.matches(
            self.connection, str(self.repo), loop_manager.plan_declarations(self.repo, commit),
        ))
        self.assertIsNone(
            loop_manager.general_planning_publication(
                self.connection, str(self.repo)
            )
        )

        loop_manager.publish_general_planning_batch(
            self.connection,
            str(self.repo),
            self.repo,
            "main",
            result,
            commit,
            specification,
            planning,
        )

    def test_batch_failure_rolls_back_rows_audit_and_success_markers(self):
        result, commit, specification, planning = self.prepare_batch()
        self.connection.execute(
            """
            CREATE TRIGGER reject_cancellation
            BEFORE INSERT ON execplan_cancellations
            BEGIN
                SELECT RAISE(ABORT, 'injected cancellation failure');
            END
            """
        )

        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "injected cancellation failure"
        ):
            loop_manager.publish_general_planning_batch(
                self.connection,
                str(self.repo),
                self.repo,
                "main",
                result,
                commit,
                specification,
                planning,
            )

        rows = self.connection.execute(
            "SELECT plan_id, path FROM exec_plans WHERE repo = ? ORDER BY plan_id",
            (str(self.repo),),
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [("G-001", self.paths["G-001"]), ("G-002", self.paths["G-002"])],
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM execplan_cancellations WHERE repo = ?",
                (str(self.repo),),
            ).fetchone()[0],
            0,
        )
        project = self.connection.execute(
            "SELECT phase, processed_sha FROM projects WHERE repo = ?",
            (str(self.repo),),
        ).fetchone()
        self.assertEqual(tuple(project), ("PLANNING", None))
        self.assertFalse(planning_input_state.has_baseline(
            self.connection, str(self.repo),
        ))
        self.assertIsNotNone(
            loop_manager.general_planning_publication(
                self.connection, str(self.repo)
            )
        )

    def test_activation_race_rejects_entire_batch_before_intent(self):
        result, commit, specification, planning = self.prepare_batch(
            record_intent=False
        )
        self.connection.execute(
            "UPDATE exec_plans SET ever_started = 1 "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        )
        self.connection.commit()

        with self.assertRaisesRegex(
            loop_manager.PlannerValidationError,
            "snapshot 이후 실행 상태가 변경",
        ):
            loop_manager.record_general_planning_publication_intent(
                self.connection,
                str(self.repo),
                self.repo,
                result,
                commit,
                specification,
                planning,
            )

        self.assertIsNone(
            loop_manager.general_planning_publication(
                self.connection, str(self.repo)
            )
        )

    def test_mutable_snapshot_includes_pr_free_ready_with_execution_evidence(self):
        self.connection.execute(
            "UPDATE exec_plans SET worker_session_id = 'worker-session' "
            "WHERE repo = ? AND plan_id = 'G-002'",
            (str(self.repo),),
        )
        self.connection.commit()

        snapshot = loop_manager.mutable_ready_snapshot(
            self.connection, str(self.repo), self.repo, self.base
        )

        self.assertEqual(
            [plan.plan_id for plan in snapshot.plans], ["G-001", "G-002"]
        )

    def test_started_revision_preserves_execution_audit_and_worker_level(self):
        from loop_manager_internal.storage.planning_publication_state import apply_mutable_catalog_changes
        self.connection.execute("UPDATE exec_plans SET ever_started=1, worker_session_id='saved', "
            "retry_count=2, effective_worker_level=8, review_failure_history='{\"issue\":2}' "
            "WHERE plan_id='G-002'")
        self.connection.commit()
        snapshot = loop_manager.mutable_ready_snapshot(self.connection, str(self.repo), self.repo, self.base)
        path = self.repo / self.paths['G-002']
        path.write_text(path.read_text(encoding='utf-8') + '\n- 변경된 완료 조건을 확인한다.\n', encoding='utf-8')
        result = loop_manager.validate_planner_result(self.repo, self.base, [self.paths['G-002']],
            connection=self.connection, repo=str(self.repo), mutable_snapshot=snapshot)
        with self.connection:
            apply_mutable_catalog_changes(self.connection, str(self.repo), deletions=(), revisions=result.revisions,
                snapshot_token=snapshot.token, plan_commit=self.base,
                content_hash=lambda relative: hashlib.sha256((self.repo / relative).read_bytes()).hexdigest())
        row = self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-002'").fetchone()
        self.assertEqual((row['status'], row['ever_started'], row['worker_session_id'], row['retry_count'],
                          row['effective_worker_level'], row['review_failure_history']),
                         ('READY', 1, 'saved', 2, 8, '{"issue":2}'))

    def test_actual_pr_appearance_prevents_started_snapshot_publication(self):
        self.connection.execute("UPDATE exec_plans SET ever_started=1, branch='work' WHERE plan_id='G-002'")
        self.connection.commit()
        with mock.patch.object(loop_manager, 'pr_for_branch', return_value=None):
            snapshot = loop_manager.mutable_ready_snapshot(self.connection, str(self.repo), self.repo, self.base)
        with mock.patch.object(loop_manager, 'pr_for_branch', return_value={'url': 'https://example/pr/2'}):
            with self.assertRaisesRegex(loop_manager.PlannerValidationError, 'PR이 등록'):
                loop_manager._assert_mutable_ready_snapshot_current(self.connection, snapshot)

    def test_active_document_proposal_publication_and_restart_use_one_model_result(self):
        from types import SimpleNamespace
        from loop_manager_internal.execution import stage_documents as documents
        from loop_manager_internal.execution.execution_session import ExecutionIdentity
        from loop_manager_internal.execution.execution_runtime import ExecutionPhase
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        from tests.support.revision import definition
        with tempfile.TemporaryDirectory() as directory:
            remote = Path(directory) / 'remote.git'
            git(Path(directory), 'init', '--bare', str(remote))
            git(self.repo, 'remote', 'add', 'origin', str(remote))
            git(self.repo, 'push', 'origin', 'main')
            git(self.repo, 'switch', '-c', 'work')
            self.connection.execute("UPDATE exec_plans SET status='IMPLEMENTING', ever_started=1, branch='work', "
                "active_sha=?, worker_session_id='saved-session' WHERE plan_id='G-002'", (self.base,))
            self.connection.commit()
            row = lambda: self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-002'").fetchone()
            state = SimpleNamespace(connection=self.connection, row=row)
            context = SimpleNamespace(plan=SimpleNamespace(plan_id='G-002'), phase=ExecutionPhase.IMPLEMENTING,
                worktree=self.repo, branch='work', resolving_rebase=False)
            identity = ExecutionIdentity(repo=str(self.repo), repo_path=self.repo, state_dir=Path(directory) / 'state',
                base='main', provider='codex')
            current = replace(definition(), revision_sha=self.base, path=self.paths['G-002'],
                              sources=('docs/PRODUCT.md',))
            proposal = (self.repo / self.paths['G-002']).read_text(encoding='utf-8') + '\n- 최신 조건 반영.\n'
            raw = 'DOCUMENT_RESULT: ' + json.dumps(dict(relevant=True, implementation='satisfied',
                                                        plan_text=proposal, evidence='현재 구현과 최신 명세 대조'))
            agent = mock.Mock(return_value=loop_manager.AgentResult(0, raw))
            worker_scope = loop_manager.begin_agent_scope(self.connection, str(self.repo), self.repo, 'worker')
            self.connection.execute("UPDATE exec_plans SET agent_role='worker', agent_snapshot=? WHERE plan_id='G-002'",
                                    (worker_scope.serialize(),))
            self.connection.commit()
            effects = documents.StageDocumentEffects(git=loop_manager.git,
                capture_scope=loop_manager.capture_git_snapshot,
                validate_scope=mock.Mock(), run_agent=agent,
                prepare_snapshot=loop_manager.mutable_ready_snapshot,
                assert_snapshot=loop_manager._assert_mutable_ready_snapshot_current,
                validate_result=loop_manager.validate_planner_result,
                validate_worktree=loop_manager.validate_managed_worktree,
                pr_for_branch=mock.Mock(return_value=None), log=mock.Mock(),
                run_with_retries=lambda call, session_id: call(session_id, False))
            checkpoints = ExecutionCheckpointRepository(self.connection)
            with mock.patch.object(loop_manager, 'pr_for_branch', return_value=None):
                result = documents.refresh(effects, state, checkpoints, identity, context,
                                           replace(current, source_fingerprint='old'), current, ['docs/PRODUCT.md'])
            self.assertEqual(result['stage'], 'rebase')
            self.assertEqual(git(self.repo, 'show', f"origin/main:{self.paths['G-002']}") + '\n', proposal)
            self.assertEqual(git(self.repo, 'branch', '--show-current'), 'work')
            self.assertEqual(row()['agent_role'], 'worker')
            self.assertEqual(row()['agent_snapshot'], worker_scope.serialize())
            from loop_manager_internal.agents.scope_contracts import GitSnapshot
            baseline = GitSnapshot.deserialize(checkpoints.get(str(self.repo), 'G-002').snapshot[
                'manager_metadata_agent_baseline']['snapshot'])
            loop_manager.validate_agent_scope('worker', self.repo, baseline,
                loop_manager.capture_git_snapshot(self.repo), selected_plan_path=self.paths['G-002'],
                external_worktree=self.repo, base_branch='main', connection=self.connection)
            updated_base = git(self.repo, 'rev-parse', 'origin/main')
            git(self.repo, 'rebase', updated_base)
            self.connection.execute("UPDATE exec_plans SET active_sha=? WHERE plan_id='G-002'", (updated_base,))
            self.connection.commit()
            updated = replace(current, revision_sha=updated_base, body_hash='updated')
            first = documents.refresh(effects, state, checkpoints, identity, context, current, updated, [])
            resumed = documents.refresh(effects, state, checkpoints, identity, context, updated, updated, [])
            self.assertEqual(first, resumed)
            self.assertEqual(first['stage'], 'done')
            agent.assert_called_once()
            self.assertEqual(row()['worker_session_id'], 'saved-session')

    def test_mutable_snapshot_uses_git_blob_with_crlf_checkout(self):
        path = self.repo / self.paths["G-001"]
        git(self.repo, "config", "core.autocrlf", "true")
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        self.assertIn(b"\r\n", path.read_bytes())

        snapshot = loop_manager.mutable_ready_snapshot(
            self.connection, str(self.repo), self.repo, self.base
        )

        self.assertNotIn("\r\n", snapshot.plans[0].text)
        self.assertEqual(
            snapshot.plans[0].content_hash,
            hashlib.sha256(
                loop_manager.git_bytes(
                    self.repo, "show", f"{self.base}:{self.paths['G-001']}"
                )
            ).hexdigest(),
        )

    def test_snapshot_authority_is_checked_before_changed_plan_contents(self):
        self.connection.execute(
            "UPDATE exec_plans SET pr_url = 'https://example/pr/2' "
            "WHERE repo = ? AND plan_id = 'G-002'",
            (str(self.repo),),
        )
        self.connection.commit()
        snapshot = loop_manager.mutable_ready_snapshot(
            self.connection, str(self.repo), self.repo, self.base
        )
        excluded = self.repo / self.paths["G-002"]
        excluded.write_text(
            plan_text("G-002", "Second").replace(
                "depends_on: []", "depends_on:\n  - G-099"
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            loop_manager.PlannerValidationError,
            "snapshot 밖 ExecPlan을 변경",
        ):
            loop_manager.validate_planner_result(
                self.repo,
                self.base,
                loop_manager.changed_files(self.repo),
                connection=self.connection,
                repo=str(self.repo),
                mutable_snapshot=snapshot,
            )

    def test_remote_confirmed_intent_recovers_after_push_before_sqlite_apply(self):
        _result, commit, specification, planning = self.prepare_batch()

        recovery = loop_manager.recover_general_planning_batch(
            self.connection, str(self.repo), self.repo, "main"
        )

        self.assertEqual(recovery, "applied")
        project = self.connection.execute(
            "SELECT processed_sha, specification_fingerprint, "
            "planning_fingerprint FROM projects WHERE repo = ?",
            (str(self.repo),),
        ).fetchone()
        self.assertEqual(tuple(project), (commit, specification, planning))
        self.assertIsNone(
            loop_manager.general_planning_publication(
                self.connection, str(self.repo)
            )
        )

    def test_recovery_consumes_published_batch_but_keeps_later_user_plan(self):
        _result, commit, _specification, _planning = self.prepare_batch()
        with tempfile.TemporaryDirectory() as directory:
            other = Path(directory) / "later"
            git(self.repo, "worktree", "add", "--detach", str(other), commit)
            user_plan = other / "docs/plans/[G-004] User.md"
            user_plan.write_text(plan_text("G-004", "User"), encoding="utf-8")
            git(other, "add", "docs/plans/[G-004] User.md")
            git(other, "commit", "-m", "User plan after publication")
            later = git(other, "rev-parse", "HEAD")
            git(self.repo, "update-ref", "refs/remotes/origin/main", later)

            self.assertEqual(loop_manager.recover_general_planning_batch(
                self.connection, str(self.repo), self.repo, "main",
            ), "applied")
            self.assertTrue(planning_input_state.matches(
                self.connection, str(self.repo), loop_manager.plan_declarations(self.repo, commit),
            ))
            self.assertFalse(planning_input_state.matches(
                self.connection, str(self.repo), loop_manager.plan_declarations(self.repo, later),
            ))

    def test_cancelled_plan_id_is_reserved_from_future_allocation(self):
        result, commit, specification, planning = self.prepare_batch()
        loop_manager.publish_general_planning_batch(
            self.connection,
            str(self.repo),
            self.repo,
            "main",
            result,
            commit,
            specification,
            planning,
        )
        snapshot = loop_manager.mutable_ready_snapshot(
            self.connection, str(self.repo), self.repo, commit
        )
        reused = self.repo / "docs/plans/[G-002] Reused.md"
        reused.write_text(plan_text("G-002", "Reused"), encoding="utf-8")
        result = loop_manager.validate_planner_result(
            self.repo,
            commit,
            loop_manager.changed_files(self.repo),
            connection=self.connection,
            repo=str(self.repo),
            mutable_snapshot=snapshot,
        )

        target = self.repo / "docs/plans/[G-004] Reused.md"
        self.assertEqual(
            [(path, plan["id"]) for path, plan in result.plans],
            [("docs/plans/[G-004] Reused.md", "G-004")],
        )
        self.assertEqual(result.id_allocations[0].source_id, "G-002")
        self.assertEqual(result.id_allocations[0].target_id, "G-004")
        self.assertFalse(reused.exists())
        self.assertTrue(target.is_file())
        self.assertIsNotNone(
            self.connection.execute(
                "SELECT 1 FROM execplan_cancellations WHERE repo = ? AND plan_id = ?",
                (str(self.repo), "G-002"),
            ).fetchone()
        )

    def test_product_and_architecture_changes_have_specification_fingerprints(self):
        original = loop_manager._specification_input_fingerprint(
            self.repo, self.base
        )
        (self.repo / "docs/PRODUCT.md").write_text(
            "# Changed Product\n", encoding="utf-8"
        )
        git(self.repo, "add", "docs/PRODUCT.md")
        git(self.repo, "commit", "-m", "change product")
        product_commit = git(self.repo, "rev-parse", "HEAD")
        product = loop_manager._specification_input_fingerprint(
            self.repo, product_commit
        )
        (self.repo / "docs/ARCHITECTURE.md").write_text(
            "# Changed Architecture\n", encoding="utf-8"
        )
        git(self.repo, "add", "docs/ARCHITECTURE.md")
        git(self.repo, "commit", "-m", "change architecture")
        architecture = loop_manager._specification_input_fingerprint(
            self.repo, git(self.repo, "rev-parse", "HEAD")
        )

        self.assertNotEqual(original, product)
        self.assertNotEqual(product, architecture)

    def test_active_requirement_create_update_delete_changes_specification(self):
        fingerprints = [
            loop_manager._specification_input_fingerprint(self.repo, self.base)
        ]
        extra = self.repo / "docs/requirements/Extra.md"
        extra.write_text("# Extra\n", encoding="utf-8")
        git(self.repo, "add", "docs/requirements/Extra.md")
        git(self.repo, "commit", "-m", "add requirement")
        fingerprints.append(
            loop_manager._specification_input_fingerprint(
                self.repo, git(self.repo, "rev-parse", "HEAD")
            )
        )
        extra.write_text("# Updated Extra\n", encoding="utf-8")
        git(self.repo, "add", "docs/requirements/Extra.md")
        git(self.repo, "commit", "-m", "update requirement")
        fingerprints.append(
            loop_manager._specification_input_fingerprint(
                self.repo, git(self.repo, "rev-parse", "HEAD")
            )
        )
        extra.unlink()
        git(self.repo, "add", "docs/requirements/Extra.md")
        git(self.repo, "commit", "-m", "delete requirement")
        fingerprints.append(
            loop_manager._specification_input_fingerprint(
                self.repo, git(self.repo, "rev-parse", "HEAD")
            )
        )

        self.assertTrue(
            all(left != right for left, right in zip(fingerprints, fingerprints[1:]))
        )
        self.assertEqual(fingerprints[0], fingerprints[-1])

    def test_byte_identical_requirement_archive_is_classified_separately(self):
        active = self.repo / "docs/requirements/R.md"
        archive = self.repo / "docs/requirements/archive/R.md"
        archive.parent.mkdir()
        active.rename(archive)
        git(self.repo, "add", "docs/requirements/R.md", "docs/requirements/archive/R.md")
        git(self.repo, "commit", "-m", "archive requirement")
        archived_commit = git(self.repo, "rev-parse", "HEAD")

        self.assertTrue(
            loop_manager._is_requirement_archive_only_change(
                self.repo, self.base, archived_commit
            )
        )
        self.assertNotEqual(
            loop_manager._specification_input_fingerprint(self.repo, self.base),
            loop_manager._specification_input_fingerprint(
                self.repo, archived_commit
            ),
        )

    def test_requirement_archive_with_source_change_requires_replanning(self):
        active = self.repo / "docs/requirements/R.md"
        archive = self.repo / "docs/requirements/archive/R.md"
        archive.parent.mkdir()
        active.rename(archive)
        (self.repo / "source.py").write_text("VALUE = 2\n", encoding="utf-8")
        git(self.repo, "add", "docs/requirements/R.md",
            "docs/requirements/archive/R.md", "source.py")
        git(self.repo, "commit", "-m", "archive and change source")
        changed = git(self.repo, "rev-parse", "HEAD")

        self.assertFalse(loop_manager._is_requirement_archive_only_change(
            self.repo, self.base, changed
        ))
        self.assertNotEqual(
            loop_manager._planning_input_fingerprint(self.repo, self.base),
            loop_manager._planning_input_fingerprint(self.repo, changed),
        )

    def test_plan_edits_and_archive_change_full_input_but_not_other_input(self):
        baseline = loop_manager._planning_input_fingerprint(self.repo, self.base)
        other = loop_manager._nonplan_input_fingerprint(self.repo, self.base)
        plan = self.repo / self.paths["G-001"]
        plan.write_text(plan_text("G-001", "Edited First"), encoding="utf-8")
        git(self.repo, "add", self.paths["G-001"])
        git(self.repo, "commit", "-m", "edit plan")
        edited = git(self.repo, "rev-parse", "HEAD")

        archive = self.repo / "docs/plans/archive" / plan.name
        archive.parent.mkdir()
        plan.rename(archive)
        git(self.repo, "add", "docs/plans")
        git(self.repo, "commit", "-m", "archive plan")
        archived = git(self.repo, "rev-parse", "HEAD")

        self.assertNotEqual(loop_manager._planning_input_fingerprint(self.repo, edited), baseline)
        self.assertNotEqual(loop_manager._planning_input_fingerprint(self.repo, archived), baseline)
        self.assertEqual(
            [loop_manager._nonplan_input_fingerprint(self.repo, revision)
             for revision in (edited, archived)], [other, other],
        )

        (self.repo / "AGENTS.md").write_text("# New guidance\n", encoding="utf-8")
        git(self.repo, "add", "AGENTS.md")
        git(self.repo, "commit", "-m", "change guidance")
        self.assertNotEqual(
            loop_manager._planning_input_fingerprint(
                self.repo, git(self.repo, "rev-parse", "HEAD")
            ),
            baseline,
        )

    def test_verified_plan_delta_preserves_unrelated_user_plan_change(self):
        baseline = loop_manager.plan_declarations(self.repo, self.base)
        planning_input_state.record_general(
            self.connection, str(self.repo), self.base, baseline,
        )
        first = self.paths["G-001"]
        second = self.paths["G-002"]
        own = ("blob", "verified-new")
        planning_input_state.advance_verified_delta(
            self.connection, str(self.repo), "maintenance-commit",
            {first: (baseline[first], own)},
        )
        self.assertTrue(planning_input_state.matches(
            self.connection, str(self.repo), {**baseline, first: own},
        ))
        self.assertFalse(planning_input_state.matches(
            self.connection, str(self.repo), {first: own, second: ("blob", "user-edit")},
        ))
        self.assertFalse(planning_input_state.advance_verified_delta(
            self.connection, str(self.repo), "unverified-drift",
            {second: (("blob", "user-edit"), None)},
        ))
        row = self.connection.execute(
            "SELECT scope, reference_revision, last_verified_revision, fingerprint_format "
            "FROM planning_input_baselines WHERE repo = ?", (str(self.repo),),
        ).fetchone()
        self.assertEqual(tuple(row), (
            "general", self.base, "maintenance-commit", planning_input_state.FORMAT,
        ))


class MutableReadyMetadataCasTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.execute(loop_manager.SCHEMA)
        self.connection.execute(loop_manager.EXEC_PLANS_SCHEMA)
        self.connection.execute(loop_manager.EXECPLAN_CANCELLATIONS_SCHEMA)
        maintenance_state.ensure_schema(self.connection)
        self.connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES ('repo', 'main')"
        )
        self.connection.execute(
            """
            INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on, priority,
                content_hash, status, plan_sha
            ) VALUES (
                'repo', 'G-001', 'docs/plans/[G-001] Plan.md', 'Plan',
                '["docs/PRODUCT.md"]', '[]', 'normal', 'hash', 'READY', 'base'
            )
            """
        )
        self.connection.commit()
        plan = ReadyPlanSnapshot(
            plan_id="G-001",
            path="docs/plans/[G-001] Plan.md",
            title="Plan",
            sources=("docs/PRODUCT.md",),
            depends_on=(),
            complexity="low",
            risk="low",
            priority="normal",
            content_hash="hash",
            plan_sha="base",
            text="plan body",
            current_complexity=None,
            current_risk=None,
            current_metadata_recorded=True,
        )
        self.snapshot = MutableReadySnapshot(
            repo="repo",
            active_sha="base",
            plans=(plan,),
            token=mutable_snapshot_token("repo", "base", (plan,)),
        )

    def test_snapshot_reader_captures_sqlite_cache_values(self):
        contents = plan_text("G-001", "Plan").encode("utf-8")
        self.connection.execute(
            "UPDATE exec_plans SET content_hash = ?, plan_sha = ?, "
            "current_complexity = 'low', current_risk = 'high' "
            "WHERE repo = 'repo'",
            (hashlib.sha256(contents).hexdigest(), "a" * 40),
        )
        row = self.connection.execute(
            "SELECT * FROM exec_plans WHERE repo = 'repo'"
        ).fetchone()

        snapshot = loop_manager._ready_plan_snapshot_from_contents(row, contents)

        self.assertEqual(snapshot.complexity, "low")
        self.assertEqual(snapshot.risk, "low")
        self.assertEqual(snapshot.current_complexity, "low")
        self.assertEqual(snapshot.current_risk, "high")
        self.assertTrue(snapshot.current_metadata_recorded)

    def test_snapshot_rejects_exact_complexity_and_risk_cache_changes(self):
        self.assertIsNone(self.connection.execute(
            "SELECT name FROM sqlite_master WHERE name='execplan_catalog_holds'"
        ).fetchone())
        loop_manager._assert_mutable_ready_snapshot_current(
            self.connection, self.snapshot
        )
        for field, value in (
            ("current_complexity", "low"),
            ("current_risk", "low"),
        ):
            with self.subTest(field=field):
                self.connection.execute(
                    f"UPDATE exec_plans SET {field} = ? WHERE repo = 'repo'",
                    (value,),
                )
                with self.assertRaisesRegex(
                    loop_manager.PlannerValidationError,
                    "snapshot metadata가 변경",
                ):
                    loop_manager._assert_mutable_ready_snapshot_current(
                        self.connection, self.snapshot
                    )
                self.connection.execute(
                    f"UPDATE exec_plans SET {field} = NULL WHERE repo = 'repo'"
                )
        self.assertIsNone(self.connection.execute(
            "SELECT name FROM sqlite_master WHERE name='execplan_catalog_holds'"
        ).fetchone())

    def test_recovery_goals_without_catalog_schema_preserve_registered_targets(self):
        from loop_manager_internal.catalog import remote_deletions
        evidence = json.dumps({"text": "deleted goal", "row": {}, "affected": ["G-001"], "resolved": {}})
        self.connection.execute(
            """INSERT INTO execplan_cancellations (
                repo, plan_id, path, title, sources, depends_on, complexity,
                risk, priority, content_hash, plan_sha, deletion_commit, recovery_evidence
            ) VALUES ('repo', 'G-002', 'docs/plans/[G-002] Deleted.md', 'Deleted',
                '["docs/PRODUCT.md"]', '[]', 'low', 'low', 'normal', 'hash',
                'base', 'commit', ?)""", (evidence,))
        schemas = tuple(self.connection.execute("SELECT name, sql FROM sqlite_master ORDER BY name"))
        changes = self.connection.total_changes
        goal = json.loads(remote_deletions.recovery_goals(self.connection, "repo")[0])
        self.assertEqual(goal["id"], "G-002")
        self.assertEqual(goal["affected_ids"], ["G-001"])
        self.assertEqual(goal["evidence"], evidence)
        self.assertEqual(tuple(self.connection.execute("SELECT name, sql FROM sqlite_master ORDER BY name")), schemas)
        self.assertEqual(self.connection.total_changes, changes)
        with self.assertRaisesRegex(loop_manager.PlannerValidationError, "삭제 목표 감사"):
            loop_manager._assert_mutable_ready_snapshot_current(self.connection, self.snapshot)

    def test_publication_cas_rejects_cache_change_after_snapshot_guard(self):
        original = self.snapshot.plans[0]
        plan = {
            "id": original.plan_id,
            "title": original.title,
            "sources": list(original.sources),
            "depends_on": [],
            "complexity": "low",
            "risk": "low",
            "priority": "normal",
        }
        publication = loop_manager.GeneralPlanningPublication(
            "repo", "prepared", "base", "commit", self.snapshot.token,
            "spec", "planning", "", "",
        )
        real_cas = planning_publication_state._mutable_ready_cas_where
        for operation in ("delete", "revise", "rename"):
            with self.subTest(operation=operation):
                error_name = {
                    "delete": "deletion",
                    "revise": "revision",
                    "rename": "rename",
                }[operation]
                self.connection.commit()
                result = loop_manager.ValidatedPlanningResult(
                    plans=(),
                    publication_paths=(),
                    deletions=(PlanDeletion(original),) if operation == "delete" else (),
                    revisions=(
                        PlanRevision(
                            original.path if operation == "revise"
                            else "docs/plans/[G-001] Renamed.md",
                            plan,
                            original,
                        ),
                    ) if operation != "delete" else (),
                    mutable_snapshot=self.snapshot,
                )

                def change_cache_after_guard():
                    self.connection.execute(
                        "UPDATE exec_plans SET current_risk = 'high' "
                        "WHERE repo = 'repo' AND plan_id = 'G-001'"
                    )
                    return real_cas()

                with mock.patch.object(
                    loop_manager, "general_planning_publication",
                    return_value=publication,
                ), mock.patch.object(
                    planning_publication_state, "_mutable_ready_cas_where",
                    side_effect=change_cache_after_guard,
                ), mock.patch.object(loop_manager, "file_hash", return_value="newhash"):
                    with self.assertRaisesRegex(
                        loop_manager.PlannerValidationError,
                        f"mutable READY {error_name}이 변경",
                    ):
                        loop_manager._apply_general_planning_batch(
                            self.connection, "repo", Path("/unused"), result,
                            "commit", "spec", "planning",
                        )
                row = self.connection.execute(
                    "SELECT path, current_risk FROM exec_plans "
                    "WHERE repo = 'repo' AND plan_id = 'G-001'"
                ).fetchone()
                self.assertEqual(tuple(row), (original.path, None))
                self.assertEqual(
                    self.connection.execute(
                        "SELECT COUNT(*) FROM execplan_cancellations"
                    ).fetchone()[0],
                    0,
                )

    def test_manifest_round_trip_preserves_nullable_cache_snapshot(self):
        result = loop_manager.ValidatedPlanningResult(
            plans=(),
            publication_paths=(),
            deletions=(PlanDeletion(self.snapshot.plans[0]),),
            mutable_snapshot=self.snapshot,
        )

        restored = loop_manager._planning_result_from_manifest(
            loop_manager._planning_result_manifest(result)
        )

        self.assertEqual(restored.mutable_snapshot, self.snapshot)
        self.assertEqual(restored.deletions, result.deletions)


class EverStartedMigrationTest(unittest.TestCase):
    def test_existing_rows_are_backfilled_from_execution_evidence(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        legacy_schema = loop_manager.EXEC_PLANS_SCHEMA.replace(
            "    ever_started INTEGER NOT NULL DEFAULT 0,\n", ""
        )
        connection.execute(legacy_schema)
        for plan_id, status, branch in (
            ("G-001", "READY", None),
            ("G-002", "READY", "loop-manager/G-002"),
            ("G-003", "MERGED", None),
        ):
            connection.execute(
                """
                INSERT INTO exec_plans (
                    repo, plan_id, path, title, sources, depends_on,
                    priority, content_hash, status, plan_sha, branch
                ) VALUES ('repo', ?, ?, ?, '["docs/PRODUCT.md"]', '[]',
                          'normal', 'hash', ?, 'base', ?)
                """,
                (
                    plan_id,
                    f"docs/plans/[{plan_id}] Plan.md",
                    "Plan",
                    status,
                    branch,
                ),
            )

        loop_manager._ensure_execplans_schema(connection)

        rows = connection.execute(
            "SELECT plan_id, ever_started FROM exec_plans ORDER BY plan_id"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [("G-001", 0), ("G-002", 1), ("G-003", 1)],
        )
        connection.close()

    def test_existing_column_is_backfilled_after_interrupted_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite"
            initial = sqlite3.connect(database)
            initial.execute(loop_manager.SCHEMA)
            initial.execute(loop_manager.EXEC_PLANS_SCHEMA)
            for plan_id, status, branch, ever_started in (
                ("G-001", "READY", None, 0),
                ("G-002", "READY", "loop-manager/G-002", 0),
                ("G-003", "MERGED", None, 0),
                ("G-004", "READY", None, 1),
            ):
                initial.execute(
                    """
                    INSERT INTO exec_plans (
                        repo, plan_id, path, title, sources, depends_on,
                        priority, content_hash, status, plan_sha, branch,
                        ever_started
                    ) VALUES ('repo', ?, ?, ?, '["docs/PRODUCT.md"]', '[]',
                              'normal', 'hash', ?, 'base', ?, ?)
                    """,
                    (
                        plan_id,
                        f"docs/plans/[{plan_id}] Plan.md",
                        "Plan",
                        status,
                        branch,
                        ever_started,
                    ),
                )
            initial.commit()
            initial.close()

            with closing(sqlite3.connect(database)) as resumed:
                resumed.row_factory = sqlite3.Row
                loop_manager._ensure_execplans_schema(resumed)
                self.assertFalse(resumed.in_transaction)
                loop_manager._ensure_execplans_schema(resumed)
                self.assertFalse(resumed.in_transaction)

                rows = resumed.execute(
                    "SELECT plan_id, status, branch, ever_started FROM exec_plans "
                    "ORDER BY plan_id"
                ).fetchall()
                self.assertEqual(
                    [tuple(row) for row in rows],
                    [
                        ("G-001", "READY", None, 0),
                        ("G-002", "READY", "loop-manager/G-002", 1),
                        ("G-003", "MERGED", None, 1),
                        ("G-004", "READY", None, 1),
                    ],
                )


if __name__ == "__main__":
    unittest.main()
