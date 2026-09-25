import sqlite3
import subprocess
import tempfile
import unittest
import hashlib
import json
from pathlib import Path
from unittest import mock

import document_drafts
import loop_manager
import maintenance_git
import maintenance_state


class DocumentDraftTest(unittest.TestCase):
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
        draft = self._create()
        show = parser.parse_args(["document-drafts", str(self.repo), "show",
                                  draft.draft_id])
        with mock.patch.object(loop_manager, "is_loop_manager_repository", return_value=True), \
             mock.patch.object(loop_manager, "connect_database", return_value=self.database), \
             mock.patch.object(loop_manager, "operation_print") as printed:
            self.assertEqual(loop_manager._document_drafts_command(show, self.root, self.root / "config.ini"), 0)
            self.assertIn(draft.proposed_hash, printed.call_args.args[0])
        args = parser.parse_args(["document-drafts", str(self.repo), "decide",
                                  draft.draft_id, draft.proposed_hash, "approve"])
        self.assertEqual((args.draft_id, args.proposed_hash, args.decision),
                         (draft.draft_id, draft.proposed_hash, "approve"))
        with mock.patch.object(loop_manager, "is_loop_manager_repository", return_value=True), \
             mock.patch.object(loop_manager, "connect_database", return_value=self.database), \
             mock.patch.object(loop_manager, "git") as fetched, \
             mock.patch.object(loop_manager, "operation_print"):
            self.assertEqual(loop_manager._document_drafts_command(args, self.root, self.root / "config.ini"), 0)
            fetched.assert_called_once()
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
        proposal = b"# Product\n\nRule.\n"
        response = "DOCUMENT_DRAFT_RESULT: " + json.dumps({
            "status": "candidate", "content": proposal.decode(),
            "meaning_evidence": "Only duplicate wording changed; product behavior remains unchanged.",
        })
        args = loop_manager.build_parser().parse_args([
            "document-drafts", str(self.repo), "generate", "docs/PRODUCT.md",
        ])
        with mock.patch.object(loop_manager, "is_loop_manager_repository", return_value=True), \
             mock.patch.object(loop_manager, "connect_database", return_value=self.database), \
             mock.patch.object(loop_manager, "git"), \
             mock.patch.object(loop_manager, "run_agent", return_value=(0, response)) as agent, \
             mock.patch.object(loop_manager, "operation_print"):
            self.assertEqual(loop_manager._document_drafts_command(
                args, self.root, self.root / "config.ini"), 0)
            self.assertTrue(agent.call_args.kwargs["read_only"])
            self.assertEqual(agent.call_args.args[1], loop_manager.ROOT)
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


class MaintenanceStateRepositoryTest(unittest.TestCase):
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
        cycle, created = repository.begin_assessment_if_due("one", 5, "a" * 40)
        self.assertTrue(created)
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

    def test_plan_sets_round_trip_deterministically_and_reject_invalid_ids(self):
        connection = self._connection()
        for plan_id in ("G-010", "G-002", "G-003"):
            self._insert_plan(connection, "one", plan_id, "READY")
        repository = maintenance_state.MaintenanceRepository(connection)
        cycle, _ = repository.begin_assessment_if_due("one", 5, "a" * 40)
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
        cycle, _ = repository.begin_assessment_if_due("one", 5, "a" * 40)
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
                "maintenance_git._bounded_git_output",
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


if __name__ == "__main__":
    unittest.main()
