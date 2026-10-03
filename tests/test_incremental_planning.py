import hashlib
from contextlib import closing
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
