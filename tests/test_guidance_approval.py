import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from loop_manager_internal.documents import guidance_approval
import loop_manager
from tests.support.guidance import PATCH, active_connection, git, git_result, guidance_plan, repository


class GuidanceFormatTest(unittest.TestCase):
    def test_legacy_common_provenance_survives_injection_and_decision_updates(self):
        from types import SimpleNamespace
        from loop_manager_internal.documents.guidance_service import legacy_common_identities
        old = guidance_plan().replace('Scope: `project`', 'Scope: `common`')
        proposal, = guidance_approval.parse_guidance_document(old).proposals
        calls = []
        def git_port(_root, *args, **_kwargs):
            calls.append(args)
            if args[0] == 'log':
                return SimpleNamespace(returncode=0, stdout='a' * 40 + '\n')
            if args[0] == 'show':
                return SimpleNamespace(returncode=0, stdout=old)
            return SimpleNamespace(returncode=0, stdout='')
        self.assertEqual(legacy_common_identities(git_port, Path('/repo'), 'b' * 40, 'docs/plans/old.md'),
                         {proposal.identity})
        self.assertIn(('show', 'a' * 40 + '^:docs/plans/old.md'), calls)
        self.assertEqual(proposal.target, 'AGENTS.md')
        changed = guidance_approval.parse_guidance_document(old.replace('Approved rule', 'Different rule')).proposals[0]
        self.assertNotIn(changed.identity, {proposal.identity})
        def before_injection(_root, *args, **_kwargs):
            return SimpleNamespace(returncode=1 if args[0] == 'cat-file' else 0,
                                   stdout=old if args[0] == 'show' else '')
        self.assertEqual(legacy_common_identities(before_injection, Path('/repo'), 'b' * 40, 'old.md'),
                         {proposal.identity})

    def test_preapproval_baseline_is_bound_without_changing_legacy_decisions(self):
        approved = guidance_plan(decision='approve', revision='published')
        with_baseline = approved.replace('Revision: `published`\n', 'Revision: `published`\nBaseline: `' + 'a' * 40 + '`\n')
        first = guidance_approval.parse_guidance_document(approved)
        second = guidance_approval.parse_guidance_document(with_baseline)
        changed = guidance_approval.parse_guidance_document(with_baseline.replace('a' * 40, 'b' * 40))
        self.assertEqual(first.structure_hash, second.structure_hash)
        self.assertEqual(second.proposals[0].decision.baseline, 'a' * 40)
        self.assertNotEqual(first.proposals[0].decision.identity, second.proposals[0].decision.identity)
        self.assertNotEqual(second.proposals[0].decision.identity, changed.proposals[0].decision.identity)
        with self.assertRaises(guidance_approval.GuidanceFormatError):
            guidance_approval.parse_guidance_document(with_baseline.replace('a' * 40, 'HEAD'))

    def test_parser_binds_target_patch_and_decision_deterministically(self):
        pending = guidance_approval.parse_guidance_document(guidance_plan())
        approved = guidance_approval.parse_guidance_document(
            guidance_plan(decision="approve", revision="1")
        )

        self.assertEqual(pending.structure_hash, approved.structure_hash)
        self.assertEqual(pending.proposals[0].identity, approved.proposals[0].identity)
        self.assertNotEqual(
            pending.proposals[0].decision.identity,
            approved.proposals[0].decision.identity,
        )
        self.assertEqual(
            guidance_approval.apply_patch_to_content(
                b"# Agents\n", "AGENTS.md", approved.proposals[0].patch
            ),
            b"# Agents\nApproved rule\n",
        )
        self.assertEqual(
            guidance_approval.guidance_patch_hash("AGENTS.md", PATCH),
            guidance_approval.guidance_patch_hash("AGENTS.md", PATCH.replace("\n", "\r\n")),
        )

    def test_parser_rejects_incomplete_ambiguous_and_out_of_scope_patches(self):
        cases = (
            guidance_plan().replace("Impact: The Worker cannot finish safely without it.\n", ""),
            guidance_plan().replace("Decision: `pending`", "Decision: `approve`"),
            guidance_plan().replace("b/AGENTS.md", "b/docs/PLANS.md"),
            guidance_plan() + "\n### Agent Guidance User Decision: worker-rule\n",
        )
        for value in cases:
            with self.subTest(value=value[-80:]), self.assertRaises(
                guidance_approval.GuidanceFormatError
            ):
                guidance_approval.parse_guidance_document(value)


