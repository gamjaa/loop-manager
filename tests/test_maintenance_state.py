import io
import sqlite3
import subprocess
import tempfile
import unittest
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from unittest import mock

from loop_manager_internal.documents import document_drafts
from loop_manager_internal.documents import document_publication
import loop_manager
from loop_manager_internal.maintenance import maintenance_git
from loop_manager_internal.maintenance import maintenance_file_inventory
from loop_manager_internal.storage import maintenance_file_ledger
from loop_manager_internal.maintenance import maintenance_file_publication
from loop_manager_internal.storage import maintenance_state


class DocumentDraftTest(unittest.TestCase):
    def test_approved_document_bytes_apply_once_and_reject_unrelated_edits(self):
        draft = self._create()
        approved = self.drafts.decide(
            draft.draft_id, draft.proposed_hash, "approve", self.repo, "main",
        )
        self.drafts.bind_plan(str(self.repo), approved.draft_id, "G-300")
        self.assertEqual(
            self.drafts.for_plan(str(self.repo), "G-300").draft_id,
            approved.draft_id,
        )
        with self.assertRaises(document_drafts.DocumentDraftError):
            self.drafts.bind_plan(str(self.repo), approved.draft_id, "G-301")
        worktree = self.root / "managed"
        self._git("worktree", "add", "--detach", str(worktree), self.source_sha)
        document_publication.apply_approved_bytes(approved, worktree, self.source_sha)
        document_publication.apply_approved_bytes(approved, worktree, self.source_sha)
        self.assertEqual((worktree / approved.target_path).read_bytes(), approved.proposed_content)
        (worktree / "AGENTS.md").write_text("unauthorized\n", encoding="utf-8")
        with self.assertRaises(document_drafts.DocumentDraftError):
            document_publication.validate_exact_worktree(approved, worktree, self.source_sha)
        (worktree / "AGENTS.md").unlink()
        (worktree / approved.target_path).chmod(0o755)
        with self.assertRaises(document_drafts.DocumentDraftError):
            document_publication.validate_exact_worktree(approved, worktree, self.source_sha)

    def test_newly_stale_approval_records_failed_planning_check(self):
        draft = self._create()
        self.drafts.decide(
            draft.draft_id, draft.proposed_hash, "approve", self.repo, "main",
        )
        self.assertEqual(
            len(self.drafts.unplanned_approved(str(self.repo), self.repo, "main")), 1,
        )
        (self.repo / draft.target_path).write_bytes(b"# Product\n\nChanged.\n")
        self._git("add", draft.target_path)
        self._git("commit", "-m", "change original")
        self._git("update-ref", "refs/remotes/origin/main", self._git("rev-parse", "HEAD"))
        self.assertFalse(self.drafts.unplanned_approved(str(self.repo), self.repo, "main"))
        with self.assertRaises(document_drafts.DocumentDraftError):
            self.drafts.assert_current(
                self.drafts.get(draft.draft_id), self.repo, "main",
            )
        recorded = self.drafts.get(draft.draft_id)
        self.assertEqual(recorded.requested_decision, "approve")
        self.assertIn("원본 revision", recorded.publication_error)

    def test_document_plan_does_not_change_its_own_source_snapshot(self):
        target = document_publication.R027_SOURCE
        self.database.execute(
            "INSERT INTO exec_plans (repo, plan_id, path, title, sources, "
            "depends_on, priority, content_hash, status) "
            "VALUES (?, 'G-300', 'docs/plans/[G-300] Document.md', "
            "'Document', ?, '[]', 'normal', 'hash', 'READY')",
            (str(self.repo), json.dumps([target])),
        )
        self.assertEqual(
            document_drafts.source_relationships(
                self.database, str(self.repo), target,
                exclude_plan_id="G-300",
            ),
            "[]",
        )
        self.assertIn(
            "G-300",
            document_drafts.source_relationships(self.database, str(self.repo), target),
        )

    def test_approved_listing_keeps_draft_after_its_own_plan_is_published(self):
        draft = self._create()
        approved = self.drafts.decide(
            draft.draft_id, draft.proposed_hash, 'approve', self.repo, 'main',
        )
        path = 'docs/plans/[G-300] Approved Product.md'
        target = self.repo / path
        target.parent.mkdir(parents=True)
        target.write_text(
            '---\nid: G-300\ntitle: Approved Product\nsources:\n'
            '  - docs/PRODUCT.md\ndepends_on: []\ncomplexity: low\n'
            'risk: low\npriority: normal\n---\n\n## Purpose\n\nApproved change.\n',
            encoding='utf-8',
        )
        self._git('add', path)
        self._git('commit', '-m', 'publish approved plan')
        self._git('update-ref', 'refs/remotes/origin/main', self._git('rev-parse', 'HEAD'))
        self.database.execute(
            "INSERT INTO exec_plans (repo, plan_id, path, title, sources, "
            "depends_on, priority, content_hash, status) "
            "VALUES (?, 'G-300', ?, 'Approved Product', ?, '[]', 'normal', 'hash', 'READY')",
            (str(self.repo), path, json.dumps(['docs/PRODUCT.md'])),
        )
        self.drafts.bind_plan(str(self.repo), approved.draft_id, 'G-300')
        self.assertEqual(
            [item.draft_id for item in self.drafts.approved(str(self.repo), self.repo, 'main')],
            [approved.draft_id],
        )

    def test_r027_document_approval_is_planned_before_other_drafts(self):
        product = mock.Mock(target_path="docs/PRODUCT.md")
        requirement = mock.Mock(target_path=document_publication.R027_SOURCE)
        with mock.patch.object(
            self.drafts, 'unplanned_approved', return_value=(product, requirement),
        ):
            self.assertIs(
                self.drafts.planning_candidate(str(self.repo), self.repo, 'main'),
                requirement,
            )

    def test_corrupt_approved_bytes_are_audited_without_creating_plan(self):
        draft = self._create()
        self.drafts.decide(
            draft.draft_id, draft.proposed_hash, 'approve', self.repo, 'main',
        )
        self.database.execute(
            "UPDATE document_drafts SET proposed_content = ? WHERE draft_id = ?",
            (b'corrupt', draft.draft_id),
        )
        self.assertFalse(self.drafts.unplanned_approved(str(self.repo), self.repo, 'main'))
        row = self.database.execute(
            "SELECT requested_decision, plan_id, publication_error "
            "FROM document_drafts WHERE draft_id = ?", (draft.draft_id,),
        ).fetchone()
        self.assertEqual(row['requested_decision'], 'approve')
        self.assertIsNone(row['plan_id'])
        self.assertIn('손상', row['publication_error'])

    def test_approved_document_target_cannot_escape_managed_worktree(self):
        draft = self._create()
        approved = self.drafts.decide(
            draft.draft_id, draft.proposed_hash, 'approve', self.repo, 'main',
        )
        worktree = self.root / 'managed'
        worktree.mkdir()
        (worktree / 'docs').symlink_to(self.repo / 'docs', target_is_directory=True)
        with self.assertRaises(document_drafts.DocumentDraftError):
            document_publication.apply_approved_bytes(
                approved, worktree, self.source_sha,
            )
        self.database.execute(
            "UPDATE document_drafts SET target_path = '../AGENTS.md' WHERE draft_id = ?",
            (draft.draft_id,),
        )
        with self.assertRaises(document_drafts.DocumentDraftError):
            self.drafts.get(draft.draft_id)

    def test_approved_source_graph_change_blocks_plan_and_application(self):
        draft = self._create()
        approved = self.drafts.decide(
            draft.draft_id, draft.proposed_hash, 'approve', self.repo, 'main',
        )
        path = 'docs/plans/[G-300] Product Source.md'
        plan = self.repo / path
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text(
            '---\nid: G-300\ntitle: Product Source\nsources:\n'
            '  - docs/PRODUCT.md\ndepends_on: []\ncomplexity: low\n'
            'risk: low\npriority: normal\n---\n\n## Purpose\n\nSource.\n',
            encoding='utf-8',
        )
        self._git('add', path)
        self._git('commit', '-m', 'new source relation')
        self._git('update-ref', 'refs/remotes/origin/main', self._git('rev-parse', 'HEAD'))
        self.database.execute(
            "INSERT INTO exec_plans (repo, plan_id, path, title, sources, "
            "depends_on, priority, content_hash, status) "
            "VALUES (?, 'G-300', ?, 'Product Source', ?, '[]', 'normal', 'hash', 'READY')",
            (str(self.repo), path, json.dumps(['docs/PRODUCT.md'])),
        )
        self.assertFalse(self.drafts.unplanned_approved(str(self.repo), self.repo, 'main'))
        with self.assertRaises(document_drafts.DocumentDraftError):
            self.drafts.assert_current(approved, self.repo, 'main')
        self.assertIn('source 관계', self.drafts.get(draft.draft_id).publication_error)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "source"
        self.repo.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")
        self.documents = {
            "docs/PRODUCT.md": b"# Product\n\nRepeated rule.\n",
            "docs/ARCHITECTURE.md": b"# Architecture\n\nRepeated explanation.\n",
        }
        for name, content in self.documents.items():
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        self._git("add", "-A")
        self._git("commit", "-m", "source")
        self.source_sha = self._git("rev-parse", "HEAD")
        self._git("update-ref", "refs/remotes/origin/main", self.source_sha)

        self.database = sqlite3.connect(self.root / "state.db")
        self.addCleanup(self.database.close)
        self.database.row_factory = sqlite3.Row
        self.database.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.database)
        self.database.execute(
            "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
            (str(self.repo),),
        )
        self.state = maintenance_state.MaintenanceRepository(self.database)
        cycle, _ = self.state.begin_assessment_if_due(
            str(self.repo), 5, self.source_sha, self_repository=True,
        )
        findings = []
        for path in self.documents:
            findings.append({
                "id": "doc-" + ("product" if "PRODUCT" in path else "architecture"),
                "kind": "document", "path": path,
                "evidence": "A duplicated explanation can be removed without a policy decision.",
                "status": "candidate", "plan_ids": [],
                "source_hash": hashlib.sha256(self.documents[path]).hexdigest(),
            })
        findings.append({
            "id": "policy-choice", "kind": "document", "path": "docs/PRODUCT.md",
            "evidence": "A product policy choice requires explicit user approval first.",
            "status": "decision_required", "plan_ids": [],
            "source_hash": hashlib.sha256(self.documents["docs/PRODUCT.md"]).hexdigest(),
        })
        report = {
            "source_revision": self.source_sha,
            "coverage": {key: "Reviewed document and code boundaries."
                         for key in ("code", "tests", "documents")},
            "findings": findings,
        }
        self.state.record_full_assessment(
            str(self.repo), expected_revision=cycle.revision,
            source_sha=self.source_sha, result=json.dumps(report),
        )
        self.state.advance_success_baseline(
            str(self.repo), expected_phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            expected_revision=cycle.revision, remote_commit=self.source_sha,
        )
        self.drafts = document_drafts.DocumentDraftRepository(self.database)

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True,
            capture_output=True, text=True,
        )
        return result.stdout.strip()

    def _create(self, path: str = "docs/PRODUCT.md", content: bytes = b"# Product\n\nRule.\n"):
        return self.drafts.create(
            str(self.repo), self.repo, "main", path, content,
            "Removed only repeated wording; the product rule and exception remain intact.",
        )

    def test_complete_artifact_restart_per_document_and_exact_consent(self):
        product = self._create()
        architecture = self._create(
            "docs/ARCHITECTURE.md", b"# Architecture\n\nExplanation.\n",
        )
        self.database.commit()
        reopened = sqlite3.connect(self.root / "state.db")
        self.addCleanup(reopened.close)
        reopened.row_factory = sqlite3.Row
        drafts = document_drafts.DocumentDraftRepository(reopened)
        artifact = document_drafts.review_artifact(drafts.get(product.draft_id))
        for value in (product.original_hash, product.proposed_hash,
                      product.original_revision, product.complete_diff,
                      product.proposed_content.decode(), product.original_content.decode()):
            self.assertIn(value, artifact)
        with self.assertRaises(document_drafts.DocumentDraftError):
            drafts.decide(product.draft_id, "0" * 64, "approve", self.repo, "main")
        approved = drafts.decide(product.draft_id, product.proposed_hash, "approve",
                                 self.repo, "main")
        rejected = drafts.decide(architecture.draft_id, architecture.proposed_hash,
                                 "reject", self.repo, "main")
        self.assertEqual((approved.state, rejected.state), ("approved", "rejected"))
        self.assertEqual(approved.requested_decision, "approve")
        self.assertEqual([d.draft_id for d in drafts.approved(str(self.repo), self.repo, "main")],
                         [product.draft_id])
        with self.assertRaises(document_drafts.DocumentDraftError):
            drafts.decide(product.draft_id, product.proposed_hash, "approve", self.repo, "main")
        with self.assertRaises(document_drafts.DocumentDraftError):
            drafts.create(str(self.repo), self.repo, "main", product.target_path,
                          b"# Alternative\n", "A sufficiently specific meaning preservation statement.")
        self.assertEqual((self.repo / product.target_path).read_bytes(), self.documents[product.target_path])

    def test_stale_original_or_source_relation_requires_fresh_review(self):
        draft = self._create()
        product = self.repo / "docs/PRODUCT.md"
        product.write_text("# Product\n\nChanged rule.\n", encoding="utf-8")
        self._git("add", "docs/PRODUCT.md")
        self._git("commit", "-m", "change product")
        self._git("update-ref", "refs/remotes/origin/main", self._git("rev-parse", "HEAD"))
        stale = self.drafts.decide(draft.draft_id, draft.proposed_hash, "approve",
                                   self.repo, "main")
        self.assertEqual((stale.state, stale.requested_decision), ("stale", "approve"))
        self.assertFalse(self.drafts.approved(str(self.repo), self.repo, "main"))
        fresh = self._create(content=b"# Product\n\nChanged rule.\n\nClarified.\n")
        self.assertNotEqual(fresh.original_hash, draft.original_hash)
        plan_path = self.repo / "docs/plans/[G-200] A plan.md"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(
            "---\nid: G-200\ntitle: A plan\nsources:\n"
            "  - docs/PRODUCT.md\ndepends_on: []\ncomplexity: low\n"
            "risk: low\npriority: normal\n---\n\n## Purpose\n\nPreserve behavior.\n",
            encoding="utf-8",
        )
        self._git("add", "docs/plans")
        self._git("commit", "-m", "add source relationship")
        self._git("update-ref", "refs/remotes/origin/main", self._git("rev-parse", "HEAD"))
        self.database.execute(
            "INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, "
            "priority, content_hash, status) VALUES (?, 'G-200', 'docs/plans/G-200.md', "
            "'A plan', ?, '[]', 'normal', 'hash', 'READY')",
            (str(self.repo), '["docs/PRODUCT.md"]'),
        )
        stale_sources = self.drafts.decide(fresh.draft_id, fresh.proposed_hash, "approve",
                                           self.repo, "main")
        self.assertEqual(stale_sources.state, "stale")
        self.assertIn("source", stale_sources.decision_reason)

    def test_persisted_byte_tampering_cannot_be_reviewed_or_approved(self):
        draft = self._create()
        self.database.execute(
            "UPDATE document_drafts SET proposed_content = ? WHERE draft_id = ?",
            (b"different", draft.draft_id),
        )
        with self.assertRaises(document_drafts.DocumentDraftError):
            self.drafts.get(draft.draft_id)
        with self.assertRaises(document_drafts.DocumentDraftError):
            self.drafts.decide(draft.draft_id, draft.proposed_hash, "approve",
                               self.repo, "main")

    def test_complete_diff_marks_missing_final_newline(self):
        diff = document_drafts._complete_diff(
            "docs/PRODUCT.md", b"old", b"new",
        )
        self.assertIn("-old\n\\ No newline at end of file", diff)
        self.assertIn("+new\n\\ No newline at end of file", diff)

    def test_policy_findings_uncertain_meaning_and_ownership_are_excluded(self):
        self.assertEqual(self.drafts.candidates(str(self.repo))["docs/PRODUCT.md"],
                         ("doc-product",))
        with self.assertRaises(document_drafts.DocumentDraftError):
            self.drafts.create(str(self.repo), self.repo, "main", "docs/requirements/archive/old.md",
                               b"changed", "A sufficiently specific meaning preservation statement.")
        with self.assertRaises(document_drafts.DocumentDraftError):
            self.drafts.create(str(self.repo), self.repo, "main", "docs/PRODUCT.md",
                               b"changed", "uncertain")
        draft = self._create()
        uncertain = self.drafts.decide(draft.draft_id, draft.proposed_hash, "approve",
                                       self.repo, "main", uncertain_reason="Cannot prove rule equivalence")
        self.assertEqual(uncertain.state, "stale")
        self.database.execute("UPDATE projects SET phase = 'PROJECT_BLOCKED' WHERE repo = ?",
                              (str(self.repo),))
        with self.assertRaises(document_drafts.DocumentDraftError):
            self._create()

    def test_cli_has_explicit_identity_and_hash_boundary(self):
        parser = loop_manager.build_parser()
        proposal = b"# Product\r\n\r\nRule.\r\n"
        draft = self._create(content=proposal)
        show = parser.parse_args(["document-drafts", str(self.repo), "show",
                                  draft.draft_id])
        output = io.BytesIO()
        with mock.patch.object(loop_manager, "is_loop_manager_repository", return_value=True), \
             mock.patch.object(loop_manager, "connect_database", return_value=self.database), \
             mock.patch.object(loop_manager.sys, "stdout", mock.Mock(buffer=output)):
            self.assertEqual(loop_manager._document_drafts_command(show, self.root, self.root / "config.ini"), 0)
        review = output.getvalue()
        self.assertIn(draft.proposed_hash.encode("ascii"), review)
        self.assertIn(
            f"=== Complete proposal ({len(proposal)} bytes) ===\n".encode("ascii") + proposal,
            review,
        )
        self.assertIn(draft.complete_diff.encode("utf-8"), review)
        args = parser.parse_args(["document-drafts", str(self.repo), "decide",
                                  draft.draft_id, draft.proposed_hash, "approve"])
        self.assertEqual((args.draft_id, args.proposed_hash, args.decision),
                         (draft.draft_id, draft.proposed_hash, "approve"))
        with mock.patch.object(loop_manager, "is_loop_manager_repository", return_value=True), \
             mock.patch.object(loop_manager, "connect_database", return_value=self.database), \
             mock.patch.object(loop_manager, "_recorded_base_fetch", return_value="base") as fetched, \
             mock.patch.object(loop_manager, "operation_print"):
            self.assertEqual(loop_manager._document_drafts_command(args, self.root, self.root / "config.ini"), 0)
            fetched.assert_called_once_with(
                self.repo, self.root, "main", owner="project",
            )
        self.assertEqual(self.drafts.get(draft.draft_id).state, "approved")
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            parser.parse_args(["document-drafts", str(self.repo), "decide",
                               "draft-id", "approve"])

    def test_read_only_generation_persists_complete_candidate(self):
        prompt, identity = self.drafts.generation_prompt(
            str(self.repo), self.repo, "main", "docs/PRODUCT.md",
        )
        self.assertIn("doc-product", prompt)
        self.assertNotIn("policy-choice", prompt)
        self.assertEqual(identity[1], hashlib.sha256(self.documents["docs/PRODUCT.md"]).hexdigest())
        proposal = b"# Product\r\n\r\nRule.\r\n"
        response = "DOCUMENT_DRAFT_RESULT: " + json.dumps({
            "status": "candidate", "content": proposal.decode(),
            "meaning_evidence": "Only duplicate wording changed; product behavior remains unchanged.",
        })
        args = loop_manager.build_parser().parse_args([
            "document-drafts", str(self.repo), "generate", "docs/PRODUCT.md",
        ])
        output = io.BytesIO()
        with mock.patch.object(loop_manager, "is_loop_manager_repository", return_value=True), \
             mock.patch.object(loop_manager, "connect_database", return_value=self.database), \
             mock.patch.object(loop_manager, "_recorded_base_fetch", return_value="base") as fetched, \
             mock.patch.object(loop_manager, "run_agent", return_value=(0, response)) as agent, \
             mock.patch.object(loop_manager.sys, "stdout", mock.Mock(buffer=output)):
            self.assertEqual(loop_manager._document_drafts_command(
                args, self.root, self.root / "config.ini"), 0)
            fetched.assert_called_once_with(
                self.repo, self.root, "main", owner="project",
            )
            self.assertTrue(agent.call_args.kwargs["read_only"])
            self.assertEqual(agent.call_args.args[1], loop_manager.ROOT)
        self.assertIn(
            f"=== Complete proposal ({len(proposal)} bytes) ===\n".encode("ascii") + proposal,
            output.getvalue(),
        )
        self.assertEqual(self.drafts.list(str(self.repo))[0].proposed_content, proposal)
        with self.assertRaises(document_drafts.DocumentDraftError):
            self.drafts.generation_prompt(str(self.repo), self.repo, "main", "docs/PRODUCT.md")
        self.assertIsNone(document_drafts.parse_generated_draft(
            'DOCUMENT_DRAFT_RESULT: {"status":"uncertain","reason":"Policy meaning unclear"}'
        ))

    def test_self_only_cli_and_pr_open_ownership_gate(self):
        args = loop_manager.build_parser().parse_args([
            "document-drafts", str(self.repo), "list",
        ])
        with mock.patch.object(loop_manager, "is_loop_manager_repository", return_value=False), \
             self.assertRaises(document_drafts.DocumentDraftError):
            loop_manager._document_drafts_command(args, self.root, self.root / "config.ini")
        self.database.execute(
            "INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, "
            "priority, content_hash, status) VALUES (?, 'G-201', 'docs/plans/G-201.md', "
            "'Open PR', '[]', '[]', 'normal', 'hash', 'PR_OPEN')",
            (str(self.repo),),
        )
        with self.assertRaises(document_drafts.DocumentDraftError):
            self._create()


