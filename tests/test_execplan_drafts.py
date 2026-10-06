"""Unit coverage for published ExecPlan decision holds; Git/agents are fakes."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.catalog import execplan_catalog, execplan_drafts, catalog_service
from loop_manager_internal.planning import planner_publication, planner_validation
from loop_manager_internal.planning.planning_catalog import (
    MutableReadySnapshot, mutable_snapshot_token, snapshot_from_dict, snapshot_to_dict,
)
from loop_manager_internal.planning.planning_contracts import choose_planning_decision, PlanningDecisionKind
from loop_manager_internal.storage import execplan_catalog_state, execplan_draft_state, runtime_state
from loop_manager_internal.notifications import work_exhaustion
from tests.support.catalog import plan_text, make_catalog_root, FakeDefinitionPort
from tests.support.database import AutomationCatalogFixture


QUESTION = """\n## Draft Decision

Question: Which storage policy should this goal use?
Facts: Both policies preserve the accepted goal.
Options: A retains history; B retains only the current value.
Impact: The choice changes the implementation and recovery tests.
"""
PATH = "docs/plans/[G-001] Decision.md"


def draft_text(plan_id="G-001", title="Decision"):
    return plan_text(plan_id, title).replace("risk: low\n", "risk: low\ndraft: true\n") + QUESTION


def accepted_text(text, revision, decision="approve", answer="Use policy A and retain history."):
    return text.replace("draft: true\n", "") + (
        f"\n## User Decision\n\nDecision: {decision}\nRevision: {revision}\nAnswer: {answer}\n"
    )


class ExecPlanDraftDocumentTest(unittest.TestCase):
    def test_optional_strict_readiness_and_required_question_contract(self):
        legacy = execplan_catalog.parse_execplan_text(PATH, plan_text("G-001", "Decision"))
        self.assertNotIn("draft", legacy)
        parsed = execplan_catalog.parse_execplan_text(PATH, draft_text())
        self.assertIs(parsed["draft"], True)
        self.assertIn("Which storage", parsed["draft_question"])
        for value in ("yes", "TRUE", "1", "[]", ""):
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, "draft"):
                execplan_catalog.parse_execplan_text(PATH, draft_text().replace("draft: true", "draft: " + value))
        for field in ("Question", "Facts", "Options", "Impact"):
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "Draft Decision"):
                execplan_catalog.parse_execplan_text(PATH, draft_text().replace(field + ":", "Missing:"))
        with self.assertRaisesRegex(RuntimeError, "중복"):
            execplan_catalog.parse_execplan_text(PATH, draft_text() + "Question: second?\n")

    def test_draft_keeps_existing_identity_source_and_dependency_validation(self):
        for text in (draft_text().replace("complexity: low", "complexity: invalid"),
                     draft_text().replace("docs/PRODUCT.md", "README.md")):
            with self.assertRaises(RuntimeError):
                execplan_catalog.parse_execplan_text(PATH, text)
        with self.assertRaisesRegex(RuntimeError, "depends_on"):
            execplan_catalog.snapshot_from_documents({PATH: draft_text().replace("depends_on: []", "depends_on:\n  - G-999")})

    def test_agent_cannot_approve_clear_or_repeat_a_question(self):
        original = draft_text()
        for text in (original.replace("draft: true\n", ""), accepted_text(original, "a" * 64)):
            with self.subTest(text=text[-80:]), self.assertRaises(RuntimeError):
                execplan_drafts.validate_agent_change(execplan_catalog.parse_execplan_text(PATH, text), original)
        with self.assertRaises(RuntimeError):
            execplan_drafts.validate_agent_change(execplan_catalog.parse_execplan_text(PATH, accepted_text(original, "a" * 64)))
        catalog = execplan_catalog.snapshot_from_documents({PATH: original,
            "docs/plans/[G-002] Duplicate.md": draft_text("G-002", "Duplicate")}).active_metadata()
        with self.assertRaisesRegex(RuntimeError, "중복"):
            execplan_drafts.validate_questions(catalog)
        independent = draft_text("G-002", "Duplicate").replace("A concrete structural responsibility problem", "A different feature")
        catalog = execplan_catalog.snapshot_from_documents({PATH: original,
            "docs/plans/[G-002] Duplicate.md": independent}).active_metadata()
        execplan_drafts.validate_questions(catalog)

    def test_prompt_and_snapshot_expose_readiness_without_execution_state(self):
        from loop_manager_internal.maintenance.maintenance_planning import ReadyPlanSnapshot
        text = draft_text()
        snapshot = ReadyPlanSnapshot("G-001", PATH, "Decision", ("docs/PRODUCT.md",), (),
                                     "low", "low", "normal", hashlib.sha256(text.encode()).hexdigest(), "a" * 40, text)
        batch = MutableReadySnapshot("repo", "a" * 40, (snapshot,), mutable_snapshot_token("repo", "a" * 40, (snapshot,)))
        self.assertIs(json.loads(batch.prompt_payload())["plans"][0]["draft"], True)
        prompt = loop_manager.planner_prompt(None, mutable_snapshot=batch)
        self.assertIn("draft: true", prompt)
        self.assertIn("decision_wait.revision", prompt)
        self.assertIn("자기 승인", prompt)
        for text, expected in ((text, True), (plan_text("G-001", "Decision"), False),
                               (text.replace("draft: true", "draft: false"), False)):
            with self.subTest(draft=expected, text=text[:160]):
                original = replace(snapshot, text=text, content_hash=hashlib.sha256(text.encode()).hexdigest())
                restored = snapshot_from_dict(snapshot_to_dict(original))
                self.assertIs(restored.draft, expected)
                restored_batch = replace(batch, plans=(restored,))
                self.assertIs(json.loads(restored_batch.prompt_payload())["plans"][0]["draft"], expected)

    def test_mixed_batch_is_valid_but_agent_decision_is_a_validation_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_catalog_root(directory)
            (root / PATH).write_text(draft_text(), encoding="utf-8")
            second = "docs/plans/[G-002] Independent.md"
            (root / second).write_text(plan_text("G-002", "Independent"), encoding="utf-8")
            effects = planner_validation.ValidatePlannerResultEffects(
                _parse_execplan=loop_manager._parse_execplan,
                _stored_string_list=lambda value, _field: tuple(json.loads(value)),
                _validate_future_idea_separation=mock.Mock(), changed_files=lambda *_: [PATH, second],
                contains_guidance_sections=lambda _: False,
                file_hash=lambda path: hashlib.sha256(path.read_bytes()).hexdigest(),
                git=lambda *_args, **_kw: SimpleNamespace(returncode=0, stdout=""),
                load_execplan_catalog=loop_manager.load_execplan_catalog,
                mutable_snapshot_token=mutable_snapshot_token,
            )
            result = planner_validation.validate_planner_result(effects, root, "a" * 40, [])
            self.assertEqual(len(result.plans), 2)
            self.assertIs(result.plans[0][1]["draft"], True)
            restored = loop_manager._planning_result_from_manifest(loop_manager._planning_result_manifest(result))
            self.assertEqual(restored, result)
            self.assertIs(restored.plans[0][1]["draft"], True)
            (root / PATH).write_text(accepted_text(draft_text(), "a" * 64), encoding="utf-8")
            with self.assertRaisesRegex(loop_manager.PlannerValidationError, "User Decision"):
                planner_validation.validate_planner_result(effects, root, "a" * 40, [])


class ExecPlanDraftCatalogTest(AutomationCatalogFixture, unittest.TestCase):
    def setUp(self):
        self.connection = self._connection()
        self.inputs = {"docs/PRODUCT.md": "product", "docs/ARCHITECTURE.md": "architecture"}
        self.text = draft_text()
        self.documents = {PATH: self.text}
        self.reconcile(self.documents, "a" * 40)

    def reconcile(self, documents, revision="b" * 40, *, inputs=None, fault=None, revisions=None):
        snapshot = execplan_catalog.snapshot_from_documents(documents, revision_sha=revision)
        pinned = execplan_catalog_state.PinnedCatalog(revision, snapshot,
            frozenset({"docs/PRODUCT.md"}), draft_inputs=self.inputs if inputs is None else inputs,
            remote_url="https://github.com/owner/project")
        repository = execplan_catalog_state.DefinitionStateRepository(self.connection, fault_hook=fault)
        return execplan_catalog_state.CatalogReconciliationService(
            repository, FakeDefinitionPort(pinned, revisions=revisions)).reconcile("repo")

    def waiting(self):
        return execplan_draft_state.evidence(self.connection, "repo", "G-001")

    def test_mixed_catalog_excludes_draft_and_transitive_dependencies(self):
        documents = dict(self.documents)
        for plan_id, title, deps in (("G-002", "Direct", ("G-001",)),
                                    ("G-003", "Indirect", ("G-002",)), ("G-004", "Independent", ())):
            documents[f"docs/plans/[{plan_id}] {title}.md"] = plan_text(plan_id, title, depends_on=deps)
        self.reconcile(documents)
        decision = loop_manager.catalog_schedule_decision(self.connection, "repo")
        self.assertEqual(decision.runnable_ready["plan_id"], "G-004")
        self.assertTrue(decision.has_draft)
        self.assertFalse(decision.project_blocked)
        self.assertFalse(decision.has_plan_blocked)
        row = self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-001'").fetchone()
        self.assertEqual(row["workspace_owned"], 0)
        self.assertFalse(row["ever_started"])
        for field in ("branch", "worktree", "worker_session_id", "reviewer_session_id", "pr_url"):
            self.assertIsNone(row[field])
        for action in (
            lambda: runtime_state.RuntimeStateRepository(self.connection).activate("repo", "G-001", activation_sha="base", branch="worker", worktree="/owned"),
            lambda: runtime_state.RuntimeStateRepository(self.connection).reserve_workspace("repo", "G-001", reserved_value=2, allowed_values=(0,)),
            lambda: runtime_state.RuntimeStateRepository(self.connection).update_plan("repo", "G-001", plan_values={"workspace_owned": 1}, enqueue=False),
        ):
            with self.assertRaisesRegex(runtime_state.RuntimeStateError, "draft"):
                action()

    def test_complete_wait_keeps_question_link_and_does_not_block_new_planning(self):
        decision = loop_manager.catalog_schedule_decision(self.connection, "repo")
        self.assertFalse(decision.has_ready)
        self.assertTrue(decision.has_waiting)
        reason = work_exhaustion.collect(self.connection, "repo", cycle="cycle", active_requirements=())
        self.assertFalse(reason.completed)
        self.assertEqual(reason.plans[0]["status"], "DRAFT")
        self.assertIn("Question:", reason.plans[0]["question"])
        self.assertIn("/blob/" + "a" * 40, reason.plans[0]["url"])
        from loop_manager_internal.notifications.rendering import _message
        self.assertIn("Which storage policy", reason.describe())
        self.assertIn(reason.plans[0]["url"], _message("work_waiting", reason.payload("project")))
        self.assertIn("Revision: " + reason.plans[0]["revision"], _message("work_waiting", reason.payload("project")))
        for changed, expected in ((False, PlanningDecisionKind.STABLE_WAIT), (True, PlanningDecisionKind.RUN_SPECIFICATION_PLANNING)):
            selected = choose_planning_decision(has_active=False, specification_changed=changed,
                runnable_ready=False, planning_input_changed=False, has_waiting_catalog=decision.has_waiting)
            self.assertEqual(selected.kind, expected)
        compact = json.loads(loop_manager.compact_execplan_catalog(self.connection, "repo"))
        self.assertIs(compact[0]["draft"], True)
        self.assertEqual(compact[0]["decision_wait"]["revision"], self.waiting()["revision"])

    def test_only_remote_explicit_current_decision_releases_draft(self):
        revision = self.waiting()["revision"]
        documents = {PATH: accepted_text(self.text, revision)}
        self.reconcile(documents)
        self.assertFalse(self.waiting()["pending"])
        self.assertEqual(loop_manager.select_ready_execplan(self.connection, "repo")["plan_id"], "G-001")
        self.reconcile(documents, "c" * 40)
        self.assertFalse(self.waiting()["pending"])

    def test_valid_decision_still_waits_for_merged_dependencies(self):
        dependency = "docs/plans/[G-002] Prerequisite.md"
        pending = self.text.replace("depends_on: []", "depends_on:\n  - G-002")
        documents = {PATH: pending, dependency: plan_text("G-002", "Prerequisite")}
        self.reconcile(documents)
        self.connection.execute("UPDATE exec_plans SET status='PR_OPEN' WHERE plan_id='G-002'")
        self.connection.commit()
        documents[PATH] = accepted_text(pending, self.waiting()["revision"])
        self.reconcile(documents, "c" * 40)
        self.assertFalse(self.waiting()["pending"])
        self.assertIsNone(loop_manager.select_ready_execplan(self.connection, "repo"))
        self.connection.execute("UPDATE exec_plans SET status='MERGED' WHERE plan_id='G-002'")
        self.connection.commit()
        self.assertEqual(loop_manager.select_ready_execplan(self.connection, "repo")["plan_id"], "G-001")

    def test_database_reopen_keeps_hold_and_question(self):
        import sqlite3
        from loop_manager_internal.storage import database
        previous = self.waiting()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            with sqlite3.connect(path) as saved:
                self.connection.backup(saved)
            with sqlite3.connect(path) as restarted:
                restarted.row_factory = sqlite3.Row
                database._ensure_execplans_schema(restarted)
                self.assertEqual(execplan_draft_state.evidence(restarted, "repo", "G-001"), previous)
                self.assertIsNone(loop_manager.select_ready_execplan(restarted, "repo"))
    def test_missing_answer_marker_only_stale_rejected_and_deferred_decisions_wait(self):
        revision = self.waiting()["revision"]
        for text in (self.text.replace("draft: true\n", ""),
                     accepted_text(self.text, revision, answer=""),
                     accepted_text(self.text, "f" * 64),
                     accepted_text(self.text, revision, decision="reject"),
                     accepted_text(self.text, revision, decision="defer")):
            with self.subTest(text=text[-80:]):
                self.reconcile({PATH: text})
                self.assertTrue(self.waiting()["pending"])
                self.assertIsNone(loop_manager.select_ready_execplan(self.connection, "repo"))

    def test_scope_and_related_inputs_require_a_new_decision(self):
        revision = self.waiting()["revision"]
        accepted = accepted_text(self.text, revision)
        self.reconcile({PATH: accepted}, inputs={**self.inputs, "docs/PRODUCT.md": "changed"})
        self.assertTrue(self.waiting()["pending"])
        self.assertNotEqual(self.waiting()["revision"], revision)
        self.reconcile({PATH: accepted + "\n## New Goal\n\nChanged goal\n"})
        self.assertTrue(self.waiting()["pending"])

    def test_unrelated_remote_advance_keeps_wait_identity_and_question(self):
        previous = self.waiting()
        self.reconcile(self.documents, "c" * 40, inputs={**self.inputs, "docs/requirements/Other.md": "new"})
        self.assertEqual(self.waiting(), previous)
        self.reconcile({PATH: self.text.replace(QUESTION, "").replace("draft: true\n", "")})
        self.assertTrue(self.waiting()["pending"])
        self.assertEqual(self.waiting()["question"], previous["question"])
        catalog = execplan_catalog.snapshot_from_documents({PATH: plan_text("G-001", "Decision"),
            "docs/plans/[G-002] Duplicate.md": draft_text("G-002", "Duplicate")}).active_metadata()
        with self.assertRaisesRegex(RuntimeError, "중복"):
            execplan_draft_state.validate_questions(self.connection, "repo", catalog)

    def test_catalog_interruption_rolls_back_approval_and_restart_adopts_it_once(self):
        previous = self.waiting()
        documents = {PATH: accepted_text(self.text, previous["revision"])}
        def fault(stage):
            if stage == "after-plan:G-001":
                raise KeyboardInterrupt("catalog interrupted")
        with self.assertRaises(KeyboardInterrupt):
            self.reconcile(documents, fault=fault)
        self.assertEqual(self.waiting(), previous)
        self.reconcile(documents)
        self.reconcile(documents)
        self.assertFalse(self.waiting()["pending"])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM execplan_drafts").fetchone()[0], 1)

    def test_stale_remote_does_not_adopt_decision(self):
        previous = self.waiting()
        documents = {PATH: accepted_text(self.text, previous["revision"])}
        result = self.reconcile(documents, revisions=["c" * 40] * 8)
        self.assertEqual(result.kind, execplan_catalog_state.ReconciliationKind.STALE)
        self.assertEqual(self.waiting(), previous)

    def test_remote_question_edit_preserves_previous_decision_audit(self):
        accepted = accepted_text(self.text, self.waiting()["revision"])
        self.reconcile({PATH: accepted})
        decisions = self.waiting()["decisions"]
        self.assertTrue(decisions[0]["accepted"])
        self.reconcile({PATH: self.text.replace("storage policy", "retention policy")})
        self.assertTrue(self.waiting()["pending"])
        self.assertEqual(self.waiting()["decisions"], decisions)

    def test_executed_plan_cannot_become_draft(self):
        self._insert_plan(self.connection, "G-002", "MERGED", title="Executed", plan_sha="a" * 40)
        documents = {**self.documents, "docs/plans/[G-002] Executed.md": draft_text("G-002", "Executed")}
        result = self.reconcile(documents)
        self.assertEqual(result.kind, execplan_catalog_state.ReconciliationKind.INVALID_CATALOG)
        self.assertEqual(result.diagnostic.code, "draft-owned")


class ExecPlanDraftPublicationTest(AutomationCatalogFixture, unittest.TestCase):
    def test_git_catalog_port_pins_decision_inputs_and_links_to_the_observed_revision(self):
        text = draft_text()
        snapshot = execplan_catalog.snapshot_from_documents({PATH: text})
        files = {"docs/PRODUCT.md": b"# Product\r\n", "docs/ARCHITECTURE.md": b"# Architecture\n"}
        calls = []
        def git_bytes(_repo, *args):
            calls.append(args)
            if args[0] == "ls-tree":
                return b"".join(b"100644 blob " + b"a" * 40 + b"\t" + path.encode() + b"\0" for path in files)
            self.assertEqual(args[0], "show")
            revision, path = args[1].split(":", 1)
            self.assertEqual(revision, "a" * 40)
            return files[path]
        git = mock.Mock(side_effect=lambda _repo, *args, **kwargs: SimpleNamespace(stdout=
            "git@github.com:owner/project.git" if args[0] == "remote" else "a" * 40))
        effects = catalog_service.GitCatalogDefinitionPortEffects(
            git=git, git_bytes=git_bytes, _git_tree_entries=mock.Mock(),
            revision_execplan_snapshot=mock.Mock(return_value=snapshot))
        port = catalog_service._GitCatalogDefinitionPort(effects, Path("/repo"), "origin/main", lambda: frozenset())
        result = port.load()
        self.assertEqual(result.draft_inputs["docs/PRODUCT.md"], execplan_drafts.content_digest(b"# Product\n"))
        self.assertEqual(result.remote_url, "https://github.com/owner/project")
        self.assertIs(port.load(), result)
        self.assertEqual(len(calls), 3)

    def test_worker_scope_rejects_self_approval_and_preserves_existing_user_decision(self):
        from loop_manager_internal.agents.scope_contracts import GitSnapshot, AgentChange, AgentScopeError
        from loop_manager_internal.agents.scope_validation import ScopeValidationEffects, validate_agent_scope
        original = accepted_text(draft_text(), "a" * 64)
        snapshot = GitSnapshot("a" * 40, "worker", {}, {}, {}, {}, {}, {})
        with tempfile.TemporaryDirectory() as directory:
            root = make_catalog_root(directory)
            effects = ScopeValidationEffects(
                _agent_git_resources=mock.Mock(), repository=mock.Mock(), _current_rebase_service=mock.Mock(),
                _git_common_directory=mock.Mock(), _owned_rebase_state_dir=mock.Mock(),
                _plan_id_from_path=lambda _: "G-001", _rebase_in_progress_for_branch=mock.Mock(),
                _scope_snapshot_with_start_ignore_rules=lambda _root, _before, after: after,
                agent_snapshot_changes=lambda *_: [AgentChange(PATH, ("worktree",))],
                capture_git_snapshot=mock.Mock(), git=mock.Mock(return_value=SimpleNamespace(returncode=0, stdout=original)),
                operation_log=mock.Mock(), validate_managed_worktree=mock.Mock())
            (root / PATH).write_text(original + "\n", encoding="utf-8")
            validate_agent_scope("worker", root, snapshot, snapshot, selected_plan_path=PATH, effects=effects)
            (root / PATH).write_text(original.replace("Use policy A", "Use policy B"), encoding="utf-8")
            with self.assertRaisesRegex(AgentScopeError, "User Decision"):
                validate_agent_scope("worker", root, snapshot, snapshot, selected_plan_path=PATH, effects=effects)

    def test_atomic_mixed_registration_resumes_without_duplicate_draft_or_approval(self):
        connection = self._connection()
        with tempfile.TemporaryDirectory() as directory:
            root = make_catalog_root(directory)
            second = "docs/plans/[G-002] Independent.md"
            (root / PATH).write_text(draft_text(), encoding="utf-8")
            (root / second).write_text(plan_text("G-002", "Independent"), encoding="utf-8")
            plans = [(path, loop_manager._parse_execplan(root, root / path)) for path in (PATH, second)]
            hashes = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
            git = mock.Mock(return_value=SimpleNamespace(stdout="git@github.com:owner/project.git"))
            rows = planner_publication.RegisterExecplanRowsEffects(
                _ensure_execplans_schema=loop_manager._ensure_execplans_schema, file_hash=hashes, git=git)
            def interrupted_hash(path):
                if path.name == Path(second).name:
                    raise KeyboardInterrupt("registration interrupted")
                return hashes(path)
            interrupted = replace(rows, file_hash=interrupted_hash)
            effects = planner_publication.RegisterExecplansEffects(_register_execplan_rows=lambda *args:
                planner_publication._register_execplan_rows(interrupted, *args))
            with self.assertRaises(KeyboardInterrupt):
                planner_publication.register_execplans(effects, connection, "repo", root, plans, "a" * 40, "fingerprint")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM exec_plans").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM execplan_drafts").fetchone()[0], 0)
            effects = replace(effects, _register_execplan_rows=lambda *args:
                planner_publication._register_execplan_rows(rows, *args))
            for _ in range(2):
                planner_publication.register_execplans(effects, connection, "repo", root, plans, "a" * 40, "fingerprint")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM exec_plans").fetchone()[0], 2)
            self.assertEqual(execplan_draft_state.pending_ids(connection, "repo"), {"G-001"})
            saved = execplan_draft_state.evidence(connection, "repo", "G-001")
            self.assertIn("https://github.com/owner/project/blob/", saved["url"])
            self.assertEqual(loop_manager.select_ready_execplan(connection, "repo")["plan_id"], "G-002")
            self.assertTrue(all(call.args[1:] == ("remote", "get-url", "origin") for call in git.call_args_list))
