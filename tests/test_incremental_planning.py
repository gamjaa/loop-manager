from tests.support.fixed_samples import use_fixed_guidance
import hashlib
import json
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.catalog import execplan_catalog
from tests.support.git import isolated_git_test
from loop_manager_internal.storage import (
    execplan_catalog_state, maintenance_state, planning_publication_state,
    planning_input_state,
)
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


@isolated_git_test
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

    def test_git_adapter_ignores_deleted_verified_merged_archive_for_dependencies(self):
        original_path = self.paths["G-001"]
        archived_path = "docs/plans/archive/[G-001] First.md"
        original_file = self.repo / original_path
        archive_file = self.repo / archived_path
        archived_text = original_file.read_text(encoding="utf-8")
        archive_file.parent.mkdir(parents=True)
        archive_file.write_text(archived_text, encoding="utf-8", newline="\n")
        original_file.unlink()
        dependent_path = self.repo / self.paths["G-002"]
        dependent_text = plan_text("G-002", "Second").replace(
            "depends_on: []", "depends_on:\n  - G-001",
        )
        dependent_path.write_text(dependent_text, encoding="utf-8", newline="\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "archive merged prerequisite")
        archive_revision = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", archive_revision)
        entry = execplan_catalog.snapshot_from_documents(
            {archived_path: archived_text},
        ).archived["G-001"]
        self.connection.execute(
            "UPDATE projects SET active_sha=? WHERE repo=?",
            (archive_revision, str(self.repo)),
        )
        self.connection.execute(
            "UPDATE exec_plans SET path=?, status='MERGED', content_hash=?, "
            "current_revision_sha=? WHERE repo=? AND plan_id='G-001'",
            (archived_path, entry.content_hash, archive_revision,
             str(self.repo)),
        )
        self.connection.execute(
            "UPDATE exec_plans SET depends_on=?, content_hash=?, current_revision_sha=? "
            "WHERE repo=? AND plan_id='G-002'",
            (json.dumps(["G-001"]), hashlib.sha256(dependent_text.encode()).hexdigest(),
             archive_revision, str(self.repo)),
        )
        execplan_catalog_state.CatalogStateRepository(self.connection).record(
            str(self.repo), entry,
            original_path=original_path,
            original_content_hash=entry.content_hash,
            plan_sha=self.base,
            proof=execplan_catalog_state.MergeProof(
                True, kind=execplan_catalog_state.MergeProofKind.GITHUB_PR,
                plan_sha=self.base, publication_content_hash=entry.content_hash,
                revision=archive_revision,
            ),
            diagnostic=None, identity_verified=True,
        )
        self.connection.commit()
        adopted = loop_manager.reconcile_execplan_catalog(
            self.connection, self.repo, str(self.repo), "main",
        )
        self.assertTrue(adopted.usable)

        archive_file.unlink()
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "remove merged archive document")
        deletion_revision = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", deletion_revision)
        self.connection.execute(
            "UPDATE projects SET active_sha=? WHERE repo=?",
            (deletion_revision, str(self.repo)),
        )
        self.connection.commit()

        result = loop_manager.reconcile_execplan_catalog(
            self.connection, self.repo, str(self.repo), "main",
        )

        self.assertTrue(result.usable)
        self.assertNotIn("G-002", result.withheld_plan_ids)
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM execplan_catalog_holds WHERE repo=? AND plan_id='G-002'",
            (str(self.repo),),
        ).fetchone())

    def test_remote_deletion_started_history_and_dependents_are_scoped(self):
        result, _ = self._remote_deletion(started=True, new_plan=True)
        self.assertEqual(set(result.withheld_plan_ids), {"G-001", "G-002"})
        self.assertEqual(self.connection.execute("SELECT worker_session_id FROM exec_plans WHERE plan_id='G-001'").fetchone()[0], "saved")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM execplan_cancellations").fetchone()[0], 0)
        self.assertIsNone(loop_manager.scoped_blocking.project_block(self.connection, str(self.repo)))
        self.assertIsNotNone(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-007'").fetchone())
        repeated = loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
        self.assertEqual(set(repeated.withheld_plan_ids), {"G-001", "G-002"})

    def test_proven_finished_writer_cancellation_preserves_started_history(self):
        self._remote_deletion(started=True, new_plan=True)
        self.connection.execute("UPDATE exec_plans SET worker_result='완료된 Worker 결과' WHERE plan_id='G-001'")
        self.connection.commit()
        result = loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
        self.assertEqual(set(result.withheld_plan_ids), {"G-002"})
        self.assertIsNone(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-001'").fetchone())
        audit = self.connection.execute("SELECT * FROM execplan_cancellations WHERE plan_id='G-001'").fetchone()
        history = json.loads(audit["recovery_evidence"])
        self.assertEqual(history["row"]["worker_session_id"], "saved")
        self.assertEqual(history["row"]["worker_result"], "완료된 Worker 결과")
        self.assertEqual(history["affected"], ["G-002"])
        repeated = loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
        self.assertEqual(set(repeated.withheld_plan_ids), {"G-002"})
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM execplan_cancellations").fetchone()[0], 1)
        self.assertIsNotNone(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-007'").fetchone())

    def test_requirement_deletion_cancels_only_withdrawn_and_preserves_shared_goal(self):
        self._requirement_deletion_recovers()

    def test_requirement_deletion_started_history_and_shared_resume_are_preserved(self):
        self._requirement_deletion_recovers(started=True)

    def _requirement_deletion_recovers(self, *, started=False):
        from loop_manager_internal.catalog import remote_deletions
        source = "docs/requirements/R.md"
        first = plan_text("G-001", "First").replace("  - docs/PRODUCT.md", "  - " + source)
        second = plan_text("G-002", "Second").replace("  - docs/PRODUCT.md", "  - docs/PRODUCT.md\n  - " + source)
        for key, text in (("G-001", first), ("G-002", second)):
            (self.repo / self.paths[key]).write_text(text, encoding="utf-8", newline="\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "connect requirement goals")
        base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", base)
        loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
        if started:
            self.connection.execute("UPDATE exec_plans SET ever_started=1, worker_session_id='saved', worker_result='completed'")
            self.connection.execute("UPDATE exec_plans SET status='IMPLEMENTING' WHERE plan_id='G-002'")
            self.connection.commit()
        (self.repo / source).unlink()
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "withdraw only requirement")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.base)
        observed = loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
        self.assertEqual(set(observed.withheld_plan_ids), {"G-001", "G-002"})
        loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
        snapshot = loop_manager.mutable_ready_snapshot(self.connection, str(self.repo), self.repo, self.base)
        self.assertEqual(set(snapshot.by_id), {"G-001", "G-002"})
        goal = json.loads(snapshot.recovery_goals[0])
        self.assertEqual(goal["kind"], "missing-source")
        self.assertIn("# Requirement", json.loads(goal["evidence"])["text"])
        decisions = [{"id": "G-001", "source": source, "outcome": "withdrawn", "reason": "삭제된 요구만의 목표가 철회됨"},
                     {"id": "G-002", "source": source, "outcome": "retained", "reason": "PRODUCT의 공유 목표가 남아 있음"}]
        self.connection.execute("UPDATE projects SET planning_result=? WHERE repo=?",
            ("REQUIREMENT_RECOVERY: " + json.dumps(decisions), str(self.repo)))
        self.connection.commit()
        if not started:
            (self.repo / self.paths["G-001"]).unlink()
        (self.repo / self.paths["G-002"]).write_text(plan_text("G-002", "Second"), encoding="utf-8", newline="\n")
        result = loop_manager.validate_planner_result(self.repo, self.base, loop_manager.changed_files(self.repo),
            connection=self.connection, repo=str(self.repo), mutable_snapshot=snapshot)
        self.assertEqual([item.snapshot.plan_id for item in result.deletions], ["G-001"])
        self.assertEqual([item.snapshot.plan_id for item in result.revisions], ["G-002"])
        self.assertEqual(loop_manager.validate_planner_result(self.repo, self.base, loop_manager.changed_files(self.repo),
            connection=self.connection, repo=str(self.repo), mutable_snapshot=snapshot), result)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "publish requirement recovery")
        published = git(self.repo, "rev-parse", "HEAD")
        with self.connection:
            planning_publication_state.apply_mutable_catalog_changes(self.connection, str(self.repo),
                deletions=result.deletions, revisions=result.revisions, snapshot_token=snapshot.token,
                plan_commit=published, content_hash=lambda path: hashlib.sha256((self.repo / path).read_bytes()).hexdigest())
            remote_deletions.resolve_recovery(self.connection, snapshot, published, {"G-001", "G-002"})
            from loop_manager_internal.storage.runtime_state import RuntimeStateRepository
            RuntimeStateRepository(self.connection).clear(str(self.repo))
        git(self.repo, "update-ref", "refs/remotes/origin/main", published)
        refreshed = loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
        self.assertTrue(refreshed.usable, refreshed)
        self.assertIsNone(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-001'").fetchone())
        row = self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-002'").fetchone()
        self.assertEqual(json.loads(row["sources"]), ["docs/PRODUCT.md"])
        self.assertEqual(row["status"], "IMPLEMENTING" if started else "READY",
            [dict(held) for held in self.connection.execute("SELECT * FROM execplan_catalog_holds")])
        if started:
            self.assertEqual(row["worker_session_id"], "saved")
        audit = self.connection.execute("SELECT recovery_evidence FROM execplan_cancellations WHERE plan_id='G-001'").fetchone()
        self.assertEqual(json.loads(audit[0])["judgment"]["decision"]["outcome"], "withdrawn")
        if started:
            self.assertEqual(json.loads(audit[0])["row"]["worker_session_id"], "saved")

    def test_started_remote_deletion_rechecks_actual_pr_before_cancelling(self):
        self._remote_deletion(started=True, new_plan=True)
        self.connection.execute("UPDATE exec_plans SET branch='loop-manager/G-001', worker_result='completed' WHERE plan_id='G-001'")
        self.connection.commit()
        for response in ({"url": "https://example.test/pull/1"}, RuntimeError("조회 불가")):
            with self.subTest(response=response), mock.patch.object(loop_manager, "pr_for_branch") as observe:
                if isinstance(response, Exception):
                    observe.side_effect = response
                else:
                    observe.return_value = response
                result = loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
                self.assertIn("G-001", result.withheld_plan_ids)
                self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM execplan_cancellations").fetchone()[0], 0)
                self.assertIsNotNone(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-007'").fetchone())
        with mock.patch.object(loop_manager, "pr_for_branch", return_value=None):
            loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
        audit = json.loads(self.connection.execute("SELECT recovery_evidence FROM execplan_cancellations WHERE plan_id='G-001'").fetchone()[0])
        self.assertEqual(audit["row"]["branch"], "loop-manager/G-001")
        self.assertTrue(audit["cleanup_pending"])

    def test_requirement_body_answer_is_proven_and_cleanup_is_publication_bound(self):
        from loop_manager_internal.catalog import draft_decisions, execplan_catalog, execplan_drafts
        from loop_manager_internal.storage import execplan_draft_state
        from tests.support.catalog import draft_text
        git(self.repo, "remote", "add", "origin", "https://github.com/example/project.git")
        path = self.paths["G-001"]
        source = "docs/requirements/[R-001] Original.md"
        renamed = "docs/requirements/[R-001] Renamed.md"
        (self.repo / "docs/requirements/R.md").unlink()
        (self.repo / source).write_text("# [R-001] Original\n", encoding="utf-8", newline="\n")
        text = draft_text("G-001", "First").replace("docs/PRODUCT.md", source)
        (self.repo / path).write_text(text, encoding="utf-8", newline="\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "publish draft")
        base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", base)
        loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
        answer = "Use policy A and retain history."
        (self.repo / source).rename(self.repo / renamed)
        (self.repo / renamed).write_text("# [R-001] Renamed\n\n" + answer + "\n", encoding="utf-8", newline="\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "user answers requirement")
        base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", base)
        loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
        saved = execplan_draft_state.evidence(self.connection, str(self.repo), "G-001")
        self.assertTrue(saved["user_changes"][renamed]["verified"])
        snapshot = loop_manager.mutable_ready_snapshot(self.connection, str(self.repo), self.repo, base)
        (self.repo / path).write_text(draft_decisions.replace_section(text, "Implementation", answer), encoding="utf-8", newline="\n")
        assessment = [{"id": "G-001", "revision": saved["revision"], "remaining": "",
            "resolved": [{"question": draft_decisions.questions(saved["question"])[0],
                          "answer": answer, "source": renamed, "quote": answer}]}]
        self.connection.execute("UPDATE projects SET planning_result=? WHERE repo=?", (draft_decisions.PREFIX + json.dumps(assessment), str(self.repo)))
        self.connection.commit()
        result = loop_manager.validate_planner_result(self.repo, base, loop_manager.changed_files(self.repo),
            connection=self.connection, repo=str(self.repo), mutable_snapshot=snapshot)
        restored = loop_manager._planning_result_from_manifest(loop_manager._planning_result_manifest(result))
        self.assertEqual(restored, result)
        self.assertEqual(len(result.revisions), 1)
        self.assertNotIn("## Draft Decision", (self.repo / path).read_text())
        self.assertIn("G-001", execplan_draft_state.pending_ids(self.connection, str(self.repo)))
        with self.assertRaisesRegex(RuntimeError, "근거"), self.connection:
            planning_publication_state.apply_mutable_catalog_changes(self.connection, str(self.repo),
                deletions=(), revisions=result.revisions, snapshot_token=snapshot.token, plan_commit=base,
                content_hash=lambda path: execplan_catalog.plan_content_hash((self.repo / path).read_bytes()), draft_inputs=lambda plan: {})
        self.assertIn("G-001", execplan_draft_state.pending_ids(self.connection, str(self.repo)))
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "publish answer cleanup")
        published = git(self.repo, "rev-parse", "HEAD")
        with self.connection:
            planning_publication_state.apply_mutable_catalog_changes(self.connection, str(self.repo),
                deletions=(), revisions=result.revisions, snapshot_token=snapshot.token, plan_commit=published,
                content_hash=lambda path: execplan_catalog.plan_content_hash((self.repo / path).read_bytes()),
                draft_inputs=lambda plan: execplan_drafts.input_digests(self.repo, plan))
        self.assertNotIn("G-001", execplan_draft_state.pending_ids(self.connection, str(self.repo)))
        history = execplan_draft_state.evidence(self.connection, str(self.repo), "G-001")["resolutions"]
        self.assertEqual(history[0]["user_changes"][renamed]["commit"], base)
        self.assertEqual(history[0]["publication_revision"], published)

    def test_managed_or_unrecognized_author_does_not_create_user_approval(self):
        from loop_manager_internal.storage import execplan_draft_state
        from tests.support.catalog import draft_text, accepted_text
        git(self.repo, "remote", "add", "origin", "https://github.com/example/project.git")
        path = self.paths["G-001"]
        text = draft_text("G-001", "First")
        (self.repo / path).write_text(text, encoding="utf-8", newline="\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "publish draft")
        base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", base)
        loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
        revision = execplan_draft_state.evidence(self.connection, str(self.repo), "G-001")["revision"]
        (self.repo / path).write_text(accepted_text(text, revision), encoding="utf-8", newline="\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "--author=Other <other@example.test>", "-m", "foreign answer")
        remote = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", remote)
        loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
        saved = execplan_draft_state.evidence(self.connection, str(self.repo), "G-001")
        self.assertFalse(saved["user_changes"][path]["verified"])
        (self.repo / path).write_text(accepted_text(text, revision, answer="Use policy B."), encoding="utf-8", newline="\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "managed answer")
        managed = git(self.repo, "rev-parse", "HEAD")
        self.connection.execute("UPDATE exec_plans SET publication_sha=? WHERE plan_id='G-001'", (managed,))
        self.connection.commit()
        git(self.repo, "update-ref", "refs/remotes/origin/main", managed)
        loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
        saved = execplan_draft_state.evidence(self.connection, str(self.repo), "G-001")
        self.assertFalse(saved["user_changes"][path]["verified"])
        self.assertFalse(saved["decision_ready"])
        self.assertIn("G-001", execplan_draft_state.pending_ids(self.connection, str(self.repo)))

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

    def test_deleted_goal_recovery_preserves_existing_direct_and_transitive_holds(self):
        def with_dependencies(text, dependencies):
            block = ("depends_on: []" if not dependencies else
                     "depends_on:\n" + "".join(f"  - {item}\n" for item in dependencies).rstrip())
            return text.replace("depends_on: []", block)

        dependencies = {
            "G-002": ("G-001", "G-003", "G-004"),
            "G-003": ("G-999",),
            "G-004": ("G-003",),
            "G-005": ("G-999",),
        }
        titles = {
            "G-003": "Held predecessor",
            "G-004": "Intermediate prerequisite",
            "G-005": "New held prerequisite",
            "G-006": "Independent",
        }
        for plan_id, title in titles.items():
            self.paths[plan_id] = f"docs/plans/[{plan_id}] {title}.md"
            deps = dependencies.get(plan_id, ())
            path = self.repo / self.paths[plan_id]
            path.write_text(with_dependencies(plan_text(plan_id, title), deps),
                            encoding="utf-8", newline="\n")
            self.connection.execute(
                """INSERT INTO exec_plans (
                    repo, plan_id, path, title, sources, depends_on,
                    priority, content_hash, status, plan_sha,
                    initial_worker_level, effective_worker_level
                ) VALUES (?, ?, ?, ?, '["docs/PRODUCT.md"]', ?,
                          'normal', ?, 'READY', ?, 1, 1)""",
                (str(self.repo), plan_id, self.paths[plan_id], title,
                 json.dumps(list(deps)),
                 hashlib.sha256(path.read_bytes()).hexdigest(), self.base),
            )
        target = self.repo / self.paths["G-002"]
        target.write_text(with_dependencies(
            target.read_text(encoding="utf-8"), dependencies["G-002"]),
            encoding="utf-8", newline="\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "Add pre-existing held prerequisite paths")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.base)
        self.connection.execute("UPDATE projects SET active_sha=?", (self.base,))
        self.connection.commit()
        self.assertTrue(loop_manager.reconcile_execplan_catalog(
            self.connection, self.repo, str(self.repo), "main",
        ).usable)

        archive_path = "docs/plans/archive/[G-003] Held predecessor.md"
        (self.repo / "docs/plans/archive").mkdir()
        (self.repo / self.paths["G-003"]).rename(self.repo / archive_path)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "Preserve predecessor history in archive")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.base)
        self.connection.execute("UPDATE projects SET active_sha=?", (self.base,))
        self.connection.commit()
        self.assertTrue(loop_manager.reconcile_execplan_catalog(
            self.connection, self.repo, str(self.repo), "main",
        ).usable)

        body = with_dependencies(plan_text("G-002", "Second"), dependencies["G-002"])
        self._remote_deletion(dependent=False, target_text=body)
        preexisting_holds = loop_manager.scoped_blocking.catalog_held_plan_ids(
            self.connection, str(self.repo),
        )
        self.assertTrue({"G-003", "G-004", "G-005"} <= preexisting_holds)
        snapshot = loop_manager.mutable_ready_snapshot(
            self.connection, str(self.repo), self.repo, self.base,
        )
        self.assertIn("G-002", snapshot.by_id)
        self.assertNotIn("G-003", snapshot.by_id)
        self.assertNotIn("G-005", snapshot.by_id)

        decision = {
            "deleted_id": "G-001", "outcome": "obsolete", "replacement_ids": [],
            "sources": ["docs/PRODUCT.md"], "reason": "The deleted goal is no longer required.",
        }
        revised_text = with_dependencies(
            plan_text("G-002", "Second"), ("G-003", "G-004"),
        ) + "\nDEPENDENCY_RECOVERY: " + json.dumps(decision) + "\n"
        target.write_text(revised_text, encoding="utf-8", newline="\n")

        def validate():
            return loop_manager.validate_planner_result(
                self.repo, self.base, loop_manager.changed_files(self.repo),
                connection=self.connection, repo=str(self.repo), mutable_snapshot=snapshot,
            )

        result = validate()
        self.assertEqual([item.snapshot.plan_id for item in result.revisions], ["G-002"])
        target.write_text(revised_text.replace(
            "  - G-004\n", "  - G-004\n  - G-005\n"), encoding="utf-8", newline="\n")
        self.assertEqual([item.snapshot.plan_id for item in validate().revisions], ["G-002"])
        target.write_text(revised_text, encoding="utf-8", newline="\n")

        archived_bytes = (self.repo / archive_path).read_bytes()
        archived_row_before = dict(self.connection.execute(
            "SELECT * FROM exec_plans WHERE repo=? AND plan_id='G-003'", (str(self.repo),),
        ).fetchone())
        merge_audit = self.connection.execute(
            "SELECT * FROM execplan_catalog_history WHERE repo=? AND plan_id='G-003'",
            (str(self.repo),),
        ).fetchone()
        merge_audit_before = dict(merge_audit) if merge_audit else None
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "Publish deleted goal recovery")
        commit = git(self.repo, "rev-parse", "HEAD")
        spec = loop_manager._specification_input_fingerprint(self.repo, commit)
        planning = loop_manager._planning_input_fingerprint(self.repo, commit)
        loop_manager.record_general_planning_publication_intent(
            self.connection, str(self.repo), self.repo, result, commit, spec, planning,
        )
        with self.assertRaisesRegex(loop_manager.PlannerValidationError, "push"):
            loop_manager.publish_general_planning_batch(
                self.connection, str(self.repo), self.repo, "main", result, commit, spec, planning,
            )
        git(self.repo, "update-ref", "refs/remotes/origin/main", commit)
        self.assertEqual(loop_manager.recover_general_planning_batch(
            self.connection, str(self.repo), self.repo, "main",
        ), "applied")

        self.assertTrue(preexisting_holds - {"G-002"} <=
                        loop_manager.scoped_blocking.catalog_held_plan_ids(
                            self.connection, str(self.repo),
                        ))
        self.assertEqual((self.repo / archive_path).read_bytes(), archived_bytes)
        self.assertEqual(dict(self.connection.execute(
            "SELECT * FROM exec_plans WHERE repo=? AND plan_id='G-003'", (str(self.repo),),
        ).fetchone()), archived_row_before)
        merge_audit = self.connection.execute(
            "SELECT * FROM execplan_catalog_history WHERE repo=? AND plan_id='G-003'",
            (str(self.repo),),
        ).fetchone()
        self.assertEqual(dict(merge_audit) if merge_audit else None, merge_audit_before)
        self.assertEqual(json.loads(self.connection.execute(
            "SELECT depends_on FROM exec_plans WHERE repo=? AND plan_id='G-002'",
            (str(self.repo),),
        ).fetchone()[0]), ["G-003", "G-004"])
        self.assertEqual(loop_manager.select_ready_execplan(
            self.connection, str(self.repo),
        )["plan_id"], "G-006")

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
        target.write_text(plan_text("G-003", "Replacement").replace(
            "depends_on: []", "depends_on:\n  - G-004"), encoding="utf-8")
        (self.repo / "docs/plans/[G-004] Cycle.md").write_text(
            plan_text("G-004", "Cycle").replace(
                "depends_on: []", "depends_on:\n  - G-003"),
            encoding="utf-8",
        )
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

        evidence = json.loads(row["recovery_evidence"])
        prior_resolution = evidence["resolved"]["G-002"]
        path = self.repo / self.paths["G-002"]
        path.write_text(
            path.read_text(encoding="utf-8") + "\nSee G-001 for historical context.\n",
            encoding="utf-8", newline="\n",
        )
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "reintroduce cancelled prerequisite reference")
        observed_revision = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", observed_revision)
        self.connection.execute(
            "UPDATE projects SET active_sha=? WHERE repo=?",
            (observed_revision, str(self.repo)),
        )
        self.connection.commit()

        reopened = loop_manager.reconcile_execplan_catalog(
            self.connection, self.repo, str(self.repo), "main",
        )
        current_evidence = json.loads(self.connection.execute(
            "SELECT recovery_evidence FROM execplan_cancellations WHERE repo=? AND plan_id='G-001'",
            (str(self.repo),),
        ).fetchone()[0])

        self.assertEqual(set(reopened.withheld_plan_ids), {"G-002"})
        self.assertNotIn("G-002", current_evidence["resolved"])
        self.assertEqual(current_evidence["resolution_history"]["G-002"], [prior_resolution])
        goal = next(json.loads(raw) for raw in loop_manager.mutable_ready_snapshot(
            self.connection, str(self.repo), self.repo, observed_revision,
        ).recovery_goals if json.loads(raw)["id"] == "G-001")
        self.assertEqual(goal["affected_ids"], ["G-002"])

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

    def test_rowless_cancellation_reference_recovers_original_identity_and_text(self):
        self._remote_deletion(dependent=False, new_plan=True)
        self.connection.execute(
            "UPDATE execplan_cancellations SET recovery_evidence=NULL WHERE plan_id='G-001'"
        )
        body = plan_text("G-007", "Independent").replace(
            "depends_on: []", "depends_on:\n  - G-001",
        )
        (self.repo / "docs/plans/[G-007] Independent.md").write_text(
            body, encoding="utf-8", newline="\n",
        )
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "reference cancelled prerequisite")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.base)
        self.connection.execute("UPDATE projects SET active_sha=?", (self.base,))
        self.connection.commit()

        result = loop_manager.reconcile_execplan_catalog(
            self.connection, self.repo, str(self.repo), "main",
        )
        evidence = json.loads(self.connection.execute(
            "SELECT recovery_evidence FROM execplan_cancellations WHERE plan_id='G-001'"
        ).fetchone()[0])
        snapshot = loop_manager.mutable_ready_snapshot(
            self.connection, str(self.repo), self.repo, self.base,
        )

        self.assertEqual(set(result.withheld_plan_ids), {"G-007"})
        self.assertIn("Implement First", evidence["text"])
        self.assertEqual(evidence["identity"]["id"], "G-001")
        self.assertEqual(evidence["identity"]["path"], self.paths["G-001"])
        self.assertIsNone(evidence["unknown_reason"])
        goal = next(json.loads(raw) for raw in snapshot.recovery_goals
                    if json.loads(raw)["id"] == "G-001")
        self.assertEqual(goal["affected_ids"], ["G-007"])

    def test_unverifiable_rowless_cancellation_holds_only_current_references(self):
        self._remote_deletion(dependent=False, new_plan=True)
        self.connection.execute(
            "UPDATE execplan_cancellations SET recovery_evidence=NULL, plan_sha='unknown' "
            "WHERE plan_id='G-001'"
        )
        dependent = plan_text("G-007", "Independent").replace(
            "depends_on: []", "depends_on:\n  - G-001",
        )
        (self.repo / "docs/plans/[G-007] Independent.md").write_text(
            dependent, encoding="utf-8", newline="\n",
        )
        (self.repo / "docs/plans/[G-008] Unrelated.md").write_text(
            plan_text("G-008", "Unrelated"), encoding="utf-8", newline="\n",
        )
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "reference unverifiable cancellation")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.base)
        self.connection.execute("UPDATE projects SET active_sha=?", (self.base,))
        self.connection.commit()

        result = loop_manager.reconcile_execplan_catalog(
            self.connection, self.repo, str(self.repo), "main",
        )
        evidence = json.loads(self.connection.execute(
            "SELECT recovery_evidence FROM execplan_cancellations WHERE plan_id='G-001'"
        ).fetchone()[0])
        unrelated = self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo=? AND plan_id='G-008'", (str(self.repo),),
        ).fetchone()

        self.assertEqual(set(result.withheld_plan_ids), {"G-007"})
        self.assertEqual(evidence["text"], "")
        self.assertIsNotNone(evidence["unknown_reason"])
        self.assertEqual(evidence["affected"], ["G-007"])
        self.assertIsNotNone(unrelated)
        self.assertEqual(unrelated["status"], "READY")

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

    def test_rowless_hold_deletion_and_later_reference_is_reobserved(self):
        from loop_manager_internal.catalog import remote_deletions

        held_path = "docs/plans/[G-008] Held.md"
        held_body = plan_text("G-008", "Held").replace("priority: normal\n", "")
        (self.repo / held_path).write_text(held_body, encoding="utf-8", newline="\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "add invalid pre-registration plan")
        original_revision = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", original_revision)
        self.connection.execute("UPDATE projects SET active_sha=?", (original_revision,))
        self.connection.commit()
        registered = loop_manager.reconcile_execplan_catalog(
            self.connection, self.repo, str(self.repo), "main",
        )
        self.assertTrue(registered.usable)
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM exec_plans WHERE repo=? AND plan_id='G-008'", (str(self.repo),),
        ).fetchone())
        self.assertIsNotNone(self.connection.execute(
            "SELECT 1 FROM execplan_catalog_holds WHERE repo=? AND plan_id='G-008'", (str(self.repo),),
        ).fetchone())

        (self.repo / held_path).unlink()
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "delete held pre-registration plan")
        deletion_revision = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", deletion_revision)
        self.connection.execute("UPDATE projects SET active_sha=?", (deletion_revision,))
        self.connection.commit()
        deleted = loop_manager.reconcile_execplan_catalog(
            self.connection, self.repo, str(self.repo), "main",
        )
        self.assertTrue(deleted.usable)
        held = json.loads(self.connection.execute(
            "SELECT evidence FROM execplan_catalog_holds WHERE repo=? AND plan_id='G-008'",
            (str(self.repo),),
        ).fetchone()[0])
        first_observation = held["deleted_goal"]
        self.assertEqual(first_observation["text"], held_body)
        self.assertEqual(first_observation["original_revision"], original_revision)
        self.assertEqual(first_observation["observed_revision"], deletion_revision)
        self.assertIn("삭제 의도를 확인할 수 없습니다", first_observation["unknown_reason"])

        dependent_body = plan_text("G-009", "Dependent").replace(
            "depends_on: []", "depends_on:\n  - G-008",
        )
        dependent_path = "docs/plans/[G-009] Dependent.md"
        (self.repo / dependent_path).write_text(dependent_body, encoding="utf-8", newline="\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "add reference to deleted held plan")
        reference_revision = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", reference_revision)
        self.connection.execute("UPDATE projects SET active_sha=?", (reference_revision,))
        self.connection.commit()
        referenced = loop_manager.reconcile_execplan_catalog(
            self.connection, self.repo, str(self.repo), "main",
        )
        self.assertTrue(referenced.usable)
        self.assertIsNotNone(self.connection.execute(
            "SELECT 1 FROM exec_plans WHERE repo=? AND plan_id='G-009'", (str(self.repo),),
        ).fetchone())

        goal = next(json.loads(raw) for raw in remote_deletions.recovery_goals(
            self.connection, str(self.repo),
        ) if json.loads(raw)["id"] == "G-008")
        self.assertEqual(goal["kind"], "missing-plan")
        self.assertEqual(goal["path"], held_path)
        self.assertEqual(goal["deletion_revision"], deletion_revision)
        self.assertEqual(goal["affected_ids"], ["G-009"])
        evidence = json.loads(goal["evidence"])
        self.assertEqual(evidence["text"], held_body)
        self.assertEqual(evidence["row"]["plan_id"], "G-008")
        self.assertEqual(evidence["row"]["path"], held_path)
        self.assertEqual(evidence["observed_revision"], deletion_revision)
        self.assertEqual(evidence["last_observed_revision"], reference_revision)
        self.assertIn("삭제 의도를 확인할 수 없습니다", evidence["unknown_reason"])
        snapshot = loop_manager.mutable_ready_snapshot(
            self.connection, str(self.repo), self.repo, reference_revision,
        )
        prompt_goal = next(item for item in json.loads(snapshot.prompt_payload())["deleted_goals"]
                           if item["id"] == "G-008")
        self.assertEqual(prompt_goal["text"], held_body)
        self.assertEqual(prompt_goal["unknown_reason"], evidence["unknown_reason"])
        self.assertEqual(prompt_goal["plan_revision"], original_revision)

        repeated = loop_manager.reconcile_execplan_catalog(
            self.connection, self.repo, str(self.repo), "main",
        )
        self.assertTrue(repeated.usable)
        repeated_goal = next(json.loads(raw) for raw in remote_deletions.recovery_goals(
            self.connection, str(self.repo),
        ) if json.loads(raw)["id"] == "G-008")
        self.assertEqual(repeated_goal["deletion_revision"], deletion_revision)
        self.assertEqual(repeated_goal["affected_ids"], ["G-009"])
        self.assertEqual(json.loads(repeated_goal["evidence"])["text"], held_body)

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

    def test_remote_deletion_pending_hold_rejects_changed_plan_path(self):
        body = plan_text("G-007", "Independent").replace("depends_on: []", "depends_on:\n  - G-008") + "\nUse G-001.\n"
        (self.repo / "docs/plans/[G-007] Independent.md").write_text(body, encoding="utf-8")
        (self.repo / "docs/plans/[G-008] Invalid.md").write_text(
            plan_text("G-008", "Invalid").replace("priority: normal\n", ""), encoding="utf-8")
        (self.repo / self.paths["G-001"]).unlink()
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "delete prerequisite and add held plans")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.base)
        observed = loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
        self.assertTrue(observed.usable)
        self.assertEqual(set(observed.withheld_plan_ids), {"G-007", "G-008"})

        old_path = self.repo / "docs/plans/[G-007] Independent.md"
        new_path = self.repo / "docs/plans/[G-007] Renamed.md"
        old_path.unlink()
        renamed_body = plan_text("G-007", "Renamed").replace(
            "depends_on: []", "depends_on:\n  - G-008",
        ) + "\nUse G-001.\n"
        new_path.write_text(renamed_body, encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "move held plan to a different identity path")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.base)
        rejected = loop_manager.reconcile_execplan_catalog(self.connection, self.repo, str(self.repo), "main")
        self.assertFalse(rejected.usable)
        self.assertEqual(rejected.diagnostic.code, "unknown-plan-id")

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
        from loop_manager_internal.planning.planner_validation import MutableReadyPRConflict

        self.connection.execute("UPDATE exec_plans SET ever_started=1, branch='work' WHERE plan_id='G-002'")
        self.connection.commit()
        with mock.patch.object(loop_manager, 'pr_for_branch', return_value=None):
            snapshot = loop_manager.mutable_ready_snapshot(self.connection, str(self.repo), self.repo, self.base)
        with mock.patch.object(loop_manager, 'pr_for_branch', return_value={'url': 'https://example/pr/2'}):
            with self.assertRaisesRegex(MutableReadyPRConflict, 'PR이 등록'):
                loop_manager._assert_mutable_ready_snapshot_current(self.connection, snapshot)

    def test_pr_lookup_failure_does_not_reuse_mutable_snapshot(self):
        self.connection.execute("UPDATE exec_plans SET ever_started=1, branch='work' WHERE plan_id='G-002'")
        self.connection.commit()
        with mock.patch.object(loop_manager, 'pr_for_branch', return_value=None):
            snapshot = loop_manager.mutable_ready_snapshot(self.connection, str(self.repo), self.repo, self.base)
        with mock.patch.object(loop_manager, 'pr_for_branch', side_effect=RuntimeError('PR lookup failed')):
            with self.assertRaisesRegex(RuntimeError, 'PR lookup failed'):
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
                validate_scope=mock.Mock(),
                prepare_snapshot=loop_manager.mutable_ready_snapshot,
                assert_snapshot=loop_manager._assert_mutable_ready_snapshot_current,
                validate_result=loop_manager.validate_planner_result,
                validate_worktree=loop_manager.validate_managed_worktree,
                pr_for_branch=mock.Mock(return_value=None), log=mock.Mock(),
                )
            checkpoints = ExecutionCheckpointRepository(self.connection)
            documents.save(checkpoints, str(self.repo), 'G-002', dict(version=1, stage='proposed',
                base=self.base, phase='IMPLEMENTING', paths=['docs/PRODUCT.md'], document_only=True,
                candidate=worker_scope.head, worker_snapshot=worker_scope.serialize(),
                previous=current.serialize(), assessment=json.loads(raw.split(': ', 1)[1]), worker_result=None,
                verify_result=None, verify_base=None))
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
            agent.assert_not_called()
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


