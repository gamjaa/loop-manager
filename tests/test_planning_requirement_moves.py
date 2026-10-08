"""Real-Git unit coverage of Manager move observation and validation."""
from dataclasses import replace
from contextlib import nullcontext
import hashlib
import itertools
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.application.contracts import ValidatedPlanningResult
from loop_manager_internal.planning.planning_catalog import (
    MutableReadySnapshot, PlannerValidationError, mutable_snapshot_token,
)
from loop_manager_internal.planning.requirement_retirement import (
    RequirementRetirementPort, RetirementError, RetirementReassessment,
)
from loop_manager_internal.planning.assessment_service import PlanningAssessmentService


SOURCE = "docs/requirements/[R-001] 기능 이름.md"
ARCHIVE = "docs/requirements/archive/[R-001] 기능 이름.md"
CONTENTS = b"# Feature\n\n## Acceptance Criteria\n\n- Works end to end\n"
PLAN = "docs/plans/[G-001] New plan.md"
LEGACY = "docs/requirements/[R-002] Legacy.md"
LEGACY_ARCHIVE = "docs/requirements/archive/[R-002] Legacy.md"
PLAN_TEXT = """---
id: G-001
title: New plan
sources:
  - docs/PRODUCT.md
depends_on: []
complexity: low
risk: low
priority: normal
---

## Purpose

Implement the remaining product work.

## Acceptance Criteria

- The behavior is covered.

## Implementation

- Keep the boundary explicit.

## Validation

- Run a targeted unit test.

## Progress

- [ ] Implement

## Decision Log

- Initial plan.
"""


