import hashlib
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

import loop_manager
import maintenance_state


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
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test")
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
                plan_text(plan_id, title), encoding="utf-8"
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

    def test_mutable_snapshot_excludes_ready_with_execution_evidence(self):
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
            [plan.plan_id for plan in snapshot.plans], ["G-001"]
        )

    def test_snapshot_authority_is_checked_before_changed_plan_contents(self):
        self.connection.execute(
            "UPDATE exec_plans SET ever_started = 1 "
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

        with self.assertRaisesRegex(
            loop_manager.PlannerValidationError, "취소된 ID를 재사용"
        ):
            loop_manager.validate_planner_result(
                self.repo,
                commit,
                loop_manager.changed_files(self.repo),
                connection=self.connection,
                repo=str(self.repo),
                mutable_snapshot=snapshot,
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


if __name__ == "__main__":
    unittest.main()