class GuidanceRuntimeTest(unittest.TestCase):
    def test_proposal_blocks_until_only_user_decision_changes_then_preserves_session(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, plan_path = repository(Path(directory))
            connection = active_connection(repo, plan_path)
            loop_manager.notifications.configure_session(connection, enabled=True)
            plan = loop_manager.active_execplan(connection, str(repo))
            document = guidance_approval.parse_guidance_document(
                (repo / plan_path).read_text(encoding="utf-8")
            )
            original_head = git(repo, "rev-parse", "HEAD")
            implementation = repo / "implementation.py"
            implementation.write_text("work in progress\n", encoding="utf-8")

            with mock.patch("loop_manager.is_loop_manager_repository", return_value=False):
                loop_manager._block_for_new_guidance(
                    connection,
                    repo,
                    str(repo),
                    repo,
                    plan,
                    document,
                    document.proposals,
                    "proposal recorded",
                )

            blocked = loop_manager.plan_blocked_execplans(connection, str(repo))[0]
            self.assertEqual(
                (blocked["status"], blocked["blocked_reason"]),
                ("PLAN_BLOCKED", "guidance_approval_required"),
            )
            self.assertEqual(blocked["worker_session_id"], "worker-session")
            self.assertIsNone(blocked["blocked_fingerprint"])
            self.assertEqual((repo / "AGENTS.md").read_text(), "# Agents\n")
            self.assertEqual(git(repo, "rev-parse", "HEAD"), original_head)
            self.assertEqual(implementation.read_text(), "work in progress\n")
            pending_payload = connection.execute(
                "SELECT payload FROM notification_outbox WHERE event_type = 'blocked'"
            ).fetchone()
            self.assertIsNone(pending_payload)
            checkpoint_repository = (
                loop_manager.blocked_checkpoint_state.BlockedCheckpointRepository(
                    connection
                )
            )
            checkpoint_repository.finish_unpublished(
                checkpoint_repository.get(str(repo), "G-020"),
                "guidance approval remains local-only",
                retryable=False,
            )
            with connection:
                loop_manager.notifications.enqueue_current_project_events(
                    connection, str(repo)
                )
            payload = connection.execute(
                "SELECT payload FROM notification_outbox WHERE event_type = 'blocked'"
            ).fetchone()[0]
            self.assertIn('"execplan_id": "G-020"', payload)
            self.assertIn(
                '"proposal": "docs/plans/[G-020] Guidance.md:', payload
            )
            self.assertIn(
                '"user_decision": "docs/plans/[G-020] Guidance.md:', payload
            )

            unrelated = (repo / plan_path).read_text().replace(
                "Test.", "Changed outside decision."
            )
            (repo / plan_path).write_text(unrelated, encoding="utf-8")
            with mock.patch("loop_manager.validate_execplan_workspace_identity"), mock.patch(
                "loop_manager.make_worktree"
            ), mock.patch("loop_manager.validate_blocked_agent_scope"):
                self.assertFalse(
                    loop_manager.resume_guidance_approval_if_decided(
                        connection, repo, str(repo), "main", Path(directory)
                    )
                )
            self.assertEqual(
                loop_manager.plan_blocked_execplans(connection, str(repo))[0]["status"],
                "PLAN_BLOCKED",
            )

            approved = guidance_plan(decision="approve", revision="decision-1")
            (repo / plan_path).write_text(approved, encoding="utf-8")
            with mock.patch("loop_manager.validate_execplan_workspace_identity"), mock.patch(
                "loop_manager.make_worktree"
            ), mock.patch("loop_manager.validate_blocked_agent_scope"):
                self.assertTrue(
                    loop_manager.resume_guidance_approval_if_decided(
                        connection, repo, str(repo), "main", Path(directory)
                    )
                )

            resumed = loop_manager.active_execplan(connection, str(repo))
            project = connection.execute(
                "SELECT phase, worker_session_id FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(
                (resumed["status"], project["phase"]),
                ("IMPLEMENTING", "WORKING"),
            )
            self.assertEqual(
                (resumed["worker_session_id"], project["worker_session_id"]),
                ("worker-session", "worker-session"),
            )
            self.assertEqual(implementation.read_text(), "work in progress\n")
            self.assertEqual(
                loop_manager._guidance_worker_targets(connection, str(repo), "G-020"),
                ("AGENTS.md",),
            )

            (repo / "AGENTS.md").write_text("# Agents\nApproved rule\n", encoding="utf-8")
            self.assertFalse(
                loop_manager.inspect_worker_guidance_result(
                    connection,
                    repo,
                    str(repo),
                    repo,
                    resumed,
                    "approved patch applied",
                )
            )
            approval = connection.execute(
                "SELECT decision, approved_patch_hash, applied FROM guidance_approvals"
            ).fetchone()
            self.assertEqual((approval["decision"], approval["applied"]), ("approve", 1))
            self.assertEqual(
                approval["approved_patch_hash"],
                guidance_approval.guidance_patch_hash("AGENTS.md", PATCH),
            )

            tampered = guidance_approval.parse_guidance_document(
                guidance_plan(decision="approve", revision="changed-after-apply")
            )
            with self.assertRaisesRegex(
                loop_manager.AgentScopeError,
                "확정된 guidance User Decision",
            ):
                loop_manager._guidance_document_matches_rows(
                    tampered,
                    loop_manager._guidance_rows(connection, str(repo), "G-020"),
                    require_decisions=False,
                )

    def test_reject_and_replacement_decisions_resume_without_expanding_scope(self):
        replacement = PATCH.replace("Approved rule", "Replacement rule")
        scenarios = (
            ("reject", "reject-1", "", "# Agents\n", ("AGENTS.md",)),
            (
                "replace",
                "replace-1",
                replacement,
                "# Agents\nReplacement rule\n",
                ("AGENTS.md",),
            ),
        )
        for decision, revision, decision_patch, expected, targets in scenarios:
            with self.subTest(decision=decision), tempfile.TemporaryDirectory() as directory:
                repo, plan_path = repository(Path(directory))
                connection = active_connection(repo, plan_path)
                plan = loop_manager.active_execplan(connection, str(repo))
                document = guidance_approval.parse_guidance_document(
                    (repo / plan_path).read_text(encoding="utf-8")
                )
                with mock.patch("loop_manager.is_loop_manager_repository", return_value=False):
                    loop_manager._block_for_new_guidance(
                        connection,
                        repo,
                        str(repo),
                        repo,
                        plan,
                        document,
                        document.proposals,
                        "proposal",
                    )
                (repo / plan_path).write_text(
                    guidance_plan(
                        decision=decision,
                        revision=revision,
                        patch=decision_patch,
                    ),
                    encoding="utf-8",
                )
                with mock.patch("loop_manager.validate_execplan_workspace_identity"), mock.patch(
                    "loop_manager.make_worktree"
                ), mock.patch("loop_manager.validate_blocked_agent_scope"):
                    self.assertTrue(
                        loop_manager.resume_guidance_approval_if_decided(
                            connection, repo, str(repo), "main", Path(directory)
                        )
                    )
                self.assertEqual(
                    loop_manager._guidance_worker_targets(connection, str(repo), "G-020"),
                    targets,
                )
                (repo / "AGENTS.md").write_text(expected, encoding="utf-8")
                active = loop_manager.active_execplan(connection, str(repo))
                self.assertFalse(
                    loop_manager.inspect_worker_guidance_result(
                        connection, repo, str(repo), repo, active, "continued"
                    )
                )
                row = connection.execute(
                    "SELECT decision, applied FROM guidance_approvals"
                ).fetchone()
                self.assertEqual((row["decision"], row["applied"]), (decision, 1))

    def test_rejected_rule_can_be_followed_by_a_new_pending_proposal(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, plan_path = repository(Path(directory))
            connection = active_connection(repo, plan_path)
            plan = loop_manager.active_execplan(connection, str(repo))
            document = guidance_approval.parse_guidance_document(
                (repo / plan_path).read_text(encoding="utf-8")
            )
            with mock.patch("loop_manager.is_loop_manager_repository", return_value=False):
                loop_manager._block_for_new_guidance(
                    connection,
                    repo,
                    str(repo),
                    repo,
                    plan,
                    document,
                    document.proposals,
                    "first proposal",
                )
            rejected = guidance_plan(decision="reject", revision="reject-1")
            (repo / plan_path).write_text(rejected, encoding="utf-8")
            with mock.patch("loop_manager.validate_execplan_workspace_identity"), mock.patch(
                "loop_manager.make_worktree"
            ), mock.patch("loop_manager.validate_blocked_agent_scope"):
                self.assertTrue(
                    loop_manager.resume_guidance_approval_if_decided(
                        connection, repo, str(repo), "main", Path(directory)
                    )
                )
            active = loop_manager.active_execplan(connection, str(repo))
            self.assertFalse(
                loop_manager.inspect_worker_guidance_result(
                    connection, repo, str(repo), repo, active, "rejected"
                )
            )

            second = rejected.rstrip() + """
            
### Agent Guidance Proposal: alternative-rule
Target: `AGENTS.md`
Scope: `project`
Reason: The rejected rule cannot satisfy the plan.
Impact: An alternative exact rule is required.
Patch:
```diff
diff --git a/AGENTS.md b/AGENTS.md
--- a/AGENTS.md
+++ b/AGENTS.md
@@ -1 +1,2 @@
 # Agents
+Alternative rule
```

### Agent Guidance User Decision: alternative-rule
Decision: `pending`
Revision: ``
Patch:
```diff
```
"""
            (repo / plan_path).write_text(second, encoding="utf-8")
            active = loop_manager.active_execplan(connection, str(repo))
            self.assertTrue(
                loop_manager.inspect_worker_guidance_result(
                    connection,
                    repo,
                    str(repo),
                    repo,
                    active,
                    "alternative proposed",
                )
            )

            rows = connection.execute(
                "SELECT proposal_id, decision, applied FROM guidance_approvals "
                "ORDER BY proposal_id"
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in rows],
                [
                    ("alternative-rule", "pending", 0),
                    ("worker-rule", "reject", 1),
                ],
            )
            blocked = loop_manager.plan_blocked_execplans(connection, str(repo))[0]
            self.assertEqual(
                (blocked["status"], blocked["worker_session_id"]),
                ("PLAN_BLOCKED", "worker-session"),
            )

    def test_unappliable_proposal_does_not_enter_blocked_state(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, plan_path = repository(Path(directory))
            invalid = guidance_plan().replace("# Agents", "# Missing", 1)
            (repo / plan_path).write_text(invalid, encoding="utf-8")
            connection = active_connection(repo, plan_path)
            plan = loop_manager.active_execplan(connection, str(repo))
            document = guidance_approval.parse_guidance_document(invalid)

            with mock.patch(
                "loop_manager.is_loop_manager_repository",
                return_value=False,
            ), self.assertRaises(guidance_approval.GuidanceFormatError):
                loop_manager._block_for_new_guidance(
                    connection,
                    repo,
                    str(repo),
                    repo,
                    plan,
                    document,
                    document.proposals,
                    "invalid proposal",
                )

            current = loop_manager.active_execplan(connection, str(repo))
            self.assertEqual(current["status"], "IMPLEMENTING")
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM guidance_approvals").fetchone()[0],
                0,
            )

    def test_exact_result_mismatch_requires_a_new_decision_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, plan_path = repository(Path(directory))
            connection = active_connection(repo, plan_path)
            plan = loop_manager.active_execplan(connection, str(repo))
            document = guidance_approval.parse_guidance_document(
                (repo / plan_path).read_text(encoding="utf-8")
            )
            with mock.patch("loop_manager.is_loop_manager_repository", return_value=False):
                loop_manager._block_for_new_guidance(
                    connection,
                    repo,
                    str(repo),
                    repo,
                    plan,
                    document,
                    document.proposals,
                    "proposal",
                )
            (repo / plan_path).write_text(
                guidance_plan(decision="approve", revision="decision-1"),
                encoding="utf-8",
            )
            with mock.patch("loop_manager.validate_execplan_workspace_identity"), mock.patch(
                "loop_manager.make_worktree"
            ), mock.patch("loop_manager.validate_blocked_agent_scope"):
                self.assertTrue(
                    loop_manager.resume_guidance_approval_if_decided(
                        connection, repo, str(repo), "main", Path(directory)
                    )
                )

            (repo / "AGENTS.md").write_text(
                "# Agents\nApproved rule\nUnapproved extra\n",
                encoding="utf-8",
            )
            active = loop_manager.active_execplan(connection, str(repo))
            self.assertTrue(
                loop_manager.inspect_worker_guidance_result(
                    connection, repo, str(repo), repo, active, "bad result"
                )
            )
            invalidated = connection.execute(
                "SELECT invalidated_decision_hash, applied FROM guidance_approvals"
            ).fetchone()
            self.assertTrue(invalidated["invalidated_decision_hash"])
            self.assertEqual(invalidated["applied"], 0)

            with mock.patch("loop_manager.validate_execplan_workspace_identity"), mock.patch(
                "loop_manager.make_worktree"
            ), mock.patch("loop_manager.validate_blocked_agent_scope"):
                self.assertFalse(
                    loop_manager.resume_guidance_approval_if_decided(
                        connection, repo, str(repo), "main", Path(directory)
                    )
                )
            (repo / plan_path).write_text(
                guidance_plan(decision="approve", revision="decision-2"),
                encoding="utf-8",
            )
            with mock.patch("loop_manager.validate_execplan_workspace_identity"), mock.patch(
                "loop_manager.make_worktree"
            ), mock.patch("loop_manager.validate_blocked_agent_scope"):
                self.assertTrue(
                    loop_manager.resume_guidance_approval_if_decided(
                        connection, repo, str(repo), "main", Path(directory)
                    )
                )
            (repo / "AGENTS.md").write_text(
                "# Agents\nApproved rule\n",
                encoding="utf-8",
            )
            active = loop_manager.active_execplan(connection, str(repo))
            self.assertFalse(
                loop_manager.inspect_worker_guidance_result(
                    connection, repo, str(repo), repo, active, "corrected result"
                )
            )
            self.assertEqual(
                connection.execute("SELECT applied FROM guidance_approvals").fetchone()[0],
                1,
            )


    def test_persisted_scope_accepts_only_the_decision_edit_after_worker_proposal(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, plan_path = repository(Path(directory))
            plain = guidance_plan().split(
                "### Agent Guidance Proposal: worker-rule", 1
            )[0]
            (repo / plan_path).write_text(plain, encoding="utf-8")
            git(repo, "add", plan_path)
            git(repo, "commit", "-m", "Store plan without guidance")
            connection = active_connection(repo, plan_path)
            before = loop_manager.begin_agent_scope(
                connection,
                str(repo),
                repo,
                "worker",
                input_fingerprint="input",
            )
            (repo / plan_path).write_text(guidance_plan(), encoding="utf-8")
            after = loop_manager.capture_git_snapshot(repo)
            loop_manager.validate_agent_scope(
                "worker",
                repo,
                before,
                after,
                selected_plan_path=plan_path,
            )
            plan = loop_manager.active_execplan(connection, str(repo))
            document = guidance_approval.parse_guidance_document(
                (repo / plan_path).read_text(encoding="utf-8")
            )
            with mock.patch("loop_manager.is_loop_manager_repository", return_value=False):
                loop_manager._block_for_new_guidance(
                    connection,
                    repo,
                    str(repo),
                    repo,
                    plan,
                    document,
                    document.proposals,
                    "proposal",
                )
            (repo / plan_path).write_text(
                guidance_plan(decision="approve", revision="decision-1"),
                encoding="utf-8",
            )

            persisted = loop_manager.validate_persisted_agent_scope(
                connection,
                repo,
                Path(directory),
                str(repo),
            )
            self.assertIsNotNone(persisted)
            loop_manager.validate_blocked_agent_scope(
                connection,
                repo,
                str(repo),
                "main",
                repo,
                plan_path,
            )

    def test_approval_state_survives_database_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, plan_path = repository(root)
            database = root / "state.db"
            connection = active_connection(repo, plan_path, database)
            plan = loop_manager.active_execplan(connection, str(repo))
            document = guidance_approval.parse_guidance_document(
                (repo / plan_path).read_text(encoding="utf-8")
            )
            with mock.patch("loop_manager.is_loop_manager_repository", return_value=False):
                loop_manager._block_for_new_guidance(
                    connection,
                    repo,
                    str(repo),
                    repo,
                    plan,
                    document,
                    document.proposals,
                    "proposal",
                )
            connection.close()

            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            (repo / plan_path).write_text(
                guidance_plan(decision="approve", revision="after-restart"),
                encoding="utf-8",
            )
            with mock.patch("loop_manager.validate_execplan_workspace_identity"), mock.patch(
                "loop_manager.make_worktree"
            ), mock.patch("loop_manager.validate_blocked_agent_scope"):
                self.assertTrue(
                    loop_manager.resume_guidance_approval_if_decided(
                        connection,
                        repo,
                        str(repo),
                        "main",
                        root,
                    )
                )

            approval = connection.execute(
                "SELECT decision, decision_revision, applied "
                "FROM guidance_approvals"
            ).fetchone()
            self.assertEqual(
                tuple(approval),
                ("approve", "after-restart", 0),
            )
            resumed = loop_manager.active_execplan(connection, str(repo))
            self.assertEqual(resumed["worker_session_id"], "worker-session")
            connection.close()

    def test_planner_guidance_proposal_is_rejected_before_catalog_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _plan_path = repository(Path(directory))
            active_sha = git(repo, "rev-parse", "HEAD")
            planner_path = "docs/plans/[G-021] Planned Guidance.md"
            (repo / planner_path).write_text(
                guidance_plan()
                .replace("id: G-020", "id: G-021")
                .replace("title: Guidance", "title: Planned Guidance"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                loop_manager.PlannerValidationError,
                "guidance",
            ):
                loop_manager.validate_planner_result(
                    repo,
                    active_sha,
                    [planner_path],
                )


    def test_scope_and_role_boundaries_require_exact_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, plan_path = repository(Path(directory))
            before = loop_manager.capture_git_snapshot(repo)
            (repo / "AGENTS.md").write_text("# Changed directly\n", encoding="utf-8")
            after = loop_manager.capture_git_snapshot(repo)
            with self.assertRaises(loop_manager.AgentScopeError):
                loop_manager.validate_agent_scope(
                    "worker", repo, before, after, selected_plan_path=plan_path
                )
            loop_manager.validate_agent_scope(
                "worker",
                repo,
                before,
                after,
                selected_plan_path=plan_path,
                allowed_guidance_targets=("AGENTS.md",),
            )
            with self.assertRaises(loop_manager.AgentScopeError):
                loop_manager.validate_agent_scope(
                    "reviewer",
                    repo,
                    before,
                    after,
                )

            with self.assertRaisesRegex(loop_manager.AgentScopeError, "자체 저장소"):
                loop_manager._validate_guidance_proposal_scope(
                    (
                        guidance_approval.parse_guidance_document(
                            guidance_plan().replace("Scope: `project`", "Scope: `common`")
                        ).proposals[0],
                    ),
                    common_allowed=False,
                )

            loop_manager._validate_guidance_proposal_scope(
                (
                    guidance_approval.parse_guidance_document(
                        guidance_plan().replace("Scope: `project`", "Scope: `common`")
                    ).proposals[0],
                ),
                common_allowed=True,
            )
            self.assertFalse(
                loop_manager._worker_path_allowed(
                    "docs/PLANS.md", plan_path
                )
            )
            self.assertTrue(
                loop_manager._worker_path_allowed(
                    "docs/PLANS.md", plan_path, allow_common_plans=True
                )
            )

    def test_common_plans_scope_requires_direct_acceptance_criterion(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _plan_path = repository(Path(directory))
            incidental_path = "docs/plans/[G-030] Boundary.md"
            explicit_path = "docs/plans/[G-031] Contract.md"
            conjunctive_path = "docs/plans/[G-032] Maintenance Contract.md"
            (repo / incidental_path).write_text(
                "## Acceptance Criteria\n\n"
                "- 후속 계획의 공통 `docs/PLANS.md` 변경을 구분하는 경계를 제공한다.\n",
                encoding="utf-8",
            )
            (repo / explicit_path).write_text(
                "## Acceptance Criteria\n\n"
                "- 공통 `docs/PLANS.md`에 guidance 계약을 문서화한다.\n",
                encoding="utf-8",
            )
            (repo / conjunctive_path).write_text(
                "## Acceptance Criteria\n\n"
                "- 공통 `docs/PLANS.md`와 Planner prompt는 유지보수 예외를 설명한다.\n",
                encoding="utf-8",
            )
            git(repo, "add", incidental_path, explicit_path, conjunctive_path)
            git(repo, "commit", "-m", "Add plan scope fixtures")
            plan_sha = git(repo, "rev-parse", "HEAD")

            with mock.patch("loop_manager.is_loop_manager_repository", return_value=True):
                self.assertFalse(
                    loop_manager._published_plan_allows_common_plans(
                        repo,
                        {"plan_sha": plan_sha, "path": incidental_path},
                    )
                )
                self.assertTrue(
                    loop_manager._published_plan_allows_common_plans(
                        repo,
                        {"plan_sha": plan_sha, "path": explicit_path},
                    )
                )
                self.assertTrue(
                    loop_manager._published_plan_allows_common_plans(
                        repo,
                        {"plan_sha": plan_sha, "path": conjunctive_path},
                    )
                )
                self.assertTrue(
                    loop_manager._published_plan_allows_common_plans(
                        repo,
                        {
                            "current_revision_sha": plan_sha,
                            "plan_sha": "0" * 40,
                            "path": explicit_path,
                        },
                    )
                )
            with mock.patch("loop_manager.is_loop_manager_repository", return_value=False):
                self.assertFalse(
                    loop_manager._published_plan_allows_common_plans(
                        repo,
                        {"plan_sha": plan_sha, "path": explicit_path},
                    )
                )

    def test_self_project_scope_is_accepted_and_new_prompts_use_project(self):
        document = guidance_approval.parse_guidance_document(guidance_plan())
        loop_manager._validate_guidance_proposal_scope(
            document.proposals, common_allowed=True
        )
        loop_manager._validate_guidance_proposal_scope(
            document.proposals, common_allowed=False
        )
        prompt = loop_manager.worker_prompt("plan.md", None, guidance_common_allowed=True)
        self.assertIn("Scope: `project`", prompt)

    def test_publication_rechecks_applied_approval_and_changed_base(self):
        for changed_base, changed_result in ((False, False), (False, True), (True, False)):
            with self.subTest(base=changed_base, result=changed_result):
                with tempfile.TemporaryDirectory() as directory:
                    repo, path = repository(Path(directory))
                    connection = active_connection(repo, path)
                    plan = loop_manager.active_execplan(connection, str(repo))
                    document = guidance_approval.parse_guidance_document(guidance_plan())
                    loop_manager._block_for_new_guidance(
                        connection, repo, str(repo), repo, plan, document,
                        document.proposals, "proposal",
                    )
                    (repo / path).write_text(
                        guidance_plan(decision="approve", revision="1"), encoding="utf-8"
                    )
                    with mock.patch("loop_manager.validate_execplan_workspace_identity"), mock.patch(
                        "loop_manager.make_worktree"
                    ), mock.patch("loop_manager.validate_blocked_agent_scope"):
                        self.assertTrue(loop_manager.resume_guidance_approval_if_decided(
                            connection, repo, str(repo), "main", Path(directory)
                        ))
                    (repo / "AGENTS.md").write_text("# Agents\nApproved rule\n")
                    plan = loop_manager.active_execplan(connection, str(repo))
                    self.assertFalse(loop_manager.inspect_worker_guidance_result(
                        connection, repo, str(repo), repo, plan, "applied"
                    ))
                    old_base = git(repo, "rev-parse", "HEAD")
                    new_base = old_base
                    if changed_base:
                        (repo / "AGENTS.md").write_bytes(b"# Agents\r\nRemote rule\r\n")
                        git(repo, "add", "AGENTS.md")
                        git(repo, "commit", "-m", "Remote base guidance")
                        new_base = git(repo, "rev-parse", "HEAD")
                    (repo / "AGENTS.md").write_text(
                        "# Agents\nApproved rule\n" + ("Unapproved\n" if changed_result else "")
                    )
                    blocked = loop_manager.validate_guidance_publication(
                        connection, str(repo), repo, plan, "applied",
                        previous_base=old_base, latest_base=new_base,
                    )
                    self.assertEqual(blocked, changed_base or changed_result)
                    row = connection.execute("SELECT * FROM guidance_approvals").fetchone()
                    self.assertEqual(row["applied"], 0 if blocked else 1)
                    if blocked:
                        blocked_plan = loop_manager.plan_blocked_execplans(
                            connection, str(repo)
                        )[0]
                        snapshot = blocked_plan["agent_result_snapshot"]
                        self.assertTrue(snapshot)
                        loop_manager.hydrate_execplan_project(
                            connection, str(repo), blocked_plan
                        )
                        (repo / "unexpected.py").write_text("unapproved user edit\n")
                        with self.assertRaises(loop_manager.AgentScopeError):
                            loop_manager.validate_blocked_agent_scope(
                                connection, repo, str(repo), "main", repo, path
                            )
                        with self.assertRaises(loop_manager.AgentScopeError):
                            loop_manager.validate_persisted_agent_scope(
                                connection, repo, Path(directory), str(repo)
                            )
                        (repo / "unexpected.py").unlink()
                        loop_manager.validate_blocked_agent_scope(
                            connection, repo, str(repo), "main", repo, path
                        )
                        self.assertTrue(row["invalidated_decision_hash"])
                        self.assertEqual(
                            loop_manager.plan_blocked_execplans(
                                connection, str(repo)
                            )[0]["status"],
                            "PLAN_BLOCKED",
                        )
                    if changed_base:
                        self.assertEqual(row["baseline_content"], b"# Agents\r\nRemote rule\r\n")
                        (repo / path).write_text(
                            guidance_plan(decision="reject", revision="new-base-2"),
                            encoding="utf-8",
                        )
                        with mock.patch("loop_manager.validate_execplan_workspace_identity"), mock.patch(
                            "loop_manager.make_worktree"
                        ), mock.patch("loop_manager.validate_blocked_agent_scope"):
                            self.assertTrue(loop_manager.resume_guidance_approval_if_decided(
                                connection, repo, str(repo), "main", Path(directory)
                            ))
                        (repo / "AGENTS.md").write_bytes(row["baseline_content"])
                        resumed = loop_manager.active_execplan(connection, str(repo))
                        self.assertEqual(resumed["worker_session_id"], "worker-session")
                        self.assertFalse(loop_manager.inspect_worker_guidance_result(
                            connection, repo, str(repo), repo, resumed, "new decision"
                        ))
                        self.assertFalse(loop_manager.validate_guidance_publication(
                            connection, str(repo), repo, resumed, "new decision",
                            previous_base=new_base, latest_base=new_base,
                        ))

    def test_rebase_conflict_requires_reapproval_before_worker_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, path = repository(Path(directory))
            connection = active_connection(repo, path)
            old_base = git(repo, "rev-parse", "HEAD")
            plan = loop_manager.active_execplan(connection, str(repo))
            document = guidance_approval.parse_guidance_document(guidance_plan())
            loop_manager._block_for_new_guidance(
                connection, repo, str(repo), repo, plan, document,
                document.proposals, "proposal",
            )
            (repo / path).write_text(guidance_plan(decision="approve", revision="1"))
            with mock.patch("loop_manager.validate_execplan_workspace_identity"):
                self.assertTrue(loop_manager.resume_guidance_approval_if_decided(
                    connection, repo, str(repo), "main", Path(directory)
                ))
            (repo / "AGENTS.md").write_text("# Agents\nApproved rule\n")
            plan = loop_manager.active_execplan(connection, str(repo))
            self.assertFalse(loop_manager.inspect_worker_guidance_result(
                connection, repo, str(repo), repo, plan, "applied"
            ))
            git(repo, "add", "-A")
            git(repo, "commit", "-m", "Approved worker guidance")
            git(repo, "checkout", "-b", "upstream", old_base)
            (repo / "AGENTS.md").write_text("# Agents\nRemote rule\n")
            git(repo, "add", "AGENTS.md")
            git(repo, "commit", "-m", "Remote guidance")
            new_base = git(repo, "rev-parse", "HEAD")
            git(repo, "checkout", "main")
            result = git_result(repo, "rebase", "upstream", check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(loop_manager._rebase_in_progress_for_branch(repo, "main"))
            loop_manager.clear_agent_scope(connection, str(repo))
            self.assertTrue(loop_manager.validate_guidance_publication(
                connection, str(repo), repo, plan, "applied",
                previous_base=old_base, latest_base=new_base,
            ))
            replacement = (
                "diff --git a/AGENTS.md b/AGENTS.md\n"
                "--- a/AGENTS.md\n+++ b/AGENTS.md\n"
                "@@ -1,2 +1,3 @@\n # Agents\n Remote rule\n+Approved rule\n"
            )
            (repo / path).write_text(guidance_plan(
                decision="replace", revision="2", patch=replacement
            ))
            loop_manager.validate_persisted_agent_scope(
                connection, repo, Path(directory), str(repo)
            )
            with mock.patch("loop_manager.validate_execplan_workspace_identity"):
                self.assertTrue(loop_manager.resume_guidance_approval_if_decided(
                    connection, repo, str(repo), "main", Path(directory)
                ))
            project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(project["worker_session_id"], "worker-session")
            self.assertEqual(project["last_error"], loop_manager.REBASE_IN_PROGRESS_ERROR)
            before = loop_manager.begin_agent_scope(connection, str(repo), repo, "worker")
            (repo / "AGENTS.md").write_text("# Agents\nRemote rule\nApproved rule\n")
            loop_manager.validate_agent_scope(
                "worker", repo, before, loop_manager.capture_git_snapshot(repo),
                selected_plan_path=path,
                allowed_guidance_targets=loop_manager._guidance_worker_targets(
                    connection, str(repo), "G-020"
                ),
            )
            plan = loop_manager.active_execplan(connection, str(repo))
            self.assertFalse(loop_manager.inspect_worker_guidance_result(
                connection, repo, str(repo), repo, plan, "resolved"
            ))
            loop_manager.clear_agent_scope(connection, str(repo))
            git(repo, "add", "-A")
            git(repo, "-c", "core.editor=true", "rebase", "--continue")
            self.assertFalse(loop_manager.validate_guidance_publication(
                connection, str(repo), repo, plan, "resolved",
                previous_base=old_base, latest_base=new_base,
            ))

    def test_schema_migration_and_prompt_preserve_guidance_contract(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(exec_plans)")
        }
        self.assertIn("blocked_reason", columns)
        self.assertIn("guidance_plan_hash", columns)
        self.assertIsNotNone(
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'guidance_approvals'"
            ).fetchone()
        )

        prompt = loop_manager.worker_prompt(
            "docs/plans/[G-020] Guidance.md",
            None,
            guidance_common_allowed=False,
        )
        self.assertIn("### Agent Guidance Proposal: guidance-example", prompt)
        self.assertIn("### Agent Guidance User Decision: guidance-example", prompt)
        self.assertIn("Scope `project`만 허용", prompt)
        self.assertIn("AGENTS.md를 직접 수정하지 마라", prompt)


if __name__ == "__main__":
    unittest.main()