class PlanningRequirementMoveTest(unittest.TestCase):
    def git(self, root, *args):
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.decode("utf-8").strip()

    def case(self, *, restore=False, renames="true", include_restoration=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.git(root, "init", "-b", "main")
        self.git(root, "config", "user.email", "test@example.com")
        self.git(root, "config", "user.name", "Test")
        self.git(root, "config", "core.autocrlf", "false")
        self.git(root, "config", "diff.renames", renames)
        origin, destination = (ARCHIVE, SOURCE) if restore else (SOURCE, ARCHIVE)
        files = {
            origin: CONTENTS,
            "docs/PRODUCT.md": b"# Product\n",
            "docs/ARCHITECTURE.md": b"# Architecture\n",
            "feature.py": b"implemented = True\n",
        }
        if include_restoration:
            files[LEGACY_ARCHIVE] = CONTENTS
        for path, content in files.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        (root / "docs/plans").mkdir()
        self.git(root, "add", "-A")
        self.git(root, "commit", "-m", "base")
        base = self.git(root, "rev-parse", "HEAD")
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.executescript("""
            CREATE TABLE projects(repo TEXT PRIMARY KEY);
            CREATE TABLE exec_plans(repo TEXT, plan_id TEXT, path TEXT,
                sources TEXT, content_hash TEXT, status TEXT, pr_head_oid TEXT,
                publication_remote_oid TEXT, verify_token TEXT, review_token TEXT,
                verify_result TEXT, review_result TEXT);
            CREATE TABLE execplan_cancellations(repo TEXT, plan_id TEXT, sources TEXT);
            CREATE TABLE document_drafts(repo TEXT, target_path TEXT, state TEXT);
            INSERT INTO projects VALUES ('repo');
        """)
        connection.execute(
            "INSERT INTO exec_plans VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("repo", "G-099", "docs/plans/G-099.md", json.dumps([SOURCE]),
             "content", "PR_OPEN" if restore else "MERGED", "head", "remote",
             "verify", "review", "passed", "passed"),
        )
        if include_restoration:
            connection.execute(
                "INSERT INTO exec_plans VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("repo", "G-098", "docs/plans/G-098.md", json.dumps([LEGACY]),
                 "content", "PR_OPEN", "head", "remote", "verify", "review", "passed", "passed"),
            )
        adapter = SimpleNamespace(run=loop_manager.git, run_bytes=loop_manager.git_bytes)
        port = RequirementRetirementPort(connection, adapter)
        if restore:
            (root / origin).rename(root / destination)
            oid = self.git(root, "rev-parse", base + ":" + origin)
            connection.execute(
                "INSERT INTO requirement_audit "
                "(repo,source_path,input_token,source_oid,observed_sha,outcome,reason,stage) "
                "VALUES ('repo',?,'legacy',?,?,'restore','old unfinished source','staged')",
                (SOURCE, oid, base),
            )
            paths = (origin, destination)
        else:
            (item,) = port.pending("repo", root, base)
            result = "REQUIREMENT_JUDGMENTS: " + json.dumps([{
                "path": SOURCE, "input_token": item.token, "outcome": "fulfilled",
                "criteria_evidence": ["feature.py implements the merged behavior"],
                "relevant_paths": ["feature.py", "docs/PRODUCT.md", "docs/ARCHITECTURE.md"],
            }])
            port.record("repo", port.validate_result("repo", root, base, result))
            paths = port.stage_moves("repo", root, base, root)
        self.assertEqual(paths, (origin, destination))
        snapshot = MutableReadySnapshot(
            "repo", base, (), mutable_snapshot_token("repo", base, ()),
        )
        return SimpleNamespace(
            root=root, base=base, connection=connection, adapter=adapter,
            port=port, paths=paths, destination=destination, snapshot=snapshot,
            restore=restore,
        )

    def test_legacy_restoration_manifest_is_readable_but_cannot_be_published(self):
        case = self.case(restore=True)
        self.git(case.root, "add", "-A")
        self.git(case.root, "commit", "-m", "legacy restored candidate")
        result = ValidatedPlanningResult(
            (), case.paths, ((SOURCE, hashlib.sha256(CONTENTS).hexdigest()),),
            mutable_snapshot=case.snapshot, restoration_moves=(ARCHIVE,),
        )
        resumed = loop_manager._planning_result_from_manifest(loop_manager._planning_result_manifest(result))
        self.assertEqual(resumed, result)
        with self.assertRaisesRegex(PlannerValidationError, "자동 복원"):
            loop_manager._validate_general_planning_commit(
                case.root, resumed, self.git(case.root, "rev-parse", "HEAD"))
        self.assertEqual((case.root / SOURCE).read_bytes(), CONTENTS)

    def test_legacy_unpublished_restoration_withdrawal_keeps_unrelated_retirement(self):
        case = self.case(include_restoration=True)
        (case.root / LEGACY_ARCHIVE).rename(case.root / LEGACY)
        oid = self.git(case.root, "rev-parse", case.base + ":" + LEGACY_ARCHIVE)
        case.connection.execute(
            "INSERT INTO requirement_audit "
            "(repo,source_path,input_token,source_oid,observed_sha,outcome,reason,stage) "
            "VALUES ('repo',?,'legacy',?,?,'restore','old unfinished source','staged')",
            (LEGACY, oid, case.base),
        )
        self.git(case.root, "add", "-A")
        with self.assertRaises(RetirementReassessment):
            case.port.receipted_paths("repo", case.root, case.base, case.root)
        self.assertFalse((case.root / LEGACY).exists())
        self.assertEqual((case.root / LEGACY_ARCHIVE).read_bytes(), CONTENTS)
        restarted = RequirementRetirementPort(case.connection, case.adapter)
        self.assertEqual(restarted.receipted_paths("repo", case.root, case.base, case.root), (SOURCE, ARCHIVE))
        self.assertEqual((case.root / ARCHIVE).read_bytes(), CONTENTS)

    def test_receipt_revalidation_refreshes_base_without_new_retirements(self):
        case = self.case(include_restoration=True)
        old_base = case.base
        self.git(case.root, "commit", "--allow-empty", "-m", "unrelated base advance")
        new_base = self.git(case.root, "rev-parse", "HEAD")
        self.assertNotEqual(old_base, new_base)
        paths = case.port.receipted_paths("repo", case.root, new_base, case.root)
        self.assertIn(SOURCE, paths)
        receipt = case.connection.execute(
            "SELECT base_sha, stage FROM requirement_move_receipts WHERE source_path=?", (SOURCE,),
        ).fetchone()
        self.assertEqual(tuple(receipt), (new_base, "pending"))

    def validate(self, case, paths=None, dirty_files=None):
        return loop_manager.validate_planner_result(
            case.root, case.base, dirty_files or [], repo="repo",
            mutable_snapshot=case.snapshot,
            manager_paths=case.paths if paths is None else paths,
        )

    def test_move_and_mixed_batch_observation_survives_index_state_and_restart(self):
        for restore, state, renames, mixed in itertools.product(
            (False,), ("unstaged", "staged", "committed"), ("true", "false"), (False, True),
        ):
            with self.subTest(restore=restore, state=state, renames=renames, mixed=mixed):
                case = self.case(restore=restore, renames=renames)
                if mixed:
                    (case.root / PLAN).write_text(PLAN_TEXT, encoding="utf-8")
                if state != "unstaged":
                    self.git(case.root, "add", "-A")
                if state == "committed":
                    self.git(case.root, "commit", "-m", "planning")
                head = self.git(case.root, "rev-parse", "HEAD")
                restarted = RequirementRetirementPort(case.connection, case.adapter)
                paths = restarted.receipted_paths("repo", case.root, case.base, case.root)
                self.assertEqual(paths, case.paths)
                with mock.patch("loop_manager.run_agent", side_effect=AssertionError("Unexpected Agent call")):
                    result = self.validate(case, paths)
                observed = set(loop_manager.changed_files(case.root, case.base)) | set(
                    loop_manager.changed_files(case.root)
                )
                self.assertTrue(set(paths) <= observed)
                if state != "committed":
                    # since observes commits only; live index/worktree must supplement it.
                    self.assertEqual(loop_manager.changed_files(case.root, case.base), [])
                if mixed:
                    self.assertEqual([plan[1]["id"] for plan in result.plans], ["G-001"])
                else:
                    self.assertIsNone(result)
                    result = ValidatedPlanningResult((), (), (), mutable_snapshot=case.snapshot)
                publication_paths = tuple(sorted(set(result.publication_paths) | set(paths)))
                result = replace(
                    result, publication_paths=publication_paths,
                    publication_hashes=tuple(
                        (path, hashlib.sha256((case.root / path).read_bytes()).hexdigest())
                        for path in publication_paths if (case.root / path).is_file()
                    ),
                    retirement_moves=() if restore else (SOURCE,),
                    restoration_moves=(ARCHIVE,) if restore else (),
                )
                resumed = loop_manager._planning_result_from_manifest(
                    loop_manager._planning_result_manifest(result)
                )
                self.assertEqual(resumed, result)
                self.assertEqual(self.git(case.root, "rev-parse", "HEAD"), head)
                if state != "committed":
                    self.git(case.root, "add", "-A")
                    self.git(case.root, "commit", "-m", "planning")
                commit = self.git(case.root, "rev-parse", "HEAD")
                loop_manager._validate_general_planning_commit(case.root, resumed, commit)
                self.assertEqual((case.root / case.destination).read_bytes(), CONTENTS)
                # A repeated Manager stage uses the same receipt without another rename/commit.
                self.assertEqual(restarted.stage_moves("repo", case.root, case.base, case.root), paths)
                self.assertEqual(self.git(case.root, "rev-parse", "HEAD"), commit)

    def test_missing_or_partial_receipt_does_not_authorize_requirement_changes(self):
        for restore in (False,):
            case = self.case(restore=restore)
            self.git(case.root, "add", "-A")
            for paths in ((), case.paths[:1]):
                with self.subTest(restore=restore, paths=paths):
                    with self.assertRaises(PlannerValidationError):
                        self.validate(case, paths)

    def test_saved_dirty_paths_cannot_prove_nonexistent_manager_moves(self):
        case = self.case()
        missing = "docs/requirements/archive/nonexistent.md"
        with self.assertRaisesRegex(PlannerValidationError, "이동 영수증"):
            self.validate(case, (*case.paths, missing), [missing])

    def test_unrelated_staged_requirement_is_not_hidden_by_valid_receipt(self):
        case = self.case()
        (case.root / "docs/requirements/Other.md").write_text("Unrelated\n", encoding="utf-8")
        self.git(case.root, "add", "-A")
        with self.assertRaises(PlannerValidationError):
            self.validate(case)

    def test_restarted_receipt_refuses_tampering_collision_and_symlink(self):
        for restore, violation in itertools.product((False, True), ("tamper", "collision", "symlink")):
            with self.subTest(restore=restore, violation=violation):
                case = self.case(restore=restore)
                destination = case.root / case.destination
                if violation == "tamper":
                    destination.write_bytes(b"changed")
                elif violation == "collision":
                    (case.root / case.paths[0]).write_bytes(CONTENTS)
                restarted = RequirementRetirementPort(case.connection, case.adapter)
                original_is_symlink = Path.is_symlink
                observed = (mock.patch.object(Path, 'is_symlink', autospec=True,
                            side_effect=lambda candidate: candidate == destination or original_is_symlink(candidate))
                            if violation == 'symlink' else nullcontext())
                with observed, self.assertRaises(RetirementError):
                    restarted.receipted_paths("repo", case.root, case.base, case.root)

    def test_commit_validation_refuses_tampering_even_with_matching_file_hash(self):
        for restore in (False,):
            with self.subTest(restore=restore):
                case = self.case(restore=restore)
                (case.root / case.destination).write_bytes(b"changed")
                self.git(case.root, "add", "-A")
                self.git(case.root, "commit", "-m", "tampered planning")
                result = ValidatedPlanningResult(
                    (), tuple(sorted(case.paths)),
                    ((case.destination, hashlib.sha256(b"changed").hexdigest()),),
                    mutable_snapshot=case.snapshot,
                    retirement_moves=() if restore else (SOURCE,),
                    restoration_moves=(ARCHIVE,) if restore else (),
                )
                with self.assertRaisesRegex(PlannerValidationError, "원문이 변경"):
                    loop_manager._validate_general_planning_commit(
                        case.root, result, self.git(case.root, "rev-parse", "HEAD"),
                    )

    def assessment(self, case):
        row = SimpleNamespace(phase="PLANNING", active_sha=case.base,
                              planning_result="", planning_error=None)
        ports = SimpleNamespace(
            retirement=case.port,
            validation=SimpleNamespace(validate_general_result=lambda *args, **kwargs:
                loop_manager.validate_planner_result(
                    args[1], args[2], args[3], repo="repo",
                    mutable_snapshot=args[4], **kwargs,
                )),
            events=SimpleNamespace(error=mock.Mock()),
            state=SimpleNamespace(update=mock.Mock()),
        )
        run = SimpleNamespace(
            row=row, context=SimpleNamespace(repo="repo", repo_path=case.root),
            planning_worktree=case.root, mutable_ready_snapshot=case.snapshot,
            dirty_files=[], maintenance_planning=False, planner_scope_error=None,
            current_fingerprint="input",
        )
        return PlanningAssessmentService(ports, None), ports, run

    def test_assessment_manifest_preserves_retirements_with_historical_archive(self):
        for mixed in (False, True):
            with self.subTest(mixed=mixed):
                case = self.case(include_restoration=True)
                self.git(case.root, "add", "-A")
                if mixed:
                    (case.root / PLAN).write_text(PLAN_TEXT, encoding="utf-8")
                service, ports, run = self.assessment(case)
                for resumed in (False, True):
                    with self.subTest(resumed=resumed):
                        self.assertIsNone(service._validate_planner_result(run))
                        result = run.validated_result
                        self.assertEqual(result.retirement_moves, (SOURCE,))
                        self.assertEqual(result.restoration_moves, ())
                        self.assertEqual(set(result.publication_paths),
                                         {SOURCE, ARCHIVE} | ({PLAN} if mixed else set()))
                        self.assertEqual(set(dict(result.publication_hashes)),
                                         {ARCHIVE} | ({PLAN} if mixed else set()))
                        if not resumed:
                            self.git(case.root, "add", "-A")
                            self.git(case.root, "commit", "-m", "combined planning batch")
                        manifest = loop_manager._planning_result_manifest(result)
                        restored = loop_manager._planning_result_from_manifest(manifest)
                        loop_manager._validate_general_planning_commit(
                            case.root, restored, self.git(case.root, "rev-parse", "HEAD"),
                        )
                        run.dirty_files = []
                ports.events.error.assert_not_called()

    def test_assessment_rechecks_old_receipts_after_staging_new_moves(self):
        case = self.case(include_restoration=True)
        service, ports, run = self.assessment(case)
        stage = case.port.stage_moves

        def corrupt_old_move_after_staging(*args, **kwargs):
            paths = stage(*args, **kwargs)
            (case.root / ARCHIVE).write_bytes(b"tampered after initial check")
            return paths

        with mock.patch.object(case.port, "stage_moves", side_effect=corrupt_old_move_after_staging):
            outcome = service._validate_planner_result(run)
        self.assertTrue(outcome.error)
        self.assertEqual(outcome.kind.value, "operational_error")
        self.assertIn("영수증과 worktree", ports.state.update.call_args.kwargs["planning_error"])
        self.assertFalse(hasattr(run, "validated_result"))
        self.assertEqual((case.root / ARCHIVE).read_bytes(), b"tampered after initial check")


if __name__ == "__main__":
    unittest.main()
