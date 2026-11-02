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
        self.revisions = {}
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
            return next(value for files in [self.files, *self.revisions.values()] for value in files.values()
                        if hashlib.sha1(b"blob " + str(len(value)).encode() + b"\0" + value).hexdigest() == oid)
        if command != "ls-tree":
            raise AssertionError(command)
        paths = args[args.index("--") + 1:]
        entries = []
        files = self.revisions.get(args[args.index("--") - 1], self.files)
        for path, value in files.items():
            if not any(path == item or path.startswith(item + "/") for item in paths):
                continue
            oid = hashlib.sha1(b"blob " + str(len(value)).encode() + b"\0" + value).hexdigest()
            entries.append(f"100644 blob {oid}\t{path}".encode() + b"\0")
        return b"".join(entries)


class RequirementRetirementTest(unittest.TestCase):
    def test_execplan_decision_hold_prevents_final_requirement_judgment(self):
        from loop_manager_internal.storage import execplan_draft_state
        self.add_plan()
        self.assertEqual(len(self.port.pending("repo", self.root, "base")), 1)
        execplan_draft_state.ensure_schema(self.connection)
        execplan_draft_state.observe(self.connection, "repo", "docs/plans/[G-001] Feature.md",
            {"id": "G-001", "sources": [SOURCE], "draft": True,
             "draft_question": "Question: choose storage", "draft_scope": "scope"}, "hash", "base", agent=True)
        self.assertEqual(self.port.pending("repo", self.root, "base"), ())

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

    def seed_legacy_restoration(self):
        """Seed a move already made by an older Manager, without enabling new restores."""
        oid = hashlib.sha1(b"blob " + str(len(CONTENTS)).encode() + b"\0" + CONTENTS).hexdigest()
        (self.root / SOURCE).parent.mkdir(parents=True, exist_ok=True)
        (self.root / ARCHIVE).rename(self.root / SOURCE)
        self.connection.execute(
            "INSERT INTO requirement_audit "
            "(repo,source_path,input_token,source_oid,observed_sha,outcome,reason,stage) "
            "VALUES ('repo',?,'legacy',?,'base','restore','old unfinished source','staged')",
            (SOURCE, oid),
        )

    def test_archived_requirement_is_not_reassessed_when_old_plan_or_implementation_changes(self):
        self.git.files.pop(SOURCE)
        self.git.files[ARCHIVE] = CONTENTS
        self.add_plan(status="PR_OPEN")
        before_reads = self.git.blob_reads
        for status in ("PR_OPEN", "MERGED", "PLAN_BLOCKED"):
            self.connection.execute("UPDATE exec_plans SET status=?", (status,))
            self.git.files["feature.py"] = status.encode()
            self.assertEqual(self.port.pending("repo", self.root, status), ())
            self.assertEqual(self.port.stage_moves("repo", self.root, status, self.root), ())
        self.assertEqual(self.git.blob_reads, before_reads)
        self.assertFalse((self.root / SOURCE).exists())
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM requirement_audit").fetchone()[0], 0)

    def test_no_linked_plan_requires_final_judgment_before_archiving_direct_implementation(self):
        (item,) = self.port.pending("repo", self.root, "base")
        self.assertEqual(item.related_ids, ())
        self.assertEqual(self.port.fulfilled_moves("repo", self.root, "base"), ())
        origin = self.root / SOURCE
        origin.parent.mkdir(parents=True)
        origin.write_bytes(CONTENTS)
        self.port.record("repo", self.port.validate_result(
            "repo", self.root, "base", self.judgment_line(item)))
        self.assertEqual(self.port.stage_moves("repo", self.root, "base", self.root), (SOURCE, ARCHIVE))
        self.assertEqual((self.root / ARCHIVE).read_bytes(), CONTENTS)

    def test_all_nonterminal_source_linked_plans_defer_final_judgment(self):
        self.add_plan()
        self.add_plan("G-002", status="READY")
        for status in ("READY", "PR_OPEN", "PLAN_BLOCKED"):
            self.connection.execute("UPDATE exec_plans SET status=? WHERE plan_id='G-002'", (status,))
            self.assertEqual(self.port.pending("repo", self.root, "base"), ())
            self.assertEqual(self.port.fulfilled_moves("repo", self.root, "base"), ())
        self.connection.execute("UPDATE exec_plans SET status='MERGED' WHERE plan_id='G-002'")
        self.assertEqual(len(self.port.pending("repo", self.root, "base")), 1)

    def test_renamed_requirement_keeps_old_source_relation_and_moves_current_path(self):
        renamed = "docs/requirements/[R-001] Renamed Feature.md"
        contents = (
            b"# [R-001] Renamed Feature\n\n"
            b"## Acceptance Criteria\n\n- Works end to end\n"
        )
        self.git.files.pop(SOURCE)
        self.git.files[renamed] = contents
        self.add_plan(status="READY", source=SOURCE)
        self.assertEqual(self.port.pending("repo", self.root, "base"), ())
        self.assertEqual(self.port.fulfilled_moves("repo", self.root, "base"), ())

        self.connection.execute("UPDATE exec_plans SET status='MERGED'")
        (item,) = self.port.pending("repo", self.root, "base")
        self.assertEqual(item.path, renamed)
        self.port.record("repo", self.port.validate_result(
            "repo", self.root, "base", self.judgment_line(item),
        ))
        (self.root / renamed).parent.mkdir(parents=True, exist_ok=True)
        (self.root / renamed).write_bytes(contents)
        archived = "docs/requirements/archive/[R-001] Renamed Feature.md"
        self.assertEqual(
            self.port.stage_moves("repo", self.root, "base", self.root),
            (renamed, archived),
        )

    def test_renamed_requirement_stays_live_for_manual_work_source(self):
        from loop_manager_internal.storage.manual_work_state import ManualWorkRepository

        renamed = "docs/requirements/[R-001] Renamed Feature.md"
        contents = b"# [R-001] Renamed Feature\n\n## Acceptance Criteria\n\n- Works\n"
        self.git.files.pop(SOURCE)
        self.git.files[renamed] = contents
        groups = ManualWorkRepository(self.connection)
        groups.create(
            "repo", "start", "manual/branch", (), "head", "base",
            members=("G-009",),
            documents=({"plan": {"sources": [SOURCE]}},),
        )
        self.assertEqual(self.port.pending("repo", self.root, "base"), ())

    def test_submit_requirement_is_resolved_at_source_revision_without_blocking_base_planning(self):
        from loop_manager_internal.storage.manual_work_state import ManualWorkRepository

        independent = "docs/requirements/[R-002] Independent.md"
        submitted = "docs/requirements/[R-001] Submitted.md"
        self.git.revisions["base"] = {
            independent: b"# [R-002] Independent\n\n## Acceptance Criteria\n\n- Works\n",
            "docs/PRODUCT.md": b"# Product\n",
            "docs/ARCHITECTURE.md": b"# Architecture\n",
            "feature.py": b"implemented = True\n",
        }
        self.git.revisions["submitted-head"] = {
            submitted: b"# [R-001] Submitted\n\n## Acceptance Criteria\n\n- Submitted\n",
        }
        ManualWorkRepository(self.connection).create(
            "repo", "submit", "user/feature", (submitted,), "submitted-head", "base",
            members=("G-009",),
        )
        self.add_plan("G-009", status="MANUAL_PENDING", source=submitted)

        (item,) = self.port.pending("repo", self.root, "base")
        self.assertEqual(item.path, independent)
        self.assertEqual(item.related_ids, ())

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

    def test_pending_judgment_writes_release_transaction_before_git_and_admission(self):
        for change in ('uncertain', 'invalidated'):
            with self.subTest(change=change):
                self.connection.execute('DELETE FROM requirement_judgments')
                self.git.files[SOURCE] = CONTENTS
                if change == 'invalidated':
                    (item,) = self.port.pending('repo', self.root, 'base')
                    self.port.record('repo', self.port.validate_result(
                        'repo', self.root, 'base', self.judgment_line(item, 'unfulfilled')))
                    self.git.files['feature.py'] = b'changed implementation'
                else:
                    self.git.files[SOURCE] = b'# Requirement without acceptance criteria\n'
                self.connection.commit()
                read = self.git.run_bytes
                def observe(*args):
                    self.assertFalse(self.connection.in_transaction)
                    return read(*args)
                with mock.patch.object(self.git, 'run_bytes', side_effect=observe):
                    pending = self.port.pending('repo', self.root, 'base')
                self.assertFalse(self.connection.in_transaction)
                saved = self.port.judgments.get('repo', SOURCE)
                if change == 'uncertain':
                    self.assertEqual(pending, ())
                    self.assertEqual(saved.outcome, 'uncertain')
                else:
                    self.assertEqual(len(pending), 1)
                    self.assertIsNotNone(saved.invalidation_reason)

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

    def test_specification_change_uses_general_impact_check_before_reusing_fulfillment(self):
        from loop_manager_internal.planning.requirement_retirement import assessment_prompt
        self.add_plan()
        self.git.files["docs/PRODUCT.md"] = b"## Feature\nRule\n## Other\nOther rule\n"
        self.git.revisions["base"] = dict(self.git.files)
        (item,) = self.port.pending("repo", self.root, "base")
        self.port.record("repo", self.port.validate_result("repo", self.root, "base", self.judgment_line(
            item, paths=["feature.py", "docs/PRODUCT.md### Feature", "docs/ARCHITECTURE.md"],
        )))
        self.git.files["docs/PRODUCT.md"] += b"\n## Unrelated feature\nIndependent rule\n"
        self.git.revisions["advanced"] = dict(self.git.files)
        (check,) = self.port.pending("repo", self.root, "advanced")
        self.assertIsNotNone(check.reuse_from)
        self.assertIn("outcome=reuse", assessment_prompt((check,)))
        self.assertEqual(self.port.fulfilled_moves("repo", self.root, "advanced"), ())
        prior = check.reuse_from
        result = "REQUIREMENT_JUDGMENTS: " + json.dumps([{
            "path": SOURCE, "input_token": check.token, "outcome": "reuse",
            "criteria_evidence": list(prior.criteria_evidence), "relevant_paths": list(prior.relevant_paths),
        }])
        self.port.record("repo", self.port.validate_result("repo", self.root, "advanced", result))
        restarted = RequirementRetirementPort(self.connection, self.git)
        self.assertEqual(restarted.pending("repo", self.root, "advanced"), ())
        self.assertEqual(restarted.fulfilled_moves("repo", self.root, "advanced"), (SOURCE,))
        self.git.files["feature.py"] = b"changed implementation"
        (changed,) = restarted.pending("repo", self.root, "later")
        self.assertIsNone(changed.reuse_from)
        with self.assertRaises(RetirementError):
            restarted.validate_result("repo", self.root, "later", result.replace(check.token, changed.token))

    def test_new_applicable_sibling_rule_blocks_section_bound_retirement(self):
        self.add_plan()
        self.git.files["docs/PRODUCT.md"] = b"## Feature\nRule\n## Other\nOther rule\n"
        self.git.revisions["base"] = dict(self.git.files)
        (item,) = self.port.pending("repo", self.root, "base")
        line = self.judgment_line(item, paths=["feature.py", "docs/PRODUCT.md### Feature", "docs/ARCHITECTURE.md"])
        self.port.record("repo", self.port.validate_result("repo", self.root, "base", line))
        for contents in (
            b"## Feature\nRule\n## Other\nOther rule\n## Completion\nFeature requires durable evidence.\n",
            b"## Feature\nRule\n## Other\nFeature requires durable evidence.\n",
        ):
            self.git.files["docs/PRODUCT.md"] = contents
            self.assertEqual(self.port.fulfilled_moves("repo", self.root, "advanced"), ())
            self.assertEqual(self.port.stage_moves("repo", self.root, "advanced", self.root), ())
            with self.assertRaises(RetirementError):
                self.port.validate_result("repo", self.root, "advanced", line)
            (check,) = self.port.pending("repo", self.root, "advanced")
            self.assertIsNotNone(check.reuse_from)
        self.port.record("repo", self.port.validate_result(
            "repo", self.root, "advanced", self.judgment_line(check, "unfulfilled"),
        ))
        self.assertEqual(self.port.fulfilled_moves("repo", self.root, "advanced"), ())
        self.assertEqual(self.port.pending("repo", self.root, "advanced"), ())

    def test_unrelated_declaration_is_pinned_and_new_applicable_rule_invalidates(self):
        self.add_plan()
        self.git.files["docs/PRODUCT.md"] = b"## Feature\nRule\n"
        (item,) = self.port.pending("repo", self.root, "base")
        line = self.judgment_line(item, paths=[
            "feature.py", "docs/PRODUCT.md### Feature",
            "docs/ARCHITECTURE.md#unrelated: no architecture constraint applies to this criterion",
        ])
        self.port.record("repo", self.port.validate_result("repo", self.root, "base", line))
        restarted = RequirementRetirementPort(self.connection, self.git)
        self.assertEqual(restarted.pending("repo", self.root, "advanced"), ())
        self.assertEqual(restarted.fulfilled_moves("repo", self.root, "advanced"), (SOURCE,))
        self.git.files["docs/ARCHITECTURE.md"] += b"\n## Feature\nFeature must persist validation receipts.\n"
        self.assertEqual(restarted.fulfilled_moves("repo", self.root, "later"), ())
        self.assertEqual(restarted.stage_moves("repo", self.root, "later", self.root), ())
        self.assertEqual(len(restarted.pending("repo", self.root, "later")), 1)
        with self.assertRaises(RetirementError):
            restarted.validate_result("repo", self.root, "later", line)
        self.assertIsNotNone(restarted.judgments.get("repo", SOURCE).invalidation_reason)

    def test_unrelated_declaration_cannot_prove_missing_specification(self):
        self.add_plan()
        (item,) = self.port.pending("repo", self.root, "base")
        line = self.judgment_line(item, paths=[
            "feature.py", "docs/PRODUCT.md",
            "docs/ARCHITECTURE.md#unrelated: no architecture constraints",
        ])
        del self.git.files["docs/ARCHITECTURE.md"]
        (current,) = self.port.pending("repo", self.root, "base")
        line = line.replace(item.token, current.token)
        with self.assertRaises(RetirementError):
            self.port.validate_result("repo", self.root, "base", line)

    def test_fulfilled_result_rejects_omitted_specification_evidence(self):
        self.add_plan()
        (item,) = self.port.pending("repo", self.root, "base")
        for paths in (["feature.py"], ["feature.py", "docs/PRODUCT.md"]):
            with self.subTest(paths=paths), self.assertRaises(RetirementError):
                self.port.validate_result("repo", self.root, "base", self.judgment_line(item, paths=paths))
        self.assertIsNone(self.port.judgments.get("repo", SOURCE))
        self.assertEqual(self.port.fulfilled_moves("repo", self.root, "base"), ())

    def test_legacy_staged_restoration_is_withdrawn_without_new_audit(self):
        self.git.files.pop(SOURCE)
        self.git.files[ARCHIVE] = CONTENTS
        self.add_plan(status="PR_OPEN")
        archived = self.root / ARCHIVE
        archived.parent.mkdir(parents=True)
        archived.write_bytes(CONTENTS)
        self.seed_legacy_restoration()
        self.connection.execute("UPDATE exec_plans SET status = 'MERGED'")
        self.assertEqual(self.connection.execute("SELECT stage FROM requirement_audit").fetchone()[0], "staged")
        restarted = RequirementRetirementPort(self.connection, self.git)
        with self.assertRaises(RetirementReassessment):
            restarted.receipted_paths("repo", self.root, "advanced", self.root)
        self.assertEqual(archived.read_bytes(), CONTENTS)
        self.assertFalse((self.root / SOURCE).exists())
        self.assertEqual(restarted.receipted_paths("repo", self.root, "advanced", self.root), ())
        self.assertEqual(restarted.pending("repo", self.root, "advanced"), ())
        self.assertEqual(self.git.index_resets, [("reset", "advanced", "--", ARCHIVE, SOURCE)])

    def test_restoration_withdrawal_resumes_after_inverse_rename_interruption(self):
        self.git.files.pop(SOURCE)
        self.git.files[ARCHIVE] = CONTENTS
        self.add_plan(status="PR_OPEN")
        archived = self.root / ARCHIVE
        archived.parent.mkdir(parents=True)
        archived.write_bytes(CONTENTS)
        self.seed_legacy_restoration()
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
        self.assertEqual(restarted.pending("repo", self.root, "advanced"), ())

    def test_legacy_restoration_withdrawal_preserves_local_edits_and_receipt(self):
        self.git.files.pop(SOURCE)
        self.git.files[ARCHIVE] = CONTENTS
        self.add_plan(status="PR_OPEN")
        archived = self.root / ARCHIVE
        archived.parent.mkdir(parents=True)
        archived.write_bytes(CONTENTS)
        self.seed_legacy_restoration()
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


    def test_uncertain_judgment_exposes_decision_and_resumes_on_requirement_edit(self):
        self.add_plan()
        self.connection.execute("ALTER TABLE projects ADD COLUMN planning_error TEXT")
        self.connection.execute("ALTER TABLE projects ADD COLUMN origin TEXT")
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

        self.assertEqual(self.port.pending("repo", self.root, "published"), ())
        self.assertEqual(self.port.stage_moves("repo", self.root, "published", self.root), ())
        self.assertEqual(self.git.blob_reads, reads_before_audit)

    def test_cancelled_source_does_not_become_merged_proof(self):
        self.add_plan()
        self.connection.execute(
            "INSERT INTO execplan_cancellations VALUES (?, ?, ?)",
            ("repo", "G-002", json.dumps([SOURCE])),
        )
        self.assertEqual(self.port.pending("repo", self.root, "base"), ())

    def test_formatted_judgments_bind_verified_input_without_filling_criteria_evidence(self):
        self.add_plan()
        (item,) = self.port.pending('repo', self.root, 'base')
        values = json.loads(self.judgment_line(item).split(': ', 1)[1])
        values[0].pop('input_token')
        output = '**REQUIREMENT_JUDGMENTS** =\n' + json.dumps(values, indent=2)
        judgments = self.port.validate_result('repo', self.root, 'base', output)
        self.assertEqual(judgments[0].outcome, 'fulfilled')
        for change in ({'input_token': 'wrong'}, {'criteria_evidence': []}, {'relevant_paths': []}, {'path': 'other'}):
            changed = [values[0] | change]
            with self.subTest(change=change), self.assertRaises(RetirementError):
                self.port.validate_result('repo', self.root, 'base', '**REQUIREMENT_JUDGMENTS** =\n' + json.dumps(changed))
        with self.assertRaises(RetirementError):
            self.port.validate_result('repo', self.root, 'base', output + '\n' + output)
        duplicate = json.dumps(values).replace('"outcome": "fulfilled"', '"outcome":"unfulfilled","outcome":"fulfilled"')
        with self.assertRaises(RetirementError):
            self.port.validate_result('repo', self.root, 'base', 'REQUIREMENT_JUDGMENTS: ' + duplicate)
        self.port.record('repo', judgments)
        restarted = RequirementRetirementPort(self.connection, self.git)
        self.assertEqual(restarted.validate_result('repo', self.root, 'base', output), judgments)

    def test_saved_judgment_replay_normalizes_requirement_path_whitespace(self):
        self.add_plan()
        (item,) = self.port.pending("repo", self.root, "base")
        values = json.loads(self.judgment_line(item).split(": ", 1)[1])
        values[0]["path"] = f" {item.path} "
        original = "REQUIREMENT_JUDGMENTS: " + json.dumps(values)

        judgments = self.port.validate_result("repo", self.root, "base", original)
        self.port.record("repo", judgments)

        resumed = RequirementRetirementPort(self.connection, self.git)
        self.assertEqual(resumed.pending("repo", self.root, "base"), ())
        self.assertEqual(
            resumed.validate_result("repo", self.root, "base", original),
            judgments,
        )

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
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM requirement_audit").fetchone()[0], 0)
        self.assertEqual(self.git.files[ARCHIVE], b"historical version")

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
        self.assertEqual(self.git.blob_reads, reads + 3)
        self.assertEqual(self.port.judgments.get("repo", SOURCE).outcome, "uncertain")


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


    def test_remote_confirmed_move_marks_receipt_and_preserves_old_audit(self):
        from loop_manager_internal.storage.database import SCHEMA
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.execute(SCHEMA)
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
            _assert_mutable_ready_snapshot_current=lambda *_args, **_kwargs: None,
            _observe_mutable_ready_prs=lambda *_args: None,
            _clear_active_in_current_transaction=lambda *_args, **_kwargs: None,
            _general_planning_batch_already_applied=lambda *_args, **_kwargs: False,
            _register_execplan_rows=lambda *_args, **_kwargs: None,
            _prepare_execplan_rows=lambda *_args: None,
            file_hash=lambda *_args: "hash",
            general_planning_publication=lambda *_args: intent,
            is_loop_manager_repository=lambda *_args: False,
            plan_declarations=lambda *_args: (),
        )
        from loop_manager_internal.storage import planning_review_state
        planning_review_state.begin(connection, "repo", purpose="final", revision="base",
            input_fingerprint="planning", specification_fingerprint="spec",
            work_cycle="cycle", comparison_revision=None)
        planning_review_state.validate(connection, "repo", "base", "full review evidence", "fulfilled")
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
        self.assertEqual(tuple(stages), ("published", "observed"))
        receipt = planning_review_state.get(connection, "repo", "final")
        self.assertEqual(receipt["confirmed_revision"], "commit")
        self.assertEqual(receipt["revision"], "base")
        self.assertIsNone(planning_review_state.get(connection, "repo", "attempt"))
        # A later SQLite failure in the same remote batch cannot advance the receipt.
        planning_review_state.begin(connection, "repo", purpose="final", revision="base",
            input_fingerprint="changed", specification_fingerprint="spec",
            work_cycle="next-cycle", comparison_revision=None)
        planning_review_state.validate(connection, "repo", "base", "new full review", "fulfilled")
        connection.execute("UPDATE requirement_move_receipts SET base_sha = 'wrong-base'")
        with (mock.patch(module + ".planning_publication_state.apply_mutable_catalog_changes"),
              mock.patch(module + ".planning_publication_state.delete_general_planning_publication"),
              mock.patch(module + ".planning_input_state.record_general"),
              self.assertRaises(PlannerValidationError)):
            _apply_general_planning_batch(effects, connection, "repo", Path("/unused"), result,
                                          "commit", "spec", "planning")
        self.assertEqual(dict(planning_review_state.get(connection, "repo", "final")), dict(receipt))
        self.assertEqual(planning_review_state.get(connection, "repo", "attempt")["work_cycle"], "next-cycle")

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

        effects = ValidateGeneralPlanningCommitEffects(
            _nul_git_paths=lambda *_args: [SOURCE, ARCHIVE],
            git=git, git_bytes=git_bytes,
        )
        _validate_general_planning_commit(effects, Path("/unused"), result, "commit")
        blobs["commit:" + ARCHIVE] = b"changed"
        with self.assertRaises(PlannerValidationError):
            _validate_general_planning_commit(effects, Path("/unused"), result, "commit")


