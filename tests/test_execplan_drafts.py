"""Unit coverage for published ExecPlan decision holds; Git/agents are fakes."""
from dataclasses import replace
from contextlib import closing
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.catalog import execplan_catalog, execplan_drafts, catalog_service, remote_deletions, draft_decisions
from loop_manager_internal.planning import planner_publication, planner_validation
from loop_manager_internal.planning.planning_catalog import (
    MutableReadySnapshot, PlanDeletion, mutable_snapshot_token, snapshot_from_dict, snapshot_to_dict,
)
from loop_manager_internal.planning.planning_contracts import choose_planning_decision, PlanningDecisionKind
from loop_manager_internal.storage import (
    execplan_catalog_state, execplan_draft_state, planning_publication_state, runtime_state,
)
from loop_manager_internal.notifications import work_exhaustion
from tests.support.catalog import plan_text, make_catalog_root, FakeDefinitionPort, QUESTION, draft_text, accepted_text
from tests.support.database import AutomationCatalogFixture


PATH = "docs/plans/[G-001] Decision.md"






class ExecPlanDraftDocumentTest(unittest.TestCase):
    def test_published_plan_uses_the_same_draft_contract_as_the_catalog(self):
        legacy = plan_text("G-001", "Decision").replace("priority: normal\n", "")
        for text in (legacy, draft_text(), draft_text().replace("draft: true", "draft: false")):
            with self.subTest(text=text[:160]):
                published = loop_manager.published_execplan(PATH, text)
                self.assertEqual(published, execplan_catalog.parse_execplan_text(
                    PATH, text, validate_sources=False))
                self.assertEqual(published["priority"], "normal")
        # Published history remains readable after its source leaves the live tree.
        text = draft_text().replace("docs/PRODUCT.md", "docs/requirements/[R-999] Retired.md")
        self.assertIs(loop_manager.published_execplan(PATH, text)["draft"], True)

    def test_published_draft_rejects_invalid_readiness_and_decision_fields(self):
        for value in ("yes", "TRUE", "1", "[]", ""):
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, "draft"):
                loop_manager.published_execplan(PATH, draft_text().replace("draft: true", "draft: " + value))
        for field in ("Question", "Facts", "Options", "Impact"):
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "Draft Decision"):
                loop_manager.published_execplan(PATH, draft_text().replace(field + ":", "Missing:"))
        for text in (draft_text().replace("risk: low", "risk: invalid"),
                     draft_text().replace("docs/PRODUCT.md", "README.md"),
                     draft_text().replace("depends_on: []", "depends_on:\n  - invalid"),
                     draft_text().replace("draft: true", "draft: true\nstatus: READY")):
            with self.subTest(text=text[:160]), self.assertRaises(RuntimeError):
                loop_manager.published_execplan(PATH, text)

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

    def test_snapshot_and_prompt_expose_readiness_without_execution_state(self):
        from loop_manager_internal.maintenance.maintenance_planning import ReadyPlanSnapshot
        text = draft_text()
        snapshot = ReadyPlanSnapshot("G-001", PATH, "Decision", ("docs/PRODUCT.md",), (),
                                     "low", "low", "normal", hashlib.sha256(text.encode()).hexdigest(), "a" * 40, text)
        batch = MutableReadySnapshot("repo", "a" * 40, (snapshot,), mutable_snapshot_token("repo", "a" * 40, (snapshot,)))
        self.assertIs(json.loads(batch.prompt_payload())["plans"][0]["draft"], True)
        prompt = loop_manager.planner_prompt(None, mutable_snapshot=batch)
        self.assertIn(batch.prompt_payload(), prompt)
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
    def test_published_draft_can_be_snapshotted_without_releasing_its_hold(self):
        row = self.connection.execute(
            "SELECT * FROM exec_plans WHERE repo = 'repo' AND plan_id = 'G-001'"
        ).fetchone()
        snapshot = loop_manager._ready_plan_snapshot_from_contents(row, self.text.encode())
        self.assertIs(snapshot.draft, True)
        self.assertEqual(snapshot.text, self.text)
        self.assertEqual(snapshot.content_hash, row["content_hash"])
        self.assertEqual(execplan_draft_state.pending_ids(self.connection, "repo"), {"G-001"})
        self.assertIsNone(loop_manager.select_ready_execplan(self.connection, "repo"))

    def setUp(self):
        self.connection = self._connection()
        self.inputs = {"docs/PRODUCT.md": "product", "docs/ARCHITECTURE.md": "architecture"}
        self.text = draft_text()
        self.documents = {PATH: self.text}
        self.reconcile(self.documents, "a" * 40)

    def reconcile(self, documents, revision="b" * 40, *, inputs=None, fault=None, revisions=None, provenance=True):
        snapshot = execplan_catalog.snapshot_from_documents(documents, revision_sha=revision)
        inputs = self.inputs if inputs is None else inputs
        saved = execplan_draft_state.evidence(self.connection, "repo", "G-001") or {}
        changes = dict(saved.get("user_changes", {}))
        text = documents.get(PATH, "")
        if text != self.text and changes.get(PATH, {}).get("after") != text:
            changes[PATH] = {"verified": provenance, "revision": revision,
                "before": self.text, "after": text, "author": "user@example.test"}
        pinned = execplan_catalog_state.PinnedCatalog(revision, snapshot,
            frozenset({"docs/PRODUCT.md"} | {key for key in inputs if key.startswith("docs/requirements/")}),
            draft_inputs=inputs,
            remote_url="https://github.com/owner/project",
            draft_changes={"G-001": changes})
        repository = execplan_catalog_state.DefinitionStateRepository(self.connection, fault_hook=fault)
        return execplan_catalog_state.CatalogReconciliationService(
            repository, FakeDefinitionPort(pinned, revisions=revisions)).reconcile("repo")

    def waiting(self):
        return execplan_draft_state.evidence(self.connection, "repo", "G-001")

    def publish_decision(self, documents=None, *, inputs=None):
        """실제 정리·DB 전환을 사용하고 원격 게시 확인만 fixture로 제공한다."""
        saved = self.waiting()
        text = documents[PATH] if documents is not None else saved["user_changes"][PATH]["after"]
        row = self.connection.execute("SELECT * FROM exec_plans WHERE repo='repo' AND plan_id='G-001'").fetchone()
        original = loop_manager._ready_plan_snapshot_from_contents(row, text.encode(), allow_started=True)
        snapshot = MutableReadySnapshot("repo", row["current_revision_sha"], (original,), "fixture")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / PATH
            target.parent.mkdir(parents=True)
            target.write_text(text, encoding="utf-8", newline="\n")
            if saved.get("decision_ready"):
                answer = saved["decision"]["Answer"]
                target.write_text(draft_decisions.replace_section(text, "Implementation", answer),
                                  encoding="utf-8", newline="\n")
                assessment = [{"id": "G-001", "revision": saved["revision"], "remaining": "",
                    "resolved": [{"question": draft_decisions.questions(saved["question"])[0],
                                  "answer": answer, "source": PATH, "quote": answer}]}]
                self.connection.execute("UPDATE projects SET planning_result=? WHERE repo='repo'",
                    (draft_decisions.PREFIX + json.dumps(assessment),))
                self.connection.commit()
            prepared = draft_decisions.prepare(self.connection, "repo", root, snapshot)
            self.assertEqual(set(prepared), {"G-001"})
            cleaned = target.read_text(encoding="utf-8")
            plan = execplan_catalog.parse_execplan_text(PATH, cleaned, validate_sources=False)
            with self.connection:
                execplan_draft_state.resolve(self.connection, "repo", PATH, plan,
                    execplan_catalog.plan_content_hash(target.read_bytes()), "f" * 40, prepared["G-001"],
                    inputs=inputs or saved["basis"]["inputs"])
                self.connection.execute("UPDATE exec_plans SET content_hash=?, current_revision_sha=? WHERE repo='repo' AND plan_id='G-001'",
                    (execplan_catalog.plan_content_hash(target.read_bytes()), "f" * 40))
        if documents is not None:
            documents[PATH] = cleaned
        return cleaned

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

    def test_planner_uses_change_diff_once_and_preserves_full_decision_evidence(self):
        from loop_manager_internal.planning.validation_ports import _PlanningValidationPorts
        saved = self.waiting()
        unchanged = ''.join(f'Unchanged product context {number}\n' for number in range(25_000))
        before = unchanged + 'Policy: undecided\n'
        after = unchanged + 'Policy: keep existing storage\n'
        saved['user_changes'] = {'docs/PRODUCT.md': {
            'verified': True, 'revision': 'b' * 40, 'commit': 'c' * 40,
            'author': 'user@example.test', 'before': before, 'after': after}}
        self.connection.execute('UPDATE execplan_drafts SET evidence=? WHERE repo=? AND plan_id=?',
                                (json.dumps(saved), 'repo', 'G-001'))
        catalog = loop_manager.compact_execplan_catalog(self.connection, 'repo')
        self.assertNotIn('user_changes', json.loads(catalog)[0]['decision_wait'])
        row = self.connection.execute("SELECT * FROM exec_plans WHERE repo='repo' AND plan_id='G-001'").fetchone()
        original = loop_manager._ready_plan_snapshot_from_contents(row, self.text.encode(), allow_started=True)
        snapshot = MutableReadySnapshot('repo', 'b' * 40, (original,), 'token')
        effects = SimpleNamespace(planner_prompt=loop_manager.planner_prompt,
                                  is_loop_manager_repository=lambda _: False, git=None, git_bytes=None)
        prompt = _PlanningValidationPorts(effects, self.connection).planner_prompt(None, catalog, snapshot)
        self.assertGreater(len(json.dumps(saved['user_changes'])), 1_048_576)
        self.assertLess(len(prompt), 20_000)
        self.assertEqual(prompt.count('+Policy: keep existing storage'), 1)
        evidence = execplan_draft_state.prompt_evidence(self.connection, 'repo', 'G-001')
        change = evidence['user_changes']['docs/PRODUCT.md']
        self.assertTrue(change['verified'])
        self.assertEqual(change['revision'], 'b' * 40)
        self.assertIn('-Policy: undecided', change['diff'])
        self.assertNotIn('before', change)
        self.assertNotIn('after', change)
        self.assertEqual(self.waiting(), saved)
        self.assertTrue(self.waiting()['pending'])

    def test_only_remote_explicit_current_decision_releases_draft(self):
        revision = self.waiting()["revision"]
        documents = {PATH: accepted_text(self.text, revision)}
        self.reconcile(documents)
        self.assertTrue(self.waiting()["pending"])
        self.assertTrue(self.waiting()["decision_ready"])
        self.assertIsNone(loop_manager.select_ready_execplan(self.connection, "repo"))
        self.publish_decision(documents)
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
        self.publish_decision(documents)
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
            with closing(sqlite3.connect(path)) as saved, saved:
                self.connection.backup(saved)
            with closing(sqlite3.connect(path)) as restarted, restarted:
                restarted.row_factory = sqlite3.Row
                database._ensure_execplans_schema(restarted)
                self.assertEqual(execplan_draft_state.evidence(restarted, "repo", "G-001"), previous)
                self.assertIsNone(loop_manager.select_ready_execplan(restarted, "repo"))

    def test_preexisting_accepted_decision_keeps_readiness_until_its_basis_changes(self):
        documents = {PATH: accepted_text(self.text, self.waiting()["revision"])}
        self.reconcile(documents)
        saved = self.waiting()
        saved["pending"] = False
        self.connection.execute("UPDATE execplan_drafts SET pending=0,evidence=? WHERE repo='repo' AND plan_id='G-001'",
                                (json.dumps(saved),))
        self.connection.commit()
        self.reconcile(documents, "c" * 40)
        self.assertEqual(self.waiting(), saved)
        self.assertEqual(loop_manager.select_ready_execplan(self.connection, "repo")["plan_id"], "G-001")
        self.reconcile(documents, "d" * 40, inputs={**self.inputs, "docs/PRODUCT.md": "changed"})
        self.assertTrue(self.waiting()["pending"])
        self.assertIsNone(loop_manager.select_ready_execplan(self.connection, "repo"))

    def test_user_body_resolution_cleans_forms_and_retains_audit(self):
        answer = "Use policy A and retain history."
        text = draft_decisions.replace_section(self.text, "Implementation", answer)
        documents = {PATH: text}
        self.reconcile(documents)
        question = draft_decisions.questions(self.waiting()["question"])[0]
        result = [{"id": "G-001", "revision": self.waiting()["revision"], "remaining": "",
                   "resolved": [{"question": question, "answer": answer, "source": PATH, "quote": answer}]}]
        self.connection.execute("UPDATE projects SET planning_result=? WHERE repo='repo'",
                                (draft_decisions.PREFIX + json.dumps(result),))
        self.connection.commit()
        cleaned = self.publish_decision(documents)
        self.assertNotIn("## Draft Decision", cleaned)
        self.assertNotIn("## User Decision", cleaned)
        self.assertNotIn("draft: true", cleaned)
        self.assertIn(answer, cleaned)
        saved = self.waiting()
        self.assertFalse(saved["pending"])
        self.assertEqual(saved["resolutions"][0]["assessment"]["resolved"][0]["quote"], answer)
        self.reconcile(documents, "e" * 40)
        self.assertEqual(self.waiting(), saved)

    def test_partial_resolution_keeps_only_the_remaining_question(self):
        first = "Which storage policy should this goal use?"
        second = "Which retry policy should this goal use?"
        self.text = self.text.replace("Question: " + first, "Question: - " + first + "\n- " + second)
        self.reconcile({PATH: self.text}, "c" * 40)
        answer = "Use policy A and retain history."
        text = draft_decisions.replace_section(self.text, "Implementation", answer)
        documents = {PATH: text}
        self.reconcile(documents, "d" * 40)
        remaining = QUESTION.replace(first, second)
        result = [{"id": "G-001", "revision": self.waiting()["revision"], "remaining": remaining,
                   "resolved": [{"question": first, "answer": answer, "source": PATH, "quote": answer}]}]
        self.connection.execute("UPDATE projects SET planning_result=? WHERE repo='repo'",
                                (draft_decisions.PREFIX + json.dumps(result),))
        self.connection.commit()
        cleaned = self.publish_decision(documents)
        self.assertIn("draft: true", cleaned)
        self.assertEqual(draft_decisions.questions(self.waiting()["question"]), (second,))
        self.assertTrue(self.waiting()["pending"])
        self.assertIn("/blob/" + "f" * 40, self.waiting()["url"])
        self.assertIsNone(loop_manager.select_ready_execplan(self.connection, "repo"))

    def test_unverified_source_cannot_prepare_explicit_approval(self):
        self.reconcile({PATH: accepted_text(self.text, self.waiting()["revision"])}, provenance=False)
        self.assertFalse(self.waiting()["decision_ready"])
        self.assertTrue(self.waiting()["pending"])
        self.assertIsNone(loop_manager.select_ready_execplan(self.connection, "repo"))

    def test_missing_revision_and_unchanged_draft_marker_are_not_extra_user_work(self):
        text = accepted_text(self.text, self.waiting()["revision"]).replace("Revision: " + self.waiting()["revision"] + "\n", "")
        text = text.replace("risk: low\n", "risk: low\ndraft: true\n")
        documents = {PATH: text}
        self.reconcile(documents)
        self.assertTrue(self.waiting()["decision_ready"])
        self.publish_decision(documents)
        self.assertFalse(self.waiting()["pending"])

    def test_prepared_cleanup_restarts_without_repeating_the_question(self):
        documents = {PATH: accepted_text(self.text, self.waiting()["revision"])}
        self.reconcile(documents)
        row = self.connection.execute("SELECT * FROM exec_plans WHERE repo='repo' AND plan_id='G-001'").fetchone()
        original = loop_manager._ready_plan_snapshot_from_contents(row, documents[PATH].encode(), allow_started=True)
        snapshot = MutableReadySnapshot("repo", row["current_revision_sha"], (original,), "fixture")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / PATH
            target.parent.mkdir(parents=True)
            target.write_text(documents[PATH], encoding="utf-8", newline="\n")
            answer = self.waiting()["decision"]["Answer"]
            target.write_text(draft_decisions.replace_section(documents[PATH], "Implementation", answer), encoding="utf-8", newline="\n")
            result = [{"id": "G-001", "revision": self.waiting()["revision"], "remaining": "",
                "resolved": [{"question": draft_decisions.questions(self.waiting()["question"])[0],
                              "answer": answer, "source": PATH, "quote": answer}]}]
            self.connection.execute("UPDATE projects SET planning_result=? WHERE repo='repo'", (draft_decisions.PREFIX + json.dumps(result),))
            self.connection.commit()
            original_bytes = target.read_bytes()
            with mock.patch.object(draft_decisions.os, "replace", side_effect=KeyboardInterrupt()):
                with self.assertRaises(KeyboardInterrupt):
                    draft_decisions.prepare(self.connection, "repo", root, snapshot)
            self.assertEqual(target.read_bytes(), original_bytes)
            self.assertFalse(list(target.parent.glob(".decision-*.tmp")))
            first = draft_decisions.prepare(self.connection, "repo", root, snapshot)
            cleaned = target.read_bytes()
            self.assertTrue(self.waiting()["pending"])
            self.assertEqual(draft_decisions.prepare(self.connection, "repo", root, snapshot), first)
            self.assertEqual(target.read_bytes(), cleaned)
            plan = execplan_catalog.parse_execplan_text(PATH, cleaned.decode(), validate_sources=False)
            saved = self.waiting()
            with self.assertRaises(KeyboardInterrupt):
                with self.connection:
                    execplan_draft_state.resolve(self.connection, "repo", PATH, plan,
                        execplan_catalog.plan_content_hash(cleaned), "f" * 40, first["G-001"], inputs=saved["basis"]["inputs"])
                    raise KeyboardInterrupt()
            self.assertEqual(self.waiting(), saved)

    def test_answer_presence_without_semantic_resolution_remains_held(self):
        documents = {PATH: accepted_text(self.text, self.waiting()["revision"], answer="Approved.")}
        self.reconcile(documents)
        row = self.connection.execute("SELECT * FROM exec_plans WHERE repo='repo' AND plan_id='G-001'").fetchone()
        original = loop_manager._ready_plan_snapshot_from_contents(row, documents[PATH].encode(), allow_started=True)
        snapshot = MutableReadySnapshot("repo", row["current_revision_sha"], (original,), "fixture")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); target = root / PATH
            target.parent.mkdir(parents=True); target.write_text(documents[PATH], encoding="utf-8")
            self.assertEqual(draft_decisions.prepare(self.connection, "repo", root, snapshot), {})
        self.assertTrue(self.waiting()["pending"])
        self.assertIsNone(loop_manager.select_ready_execplan(self.connection, "repo"))
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

    def test_related_input_only_advance_rejects_previous_revision(self):
        requirement = "docs/requirements/Decision.md"
        text = self.text.replace("  - docs/PRODUCT.md", "  - docs/PRODUCT.md\n  - " + requirement)
        inputs = {**self.inputs, requirement: "requirement"}
        self.reconcile({PATH: text}, "c" * 40, inputs=inputs)
        initial_revision = self.waiting()["revision"]
        initial_inputs = dict(inputs)
        for index, path in enumerate(inputs):
            with self.subTest(path=path):
                self.reconcile({PATH: text}, f"{index * 4 + 1:040x}", inputs=inputs)
                previous_revision = self.waiting()["revision"]
                inputs = {**inputs, path: inputs[path] + " changed"}
                self.reconcile({PATH: text}, f"{index * 4 + 2:040x}", inputs=inputs)
                current_revision = self.waiting()["revision"]
                self.assertNotEqual(current_revision, previous_revision)
                self.assertEqual(self.connection.execute(
                    "SELECT content_hash FROM exec_plans WHERE plan_id='G-001'",
                ).fetchone()[0], execplan_catalog.plan_content_hash(text.encode()))
                self.reconcile({PATH: accepted_text(text, previous_revision)},
                               f"{index * 4 + 3:040x}", inputs=inputs)
                self.assertTrue(self.waiting()["pending"])
                self.assertFalse(self.waiting()["decisions"][-1]["accepted"])
                self.assertIsNone(loop_manager.select_ready_execplan(self.connection, "repo"))
                self.reconcile({PATH: accepted_text(text, current_revision)},
                               f"{index * 4 + 4:040x}", inputs=inputs)
                self.publish_decision(inputs=inputs)
                self.assertFalse(self.waiting()["pending"])
        self.reconcile({PATH: text}, "d" * 40, inputs=initial_inputs)
        self.assertNotEqual(self.waiting()["revision"], initial_revision)
        self.reconcile({PATH: accepted_text(text, initial_revision)}, "e" * 40, inputs=initial_inputs)
        self.assertTrue(self.waiting()["pending"])

    def test_remote_draft_deletion_preserves_audit_and_independent_work(self):
        paths = {key: f"docs/plans/[{key}] {title}.md" for key, title in
                 (("G-002", "Dependent"), ("G-003", "Independent"), ("G-004", "Open PR"))}
        documents = {**self.documents,
            paths["G-002"]: plan_text("G-002", "Dependent", depends_on=("G-001",)),
            paths["G-003"]: plan_text("G-003", "Independent"),
            paths["G-004"]: plan_text("G-004", "Open PR")}
        self.reconcile(documents, "b" * 40)
        self.connection.execute("UPDATE exec_plans SET status='PR_OPEN', ever_started=1, pr_url=? WHERE plan_id='G-004'",
                                ("https://github.com/owner/project/pull/4",))
        self.connection.commit()
        previous = self.waiting()
        snapshot = execplan_catalog.snapshot_from_documents(
            {path: text for path, text in documents.items() if path != PATH},
            revision_sha="c" * 40, isolate_errors=True)
        pinned = execplan_catalog_state.PinnedCatalog("c" * 40, snapshot, frozenset({"docs/PRODUCT.md"}),
            draft_inputs=self.inputs, remote_url="https://github.com/owner/project")
        def git_bytes(_repo, *args):
            if args[0] == "ls-tree":
                return b"100644 blob " + b"a" * 40 + b"\t" + PATH.encode() + b"\0"
            self.assertEqual(args[0], "show")
            self.assertTrue(args[1].startswith("b" * 40 + ":"))
            return documents[args[1].split(":", 1)[1]].encode()
        deletions = remote_deletions.prove_deletions(self.connection, "repo", Path("/repo"), pinned,
            git=mock.Mock(return_value=SimpleNamespace(returncode=0)), git_bytes=git_bytes)
        self.assertEqual([item.snapshot.plan_id for item in deletions], ["G-001"])
        repository = execplan_catalog_state.DefinitionStateRepository(self.connection)
        before = repository.capture("repo")
        with self.assertRaisesRegex(loop_manager.PlannerValidationError, "draft"), self.connection:
            planning_publication_state.apply_mutable_catalog_changes(self.connection, "repo",
                deletions=(PlanDeletion(deletions[0].snapshot),), revisions=(),
                snapshot_token="planner", plan_commit="c" * 40, content_hash=lambda _path: "")
        self.assertEqual(repository.capture("repo"), before)
        pinned = replace(pinned, deletions=deletions)
        def fault(stage):
            if stage == "after-cancellation:G-001":
                raise KeyboardInterrupt("catalog interrupted")
        with self.assertRaises(KeyboardInterrupt):
            execplan_catalog_state.CatalogReconciliationService(
                execplan_catalog_state.DefinitionStateRepository(self.connection, fault_hook=fault),
                FakeDefinitionPort(pinned)).reconcile("repo")
        self.assertEqual(repository.capture("repo"), before)
        result = execplan_catalog_state.CatalogReconciliationService(repository, FakeDefinitionPort(pinned)).reconcile("repo")
        self.assertEqual(result.withheld_plan_ids, ("G-002",))
        self.assertIsNone(self.connection.execute("SELECT 1 FROM exec_plans WHERE plan_id='G-001'").fetchone())
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM execplan_cancellations WHERE plan_id='G-001'").fetchone()[0], 1)
        self.assertEqual(self.waiting(), {**previous, "pending": False, "cancelled_revision": "c" * 40})
        self.assertNotIn("G-001", execplan_draft_state.pending_ids(self.connection, "repo"))
        self.assertIn("G-001", {item[0] for item in repository.capture("repo").reserved})
        decision = loop_manager.catalog_schedule_decision(self.connection, "repo")
        self.assertEqual(decision.runnable_ready["plan_id"], "G-003")
        self.assertFalse(decision.project_blocked)
        self.assertTrue(decision.has_pr_open)
        self.assertEqual(self.connection.execute("SELECT status, pr_url FROM exec_plans WHERE plan_id='G-004'").fetchone()[:],
                         ("PR_OPEN", "https://github.com/owner/project/pull/4"))
        pinned = replace(pinned, deletions=())
        execplan_catalog_state.CatalogReconciliationService(repository, FakeDefinitionPort(pinned)).reconcile("repo")
        self.assertEqual(self.waiting(), {**previous, "pending": False, "cancelled_revision": "c" * 40})
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM execplan_cancellations WHERE plan_id='G-001'").fetchone()[0], 1)

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
        self.publish_decision(documents)
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
        self.publish_decision()
        decisions = self.waiting()["decisions"]
        resolutions = self.waiting()["resolutions"]
        self.assertTrue(decisions[0]["accepted"])
        self.reconcile({PATH: self.text.replace("storage policy", "retention policy")})
        self.assertTrue(self.waiting()["pending"])
        self.assertEqual(self.waiting()["decisions"], decisions)
        self.assertEqual(self.waiting()["resolutions"], resolutions)

    def test_renamed_requirement_draft_input_matches_worktree_and_invalidates_approval(self):
        source = "docs/requirements/[R-001] Original.md"
        renamed = "docs/requirements/[R-001] Renamed.md"
        draft = draft_text().replace("docs/PRODUCT.md", source)
        product = b"# Product\n"
        architecture = b"# Architecture\n"

        def remote_catalog(plan, requirement, revision):
            files = {
                PATH: plan.encode(),
                "docs/PRODUCT.md": product,
                "docs/ARCHITECTURE.md": architecture,
                renamed: requirement,
            }
            blobs = {}
            records = []
            for index, (path, contents) in enumerate(files.items(), 1):
                object_id = f"{index:040x}"
                blobs[object_id] = contents
                records.append(
                    f"100644 blob {object_id}\t{path}".encode() + b"\0"
                )
            tree = b"".join(records)
            snapshot = execplan_catalog.snapshot_from_documents(
                {PATH: plan}, validate_sources=True,
                requirement_documents={renamed: requirement},
            )

            def git_bytes(_repo, *args):
                if args[0] == "ls-tree":
                    return tree
                if args[0] == "cat-file":
                    return blobs[args[-1]]
                if args[0] == "show":
                    return files[args[1].split(":", 1)[1]]
                raise AssertionError(args)

            def git(_repo, *args):
                if args[0] == "rev-parse":
                    return SimpleNamespace(stdout=revision)
                if args == ("remote", "get-url", "origin"):
                    return SimpleNamespace(stdout="git@github.com:owner/project.git")
                raise AssertionError(args)

            effects = catalog_service.GitCatalogDefinitionPortEffects(
                git=git, git_bytes=git_bytes, _git_tree_entries=mock.Mock(),
                revision_execplan_snapshot=mock.Mock(return_value=snapshot),
            )
            return catalog_service._GitCatalogDefinitionPort(
                effects, Path("/repo"), "origin/main", lambda: frozenset(),
            ).load()

        def worktree_inputs(requirement):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for path, contents in (
                    ("docs/PRODUCT.md", product),
                    ("docs/ARCHITECTURE.md", architecture),
                    (renamed, requirement),
                ):
                    target = root / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(contents)
                plan = execplan_catalog.parse_execplan_text(
                    PATH, draft, validate_sources=False,
                )
                return execplan_drafts.input_digests(root, plan)

        first_requirement = b"# [R-001] Renamed\n\nFirst criteria.\n"
        first = remote_catalog(draft, first_requirement, "c" * 40)
        self.assertEqual(first.draft_inputs, worktree_inputs(first_requirement))

        self.reconcile({PATH: draft}, first.revision_sha, inputs=first.draft_inputs)
        held = self.waiting()
        accepted = accepted_text(draft, held["revision"])
        self.reconcile({PATH: accepted}, first.revision_sha, inputs=first.draft_inputs)
        self.publish_decision(inputs=first.draft_inputs)
        self.assertFalse(self.waiting()["pending"])

        changed_requirement = b"# [R-001] Renamed\n\nChanged criteria.\n"
        changed = remote_catalog(accepted, changed_requirement, "d" * 40)
        self.assertEqual(changed.draft_inputs, worktree_inputs(changed_requirement))
        self.assertNotEqual(
            first.draft_inputs[source], changed.draft_inputs[source],
        )
        self.reconcile({PATH: accepted}, changed.revision_sha, inputs=changed.draft_inputs)
        self.assertTrue(self.waiting()["pending"])

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

    def test_worker_cannot_remove_draft_or_entire_user_decision_section(self):
        from loop_manager_internal.agents.scope_contracts import GitSnapshot, AgentChange, AgentScopeError
        from loop_manager_internal.agents.scope_validation import ScopeValidationEffects, validate_agent_scope
        snapshot = GitSnapshot("a" * 40, "worker", {}, {}, {}, {}, {}, {})
        with tempfile.TemporaryDirectory() as directory:
            root = make_catalog_root(directory)
            effects = ScopeValidationEffects(
                _agent_git_resources=mock.Mock(), repository=mock.Mock(), _current_rebase_service=mock.Mock(),
                _git_common_directory=mock.Mock(), _owned_rebase_state_dir=mock.Mock(),
                _plan_id_from_path=lambda _: "G-001", _rebase_in_progress_for_branch=mock.Mock(),
                _scope_snapshot_with_start_ignore_rules=lambda _root, _before, after: after,
                agent_snapshot_changes=lambda *_: [AgentChange(PATH, ("worktree",))],
                capture_git_snapshot=mock.Mock(), git=mock.Mock(),
                operation_log=mock.Mock(), validate_managed_worktree=mock.Mock())
            for original, changed, message in (
                (draft_text(), draft_text().replace('draft: true\n', ''), 'draft'),
                (accepted_text(draft_text(), 'a' * 64), draft_text().replace('draft: true\n', ''), 'User Decision'),
            ):
                with self.subTest(message=message):
                    effects.git.return_value = SimpleNamespace(returncode=0, stdout=original)
                    (root / PATH).write_text(changed, encoding='utf-8')
                    with self.assertRaisesRegex(AgentScopeError, message):
                        validate_agent_scope('worker', root, snapshot, snapshot,
                                             selected_plan_path=PATH, effects=effects)

    def test_worker_cannot_delete_existing_plan_without_sqlite_draft_history(self):
        from loop_manager_internal.agents.scope_contracts import GitSnapshot, AgentChange, AgentScopeError
        from loop_manager_internal.agents.scope_validation import ScopeValidationEffects, validate_agent_scope
        snapshot = GitSnapshot('a' * 40, 'worker', {PATH: ('H 100644 original 0',)}, {}, {}, {}, {}, {})
        with tempfile.TemporaryDirectory() as directory:
            root = make_catalog_root(directory)
            for original in (accepted_text(draft_text(), 'a' * 64),
                             plan_text('G-001', 'Decision') + '\n## Progress\n- Existing execution record\n'):
                with self.subTest(original=original):
                    effects = ScopeValidationEffects(
                        _agent_git_resources=mock.Mock(), repository=mock.Mock(), _current_rebase_service=mock.Mock(),
                        _git_common_directory=mock.Mock(), _owned_rebase_state_dir=mock.Mock(),
                        _plan_id_from_path=lambda _: 'G-001', _rebase_in_progress_for_branch=mock.Mock(),
                        _scope_snapshot_with_start_ignore_rules=lambda _root, _before, after: after,
                        agent_snapshot_changes=lambda *_: [AgentChange(PATH, ('worktree',))],
                        capture_git_snapshot=mock.Mock(), git=mock.Mock(return_value=SimpleNamespace(returncode=0, stdout=original)),
                        operation_log=mock.Mock(), validate_managed_worktree=mock.Mock())
                    document = root / PATH
                    document.write_text(original, encoding='utf-8')
                    document.unlink()
                    with self.assertRaisesRegex(AgentScopeError, '파일 삭제'):
                        validate_agent_scope('worker', root, snapshot, snapshot,
                            selected_plan_path=PATH, effects=effects)
                    effects.git.assert_called_once_with(root, 'show', snapshot.head + ':' + PATH)

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
                _ensure_execplans_schema=loop_manager._ensure_execplans_schema, file_hash=hashes, git=git,
                git_bytes=lambda _root, _show, spec: (root / spec.split(":", 1)[1]).read_bytes())
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