class MaintenanceTransactionTest(unittest.TestCase):
    def test_read_then_write_reserves_writer_before_competing_project(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.db'
            first = sqlite3.connect(path, timeout=0)
            second = sqlite3.connect(path, timeout=0)
            try:
                first.execute('CREATE TABLE baseline (repo TEXT, count INTEGER)')
                with maintenance_state._transaction(first):
                    self.assertEqual(first.execute('SELECT * FROM baseline').fetchall(), [])
                    # A different project must wait before obtaining a write
                    # reservation, not deadlock with our later lock upgrade.
                    with self.assertRaises(sqlite3.OperationalError) as raised:
                        second.execute('BEGIN IMMEDIATE')
                    self.assertEqual(raised.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
                    first.execute("INSERT INTO baseline VALUES ('one', 1)")
                with maintenance_state._transaction(second):
                    second.execute("INSERT INTO baseline VALUES ('two', 2)")
                self.assertEqual(first.execute('SELECT * FROM baseline ORDER BY repo').fetchall(),
                                 [('one', 1), ('two', 2)])
                self.assertFalse(first.in_transaction)
                self.assertFalse(second.in_transaction)
            finally:
                second.close()
                first.close()

    def test_failure_and_nested_savepoint_preserve_caller_transaction(self):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.execute('CREATE TABLE baseline (count INTEGER)')
        with self.assertRaises(KeyboardInterrupt):
            with maintenance_state._transaction(connection):
                connection.execute('INSERT INTO baseline VALUES (1)')
                raise KeyboardInterrupt()
        self.assertFalse(connection.in_transaction)
        self.assertEqual(connection.execute('SELECT * FROM baseline').fetchall(), [])
        connection.execute('INSERT INTO baseline VALUES (2)')
        with self.assertRaises(ValueError):
            with maintenance_state._transaction(connection):
                connection.execute('INSERT INTO baseline VALUES (3)')
                raise ValueError('abort nested operation')
        with maintenance_state._transaction(connection):
            connection.execute('INSERT INTO baseline VALUES (4)')
        self.assertTrue(connection.in_transaction)
        self.assertEqual(connection.execute('SELECT * FROM baseline').fetchall(), [(2,), (4,)])
        connection.rollback()
        self.assertEqual(connection.execute('SELECT * FROM baseline').fetchall(), [])


class MaintenanceStateRepositoryTest(unittest.TestCase):
    @staticmethod
    def _start_legacy_assessment(connection, repo, source_sha):
        maintenance_state.ensure_schema(connection)
        count = connection.execute(
            "SELECT COUNT(*) FROM exec_plans WHERE repo = ? AND status = 'MERGED'",
            (repo,),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO maintenance_cycles "
            "(repo, phase, cycle_base_sha, cycle_start_merged_count, revision) "
            "VALUES (?, 'assessment', ?, ?, 1) "
            "ON CONFLICT(repo) DO UPDATE SET phase = 'assessment', "
            "cycle_base_sha = excluded.cycle_base_sha, "
            "cycle_start_merged_count = excluded.cycle_start_merged_count, "
            "revision = maintenance_cycles.revision + 1",
            (repo, source_sha, count),
        )
        return maintenance_state.MaintenanceRepository(connection).get(repo)

    def test_self_full_assessment_is_once_and_survives_restart(self):
        connection = self._connection()
        repository = maintenance_state.MaintenanceRepository(connection)
        self.assertEqual(repository.due("one", 5, self_repository=True).reason,
                         "one_time_full_assessment")
        cycle, created = repository.begin_assessment_if_due(
            "one", 5, "a" * 40, self_repository=True)
        self.assertTrue(created)
        saved = repository.record_full_assessment(
            "one", expected_revision=cycle.revision, source_sha="a" * 40,
            result='{"findings":[]}',
        )
        self.assertEqual(maintenance_state.MaintenanceRepository(connection).get("one").full_assessment_result,
                         saved.full_assessment_result)
        completed = repository.advance_success_baseline(
            "one", expected_phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            expected_revision=cycle.revision, remote_commit="a" * 40,
        )
        self.assertTrue(completed.full_assessment_completed)
        self.assertFalse(repository.due("one", 5, self_repository=True).required)
        self.assertEqual(repository.due("two", 5).reason, "no_successful_assessment")
        connection.execute(
            "INSERT INTO execplan_catalog_revisions "
            "(repo, revision_sha, catalog_fingerprint) VALUES ('one', ?, 'catalog')",
            ("a" * 40,),
        )
        repository.initialize_file_assessment_start(
            "one", source_sha="a" * 40, expected_merged_count=0,
        )
        for number in range(1, 6):
            self._insert_plan(connection, "one", f"G-{number:03d}", "MERGED")
        periodic, started = repository.begin_assessment_if_due(
            "one", 5, "b" * 40, self_repository=True)
        self.assertTrue(started)
        restarted = repository.restart_stale_planning_context(
            "one", expected=periodic, new_base_sha="c" * 40)
        self.assertEqual(restarted.full_assessment_source_sha, "a" * 40)
        self.assertEqual(restarted.full_assessment_result, saved.full_assessment_result)

    def test_full_assessment_source_drift_discards_unpublished_result(self):
        connection = self._connection()
        repository = maintenance_state.MaintenanceRepository(connection)
        cycle, _ = repository.begin_assessment_if_due(
            "one", 5, "a" * 40, self_repository=True)
        saved = repository.record_full_assessment(
            "one", expected_revision=cycle.revision, source_sha="a" * 40,
            result='{"findings":[]}',
        )
        restarted = repository.restart_stale_planning_context(
            "one", expected=saved, new_base_sha="b" * 40,
        )
        self.assertIsNone(restarted.full_assessment_result)
        self.assertEqual(restarted.full_assessment_source_sha, "b" * 40)
        self.assertFalse(restarted.full_assessment_completed)

    def test_refactoring_publication_marks_full_assessment_without_resetting_periodic_due(self):
        connection = self._connection()
        self._insert_plan(connection, "one", "G-100", "READY")
        repository = maintenance_state.MaintenanceRepository(connection)
        cycle, _ = repository.begin_assessment_if_due(
            "one", 5, "a" * 40, self_repository=True)
        repository.record_full_assessment(
            "one", expected_revision=cycle.revision, source_sha="a" * 40,
            result='{"findings":[]}',
        )
        published = repository.transition(
            "one", expected_phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            expected_revision=cycle.revision,
            next_phase=maintenance_state.MaintenancePhase.REFACTORING,
            refactoring_plan_ids=("G-100",),
        )
        self.assertTrue(published.full_assessment_completed)
        self.assertEqual(repository.due("one", 5, self_repository=True).reason,
                         "active_cycle")

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        connection.executemany(
            "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
            (("one",), ("two",)),
        )
        connection.commit()
        return connection

    @staticmethod
    def _insert_plan(
        connection: sqlite3.Connection,
        repo: str,
        plan_id: str,
        status: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on,
                priority, content_hash, status, worker_session_id,
                verify_failure_history
            ) VALUES (?, ?, ?, ?, '[]', '[]', 'normal', ?, ?, ?, ?)
            """,
            (
                repo,
                plan_id,
                f"docs/plans/{plan_id}.md",
                plan_id,
                f"hash-{plan_id}",
                status,
                f"session-{plan_id}",
                '{"fingerprint":2}',
            ),
        )

    def test_due_is_project_scoped_and_success_baseline_advances_only_on_success(self):
        connection = self._connection()
        self._insert_plan(connection, "one", "G-001", "MERGED")
        self._insert_plan(connection, "two", "G-001", "READY")
        repository = maintenance_state.MaintenanceRepository(connection)

        self.assertEqual(repository.due("one", 5).reason, "no_successful_assessment")
        self.assertEqual(repository.due("two", 5).reason, "no_successful_assessment")
        cycle = self._start_legacy_assessment(connection, "one", "a" * 40)
        self.assertEqual(cycle.phase, maintenance_state.MaintenancePhase.ASSESSMENT)
        self.assertEqual(cycle.cycle_start_merged_count, 1)

        same_cycle, created = repository.begin_assessment_if_due("one", 1, "b" * 40)
        self.assertFalse(created)
        self.assertEqual(same_cycle, cycle)
        self._insert_plan(connection, "one", "G-002", "MERGED")
        self.assertEqual(repository.due("one", 1).reason, "active_cycle")

        completed = repository.advance_success_baseline(
            "one",
            expected_phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            expected_revision=cycle.revision,
            remote_commit="b" * 40,
        )
        self.assertEqual(completed.phase, maintenance_state.MaintenancePhase.IDLE)
        self.assertEqual(completed.last_success_merged_count, 2)
        self.assertFalse(repository.due("one", 2).required)
        self._insert_plan(connection, "one", "G-003", "MERGED")
        self.assertFalse(repository.due("one", 2).required)
        self._insert_plan(connection, "one", "G-004", "MERGED")
        self.assertTrue(repository.due("one", 2).required)
        self.assertEqual(repository.due("two", 2).reason, "no_successful_assessment")

    def test_file_assessment_start_waits_for_legacy_cycle_and_is_idempotent(self):
        connection = self._connection()
        repository = maintenance_state.MaintenanceRepository(connection)
        source = "a" * 40
        connection.execute(
            "INSERT INTO execplan_catalog_revisions "
            "(repo, revision_sha, catalog_fingerprint) VALUES ('one', ?, 'catalog')",
            (source,),
        )
        self._insert_plan(connection, "one", "G-001", "MERGED")
        # A cycle started before file baselines existed remains recoverable.
        connection.execute(
            "INSERT INTO maintenance_cycles "
            "(repo, phase, cycle_base_sha, cycle_start_merged_count, revision) "
            "VALUES ('one', 'assessment', ?, 1, 1)", (source,),
        )
        cycle = repository.get("one")
        self.assertIsNone(repository.initialize_file_assessment_start(
            "one", source_sha=source, expected_merged_count=1,
        ).file_assessment_start_merged_count)
        recovered = repository.restart_stale_planning_context(
            "one", expected=cycle, new_base_sha=source,
        )
        self.assertIsNone(recovered.file_assessment_start_merged_count)
        connection.execute(
            "UPDATE maintenance_cycles SET full_assessment_completed = 1 "
            "WHERE repo = 'one'"
        )
        completed = repository.advance_success_baseline(
            "one", expected_phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            expected_revision=recovered.revision, remote_commit=source,
        )
        self.assertIsNone(completed.file_assessment_start_merged_count)
        started = repository.initialize_file_assessment_start(
            "one", source_sha=source, expected_merged_count=1,
        )
        self.assertEqual(started.file_assessment_start_merged_count, 1)
        self.assertEqual(started.last_success_merged_count, 1)
        self.assertTrue(started.full_assessment_completed)
        self._insert_plan(connection, "one", "G-002", "MERGED")
        restarted = maintenance_state.MaintenanceRepository(connection)
        self.assertEqual(restarted.initialize_file_assessment_start(
            "one", source_sha="b" * 40, expected_merged_count=2,
        ), started)
        self.assertEqual(restarted.file_assessment_due("one", 20).merged_since_baseline, 1)

    def test_file_assessment_due_uses_first_and_success_baselines(self):
        connection = self._connection()
        repository = maintenance_state.MaintenanceRepository(connection)
        source = "a" * 40
        self.assertEqual(repository.file_assessment_due("two", 20).reason,
                         "baseline_pending")
        self.assertEqual(repository.file_assessment_due(
            "two", 20, self_repository=True,
        ).reason, "one_time_full_assessment")
        connection.execute(
            "INSERT INTO execplan_catalog_revisions "
            "(repo, revision_sha, catalog_fingerprint) VALUES ('one', ?, 'catalog')",
            (source,),
        )
        repository.initialize_file_assessment_start(
            "one", source_sha=source, expected_merged_count=0,
        )
        scheduling_port = loop_manager._PlanningStatePorts(
            connection,
            loop_manager.runtime_state.RuntimeStateRepository(connection),
            repository,
            Path("/unused"),
        )
        self.assertFalse(repository.file_assessment_due(
            "one", 20, completion_candidate=True,
        ).required)
        self.assertFalse(scheduling_port.maintenance_due("one", 20).required)
        for number in range(1, 20):
            self._insert_plan(connection, "one", f"G-{number:03d}", "MERGED")
        self.assertEqual(repository.file_assessment_due(
            "one", 20, completion_candidate=True,
        ).reason, "interval_pending")
        self.assertTrue(repository.file_assessment_due("one", 19).required)
        self._insert_plan(connection, "one", "G-020", "MERGED")
        self.assertEqual(repository.file_assessment_due("one", 20).reason,
                         "interval_reached")
        self.assertTrue(scheduling_port.maintenance_due("one", 20).required)

        # The execution gate uses the file baseline even before legacy success.
        started, created = repository.begin_assessment_if_due("one", 20, source)
        self.assertTrue(created)
        self.assertEqual(started.phase, maintenance_state.MaintenancePhase.ASSESSMENT)

        cycle, _ = repository.begin_assessment_if_due("one", 5, source)
        for bad_source, count in (("b" * 40, 20), (source, 19)):
            with self.assertRaises(maintenance_state.MaintenanceStateError):
                repository.advance_file_assessment_success_baseline(
                    "one", expected_revision=cycle.revision,
                    source_sha=bad_source, expected_merged_count=count,
                )
        self.assertIsNone(repository.get(
            "one"
        ).last_success_file_assessment_merged_count)
        recorded = repository.advance_file_assessment_success_baseline(
            "one", expected_revision=cycle.revision, source_sha=source,
            expected_merged_count=20,
        )
        self.assertEqual(recorded.last_success_file_assessment_merged_count, 20)
        self.assertIsNone(recorded.last_success_merged_count)
        repository.advance_success_baseline(
            "one", expected_phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            expected_revision=recorded.revision, remote_commit=source,
        )
        for number in range(21, 31):
            self._insert_plan(connection, "one", f"G-{number:03d}", "MERGED")
        self.assertFalse(repository.file_assessment_due("one", 20).required)
        self.assertEqual(repository.file_assessment_due(
            "one", 20, completion_candidate=True,
        ).reason, "stabilization_due")

    def test_completion_candidate_starts_only_after_successful_file_assessment(self):
        connection = self._connection()
        repository = maintenance_state.MaintenanceRepository(connection)
        source = "a" * 40
        connection.execute(
            "INSERT INTO execplan_catalog_revisions "
            "(repo, revision_sha, catalog_fingerprint) VALUES ('one', ?, 'catalog')",
            (source,),
        )
        repository.initialize_file_assessment_start(
            "one", source_sha=source, expected_merged_count=0,
        )
        for number in range(1, 11):
            self._insert_plan(connection, "one", f"G-{number:03d}", "MERGED")
        self.assertFalse(repository.begin_assessment_if_due(
            "one", 20, source, completion_candidate=True,
        )[1])
        connection.execute(
            "UPDATE maintenance_cycles SET last_success_file_assessment_merged_count = 0 "
            "WHERE repo = 'one'"
        )
        cycle, started = repository.begin_assessment_if_due(
            "one", 20, source, completion_candidate=True,
        )
        self.assertTrue(started)
        self.assertEqual(cycle.cycle_start_merged_count, 10)

    def test_empty_stabilization_advances_the_verified_file_baseline_once(self):
        connection = self._connection()
        repository = maintenance_state.MaintenanceRepository(connection)
        source = "a" * 40
        connection.execute(
            "INSERT INTO execplan_catalog_revisions "
            "(repo, revision_sha, catalog_fingerprint) VALUES ('one', ?, 'catalog')",
            (source,),
        )
        repository.initialize_file_assessment_start(
            "one", source_sha=source, expected_merged_count=0,
        )
        connection.execute(
            "UPDATE maintenance_cycles SET last_success_file_assessment_merged_count = 0 "
            "WHERE repo = 'one'"
        )
        for number in range(1, 11):
            self._insert_plan(connection, "one", f"G-{number:03d}", "MERGED")

        inventory = mock.Mock(revision=source)
        with mock.patch.object(
            maintenance_file_ledger.FileReviewRepository,
            "record_success",
        ) as record_success, mock.patch.object(
            maintenance_state,
            "verify_remote_revision",
        ) as verify_remote:
            settled = repository.settle_empty_stabilization_assessment(
                "one",
                20,
                source_sha=source,
                repository_path="/repo",
                base_branch="main",
                inventory=inventory,
            )
        record_success.assert_called_once_with(
            "one", "/repo", "main", inventory, (),
        )
        verify_remote.assert_called_once_with(Path("/repo"), "main", source)

        self.assertEqual(settled.phase, maintenance_state.MaintenancePhase.IDLE)
        self.assertEqual(settled.last_success_file_assessment_merged_count, 10)
        self.assertEqual(
            repository.file_assessment_due(
                "one", 20, completion_candidate=True,
            ).reason,
            "interval_pending",
        )
        for number in range(11, 21):
            self._insert_plan(connection, "one", f"G-{number:03d}", "MERGED")
        with self.assertRaises(maintenance_state.MaintenanceStateError):
            repository.settle_empty_stabilization_assessment(
                "one",
                20,
                source_sha="b" * 40,
                repository_path="/repo",
                base_branch="main",
                inventory=inventory,
            )
        self.assertEqual(
            repository.get("one").last_success_file_assessment_merged_count,
            10,
        )

    def test_file_assessment_rejects_unverified_source_or_count_drift(self):
        connection = self._connection()
        repository = maintenance_state.MaintenanceRepository(connection)
        source = "a" * 40
        connection.execute(
            "INSERT INTO execplan_catalog_revisions "
            "(repo, revision_sha, catalog_fingerprint) VALUES ('one', ?, 'catalog')",
            (source,),
        )
        self._insert_plan(connection, "one", "G-001", "MERGED")
        for bad_source, count in (("b" * 40, 1), (source, 0)):
            with self.assertRaises(maintenance_state.MaintenanceStateError):
                repository.initialize_file_assessment_start(
                    "one", source_sha=bad_source, expected_merged_count=count,
                )
        self.assertIsNone(repository.get("one").file_assessment_start_merged_count)
        repository.initialize_file_assessment_start(
            "one", source_sha=source, expected_merged_count=1,
        )
        connection.execute("DELETE FROM exec_plans WHERE repo = 'one'")
        with self.assertRaises(maintenance_state.MaintenanceStateError):
            repository.file_assessment_due("one", 20)

    def test_additive_file_baseline_schema_preserves_legacy_success(self):
        connection = self._connection()
        old_schema = maintenance_state.MAINTENANCE_SCHEMA.replace(
            "    file_assessment_start_merged_count INTEGER,\n", ""
        ).replace("    last_success_file_assessment_merged_count INTEGER,\n", "")
        connection.execute(old_schema)
        connection.execute(
            "INSERT INTO maintenance_cycles "
            "(repo, last_success_merged_count, full_assessment_completed) "
            "VALUES ('one', 7, 1)"
        )
        repository = maintenance_state.MaintenanceRepository(connection)
        state = repository.get("one")
        self.assertEqual(state.last_success_merged_count, 7)
        self.assertTrue(state.full_assessment_completed)
        self.assertIsNone(state.file_assessment_start_merged_count)
        self.assertIsNone(state.last_success_file_assessment_merged_count)

    def test_plan_sets_round_trip_deterministically_and_reject_invalid_ids(self):
        connection = self._connection()
        for plan_id in ("G-010", "G-002", "G-003"):
            self._insert_plan(connection, "one", plan_id, "READY")
        repository = maintenance_state.MaintenanceRepository(connection)
        cycle = self._start_legacy_assessment(connection, "one", "a" * 40)
        waiting = repository.transition(
            "one",
            expected_phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            expected_revision=cycle.revision,
            next_phase=maintenance_state.MaintenancePhase.PREREQUISITE_WAIT,
            prerequisite_plan_ids=("G-010", "G-002"),
            revalidation_plan_ids=("G-003",),
            publication_stage="remote_confirmed",
            publication_base_sha="a" * 40,
            publication_commit_sha="b" * 40,
            publication_remote_oid="b" * 40,
        )
        self.assertEqual(waiting.prerequisite_plan_ids, ("G-002", "G-010"))
        raw = connection.execute(
            "SELECT prerequisite_plan_ids FROM maintenance_cycles WHERE repo = 'one'"
        ).fetchone()[0]
        self.assertEqual(raw, '["G-002","G-010"]')
        self.assertEqual(waiting.publication_stage, "remote_confirmed")

        for invalid in (("G-002", "G-002"), ("G-999",), ("G-002", 3)):
            with self.subTest(invalid=invalid), self.assertRaises(
                maintenance_state.MaintenanceStateError
            ):
                repository.transition(
                    "one",
                    expected_phase=maintenance_state.MaintenancePhase.PREREQUISITE_WAIT,
                    expected_revision=waiting.revision,
                    next_phase=maintenance_state.MaintenancePhase.ASSESSMENT,
                    prerequisite_plan_ids=invalid,
                )
        self.assertEqual(repository.get("one"), waiting)
        connection.execute(
            "UPDATE maintenance_cycles SET refactoring_plan_ids = '[\"G-999\"]' "
            "WHERE repo = 'one'"
        )
        with self.assertRaises(maintenance_state.MaintenanceStateError):
            repository.get("one")

    def test_refactoring_baseline_waits_for_post_refactor_revalidation(self):
        connection = self._connection()
        self._insert_plan(connection, "one", "G-100", "READY")
        repository = maintenance_state.MaintenanceRepository(connection)
        cycle = self._start_legacy_assessment(connection, "one", "a" * 40)
        refactoring = repository.transition(
            "one",
            expected_phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            expected_revision=cycle.revision,
            next_phase=maintenance_state.MaintenancePhase.REFACTORING,
            refactoring_plan_ids=("G-100",),
        )
        with self.assertRaises(maintenance_state.MaintenanceStateError):
            repository.advance_success_baseline(
                "one",
                expected_phase=maintenance_state.MaintenancePhase.REFACTORING,
                expected_revision=refactoring.revision,
                remote_commit="b" * 40,
            )
        revalidation = repository.transition(
            "one",
            expected_phase=maintenance_state.MaintenancePhase.REFACTORING,
            expected_revision=refactoring.revision,
            next_phase=maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
        )
        completed = repository.advance_success_baseline(
            "one",
            expected_phase=maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
            expected_revision=revalidation.revision,
            remote_commit="c" * 40,
        )
        self.assertEqual(completed.last_success_commit, "c" * 40)

    def test_additive_schema_preserves_projects_execplans_sessions_and_failures(self):
        connection = self._connection()
        self._insert_plan(connection, "one", "G-001", "MERGED")
        before = tuple(
            connection.execute(
                """
                SELECT status, worker_session_id, verify_failure_history
                FROM exec_plans WHERE repo = 'one' AND plan_id = 'G-001'
                """
            ).fetchone()
        )
        maintenance_state.ensure_schema(connection)
        maintenance_state.ensure_schema(connection)
        after = tuple(
            connection.execute(
                """
                SELECT status, worker_session_id, verify_failure_history
                FROM exec_plans WHERE repo = 'one' AND plan_id = 'G-001'
                """
            ).fetchone()
        )
        self.assertEqual(after, before)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 2
        )

    def test_existing_database_connection_adds_maintenance_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            database = sqlite3.connect(state_dir / "loop-manager.db")
            database.row_factory = sqlite3.Row
            database.execute(loop_manager.SCHEMA)
            loop_manager._ensure_execplans_schema(database)
            database.execute(
                "INSERT INTO projects (repo, base_branch) VALUES ('one', 'main')"
            )
            self._insert_plan(database, "one", "G-001", "MERGED")
            database.commit()
            database.close()

            with loop_manager.connect_database(state_dir) as migrated:
                tables = {
                    row[0]
                    for row in migrated.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                plan = migrated.execute(
                    "SELECT status, worker_session_id FROM exec_plans "
                    "WHERE repo = 'one' AND plan_id = 'G-001'"
                ).fetchone()
            self.assertIn("maintenance_cycles", tables)
            self.assertEqual(tuple(plan), ("MERGED", "session-G-001"))


class BoundedGitChangeContextTest(unittest.TestCase):
    def _git(self, repo: Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            check=True,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        return completed.stdout.strip()

    def _commit(self, repo: Path, path: str, contents: str, message: str) -> str:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")
        self._git(repo, "add", path)
        self._git(repo, "commit", "-m", message)
        return self._git(repo, "rev-parse", "HEAD")

    def test_normal_empty_and_bounded_remote_ranges_do_not_change_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "-b", "main")
            self._git(repo, "config", "user.email", "test@example.com")
            self._git(repo, "config", "user.name", "Test")
            baseline = self._commit(repo, "z.txt", "z\n", "baseline")
            self._commit(repo, "b.txt", "b\n", "second")
            current = self._commit(repo, "a.txt", "a\n", "third")
            self._git(repo, "update-ref", "refs/remotes/origin/main", current)
            before_head = self._git(repo, "symbolic-ref", "HEAD")
            before_status = self._git(repo, "status", "--porcelain=v1")

            context = maintenance_git.bounded_remote_change_context(
                repo, "main", baseline, current
            )
            self.assertTrue(context.ok)
            self.assertEqual(
                tuple(commit.subject for commit in context.commits),
                ("second", "third"),
            )
            self.assertEqual(context.commits[0].paths, ("b.txt",))
            empty = maintenance_git.bounded_remote_change_context(
                repo, "main", current, current
            )
            self.assertEqual(empty.commits, ())

            with mock.patch.object(maintenance_git, "MAX_CHANGE_COMMITS", 1):
                bounded = maintenance_git.bounded_remote_change_context(
                    repo, "main", baseline, current
                )
            self.assertTrue(bounded.truncated)
            self.assertEqual(len(bounded.commits), 1)
            with mock.patch.object(
                maintenance_git, "MAX_CHANGE_CONTEXT_BYTES", 46
            ):
                byte_bounded = maintenance_git.bounded_remote_change_context(
                    repo, "main", baseline, current
                )
            self.assertTrue(byte_bounded.truncated)
            used = sum(
                len(commit.sha.encode("utf-8"))
                + len(commit.subject.encode("utf-8"))
                + sum(len(path.encode("utf-8")) for path in commit.paths)
                for commit in byte_bounded.commits
            )
            self.assertLessEqual(used, 46)
            self.assertEqual(self._git(repo, "symbolic-ref", "HEAD"), before_head)
            self.assertEqual(self._git(repo, "status", "--porcelain=v1"), before_status)

    def test_nonancestor_and_missing_baselines_return_diagnostic_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "-b", "main")
            self._git(repo, "config", "user.email", "test@example.com")
            self._git(repo, "config", "user.name", "Test")
            current = self._commit(repo, "tracked.txt", "value\n", "current")
            self._git(repo, "update-ref", "refs/remotes/origin/main", current)
            tree = self._git(repo, "write-tree")
            sibling = self._git(repo, "commit-tree", tree, "-m", "sibling")
            before = self._git(repo, "status", "--porcelain=v1")

            nonancestor = maintenance_git.bounded_remote_change_context(
                repo, "main", sibling, current
            )
            self.assertFalse(nonancestor.ok)
            self.assertIn("조상", nonancestor.error or "")
            missing = maintenance_git.bounded_remote_change_context(
                repo, "main", "f" * 40, current
            )
            self.assertFalse(missing.ok)
            self.assertIn("이력", missing.error or "")
            self.assertEqual(self._git(repo, "status", "--porcelain=v1"), before)

    def test_oversized_git_failure_is_not_misreported_as_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "-b", "main")
            self._git(repo, "config", "user.email", "test@example.com")
            self._git(repo, "config", "user.name", "Test")
            baseline = self._commit(repo, "one.txt", "one\n", "baseline")
            current = self._commit(repo, "two.txt", "two\n", "current")
            self._git(repo, "update-ref", "refs/remotes/origin/main", current)
            failed = subprocess.CompletedProcess(
                ["git"],
                1,
                stdout="x" * 100,
                stderr="fatal: simulated Git failure",
            )

            with mock.patch(
                "loop_manager_internal.maintenance.maintenance_git._bounded_git_output",
                return_value=(failed, True),
            ):
                context = maintenance_git.bounded_remote_change_context(
                    repo, "main", baseline, current
                )

            self.assertFalse(context.ok)
            self.assertFalse(context.truncated)
            self.assertIn("안전하게", context.error or "")

    def test_git_capture_bounds_actual_stdout_and_stderr_without_temp_spooling(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "-b", "main")
            self._git(repo, "config", "user.email", "test@example.com")
            self._git(repo, "config", "user.name", "Test")
            baseline = self._commit(repo, "tracked.txt", "value\n", "baseline")
            tree = self._git(repo, "write-tree")
            huge_subject = "s" * (maintenance_git.MAX_COMMIT_SUBJECT_BYTES * 8)
            created = subprocess.run(
                ["git", "-C", str(repo), "commit-tree", tree, "-p", baseline],
                input=huge_subject,
                check=True,
                text=True,
                encoding="utf-8",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            current = created.stdout.strip()
            self._git(repo, "update-ref", "refs/remotes/origin/main", current)

            context = maintenance_git.bounded_remote_change_context(
                repo, "main", baseline, current
            )
            self.assertTrue(context.ok)
            self.assertTrue(context.truncated)
            self.assertLessEqual(
                len(context.commits[0].subject.encode("utf-8")),
                maintenance_git.MAX_COMMIT_SUBJECT_BYTES,
            )

            invalid_object = "missing-" + (
                "x" * (maintenance_git.MAX_GIT_ERROR_BYTES * 2)
            )
            failed, output_overflow = maintenance_git._bounded_git_output(
                repo, 16, "show", invalid_object
            )
            self.assertNotEqual(failed.returncode, 0)
            self.assertFalse(output_overflow)
            self.assertLessEqual(
                len(failed.stdout.encode("utf-8")), 16
            )
            self.assertLessEqual(
                len(failed.stderr.encode("utf-8")),
                maintenance_git.MAX_GIT_ERROR_BYTES,
            )


class FileReviewLedgerTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        # Exercise Git mode fixtures even when filesystem permissions are ignored.
        self.git("config", "core.filemode", "false")
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.connection)
        self.connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')", (str(self.repo),)
        )
        maintenance_state.ensure_schema(self.connection)
        self.ledger = maintenance_file_ledger.FileReviewRepository(self.connection)

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    def write(self, path, data):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def publish(self):
        self.git("add", "-A")
        self.git("commit", "-m", "fixture")
        revision = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", revision)
        return revision

    def inventory(self, revision):
        return maintenance_file_inventory.tracked_source_inventory(self.repo, "main", revision)

    def reviewed(self, sources):
        return tuple(
            maintenance_file_ledger.ReviewedSource(
                source.path, source.content_hash, source.mode, source.revision,
                "Reviewed implementation and related test responsibilities.",
            ) for source in sources
        )

    def root_results(self, inventory):
        from tests.test_maintenance_file_review import root_results_for
        return root_results_for(self.repo, inventory)

    def record(self, inventory, reviewed):
        return self.ledger.record_success(
            str(self.repo), str(self.repo), "main", inventory, reviewed,
            root_reviews=self.root_results(inventory),
        )

    def prepare_empty_stabilization(self, source):
        self.connection.execute(
            "INSERT INTO execplan_catalog_revisions "
            "(repo, revision_sha, catalog_fingerprint) VALUES (?, ?, 'catalog')",
            (str(self.repo), source),
        )
        self.connection.execute(
            "INSERT INTO maintenance_cycles "
            "(repo, file_assessment_start_merged_count, "
            "last_success_file_assessment_merged_count, revision) "
            "VALUES (?, 0, 0, 1)",
            (str(self.repo),),
        )
        for number in range(1, 11):
            self.connection.execute(
                "INSERT INTO exec_plans "
                "(repo, plan_id, path, title, sources, depends_on, priority, "
                "content_hash, status) VALUES (?, ?, ?, ?, '[]', '[]', "
                "'normal', ?, 'MERGED')",
                (
                    str(self.repo), f"G-{number:03d}",
                    f"docs/plans/[G-{number:03d}] Done.md", "Done",
                    f"hash-{number}",
                ),
            )

    def settle_empty_stabilization(self, source, inventory):
        return maintenance_state.MaintenanceRepository(
            self.connection
        ).settle_empty_stabilization_assessment(
            str(self.repo),
            20,
            source_sha=source,
            repository_path=str(self.repo),
            base_branch="main",
            inventory=inventory,
        )

    def test_first_review_then_content_mode_rename_new_and_deleted_paths(self):
        self.write("src/alpha.go", b"package alpha\n")
        self.write("tests/check.py", b"assert True\n")
        self.write("docs/PRODUCT.md", b"# Product\n")
        first = self.publish()
        inventory = self.inventory(first)
        selected = self.ledger.selected_sources(str(self.repo), inventory)
        self.assertEqual([source.path for source in selected], ["src/alpha.go", "tests/check.py"])
        first_success = self.record(inventory, self.reviewed(selected))
        first_reviews = self.ledger.reviews(str(self.repo))
        self.assertEqual(first_success.source_revision, first)

        self.write("docs/PRODUCT.md", b"# Updated product\n")
        second = self.publish()
        unchanged = self.inventory(second)
        self.assertEqual(self.ledger.selected_sources(str(self.repo), unchanged), ())
        self.record(unchanged, ())
        self.assertEqual(self.ledger.reviews(str(self.repo)), first_reviews)
        self.assertEqual(self.ledger.last_success(str(self.repo)).source_revision, second)

        self.git("update-index", "--chmod=+x", "--", "src/alpha.go")
        (self.repo / "tests/check.py").rename(self.repo / "tests/renamed.py")
        self.write("scripts/verify", b"#!/bin/sh\nexit 0\n")
        third = self.publish()
        changed = self.inventory(third)
        selected = self.ledger.selected_sources(str(self.repo), changed)
        self.assertEqual(
            [source.path for source in selected],
            ["scripts/verify", "src/alpha.go", "tests/renamed.py"],
        )
        self.assertEqual(next(source.mode for source in selected if source.path == "src/alpha.go"), "100755")
        with self.assertRaises(maintenance_file_ledger.FileReviewLedgerError):
            self.record(changed, self.reviewed(selected[:-1]))
        self.assertEqual(self.ledger.last_success(str(self.repo)).source_revision, second)
        self.record(changed, self.reviewed(selected))
        retired = {review.path: review for review in self.ledger.reviews(str(self.repo), include_retired=True)}
        self.assertFalse(retired["tests/check.py"].active)
        self.assertEqual(retired["src/alpha.go"].mode, "100755")
        self.assertEqual(retired["tests/renamed.py"].source_revision, third)

        (self.repo / "src/alpha.go").unlink()
        fourth = self.publish()
        deleted = self.inventory(fourth)
        self.assertEqual(self.ledger.selected_sources(str(self.repo), deleted), ())
        self.record(deleted, ())
        self.assertNotIn("src/alpha.go", {review.path for review in self.ledger.reviews(str(self.repo))})
        self.assertFalse(
            {review.path: review for review in self.ledger.reviews(str(self.repo), include_retired=True)}
            ["src/alpha.go"].active
        )

    def test_empty_stabilization_retires_deleted_review_before_same_path_returns(self):
        self.write("src/alpha.go", b"package alpha\n")
        first = self.publish()
        first_inventory = self.inventory(first)
        self.record(first_inventory, self.reviewed(first_inventory.sources))
        (self.repo / "src/alpha.go").unlink()
        empty_source = self.publish()
        empty_inventory = self.inventory(empty_source)
        self.assertEqual(empty_inventory.sources, ())
        self.prepare_empty_stabilization(empty_source)

        settled = self.settle_empty_stabilization(empty_source, empty_inventory)

        self.assertEqual(settled.last_success_file_assessment_merged_count, 10)
        retired = {review.path: review for review in self.ledger.reviews(
            str(self.repo), include_retired=True,
        )}
        self.assertFalse(retired["src/alpha.go"].active)
        self.assertEqual(self.ledger.root_placements(str(self.repo)), ())
        self.write("src/alpha.go", b"package alpha\n")
        returned_source = self.publish()
        returned_inventory = self.inventory(returned_source)
        self.assertEqual(
            [source.path for source in self.ledger.selected_sources(
                str(self.repo), returned_inventory,
            )],
            ["src/alpha.go"],
        )

    def test_empty_selection_requires_investigated_root_placements(self):
        self.write(".agents/guide.txt", b"guidance\n")
        self.write("AGENTS.md", b"# guidance\n")
        self.write("README.md", b"# project\n")
        self.write("config/settings.ini", b"[loop]\n")
        self.write("docs/PRODUCT.md", b"# Product\n")
        revision = self.publish()
        inventory = self.inventory(revision)

        self.assertEqual(inventory.sources, ())
        self.assertEqual(self.ledger.selected_sources(str(self.repo), inventory), ())
        with self.assertRaisesRegex(maintenance_file_ledger.FileReviewLedgerError, "루트"):
            self.ledger.record_success(str(self.repo), str(self.repo), "main", inventory, ())
        self.assertIsNone(self.ledger.last_success(str(self.repo)))
        self.record(inventory, ())

        placements = {item.path: item for item in self.ledger.root_placements(str(self.repo))}
        self.assertEqual(placements[".agents"].category, "tool_required")
        self.assertEqual(placements["AGENTS.md"].category, "tool_required")
        self.assertEqual(placements["docs"].category, "tool_required")
        self.assertEqual(placements["README.md"].category, "uncertain")
        self.assertEqual(placements["config"].category, "uncertain")
        rows = self.connection.execute(
            "SELECT DISTINCT source_revision FROM root_placement_reviews WHERE repo = ?",
            (str(self.repo),),
        ).fetchall()
        self.assertEqual([row[0] for row in rows], [revision])

    def test_root_investigation_rejects_missing_fields_forged_references_and_stale_results(self):
        self.write("scratch.png", b"\x89PNG\x00asset")
        revision = self.publish()
        inventory = self.inventory(revision)
        good = self.root_results(inventory)
        for invalid in ((), (dict(good[0], ownership=""),),
                        (dict(good[0], references=[{"path": ".", "excerpt": "invented evidence"}]),),
                        (dict(good[0], category="execution_build_deploy"),),
                        (dict(good[0], destination="assets/scratch.png"),)):
            with self.subTest(invalid=invalid):
                with self.assertRaises(maintenance_file_ledger.FileReviewLedgerError):
                    self.ledger.record_success(str(self.repo), str(self.repo), "main", inventory, (), root_reviews=invalid)
                self.assertIsNone(self.ledger.last_success(str(self.repo)))
        self.ledger.record_success(str(self.repo), str(self.repo), "main", inventory, (), root_reviews=good)
        self.ledger.record_success(str(self.repo), str(self.repo), "main", inventory, ())
        self.write("README.md", b"Asset is not used by the application.\n")
        changed = self.inventory(self.publish())
        with self.assertRaises(maintenance_file_ledger.FileReviewLedgerError):
            self.ledger.record_success(str(self.repo), str(self.repo), "main", changed, ())
        self.assertEqual(self.ledger.last_success(str(self.repo)).source_revision, revision)

    def test_root_placement_uses_agent_usage_evidence_and_rechecks_reference_drift(self):
        self.write("scratch.png", b"\x89PNG\x00asset")
        self.write("README.md", b"User preview loads scratch.png from the repository root.\n")
        inventory = self.inventory(self.publish())
        roots = list(self.root_results(inventory))
        asset = next(item for item in roots if item["path"] == "scratch.png")
        asset.update(category="execution_build_deploy", user_path="User preview loads the image from the repository root.",
                     ownership="Preview UI owns the image referenced by README.md.",
                     references=[{"path": "README.md", "excerpt": "User preview loads scratch.png from the repository root."}],
                     reason="The documented preview path requires the image at the root.")
        self.ledger.record_success(str(self.repo), str(self.repo), "main", inventory, (), root_reviews=roots)
        self.assertEqual({item.path: item for item in self.ledger.root_placements(str(self.repo))}["scratch.png"].category, "execution_build_deploy")
        self.write("README.md", b"User preview no longer uses a root image.\n")
        changed = self.inventory(self.publish())
        self.assertFalse(maintenance_file_publication.sources_match_remote(
            self.repo, "main", changed.revision,
            maintenance_file_publication.AssessmentEvidence(inventory, (), (), 0, tuple(roots))))
        with self.assertRaises(maintenance_file_ledger.FileReviewLedgerError):
            self.ledger.record_success(str(self.repo), str(self.repo), "main", changed, (), root_reviews=roots)

    def test_empty_stabilization_source_drift_keeps_ledger_and_baseline(self):
        self.write("src/alpha.go", b"package alpha\n")
        first = self.publish()
        first_inventory = self.inventory(first)
        self.record(first_inventory, self.reviewed(first_inventory.sources))
        (self.repo / "src/alpha.go").unlink()
        empty_source = self.publish()
        empty_inventory = self.inventory(empty_source)
        self.prepare_empty_stabilization(empty_source)
        self.write("src/new.go", b"package new\n")
        self.publish()

        with self.assertRaises(maintenance_file_inventory.SourceInventoryError):
            self.settle_empty_stabilization(empty_source, empty_inventory)

        cycle = maintenance_state.MaintenanceRepository(self.connection).get(
            str(self.repo)
        )
        self.assertEqual(cycle.last_success_file_assessment_merged_count, 0)
        self.assertTrue({review.path: review for review in self.ledger.reviews(
            str(self.repo), include_retired=True,
        )}["src/alpha.go"].active)

    def test_wrong_identity_and_remote_drift_leave_success_unchanged(self):
        self.write("src/alpha.go", b"package alpha\n")
        first = self.publish()
        inventory = self.inventory(first)
        selected = self.ledger.selected_sources(str(self.repo), inventory)
        result = self.reviewed(selected)[0]
        self.git("update-ref", "refs/remotes/origin/feature", first)
        with self.assertRaises(maintenance_file_ledger.FileReviewLedgerError):
            self.ledger.record_success(
                str(self.repo), str(self.repo), "feature", inventory, (result,),
            )
        with self.assertRaises(maintenance_file_ledger.FileReviewLedgerError):
            self.record(inventory, (replace(result, content_hash="0" * 64),))
        with self.assertRaises(maintenance_file_ledger.FileReviewLedgerError):
            self.record(inventory, (replace(result, evidence="too short"),))
        forged = replace(inventory, sources=(replace(selected[0], content_hash="0" * 64),))
        with self.assertRaises(maintenance_file_ledger.FileReviewLedgerError):
            self.record(forged, self.reviewed(forged.sources))
        self.assertIsNone(self.ledger.last_success(str(self.repo)))
        self.write("src/beta.go", b"package beta\n")
        self.publish()
        with self.assertRaises(maintenance_file_inventory.SourceInventoryError):
            self.record(inventory, self.reviewed(selected))
        self.assertIsNone(self.ledger.last_success(str(self.repo)))

    def test_content_change_and_project_isolation(self):
        self.write("src/alpha.go", b"package alpha\n")
        first = self.publish()
        inventory = self.inventory(first)
        selected = self.ledger.selected_sources(str(self.repo), inventory)
        self.record(inventory, self.reviewed(selected))
        other = str(self.repo) + "-other"
        self.connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')", (other,)
        )
        self.assertEqual(self.ledger.selected_sources(other, inventory), selected)
        with self.assertRaises(maintenance_file_ledger.FileReviewLedgerError):
            self.ledger.record_success(other, str(self.repo), "main", inventory, self.reviewed(selected))

        self.write("src/alpha.go", b"package alpha\nfunc changed() {}\n")
        second = self.publish()
        changed = self.inventory(second)
        current = self.ledger.selected_sources(str(self.repo), changed)
        self.assertEqual(len(current), 1)
        self.assertNotEqual(current[0].content_hash, selected[0].content_hash)
        self.record(changed, self.reviewed(current))
        self.assertEqual(self.ledger.reviews(str(self.repo))[0].source_revision, second)
        self.assertEqual(self.ledger.selected_sources(other, changed), current)

    def test_success_write_rolls_back_file_rows_when_project_record_fails(self):
        self.write("src/alpha.go", b"package alpha\n")
        revision = self.publish()
        inventory = self.inventory(revision)
        selected = self.ledger.selected_sources(str(self.repo), inventory)
        self.connection.execute(
            "CREATE TRIGGER reject_file_success BEFORE INSERT ON file_assessment_successes "
            "BEGIN SELECT RAISE(ABORT, 'simulated success failure'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.record(inventory, self.reviewed(selected))
        self.assertEqual(self.ledger.reviews(str(self.repo)), ())
        self.assertEqual(self.ledger.root_placements(str(self.repo)), ())
        self.assertIsNone(self.ledger.last_success(str(self.repo)))

    def test_file_publication_requires_complete_reviews_and_accepts_empty_inventory(self):
        self.write("docs/PRODUCT.md", b"# Product\n")
        empty_revision = self.publish()
        from tests.test_maintenance_file_review import review, root_results_for
        provisional = review.ProvisionalReviewRepository(self.connection)
        inventory = self.inventory(empty_revision)
        source = maintenance_file_ledger.root_review_source(self.repo, inventory)
        provisional.start(str(self.repo), 1, source)
        provisional.complete(str(self.repo), 1, source, {"root_reviews": list(root_results_for(self.repo, inventory)),
            "path": source.path, "content_hash": source.content_hash, "mode": source.mode, "source_revision": source.revision})
        empty = maintenance_file_publication.collect(
            self.connection, str(self.repo), self.repo, "main", 1,
            empty_revision,
        )
        self.assertEqual(empty.reviewed, ())
        self.assertEqual(empty.code_maps, ())
        self.write("src/new.go", b"package new\n")
        revision = self.publish()
        with self.assertRaisesRegex(
            maintenance_file_publication.FileAssessmentPublicationError,
            "검토",
        ):
            maintenance_file_publication.collect(
                self.connection, str(self.repo), self.repo, "main", 1,
                revision,
            )

    def test_published_file_evidence_and_count_commit_atomically(self):
        self.write("src/alpha.go", b"package alpha\n")
        source = self.publish()
        inventory = self.inventory(source)
        selected = self.ledger.selected_sources(str(self.repo), inventory)
        evidence = maintenance_file_publication.AssessmentEvidence(
            inventory, self.reviewed(selected),
            ((selected[0].path, json.dumps({
                "responsibility": "Alpha package responsibilities",
                "entry_points": [], "public_symbols": [],
                "direct_references": [], "related_tests": [],
            })),), 0, self.root_results(inventory),
        )
        self.connection.execute(
            "INSERT INTO execplan_catalog_revisions "
            "(repo, revision_sha, catalog_fingerprint) VALUES (?, ?, 'catalog')",
            (str(self.repo), source),
        )
        self.connection.execute(
            "INSERT INTO maintenance_cycles "
            "(repo, phase, cycle_base_sha, file_assessment_start_merged_count, revision) "
            "VALUES (?, 'assessment', ?, 0, 1)", (str(self.repo), source),
        )
        self.connection.commit()
        port = loop_manager._PlanningStatePorts(
            self.connection,
            loop_manager.runtime_state.RuntimeStateRepository(self.connection),
            maintenance_state.MaintenanceRepository(self.connection),
            Path("/unused"),
        )
        with mock.patch.object(
            port._maintenance_repository,
            "advance_file_assessment_success_baseline",
            side_effect=maintenance_state.MaintenanceStateError("injected"),
        ):
            with self.assertRaisesRegex(maintenance_state.MaintenanceStateError, "injected"):
                with self.connection:
                    port.complete_file_assessment(
                        str(self.repo), self.repo, "main", 1, source, evidence,
                    )
        self.assertEqual(self.ledger.reviews(str(self.repo)), ())
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM successful_code_maps WHERE repo = ?", (str(self.repo),)
        ).fetchone())
        with self.connection:
            port.complete_file_assessment(
                str(self.repo), self.repo, "main", 1, source, evidence,
            )
        self.assertEqual(self.ledger.reviews(str(self.repo))[0].source_revision, source)
        self.assertEqual(self.connection.execute(
            "SELECT path FROM successful_code_maps WHERE repo = ?", (str(self.repo),)
        ).fetchone()[0], "src/alpha.go")
        self.assertEqual(maintenance_state.MaintenanceRepository(
            self.connection
        ).get(str(self.repo)).last_success_file_assessment_merged_count, 0)

        self.write("src/alpha.go", b"package changed\n")
        drift = self.publish()
        with self.assertRaises(maintenance_file_ledger.FileReviewLedgerError):
            maintenance_file_publication.promote(
                self.connection, str(self.repo), self.repo, "main", drift,
                evidence,
            )

    def test_additive_schema_recheck_preserves_file_reviews(self):
        self.write("src/alpha.go", b"package alpha\n")
        revision = self.publish()
        inventory = self.inventory(revision)
        self.record(inventory, self.reviewed(inventory.sources))
        before = self.ledger.reviews(str(self.repo))
        success = self.ledger.last_success(str(self.repo))

        maintenance_state.ensure_schema(self.connection)
        maintenance_state.ensure_schema(self.connection)

        self.assertEqual(self.ledger.reviews(str(self.repo)), before)
        self.assertEqual(self.ledger.last_success(str(self.repo)), success)


if __name__ == "__main__":
    unittest.main()