class RequirementAuditWaitReasonTest(unittest.TestCase):
    def test_renamed_active_requirement_keeps_old_execplan_relation(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.executescript("""
            CREATE TABLE projects(repo TEXT PRIMARY KEY, planning_error TEXT, origin TEXT);
            CREATE TABLE exec_plans(repo TEXT, plan_id TEXT, title TEXT, status TEXT,
                depends_on TEXT, pr_url TEXT, sources TEXT, path TEXT);
            INSERT INTO projects VALUES ('repo', NULL, NULL);
        """)
        connection.execute(
            "INSERT INTO exec_plans VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("repo", "G-001", "Feature", "PR_OPEN", "[]", "", json.dumps([SOURCE]), "docs/plans/[G-001] Feature.md"),
        )
        renamed = "docs/requirements/[R-001] Renamed.md"
        snapshot = work_exhaustion.collect(
            connection, "repo", cycle="cycle", active_requirements=(renamed,),
            requirement_documents={renamed: b"# [R-001] Renamed\n"},
        )
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertIn("관련 계획 또는 archive 확인", snapshot.requirements[0]["reason"])

    def test_uncertain_historical_archive_does_not_delay_project_completion(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.executescript("""
            CREATE TABLE projects(repo TEXT PRIMARY KEY, planning_error TEXT, origin TEXT);
            CREATE TABLE exec_plans(repo TEXT, plan_id TEXT, title TEXT, status TEXT,
                depends_on TEXT, pr_url TEXT, sources TEXT, path TEXT);
            CREATE TABLE requirement_audit(repo TEXT, source_path TEXT,
                outcome TEXT, stage TEXT, reason TEXT);
            INSERT INTO projects VALUES ('repo', NULL, NULL);
        """)
        connection.execute(
            "INSERT INTO requirement_audit VALUES (?, ?, ?, ?, ?)",
            ("repo", SOURCE, "uncertain", "observed", "historical proof absent"),
        )

        snapshot = work_exhaustion.collect(
            connection, "repo", cycle="fingerprint", active_requirements=(),
        )

        self.assertTrue(snapshot.completed)
        self.assertEqual(snapshot.requirements, ())
        self.assertEqual(connection.execute("SELECT reason FROM requirement_audit").fetchone()[0],
                         "historical proof absent")


if __name__ == "__main__":
    unittest.main()
