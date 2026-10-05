"""Focused final-judgment and Manager move tests."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from loop_manager_internal.planning.requirement_retirement import (
    RequirementRetirementPort, RetirementError, RetirementReassessment,
)
from loop_manager_internal.storage.requirement_judgment_state import Judgment
from loop_manager_internal.application.contracts import ValidatedPlanningResult
from loop_manager_internal.planning.planner_publication import (
    ApplyGeneralPlanningBatchEffects, _apply_general_planning_batch,
    PlanningResultManifestEffects, PlanningResultFromManifestEffects,
    ValidateGeneralPlanningCommitEffects, _planning_result_manifest,
    _planning_result_from_manifest, _validate_general_planning_commit,
)
from loop_manager_internal.planning.planning_catalog import (
    MutableReadySnapshot, PlannerValidationError, mutable_snapshot_token,
)
from loop_manager_internal.planning.planner_validation import validate_planner_result
from loop_manager_internal.notifications import work_exhaustion


SOURCE = "docs/requirements/[R-001] Feature.md"
ARCHIVE = "docs/requirements/archive/[R-001] Feature.md"
CONTENTS = b"# Feature\n\n## Acceptance Criteria\n\n- Works end to end\n"


class FakeGit:
    def __init__(self):
        self.files = {
            SOURCE: CONTENTS,
            "docs/PRODUCT.md": b"# Product\n",
            "docs/ARCHITECTURE.md": b"# Architecture\n",
            "feature.py": b"implemented = True\n",
        }
        self.blob_reads = 0
        self.index_resets = []

    def run(self, _repo, *args):
        if args[:1] != ("reset",) or args[2:] not in (("--", SOURCE, ARCHIVE), ("--", ARCHIVE, SOURCE)):
            raise AssertionError(args)
        self.index_resets.append(args)

    def run_bytes(self, _repo, command, *args):
        if command == "cat-file":
            self.blob_reads += 1
            oid = args[-1]
            return next(value for value in self.files.values()
                        if hashlib.sha1(b"blob " + str(len(value)).encode() + b"\0" + value).hexdigest() == oid)
        if command != "ls-tree":
            raise AssertionError(command)
        paths = args[args.index("--") + 1:]
        entries = []
        for path, value in self.files.items():
            if not any(path == item or path.startswith(item + "/") for item in paths):
                continue
            oid = hashlib.sha1(b"blob " + str(len(value)).encode() + b"\0" + value).hexdigest()
            entries.append(f"100644 blob {oid}\t{path}".encode() + b"\0")
        return b"".join(entries)


class RequirementRetirementTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.executescript("""
            CREATE TABLE projects(repo TEXT PRIMARY KEY);
            CREATE TABLE exec_plans(repo TEXT, plan_id TEXT, path TEXT,
                sources TEXT, content_hash TEXT, status TEXT, pr_head_oid TEXT,
                publication_remote_oid TEXT, verify_token TEXT, review_token TEXT,
                verify_result TEXT, review_result TEXT);
            CREATE TABLE execplan_cancellations(repo TEXT, plan_id TEXT, sources TEXT);
            CREATE TABLE document_drafts(repo TEXT, target_path TEXT, state TEXT);
            INSERT INTO projects VALUES ('repo');
        """)
        self.git = FakeGit()
        self.port = RequirementRetirementPort(self.connection, self.git)

    def add_plan(self, plan_id="G-001", status="MERGED", source=SOURCE):
        self.connection.execute(
            "INSERT INTO exec_plans VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("repo", plan_id, f"docs/plans/{plan_id}.md", json.dumps([source]),
             "plan-content", status, "pr-head", "merge-confirmed", "verify", "review",
             "verify passed", "review passed"),
        )

    def judgment_line(self, item, outcome="fulfilled", paths=None):
        return "REQUIREMENT_JUDGMENTS: " + json.dumps([{
            "path": item.path, "input_token": item.token, "outcome": outcome,
            "criteria_evidence": ["feature.py and merged validation receipt"],
            "relevant_paths": paths if paths is not None else ["feature.py", "docs/PRODUCT.md", "docs/ARCHITECTURE.md"],
        }])

    def test_final_merge_defers_then_reuses_judgment_and_preserves_bytes(self):
        self.add_plan(status="PR_OPEN")
        self.assertEqual(self.port.pending("repo", self.root, "base"), ())
        self.connection.execute("UPDATE exec_plans SET status = 'MERGED'")
        (item,) = self.port.pending("repo", self.root, "base")
        self.assertEqual(item.criteria, ("Works end to end",))
        judgments = self.port.validate_result(
            "repo", self.root, "base", self.judgment_line(item),
        )
        self.port.record("repo", judgments)
        self.assertEqual(self.port.pending("repo", self.root, "base"), ())
        origin = self.root / SOURCE
        origin.parent.mkdir(parents=True)
        origin.write_bytes(CONTENTS)
        self.assertEqual(self.port.stage_moves("repo", self.root, "base", self.root),
                         (SOURCE, ARCHIVE))
        self.assertEqual((self.root / ARCHIVE).read_bytes(), CONTENTS)
        self.assertEqual(self.port.receipted_paths("repo", self.root, "base", self.root),
                         (SOURCE, ARCHIVE))
        self.assertEqual(self.port.stage_moves("repo", self.root, "base", self.root),
                         (SOURCE, ARCHIVE))

    def test_unrelated_change_reuses_but_related_change_invalidates(self):
        self.add_plan()
        (item,) = self.port.pending("repo", self.root, "base")
        self.port.record("repo", self.port.validate_result(
            "repo", self.root, "base", self.judgment_line(item, "unfulfilled"),
        ))
        self.git.files["unrelated.py"] = b"new\n"
        self.assertEqual(self.port.pending("repo", self.root, "base"), ())
        self.git.files["feature.py"] = b"implemented = False\n"
        self.assertEqual(len(self.port.pending("repo", self.root, "base")), 1)
        self.assertIsNotNone(self.port.judgments.get("repo", SOURCE).invalidation_reason)

    def test_requirement_and_related_specification_change_invalidate_cached_result(self):
        for changed_path, contents in (
            (SOURCE, CONTENTS + b"\nMore detail\n"),
            ("docs/PRODUCT.md", b"# Product with revised rule\n"),
        ):
            with self.subTest(changed_path=changed_path):
                self.connection.execute("DELETE FROM requirement_judgments")
                self.connection.execute("DELETE FROM exec_plans")
                self.git.files[SOURCE] = CONTENTS
                self.git.files["docs/PRODUCT.md"] = b"# Product\n"
                self.add_plan()
                (item,) = self.port.pending("repo", self.root, "base")
                self.port.record("repo", self.port.validate_result(
                    "repo", self.root, "base", self.judgment_line(item, "unfulfilled"),
                ))
                self.git.files[changed_path] = contents
                self.assertEqual(len(self.port.pending("repo", self.root, "base")), 1)

    def test_specification_sections_reuse_unrelated_edits_and_invalidate_related_edits(self):
        self.add_plan()
        for name in ("docs/PRODUCT.md", "docs/ARCHITECTURE.md"):
            self.git.files[name] = b"# Document\n## Feature\nRequired rule\n## Other\nOther rule\n"
        (item,) = self.port.pending("repo", self.root, "base")
        payload = json.loads(self.judgment_line(item).split(": ", 1)[1])
        payload[0]["relevant_paths"] = [
            "feature.py", "docs/PRODUCT.md### Feature", "docs/ARCHITECTURE.md### Feature",
        ]
        self.port.record("repo", self.port.validate_result(
            "repo", self.root, "base", "REQUIREMENT_JUDGMENTS: " + json.dumps(payload),
        ))
        for name in ("docs/PRODUCT.md", "docs/ARCHITECTURE.md"):
            original = self.git.files[name]
            self.git.files[name] = original.replace(b"Other rule", b"Unrelated revision")
            self.assertEqual(self.port.pending("repo", self.root, "advanced"), ())
            self.git.files[name] = original.replace(b"Required rule", b"Changed requirement")
            self.assertEqual(self.port.fulfilled_moves("repo", self.root, "advanced"), ())
            self.git.files[name] = original
        self.assertEqual(self.port.pending("repo", self.root, "advanced"), ())

    def test_product_only_evidence_does_not_track_unrelated_architecture(self):
        self.add_plan()
        self.git.files["docs/PRODUCT.md"] = b"## Feature\nRule\n"
        (item,) = self.port.pending("repo", self.root, "base")
        line = self.judgment_line(item, paths=[
            "feature.py", "docs/PRODUCT.md### Feature",
            "docs/ARCHITECTURE.md#unrelated: no architecture constraint applies to this criterion",
        ])
        self.port.record("repo", self.port.validate_result("repo", self.root, "base", line))
        self.git.files["docs/ARCHITECTURE.md"] = b"Unrelated architectural change\n"
        restarted = RequirementRetirementPort(self.connection, self.git)
        self.assertEqual(restarted.pending("repo", self.root, "advanced"), ())
        self.assertEqual(restarted.fulfilled_moves("repo", self.root, "advanced"), (SOURCE,))
        self.git.files["docs/PRODUCT.md"] = b"## Feature\nChanged rule\n"
        self.assertEqual(len(restarted.pending("repo", self.root, "later")), 1)

    def test_fulfilled_result_rejects_omitted_specification_evidence(self):
        self.add_plan()
        (item,) = self.port.pending("repo", self.root, "base")
        for paths in (["feature.py"], ["feature.py", "docs/PRODUCT.md"]):
            with self.subTest(paths=paths), self.assertRaises(RetirementError):
                self.port.validate_result("repo", self.root, "base", self.judgment_line(item, paths=paths))
        self.assertIsNone(self.port.judgments.get("repo", SOURCE))
        self.assertEqual(self.port.fulfilled_moves("repo", self.root, "base"), ())

    def test_invalidated_staged_restoration_keeps_receipt_until_safe_withdrawal(self):
        self.git.files.pop(SOURCE)
        self.git.files[ARCHIVE] = CONTENTS
        self.add_plan(status="PR_OPEN")
        archived = self.root / ARCHIVE
        archived.parent.mkdir(parents=True)
        archived.write_bytes(CONTENTS)
        self.port.stage_restorations("repo", self.root, "base", self.root)
        self.connection.execute("UPDATE exec_plans SET status = 'MERGED'")
        self.assertEqual(self.port.audit_restorations("repo", self.root, "advanced"), ())
        self.assertEqual(self.connection.execute("SELECT stage FROM requirement_audit").fetchone()[0], "staged")
        restarted = RequirementRetirementPort(self.connection, self.git)
        with self.assertRaises(RetirementReassessment):
            restarted.receipted_paths("repo", self.root, "advanced", self.root)
        self.assertEqual(archived.read_bytes(), CONTENTS)
        self.assertFalse((self.root / SOURCE).exists())
        self.assertEqual(restarted.receipted_paths("repo", self.root, "advanced", self.root), ())
        self.assertEqual(len(restarted.audit_pending("repo", self.root, "advanced")), 1)
        self.assertEqual(self.git.index_resets, [("reset", "advanced", "--", ARCHIVE, SOURCE)])

    def test_restoration_withdrawal_resumes_after_inverse_rename_interruption(self):
        self.git.files.pop(SOURCE)
        self.git.files[ARCHIVE] = CONTENTS
        self.add_plan(status="PR_OPEN")
        archived = self.root / ARCHIVE
        archived.parent.mkdir(parents=True)
        archived.write_bytes(CONTENTS)
        self.port.stage_restorations("repo", self.root, "base", self.root)
        self.connection.execute("UPDATE exec_plans SET status = 'MERGED'")
        with mock.patch.object(self.git, "run", side_effect=RuntimeError("interrupted index reset")):
            with self.assertRaises(RuntimeError):
                self.port.receipted_paths("repo", self.root, "advanced", self.root)
        self.assertEqual(archived.read_bytes(), CONTENTS)
        self.assertEqual(self.connection.execute("SELECT stage FROM requirement_audit").fetchone()[0], "staged")
        restarted = RequirementRetirementPort(self.connection, self.git)
        with self.assertRaises(RetirementReassessment):
            restarted.receipted_paths("repo", self.root, "advanced", self.root)
        self.assertEqual(restarted.receipted_paths("repo", self.root, "advanced", self.root), ())
        self.assertEqual(len(restarted.audit_pending("repo", self.root, "advanced")), 1)

    def test_invalidated_restoration_preserves_local_edits_and_receipt(self):
        self.git.files.pop(SOURCE)
        self.git.files[ARCHIVE] = CONTENTS
        self.add_plan(status="PR_OPEN")
        archived = self.root / ARCHIVE
        archived.parent.mkdir(parents=True)
        archived.write_bytes(CONTENTS)
        self.port.stage_restorations("repo", self.root, "base", self.root)
        (self.root / SOURCE).write_bytes(b"user edit")
        self.connection.execute("UPDATE exec_plans SET status = 'MERGED'")
        with self.assertRaises(RetirementError):
            self.port.receipted_paths("repo", self.root, "advanced", self.root)
        self.assertEqual((self.root / SOURCE).read_bytes(), b"user edit")
        self.assertEqual(self.connection.execute("SELECT stage FROM requirement_audit").fetchone()[0], "staged")

    def test_invalidated_move_withdrawal_preserves_unrelated_files_and_reassesses(self):
        self.add_plan()
        (item,) = self.port.pending("repo", self.root, "base")
        self.port.record("repo", self.port.validate_result(
            "repo", self.root, "base", self.judgment_line(item),
        ))
        origin = self.root / SOURCE
        origin.parent.mkdir(parents=True)
        origin.write_bytes(CONTENTS)
        other = self.root / "other.txt"
        other.write_bytes(b"keep local work")
        self.port.stage_moves("repo", self.root, "base", self.root)
        self.git.files["feature.py"] = b"Changed implementation"
        with self.assertRaises(RetirementReassessment):
            self.port.receipted_paths("repo", self.root, "advanced", self.root)
        restarted = RequirementRetirementPort(self.connection, self.git)
        self.assertEqual(restarted.receipted_paths("repo", self.root, "advanced", self.root), ())
        self.assertEqual(origin.read_bytes(), CONTENTS)
        self.assertEqual(self.git.index_resets, [("reset", "advanced", "--", SOURCE, ARCHIVE)])
        self.assertEqual(other.read_bytes(), b"keep local work")
        (fresh,) = restarted.pending("repo", self.root, "advanced")
        restarted.record("repo", restarted.validate_result(
            "repo", self.root, "advanced", self.judgment_line(fresh),
        ))
        self.assertEqual(restarted.stage_moves("repo", self.root, "advanced", self.root),
                         (SOURCE, ARCHIVE))

    def test_invalidated_move_does_not_overwrite_modified_archive(self):
        self.add_plan()
        (item,) = self.port.pending("repo", self.root, "base")
        self.port.record("repo", self.port.validate_result(
            "repo", self.root, "base", self.judgment_line(item),
        ))
        origin = self.root / SOURCE
        origin.parent.mkdir(parents=True)
        origin.write_bytes(CONTENTS)
        self.port.stage_moves("repo", self.root, "base", self.root)
        (self.root / ARCHIVE).write_bytes(b"local edit")
        self.git.files["feature.py"] = b"changed"
        with self.assertRaises(RetirementError):
            self.port.receipted_paths("repo", self.root, "advanced", self.root)
        self.assertEqual((self.root / ARCHIVE).read_bytes(), b"local edit")
        self.assertEqual(self.connection.execute(
            "SELECT count(*) FROM requirement_move_receipts",
        ).fetchone()[0], 1)

    def test_missing_or_ambiguous_specification_section_cannot_prove_fulfillment(self):
        self.add_plan()
        (item,) = self.port.pending("repo", self.root, "base")
        payload = json.loads(self.judgment_line(item).split(": ", 1)[1])
        payload[0]["relevant_paths"].append("docs/PRODUCT.md### Feature")
        for contents in (b"# Product\n", b"## Feature\nOne\n## Feature\nTwo\n"):
            self.git.files["docs/PRODUCT.md"] = contents
            (current,) = self.port.pending("repo", self.root, "base")
            payload[0]["input_token"] = current.token
            with self.assertRaises(RetirementError):
                self.port.validate_result(
                    "repo", self.root, "base", "REQUIREMENT_JUDGMENTS: " + json.dumps(payload),
                )

    def test_staged_retirement_rebinds_receipt_after_unrelated_base_advance(self):
        self.add_plan()
        (item,) = self.port.pending("repo", self.root, "base")
        line = self.judgment_line(item)
        self.port.record("repo", self.port.validate_result("repo", self.root, "base", line))
        origin = self.root / SOURCE
        origin.parent.mkdir(parents=True)
        origin.write_bytes(CONTENTS)
        self.port.stage_moves("repo", self.root, "base", self.root)
        self.git.files["unrelated.py"] = b"new"
        self.assertEqual(self.port.receipted_paths("repo", self.root, "advanced", self.root),
                         (SOURCE, ARCHIVE))
        self.port.validate_result("repo", self.root, "advanced", line)
        self.port.stage_moves("repo", self.root, "advanced", self.root)
        row = self.connection.execute("SELECT base_sha FROM requirement_move_receipts").fetchone()
        self.assertEqual(row[0], "advanced")
        self.assertEqual((self.root / ARCHIVE).read_bytes(), CONTENTS)
        self.git.files["feature.py"] = b"changed"
        with self.assertRaises(RetirementError):
            self.port.receipted_paths("repo", self.root, "later", self.root)

    def test_staged_restoration_rebinds_receipt_after_unrelated_base_advance(self):
        self.git.files.pop(SOURCE)
        self.git.files[ARCHIVE] = CONTENTS
        self.add_plan(status="PR_OPEN")
        archived = self.root / ARCHIVE
        archived.parent.mkdir(parents=True)
        archived.write_bytes(CONTENTS)
        self.port.stage_restorations("repo", self.root, "base", self.root)
        self.git.files["unrelated.py"] = b"new"
        self.assertEqual(self.port.receipted_paths("repo", self.root, "advanced", self.root),
                         (ARCHIVE, SOURCE))
        self.port.stage_restorations("repo", self.root, "advanced", self.root)
        row = self.connection.execute("SELECT observed_sha FROM requirement_audit").fetchone()
        self.assertEqual(row[0], "advanced")
        self.assertEqual((self.root / SOURCE).read_bytes(), CONTENTS)

    def test_uncertain_judgment_exposes_decision_and_resumes_on_requirement_edit(self):
        self.add_plan()
        self.connection.execute("ALTER TABLE projects ADD COLUMN planning_error TEXT")
        for column in ("title", "depends_on", "pr_url"):
            self.connection.execute(f"ALTER TABLE exec_plans ADD COLUMN {column} TEXT")
        (item,) = self.port.pending("repo", self.root, "base")
        self.port.record("repo", self.port.validate_result(
            "repo", self.root, "base", self.judgment_line(item, "uncertain"),
        ))
        self.assertEqual(self.port.pending("repo", self.root, "base"), ())
        snapshot = work_exhaustion.collect(
            self.connection, "repo", cycle="cycle", active_requirements=(SOURCE,),
        )
        self.assertFalse(snapshot.completed)
        reason = snapshot.requirements[0]["reason"]
        self.assertIn(SOURCE, reason)
        self.assertIn("feature.py and merged validation receipt", reason)
        self.assertIn("수정하면", reason)
        self.git.files[SOURCE] += b"\nUser decision clarifies the criterion.\n"
        self.assertEqual(len(self.port.pending("repo", self.root, "advanced")), 1)
        self.assertEqual(self.port.fulfilled_moves("repo", self.root, "advanced"), ())

    def test_staged_move_refuses_changed_catalog_relationship_on_resume(self):
        self.add_plan()
        (item,) = self.port.pending("repo", self.root, "base")
        self.port.record("repo", self.port.validate_result(
            "repo", self.root, "base", self.judgment_line(item),
        ))
        origin = self.root / SOURCE
        origin.parent.mkdir(parents=True)
        origin.write_bytes(CONTENTS)
        self.port.stage_moves("repo", self.root, "base", self.root)
        self.connection.execute("UPDATE exec_plans SET status = 'PR_OPEN'")

        with self.assertRaises(RetirementError):
            self.port.receipted_paths("repo", self.root, "base", self.root)
        self.assertEqual((self.root / SOURCE).read_bytes(), CONTENTS)
        self.assertFalse((self.root / ARCHIVE).exists())
        self.assertEqual(self.port.receipted_paths("repo", self.root, "base", self.root), ())
        self.assertEqual(self.port.pending("repo", self.root, "base"), ())
        self.connection.execute("UPDATE exec_plans SET status = 'MERGED'")
        self.assertEqual(len(self.port.pending("repo", self.root, "base")), 1)

    def test_manager_retirement_is_not_reaudited_as_historical_archive(self):
        self.add_plan()
        (item,) = self.port.pending("repo", self.root, "base")
        self.port.record("repo", self.port.validate_result(
            "repo", self.root, "base", self.judgment_line(item),
        ))
        origin = self.root / SOURCE
        origin.parent.mkdir(parents=True)
        origin.write_bytes(CONTENTS)
        self.port.stage_moves("repo", self.root, "base", self.root)
        self.connection.execute(
            "UPDATE requirement_move_receipts SET stage = 'published' WHERE repo = 'repo'",
        )
        self.git.files.pop(SOURCE)
        self.git.files[ARCHIVE] = CONTENTS
        reads_before_audit = self.git.blob_reads

        self.assertEqual(self.port.audit_pending("repo", self.root, "published"), ())
        self.assertEqual(self.git.blob_reads, reads_before_audit)

    def test_cancelled_source_does_not_become_merged_proof(self):
        self.add_plan()
        self.connection.execute(
            "INSERT INTO execplan_cancellations VALUES (?, ?, ?)",
            ("repo", "G-002", json.dumps([SOURCE])),
        )
        self.assertEqual(self.port.pending("repo", self.root, "base"), ())

    def test_duplicate_final_judgments_are_rejected(self):
        self.add_plan()
        (item,) = self.port.pending("repo", self.root, "base")
        line = self.judgment_line(item)

        with self.assertRaises(RetirementError):
            self.port.validate_result("repo", self.root, "base", line + "\n" + line)

    def test_fulfilled_judgment_requires_existing_regular_evidence(self):
        self.add_plan()
        (item,) = self.port.pending("repo", self.root, "base")
        line = self.judgment_line(item).replace("feature.py", "missing.py")

        with self.assertRaises(RetirementError):
            self.port.validate_result("repo", self.root, "base", line)

    def test_collision_never_overwrites_archive(self):
        self.add_plan()
        (item,) = self.port.pending("repo", self.root, "base")
        self.port.record("repo", self.port.validate_result(
            "repo", self.root, "base", self.judgment_line(item),
        ))
        origin = self.root / SOURCE
        target = self.root / ARCHIVE
        target.parent.mkdir(parents=True)
        origin.write_bytes(CONTENTS)
        target.write_bytes(b"unrelated")
        with self.assertRaises(RetirementError):
            self.port.stage_moves("repo", self.root, "base", self.root)
        self.assertEqual(target.read_bytes(), b"unrelated")

    def test_archive_collision_does_not_block_unrelated_final_requirement(self):
        second = "docs/requirements/[R-002] Other.md"
        self.git.files[second] = CONTENTS
        self.git.files[ARCHIVE] = b"historical version"
        self.add_plan()
        self.add_plan("G-002", source=second)

        pending = self.port.pending("repo", self.root, "base")

        self.assertEqual(tuple(item.path for item in pending), (second,))
        self.assertEqual(self.port.audit_restorations("repo", self.root, "base"), ())
        row = self.connection.execute(
            "SELECT outcome FROM requirement_audit WHERE source_path = ?", (SOURCE,),
        ).fetchone()
        self.assertEqual(row["outcome"], "uncertain")

    def test_malformed_requirement_keeps_uncertain_result_without_blocking_other_input(self):
        second = "docs/requirements/[R-002] Other.md"
        self.git.files[SOURCE] = b"# Missing criteria\n"
        self.git.files[second] = CONTENTS
        self.add_plan()
        self.add_plan("G-002", source=second)

        self.assertEqual(tuple(item.path for item in self.port.pending("repo", self.root, "base")),
                         (second,))
        reads = self.git.blob_reads
        self.assertEqual(tuple(item.path for item in self.port.pending("repo", self.root, "base")),
                         (second,))
        self.assertEqual(self.git.blob_reads, reads + 1)
        self.assertEqual(self.port.judgments.get("repo", SOURCE).outcome, "uncertain")

    def test_audit_restores_only_proven_unfinished_source_and_resumes(self):
        self.git.files.pop(SOURCE)
        self.git.files[ARCHIVE] = CONTENTS
        self.add_plan(status="PR_OPEN")
        self.assertEqual(self.port.audit_restorations("repo", self.root, "base"), (SOURCE,))
        self.assertEqual(self.git.blob_reads, 0)
        self.assertEqual(self.port.audit_restorations("repo", self.root, "base"), (SOURCE,))
        self.assertEqual(self.git.blob_reads, 0)
        archived = self.root / ARCHIVE
        archived.parent.mkdir(parents=True)
        archived.write_bytes(CONTENTS)
        self.assertEqual(self.port.stage_restorations("repo", self.root, "base", self.root),
                         (ARCHIVE, SOURCE))
        self.assertEqual((self.root / SOURCE).read_bytes(), CONTENTS)
        self.assertEqual(self.port.receipted_paths("repo", self.root, "base", self.root),
                         (ARCHIVE, SOURCE))
        self.assertEqual(self.port.stage_restorations("repo", self.root, "base", self.root),
                         (ARCHIVE, SOURCE))

    def test_audit_asks_planner_once_for_all_merged_partial_coverage(self):
        self.git.files.pop(SOURCE)
        self.git.files[ARCHIVE] = CONTENTS
        self.add_plan()
        self.port.judgments.put("repo", Judgment(
            SOURCE, "old-fulfilled", "fulfilled", ("old evidence",), ("feature.py",), "old",
        ))
        self.assertEqual(self.port.audit_restorations("repo", self.root, "base"), ())
        self.assertEqual(self.git.blob_reads, 1)
        (item,) = self.port.audit_pending("repo", self.root, "base")
        self.assertEqual(item.criteria, ("Works end to end",))
        self.assertEqual(self.port.audit_restorations("repo", self.root, "base"), ())
        self.assertEqual(self.git.blob_reads, 1)
        self.port.record_audit_result("repo", self.root, "base", "REQUIREMENT_AUDIT: " + json.dumps([{
            "path": SOURCE, "input_token": item.token, "outcome": "premature",
            "criteria_evidence": ["Merged code lacks the required behavior"],
            "relevant_paths": ["feature.py"], "reason": "Partial coverage remains",
        }]))
        self.assertEqual(self.port.audit_pending("repo", self.root, "base"), ())
        self.assertEqual(self.port.audit_restorations("repo", self.root, "base"), (SOURCE,))
        self.assertEqual(self.git.blob_reads, 1)
        self.assertIsNotNone(self.port.judgments.get("repo", SOURCE).invalidation_reason)
        row = self.connection.execute(
            "SELECT outcome, reason FROM requirement_audit WHERE source_path = ?",
            (SOURCE,),
        ).fetchone()
        self.assertEqual(row["outcome"], "restore")
        self.git.files["feature.py"] = b"implemented = False\n"
        self.assertEqual(len(self.port.audit_pending("repo", self.root, "base")), 1)

    def test_audit_preserves_superseded_goal_and_cancellation_uncertainty(self):
        self.git.files.pop(SOURCE)
        self.git.files[ARCHIVE] = CONTENTS
        self.add_plan()
        (item,) = self.port.audit_pending("repo", self.root, "base")
        self.port.record_audit_result("repo", self.root, "base", "REQUIREMENT_AUDIT: " + json.dumps([{
            "path": SOURCE, "input_token": item.token, "outcome": "superseded",
            "criteria_evidence": ["Product replacement decision covers the former goal"],
            "relevant_paths": ["feature.py"], "reason": "Goal was replaced",
        }]))
        self.assertEqual(self.port.audit_restorations("repo", self.root, "base"), ())
        self.assertEqual(self.port.audit_pending("repo", self.root, "base"), ())
        row = self.connection.execute(
            "SELECT outcome FROM requirement_audit WHERE source_path = ?", (SOURCE,),
        ).fetchone()
        self.assertEqual(row["outcome"], "superseded")
        self.connection.execute("UPDATE exec_plans SET status = 'PR_OPEN'")
        self.connection.execute(
            "INSERT INTO execplan_cancellations VALUES (?, ?, ?)",
            ("repo", "G-002", json.dumps([SOURCE])),
        )
        self.assertEqual(self.port.audit_restorations("repo", self.root, "base"), ())
        row = self.connection.execute(
            "SELECT outcome, reason FROM requirement_audit WHERE source_path = ?",
            (SOURCE,),
        ).fetchone()
        self.assertEqual(row["outcome"], "uncertain")
        self.assertIn("cancelled", row["reason"])

    def test_audit_rejects_missing_criterion_evidence(self):
        self.git.files.pop(SOURCE)
        self.git.files[ARCHIVE] = CONTENTS
        self.add_plan()
        (item,) = self.port.audit_pending("repo", self.root, "base")
        with self.assertRaises(RetirementError):
            self.port.record_audit_result("repo", self.root, "base", "REQUIREMENT_AUDIT: " + json.dumps([{
                "path": SOURCE, "input_token": item.token, "outcome": "premature",
                "criteria_evidence": [], "relevant_paths": ["feature.py"], "reason": "Partial",
            }]))
        self.assertEqual(len(self.port.audit_pending("repo", self.root, "base")), 1)

    def test_audit_keeps_malformed_historical_criteria_uncertain_without_rereading(self):
        self.git.files.pop(SOURCE)
        self.git.files[ARCHIVE] = b"# Former goal without criteria\n"
        self.add_plan()

        self.assertEqual(self.port.audit_pending("repo", self.root, "base"), ())
        self.assertEqual(self.port.audit_pending("repo", self.root, "base"), ())
        self.assertEqual(self.git.blob_reads, 1)
        row = self.connection.execute(
            "SELECT outcome, reason FROM requirement_audit WHERE source_path = ?", (SOURCE,),
        ).fetchone()
        self.assertEqual(row["outcome"], "uncertain")
        self.assertIn("Acceptance Criteria", row["reason"])


class RetirementPublicationManifestTest(unittest.TestCase):
    def test_withdrawn_move_uses_existing_planner_validation_retry(self):
        from loop_manager_internal.planning.assessment_service import PlanningAssessmentService
        from loop_manager_internal.planning.planning_contracts import PlanningOutcomeKind
        ports = SimpleNamespace(
            retirement=SimpleNamespace(receipted_paths=mock.Mock(
                side_effect=RetirementReassessment("reassess current evidence"))),
            state=SimpleNamespace(update=mock.Mock()), events=SimpleNamespace(error=mock.Mock()),
        )
        service = PlanningAssessmentService(ports, mock.Mock())
        row = SimpleNamespace(phase="PLANNING", active_sha="advanced", planning_retry_count=0,
                              planning_result_pending_validation=0, planning_validation_error=None)
        run = SimpleNamespace(row=row, context=SimpleNamespace(repo="repo", repo_path=Path("/repo")),
                              planning_worktree=Path("/worktree"), dirty_files=[], planner_scope_error=None,
                              maintenance_planning=False, mutable_ready_snapshot=object())
        outcome = service._validate_planner_result(run)
        self.assertEqual(outcome.kind, PlanningOutcomeKind.VALIDATION_RETRY)
        self.assertEqual(ports.state.update.call_args.kwargs["planning_result_completed"], 0)
        self.assertEqual(ports.state.update.call_args.kwargs["planning_result_pending_validation"], 0)


    def test_remote_confirmed_move_marks_receipt_and_audit_together(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.executescript("""
            CREATE TABLE requirement_move_receipts(
                repo TEXT, source_path TEXT, base_sha TEXT, stage TEXT, updated_at TEXT);
            CREATE TABLE requirement_audit(
                repo TEXT, source_path TEXT, stage TEXT, updated_at TEXT);
            INSERT INTO requirement_move_receipts VALUES
                ('repo', 'docs/requirements/[R-001] Feature.md', 'base', 'pending', NULL);
            INSERT INTO requirement_audit(repo, source_path, stage) VALUES
                ('repo', 'docs/requirements/[R-001] Feature.md', 'observed');
        """)
        snapshot = MutableReadySnapshot("repo", "base", (),
                                        mutable_snapshot_token("repo", "base", ()))
        result = ValidatedPlanningResult(
            (), (SOURCE, ARCHIVE), mutable_snapshot=snapshot,
            retirement_moves=(SOURCE,),
        )
        intent = SimpleNamespace(
            stage="prepared", base_sha="base", commit_sha="commit",
            snapshot_token=snapshot.token, specification_fingerprint="spec",
            planning_fingerprint="planning",
        )
        effects = ApplyGeneralPlanningBatchEffects(
            _assert_mutable_ready_snapshot_current=lambda *_args: None,
            _clear_active_in_current_transaction=lambda *_args, **_kwargs: None,
            _general_planning_batch_already_applied=lambda *_args: False,
            _register_execplan_rows=lambda *_args: None,
            file_hash=lambda *_args: "hash",
            general_planning_publication=lambda *_args: intent,
            is_loop_manager_repository=lambda *_args: False,
            plan_declarations=lambda *_args: (),
        )
        module = "loop_manager_internal.planning.planner_publication"
        with (mock.patch(module + ".planning_publication_state.apply_mutable_catalog_changes"),
              mock.patch(module + ".planning_publication_state.delete_general_planning_publication"),
              mock.patch(module + ".planning_input_state.record_general")):
            _apply_general_planning_batch(
                effects, connection, "repo", Path("/unused"), result,
                "commit", "spec", "planning",
            )
        stages = connection.execute(
            "SELECT (SELECT stage FROM requirement_move_receipts), "
            "(SELECT stage FROM requirement_audit)",
        ).fetchone()
        self.assertEqual(stages, ("published", "published"))

    def test_planner_scope_uses_only_proven_manager_move_paths(self):
        effects = SimpleNamespace(
            changed_files=lambda *_args: [SOURCE, ARCHIVE],
            mutable_snapshot_token=mutable_snapshot_token,
        )
        self.assertIsNone(validate_planner_result(
            effects, Path("/unused"), "base", [], manager_paths=(SOURCE, ARCHIVE),
        ))
        with self.assertRaises(PlannerValidationError):
            validate_planner_result(effects, Path("/unused"), "base", [])

    def test_move_only_manifest_retains_manager_receipts(self):
        snapshot = MutableReadySnapshot("repo", "base", (),
                                        mutable_snapshot_token("repo", "base", ()))
        result = ValidatedPlanningResult(
            (), (SOURCE, ARCHIVE), ((ARCHIVE, "content-hash"),),
            mutable_snapshot=snapshot, retirement_moves=(SOURCE,),
        )
        manifest = _planning_result_manifest(
            PlanningResultManifestEffects(snapshot_to_dict=lambda value: value),
            result,
        )
        restored = _planning_result_from_manifest(
            PlanningResultFromManifestEffects(
                mutable_snapshot_token=mutable_snapshot_token,
                snapshot_from_dict=lambda value: value,
            ), manifest,
        )
        self.assertEqual(restored.retirement_moves, (SOURCE,))
        self.assertEqual(restored.publication_paths, (SOURCE, ARCHIVE))

    def test_commit_validation_requires_original_requirement_bytes(self):
        snapshot = MutableReadySnapshot("repo", "base", (),
                                        mutable_snapshot_token("repo", "base", ()))
        result = ValidatedPlanningResult(
            (), (SOURCE, ARCHIVE),
            ((ARCHIVE, hashlib.sha256(CONTENTS).hexdigest()),),
            mutable_snapshot=snapshot, retirement_moves=(SOURCE,),
        )
        blobs = {"base:" + SOURCE: CONTENTS, "commit:" + ARCHIVE: CONTENTS}

        def git(_worktree, *args, **kwargs):
            if args[:2] == ("rev-parse", "HEAD"):
                return SimpleNamespace(stdout="commit\n")
            if args[:2] == ("rev-list", "--parents"):
                return SimpleNamespace(stdout="commit base\n")
            if args[0] == "diff":
                return SimpleNamespace(stdout=f"{SOURCE}\n{ARCHIVE}\n")
            if args[:2] == ("cat-file", "-e"):
                return SimpleNamespace(returncode=0 if args[2] in blobs else 1)
            raise AssertionError(args)

        def git_bytes(_worktree, operation, spec):
            self.assertEqual(operation, "show")
            return blobs[spec]

        effects = ValidateGeneralPlanningCommitEffects(git=git, git_bytes=git_bytes)
        _validate_general_planning_commit(effects, Path("/unused"), result, "commit")
        blobs["commit:" + ARCHIVE] = b"changed"
        with self.assertRaises(PlannerValidationError):
            _validate_general_planning_commit(effects, Path("/unused"), result, "commit")


class RequirementAuditWaitReasonTest(unittest.TestCase):
    def test_uncertain_archive_audit_prevents_false_project_completion(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.executescript("""
            CREATE TABLE projects(repo TEXT PRIMARY KEY, planning_error TEXT);
            CREATE TABLE exec_plans(repo TEXT, plan_id TEXT, title TEXT, status TEXT,
                depends_on TEXT, pr_url TEXT, sources TEXT);
            CREATE TABLE requirement_audit(repo TEXT, source_path TEXT,
                outcome TEXT, stage TEXT, reason TEXT);
            INSERT INTO projects VALUES ('repo', NULL);
        """)
        connection.execute(
            "INSERT INTO requirement_audit VALUES (?, ?, ?, ?, ?)",
            ("repo", SOURCE, "uncertain", "observed", "historical proof absent"),
        )

        snapshot = work_exhaustion.collect(
            connection, "repo", cycle="fingerprint", active_requirements=(),
        )

        self.assertFalse(snapshot.completed)
        self.assertEqual(snapshot.requirements[0]["path"], SOURCE)
        self.assertIn("사용자 결정", snapshot.requirements[0]["reason"])


if __name__ == "__main__":
    unittest.main()