class ManualOwnershipPlanningTest(unittest.TestCase):
    def setUp(self):
        from loop_manager_internal.planning import planner_validation

        self.repo = "repo"
        self.repository = Path("/unused/repository")
        self.base = "a" * 40
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
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
            (self.repo, self.base),
        )
        for plan_id in ("G-001", "G-002"):
            title = "First" if plan_id == "G-001" else "Second"
            self.connection.execute(
                """
                INSERT INTO exec_plans (
                    repo, plan_id, path, title, sources, depends_on,
                    priority, content_hash, status, plan_sha,
                    initial_worker_level, effective_worker_level
                ) VALUES (?, ?, ?, ?, '[\"docs/PRODUCT.md\"]', '[]',
                          'normal', ?, 'READY', ?, 1, 1)
                """,
                (
                    self.repo,
                    plan_id,
                    f"docs/plans/[{plan_id}] {title}.md",
                    title,
                    f"hash-{plan_id}",
                    self.base,
                ),
            )
        self.connection.commit()

        self.planning = planner_validation
        self.snapshot_effects = planner_validation.MutableReadySnapshotEffects(
            _ensure_execplans_schema=loop_manager._ensure_execplans_schema,
            _ready_plan_snapshot_at_revision=self._ready_plan_snapshot_at_revision,
            _row_is_mutable_ready=lambda row: planner_validation._row_is_mutable_ready(
                planner_validation.RowIsMutableReadyEffects(is_unowned_ready=lambda _row: True),
                row,
            ),
            mutable_snapshot_token=mutable_snapshot_token,
            pr_for_branch=mock.Mock(return_value=None),
        )

    def _ready_plan_snapshot_at_revision(self, _repository, revision, row, *, allow_started=False):
        return ReadyPlanSnapshot(
            plan_id=row["plan_id"],
            path=row["path"],
            title=row["title"],
            sources=tuple(json.loads(row["sources"])),
            depends_on=tuple(json.loads(row["depends_on"])),
            complexity="low",
            risk="low",
            priority=row["priority"],
            content_hash=row["content_hash"],
            plan_sha=revision,
            text=plan_text(row["plan_id"], row["title"]),
            current_metadata_recorded=True,
        )

    def mutable_snapshot(self):
        return self.planning.mutable_ready_snapshot(
            self.snapshot_effects, self.connection, self.repo, self.repository, self.base
        )

    def test_mutable_snapshot_excludes_live_manual_owners_and_preparation_intents(self):
        from loop_manager_internal.storage.manual_work_state import ManualWorkRepository

        store = ManualWorkRepository(self.connection)
        start = store.create(
            self.repo, "start", "manual-start", (), self.base, self.base,
            members=("G-001",),
        )
        store.update(start, owner_id="G-001", branch="manual-start", worktree="/tmp/manual-start")
        submit = store.create(
            self.repo, "submit", "manual-submit", (), self.base, self.base,
            members=("G-002",),
        )
        store.update(submit, state="REGISTERING", owner_id="G-002",
                     branch="manual-submit", worktree="/tmp/manual-submit")

        snapshot = self.mutable_snapshot()

        self.assertEqual(snapshot.plans, ())
        submit = store.update(submit, state="AUTOMATIC")
        store.accept(submit, head=self.base, base=self.base, input_token="accepted")
        self.assertEqual(self.mutable_snapshot().plans, ())

    def test_mutable_snapshot_excludes_every_member_of_accepted_manual_group(self):
        from loop_manager_internal.storage.manual_work_state import ManualWorkRepository

        store = ManualWorkRepository(self.connection)
        group = store.create(
            self.repo, "submit", "manual-submit", (), self.base, self.base,
            members=("G-001", "G-002"),
        )
        group = store.update(group, state="AUTOMATIC", owner_id="G-001",
                             branch="manual-submit", worktree="/tmp/manual-submit")
        store.accept(group, head=self.base, base=self.base, input_token="accepted")

        self.assertEqual(self.mutable_snapshot().plans, ())

    def test_orphan_membership_without_release_evidence_keeps_plan_protected(self):
        from loop_manager_internal.storage.manual_work_state import ManualWorkRepository

        store = ManualWorkRepository(self.connection)
        orphan = store.create(
            self.repo, "start", "orphaned-start", (), self.base, self.base,
            members=("G-001",),
        )
        self.connection.execute(
            "DELETE FROM manual_work_groups WHERE repo=? AND group_id=?",
            (self.repo, orphan["group_id"]),
        )
        merged = store.create(
            self.repo, "submit", "finished-submit", (), self.base, self.base,
            members=("G-002",),
        )
        store.update(merged, state="MERGED", owner_id="G-002")

        snapshot = self.mutable_snapshot()

        self.assertEqual([plan.plan_id for plan in snapshot.plans], ["G-002"])

    def test_related_malformed_group_skips_only_affected_plan(self):
        from types import SimpleNamespace
        from loop_manager_internal.maintenance.maintenance_planning import PlanRevision
        from loop_manager_internal.storage import work_claims
        from loop_manager_internal.storage.manual_work_state import ManualWorkRepository

        store = ManualWorkRepository(self.connection)
        group = store.create(
            self.repo, "submit", "malformed-submit", (), self.base, self.base,
            members=("G-001",),
        )
        store.update(group, state="MANUAL_PENDING", owner_id="G-999")

        snapshot = self.mutable_snapshot()

        self.assertEqual([plan.plan_id for plan in snapshot.plans], ["G-002"])
        result = SimpleNamespace(
            publication_paths=(), plans=(),
            revisions=(PlanRevision(snapshot.plans[0].path, {}, snapshot.plans[0]),),
            deletions=(),
        )
        work_claims.assert_manual_scopes_available(self.connection, self.repo, result)

    def test_start_intent_after_snapshot_rejects_edit_without_plan_row_change(self):
        from types import SimpleNamespace
        from loop_manager_internal.maintenance.maintenance_planning import PlanRevision
        from loop_manager_internal.storage import work_claims
        from loop_manager_internal.storage.manual_work_state import ManualWorkRepository

        snapshot = self.mutable_snapshot()
        plan = snapshot.plans[0]
        before = dict(self.connection.execute(
            "SELECT * FROM exec_plans WHERE repo=? AND plan_id=?",
            (self.repo, plan.plan_id),
        ).fetchone())
        store = ManualWorkRepository(self.connection)
        group = store.create(
            self.repo, "start", "manual-start", (), self.base, self.base,
            members=(plan.plan_id,),
        )
        store.update(group, owner_id=plan.plan_id, branch="manual-start", worktree="/tmp/manual-start")
        after = dict(self.connection.execute(
            "SELECT * FROM exec_plans WHERE repo=? AND plan_id=?",
            (self.repo, plan.plan_id),
        ).fetchone())
        self.assertEqual(after, before)

        result = SimpleNamespace(
            publication_paths=(), plans=(),
            revisions=(PlanRevision(plan.path, {}, plan),), deletions=(),
        )
        with self.assertRaisesRegex(RuntimeError, "수동 계획 소유권"):
            work_claims.assert_manual_scopes_available(self.connection, self.repo, result)

    def test_stage_document_cannot_publish_a_manual_owned_plan_revision(self):
        from types import SimpleNamespace
        from loop_manager_internal.execution import stage_documents as documents
        from loop_manager_internal.execution.execution_session import ExecutionIdentity
        from loop_manager_internal.execution.execution_runtime import ExecutionPhase
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
        from tests.support.revision import definition

        store = ManualWorkRepository(self.connection)
        group = store.create(
            self.repo, "submit", "manual-submit", (), self.base, self.base,
            members=("G-001",),
        )
        group = store.update(group, state="AUTOMATIC", owner_id="G-001",
                             branch="manual-work", worktree="/tmp/manual-work")
        store.accept(group, head=self.base, base=self.base, input_token="accepted")
        self.connection.execute(
            "UPDATE exec_plans SET status='IMPLEMENTING', ever_started=1, active_sha=?, "
            "branch='manual-work', worker_session_id='manual-session' WHERE repo=? AND plan_id='G-001'",
            (self.base, self.repo),
        )
        self.connection.commit()

        current = replace(
            definition(), revision_sha=self.base,
            path="docs/plans/[G-001] First.md", title="First",
            sources=("docs/PRODUCT.md",),
        )
        state = SimpleNamespace(
            connection=self.connection,
            row=lambda: self.connection.execute(
                "SELECT * FROM exec_plans WHERE repo=? AND plan_id='G-001'",
                (self.repo,),
            ).fetchone(),
        )
        context = SimpleNamespace(
            plan=SimpleNamespace(plan_id="G-001"), phase=ExecutionPhase.IMPLEMENTING,
            worktree=self.repository, branch="manual-work", resolving_rebase=False,
        )
        identity = ExecutionIdentity(
            repo=self.repo, repo_path=self.repository, state_dir=Path("/tmp/g243-state"),
            base="main", provider="codex",
        )
        checkpoints = ExecutionCheckpointRepository(self.connection)
        documents.save(checkpoints, self.repo, "G-001", {
            "version": 1, "stage": "proposed", "base": self.base,
            "phase": "IMPLEMENTING", "paths": ["docs/PRODUCT.md"],
            "document_only": True, "previous": current.serialize(),
            "assessment": {
                "relevant": True, "implementation": "satisfied",
                "plan_text": plan_text("G-001", "Revised"),
                "evidence": "현재 구현과 최신 명세를 대조했습니다.",
            },
            "candidate": self.base,
        })
        git_result = SimpleNamespace(stdout=self.base + "\n", returncode=0)
        effects = documents.StageDocumentEffects(
            git=mock.Mock(return_value=git_result),
            capture_scope=mock.Mock(return_value=SimpleNamespace(head=self.base)),
            validate_scope=mock.Mock(),
            prepare_snapshot=lambda *args: self.mutable_snapshot(),
            assert_snapshot=mock.Mock(), validate_result=mock.Mock(),
            validate_worktree=mock.Mock(), pr_for_branch=mock.Mock(return_value=None),
            log=mock.Mock(),
        )

        with self.assertRaisesRegex(loop_manager.PlannerValidationError, "갱신 권한"):
            documents.refresh(
                effects, state, checkpoints, identity, context,
                current, current, ["docs/PRODUCT.md"],
            )

        effects.pr_for_branch.assert_has_calls([
            mock.call(self.repository, "manual-work"),
            mock.call(self.repository, "manual-work"),
        ])
        self.assertEqual(effects.pr_for_branch.call_count, 2)
        effects.git.assert_called_once_with(self.repository, "rev-parse", "origin/main")
        effects.validate_worktree.assert_not_called()

    def test_prepared_stage_document_rechecks_manual_ownership_before_push(self):
        from types import SimpleNamespace
        from loop_manager_internal.execution import stage_documents as documents
        from loop_manager_internal.execution.execution_session import ExecutionIdentity
        from loop_manager_internal.execution.execution_runtime import ExecutionPhase
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
        from tests.support.revision import definition

        original = self.mutable_snapshot().plans[0]
        store = ManualWorkRepository(self.connection)
        group = store.create(
            self.repo, "start", "manual-start", (), self.base, self.base,
            members=(original.plan_id,),
        )
        group = store.update(group, owner_id=original.plan_id, branch="manual-work",
                             worktree="/tmp/manual-work")
        current = replace(
            definition(), revision_sha=self.base, path=original.path, title=original.title,
            sources=original.sources,
        )
        state = SimpleNamespace(
            connection=self.connection,
            row=lambda: self.connection.execute(
                "SELECT * FROM exec_plans WHERE repo=? AND plan_id=?",
                (self.repo, original.plan_id),
            ).fetchone(),
        )
        identity = ExecutionIdentity(
            repo=self.repo, repo_path=self.repository, state_dir=Path("/tmp/g243-state"),
            base="main", provider="codex",
        )
        workspace = identity.state_dir / "worktrees" / hashlib.sha256(
            self.repo.encode()
        ).hexdigest()[:16] / "document-revisions" / original.plan_id / self.base
        commit = "b" * 40
        checkpoints = ExecutionCheckpointRepository(self.connection)
        documents.save(checkpoints, self.repo, original.plan_id, {
            "version": 1, "stage": "prepared", "base": self.base,
            "phase": "IMPLEMENTING", "paths": ["docs/PRODUCT.md"],
            "document_only": True, "previous": current.serialize(),
            "assessment": {
                "relevant": True, "implementation": "satisfied",
                "plan_text": plan_text(original.plan_id, "Revised"),
                "evidence": "현재 구현과 최신 명세를 대조했습니다.",
            },
            "workspace": str(workspace), "commit": commit,
            "original": documents.snapshot_to_dict(original),
        })
        git = mock.Mock(side_effect=(
            SimpleNamespace(stdout="", returncode=0),
            SimpleNamespace(stdout=self.base + "\n", returncode=0),
            SimpleNamespace(stdout="", returncode=1),
        ))
        effects = documents.StageDocumentEffects(
            git=git, capture_scope=mock.Mock(), validate_scope=mock.Mock(),
            prepare_snapshot=mock.Mock(),
            assert_snapshot=loop_manager._assert_mutable_ready_snapshot_current,
            validate_result=mock.Mock(), validate_worktree=mock.Mock(),
            pr_for_branch=mock.Mock(return_value=None), log=mock.Mock(),
        )
        context = SimpleNamespace(
            plan=SimpleNamespace(plan_id=original.plan_id), phase=ExecutionPhase.IMPLEMENTING,
            worktree=self.repository, branch="manual-work", resolving_rebase=False,
        )

        with self.assertRaisesRegex(loop_manager.PlannerValidationError, "수동 소유 계획"):
            documents.refresh(effects, state, checkpoints, identity, context,
                              current, current, [])

        self.assertEqual(git.call_count, 3)
        effects.validate_worktree.assert_called_once_with(self.repository, workspace, None)

    def test_new_execplan_change_does_not_start_document_assessment(self):
        from loop_manager_internal.execution import stage_documents as documents
        from loop_manager_internal.execution.execution_session import ExecutionIdentity
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        from tests.support.revision import definition
        current = definition()
        state = SimpleNamespace(row=lambda: {'pr_url': None})
        context = SimpleNamespace(plan=SimpleNamespace(plan_id='G-001'))
        identity = ExecutionIdentity(repo=self.repo, repo_path=self.repository,
            state_dir=Path("/unused/state"), base='main', provider='codex')
        effects = mock.Mock()
        self.assertIsNone(documents.refresh(effects, state, ExecutionCheckpointRepository(self.connection),
            identity, context, current, current, [current.path]))


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

    def test_active_maintenance_allows_general_snapshot_only_for_parked_execution(self):
        self.connection.execute("INSERT INTO maintenance_cycles (repo, phase) VALUES ('repo', 'refactoring')")
        self.connection.execute("UPDATE projects SET phase=NULL WHERE repo='repo'")
        with self.assertRaisesRegex(loop_manager.PlannerValidationError, 'maintenance cycle'):
            loop_manager.mutable_ready_snapshot(self.connection, 'repo', Path('repo'), 'base')
        with self.assertRaisesRegex(loop_manager.PlannerValidationError, 'maintenance cycle'):
            loop_manager._assert_mutable_ready_snapshot_current(self.connection, self.snapshot)
        self.connection.execute("UPDATE projects SET phase='PLANNING' WHERE repo='repo'")
        check = loop_manager.mutable_ready_snapshot(self.connection, 'repo', Path('repo'), 'base')
        self.assertEqual(check.plans, ())
        loop_manager._assert_mutable_ready_snapshot_current(self.connection, check)
        self.connection.execute("UPDATE projects SET planning_resume_plan_id = 'G-001' WHERE repo='repo'")
        with mock.patch.object(loop_manager, '_ready_plan_snapshot_at_revision', return_value=self.snapshot.plans[0]):
            snapshot = loop_manager.mutable_ready_snapshot(self.connection, 'repo', Path('repo'), 'base')
        self.assertEqual(tuple(replace(plan, manual_ownership_state=None) for plan in snapshot.plans), self.snapshot.plans)
        self.assertTrue(all(plan.manual_ownership_state for plan in snapshot.plans))
        loop_manager._assert_mutable_ready_snapshot_current(self.connection, snapshot)

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
        plan = replace(self.snapshot.plans[0], manual_ownership_state='manual-history-digest')
        snapshot = replace(
            self.snapshot,
            plans=(plan,),
            token=mutable_snapshot_token(
                self.snapshot.repo, self.snapshot.active_sha, (plan,), self.snapshot.recovery_goals
            ),
        )
        result = loop_manager.ValidatedPlanningResult(
            plans=(),
            publication_paths=(),
            deletions=(PlanDeletion(plan),),
            mutable_snapshot=snapshot,
        )

        restored = loop_manager._planning_result_from_manifest(
            loop_manager._planning_result_manifest(result)
        )

        self.assertEqual(restored.mutable_snapshot, snapshot)
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
