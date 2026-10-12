import subprocess
import unittest
from pathlib import Path
from unittest import mock

from loop_manager_internal.documents import guidance_approval
import loop_manager
from tests.support.guidance import PATCH, git_result, guidance_plan
from tests.support.git import git_test_environment


class GuidanceFormatTest(unittest.TestCase):

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
            guidance_plan().replace("Decision: `pending`", "Decision: `unknown`"),
            guidance_plan().replace("b/AGENTS.md", "b/docs/PLANS.md"),
            guidance_plan() + "\n### Agent Guidance User Decision: worker-rule\n",
        )
        for value in cases:
            with self.subTest(value=value[-80:]), self.assertRaises(
                guidance_approval.GuidanceFormatError
            ):
                guidance_approval.parse_guidance_document(value)


class GuidanceInputContractTest(unittest.TestCase):
    def parse(self, text):
        return guidance_approval.parse_guidance_document(text)

    def form(self, decision="pending", patch=PATCH):
        return guidance_plan(decision=decision, patch=patch).replace(
            "Revision: ``\n", "Comment: `` <!-- 선택: 이유 -->\n"
        ).replace(f"Decision: `{decision}`", f"Decision: `{decision}` <!-- 승인 안내 -->") + "<!-- END USER DECISION -->\n"

    def test_all_decisions_accept_optional_revision_and_displayed_original(self):
        for decision in ("pending", "approve", "reject", "replace"):
            with self.subTest(decision=decision):
                document = self.parse(self.form(decision))
                proposal, = document.proposals
                for text in (self.form(decision).rstrip("\n"), self.form(decision).replace("\n", "\r\n")):
                    self.assertEqual(self.parse(text).proposals[0].decision.identity, proposal.decision.identity)
                self.assertEqual(proposal.decision.revision, "")
                if decision == "pending":
                    with self.assertRaisesRegex(guidance_approval.GuidanceFormatError, "확정"):
                        guidance_approval.finalized_patch(proposal)
                else:
                    self.assertEqual(guidance_approval.finalized_patch(proposal), None if decision == "reject" else PATCH)

    def test_persisted_legacy_identity_fixtures_are_unchanged(self):
        legacy = guidance_plan(decision="approve", revision="legacy-revision")
        for text, expected in (
            (legacy, "e23ce09ed64f995fa237254fce1b62e80717587d3d1fc7fe2657adb20f55fdd6"),
            (legacy.replace("Revision: `legacy-revision`\n", "Revision: `legacy-revision`\nBaseline: `" + "a" * 40 + "`\n"),
             "2eecb5d4edb8a5643b3445e102947bfeb8749209da58ba6d7e133484bc541210"),
        ):
            self.assertEqual(self.parse(text).proposals[0].decision.identity, expected)

    def test_display_comments_and_memos_preserve_legacy_identities(self):
        for decision in ("pending", "approve", "reject"):
            revision = "" if decision == "pending" else "old-user-revision"
            legacy = guidance_plan(decision=decision, revision=revision)
            original = self.parse(legacy)
            displayed = guidance_plan(decision=decision, revision=revision, patch=PATCH)
            displayed = displayed.replace(f"Decision: `{decision}`", f"Decision: `{decision}` <!-- help -->")
            displayed = displayed.replace("Patch:\n```diff\n```", "Patch: <!-- help -->\n```diff\n```")
            displayed += "  <!-- END USER DECISION -->  \n## 메모\nDecision: `replace`\n- 자유 메모\n"
            for text in (displayed, displayed.replace("Revision:", "Comment: `why` <!-- 선택 -->\nRevision:")):
                actual = self.parse(text)
                self.assertEqual(actual.structure_hash, original.structure_hash)
                self.assertEqual(actual.proposals[0].decision.identity, original.proposals[0].decision.identity)
                self.assertEqual(actual.proposals[0].identity, original.proposals[0].identity)

    def test_legacy_eof_and_top_level_boundaries_and_line_endings(self):
        for tail in ("", "\n## Other section\nProtected text\n"):
            for newline in ("\n", "\r\n"):
                for final_newline in (True, False):
                    text = (guidance_plan(decision="approve", revision="legacy") + tail)
                    if not final_newline:
                        text = text.rstrip("\n")
                    actual = self.parse(text.replace("\n", newline))
                    self.assertEqual(actual.proposals[0].decision.revision, "legacy")
        first = self.parse(guidance_plan() + "\n## Other section\nOriginal\n")
        second = self.parse(guidance_plan() + "\n## Other section\nEdited\n")
        self.assertNotEqual(first.structure_hash, second.structure_hash)

    def test_marker_addition_matches_saved_legacy_suffix_without_ignoring_edits(self):
        suffix = "## Existing section\nProtected text\n## Another section\nMore text\n"
        original = self.parse(guidance_plan() + suffix)
        for memo in ("", "A note\n", "## Memo\nDecision: `reject`\n"):
            text = self.form("approve") + memo + suffix
            document = self.parse(text)
            self.assertTrue(document.matches_structure_hash(original.structure_hash))
            for edited in (text.replace("Protected text", "Edited text"),
                           text.replace("More text", "Edited text"),
                           text.replace("Test.", "Edited purpose")):
                self.assertFalse(self.parse(edited).matches_structure_hash(original.structure_hash))
        for text in (self.form("approve"), self.form("approve") + suffix.split("## Another", 1)[0]):
            self.assertFalse(self.parse(text).matches_structure_hash(original.structure_hash))

    def test_indented_backticks_in_real_diff_context_do_not_close_fence(self):
        original = b"# Agents\n    ```\n"
        patch = "diff --git a/AGENTS.md b/AGENTS.md\n--- a/AGENTS.md\n+++ b/AGENTS.md\n@@ -1,2 +1,3 @@\n # Agents\n     ```\n+Approved rule\n"
        text = guidance_plan(decision="approve", patch=patch).replace(PATCH, patch)
        for value in (text, text + "<!-- END USER DECISION -->\n", text.replace("\n", "\r\n")):
            proposal, = self.parse(value).proposals
            self.assertEqual(proposal.patch, patch)
            self.assertEqual(guidance_approval.finalized_patch(proposal), patch)
        self.assertEqual(guidance_approval.apply_patch_to_content(original, "AGENTS.md", patch),
                         b"# Agents\n    ```\nApproved rule\n")
        for indentation in (1, 3, 5, 8):
            context_patch = patch.replace("     ```", " " * indentation + "```")
            proposal, = self.parse(guidance_plan(decision="replace", patch=context_patch).replace(PATCH, context_patch)).proposals
            self.assertEqual(proposal.decision.patch, context_patch)

    def test_pending_edited_patch_waits_approve_requires_replace(self):
        edited = PATCH.replace("Approved rule", "Edited rule")
        pending = self.parse(self.form(patch=edited)).proposals[0]
        self.assertEqual(pending.decision.decision, "pending")
        with self.assertRaisesRegex(guidance_approval.GuidanceFormatError, "replace"):
            self.parse(self.form("approve", edited))
        replacement = self.parse(self.form("replace", edited)).proposals[0]
        self.assertEqual(guidance_approval.finalized_patch(replacement), edited)
        rejected = self.parse(self.form("reject", edited)).proposals[0]
        self.assertIsNone(guidance_approval.finalized_patch(rejected))
        for decision in ("pending", "approve", "reject", "replace"):
            with self.subTest(decision=decision), self.assertRaises(guidance_approval.GuidanceFormatError):
                self.parse(self.form(decision, edited.replace("AGENTS.md", "other.md")))
        with self.assertRaisesRegex(guidance_approval.GuidanceFormatError, "Patch"):
            self.parse(self.form("replace", ""))

    def test_marker_and_heading_strings_in_fences_remain_literal(self):
        patch = PATCH.replace("+Approved rule", "+<!-- END USER DECISION -->\n+### Agent Guidance User Decision: hidden")
        text = self.form("replace", patch)
        actual = self.parse(text).proposals[0]
        self.assertEqual(actual.decision.patch, patch)
        # A standalone marker in a fenced memo is also literal.
        memo = "```text\n<!-- END USER DECISION -->\n### Agent Guidance User Decision: hidden\n```\n"
        self.assertEqual(self.parse(text + memo).structure_hash, self.parse(text).structure_hash)
        # A marker inside an unclosed diff does not repair the missing fence.
        with self.assertRaises(guidance_approval.GuidanceFormatError):
            self.parse(text.rsplit("```", 1)[0] + "<!-- END USER DECISION -->")

    def test_multiple_pairs_after_memo_still_validate_guidance(self):
        first = self.form()
        second = self.form("approve").split("### Agent Guidance Proposal:", 1)[1]
        text = first + "## 메모\nDecision: `reject`\n### Agent Guidance Proposal:" + second.replace("worker-rule", "second-rule")
        actual = self.parse(text)
        self.assertEqual([p.proposal_id for p in actual.proposals], ["worker-rule", "second-rule"])
        for suffix in (
            "### Agent Guidance User Decision: worker-rule\n",
            "### Agent Guidance Proposal: INVALID ID\n",
            "### Agent Guidance Proposal:" + second,
        ):
            with self.subTest(suffix=suffix[:60]), self.assertRaises(guidance_approval.GuidanceFormatError):
                self.parse(first + suffix)

    def test_invalid_fields_markers_and_incomplete_blocks_are_rejected(self):
        valid = self.form()
        invalid = (
            valid + "<!-- END USER DECISION -->\n",
            "<!-- END USER DECISION -->\n" + valid,
            valid.replace("## Decision Log\n", "## Decision Log\n<!-- END USER DECISION -->\n"),
            valid.replace("Target:", "<!-- END USER DECISION -->\nTarget:"),
            valid.replace("Comment: ``", "Comment: ``\nComment: `duplicate`"),
            valid.replace("Comment: ``", "Comment: no-backticks"),
            valid.replace("Comment: ``", "Baseline: `HEAD`"),
            valid.replace("Comment: ``", "Revision: `bad`value`"),
            valid.replace("Comment: ``", "Decision: `approve`"),
            valid.replace("Comment: ``", "Unknown: `value`"),
            valid.replace("Comment: ``", "Baseline: `" + "a" * 40 + "`"),
            valid.replace("Comment:", "<!-- END USER DECISION -->\nComment:"),
        )
        for text in invalid:
            with self.subTest(text=text[-100:]), self.assertRaises(guidance_approval.GuidanceFormatError):
                self.parse(text)
        with self.assertRaisesRegex(guidance_approval.GuidanceFormatError, "밖"):
            self.parse("<!-- END USER DECISION -->")

    def test_fences_do_not_swallow_trailing_content_or_expand_patch(self):
        original = self.parse(guidance_plan(decision="approve", revision="legacy<!-- literal -->"))
        self.assertEqual(original.proposals[0].decision.revision, "legacy<!-- literal -->")
        # Fence-looking text on an added diff line is retained in full.
        patch = PATCH.replace("+Approved rule", "+```")
        actual = self.parse(self.form("replace", patch))
        self.assertEqual(actual.proposals[0].decision.patch, patch)
        self.assertEqual(actual.proposals[0].patch, PATCH)
        for text in (
            guidance_plan().replace("Revision: ``", "Comment: ``\nRevision: ``\nRevision: ``"),
            guidance_plan().replace("Patch:\n```diff\n```", "Patch:\n```diff\n```\nextra text\n```"),
            guidance_plan().replace("Patch:\n```diff\n```", "Patch:\n```diff\n```\nPatch:\n```diff\n```"),
            guidance_plan().replace("+Approved rule\n```", "+Approved rule\n```\nextra text\n```", 1),
        ):
            with self.subTest(text=text[-120:]), self.assertRaises(guidance_approval.GuidanceFormatError):
                self.parse(text)

    def test_only_decision_metadata_is_excluded_from_structure(self):
        valid = self.form("approve")
        document = self.parse(valid)
        self.assertNotEqual(document.structure_hash, self.parse(valid.replace("Test.", "Changed purpose")).structure_hash)
        changed = self.parse(valid.replace("needs this rule", "needs another rule"))
        self.assertNotEqual(document.structure_hash, changed.structure_hash)
        self.assertNotEqual(document.proposals[0].identity, changed.proposals[0].identity)
        # Markdown titles in an explicitly terminated memo area are notes.
        first = self.parse(valid + "## Validation\nOriginal memo\n")
        second = self.parse(valid + "## Validation\nChanged memo\n")
        self.assertEqual(first.structure_hash, second.structure_hash)
        with self.assertRaises(guidance_approval.GuidanceFormatError):
            self.parse(valid + "## Validation\n### Agent Guidance User Decision: hidden\n")


if __name__ == "__main__":
    unittest.main()


class CommonPlansScopeTest(unittest.TestCase):
    def test_common_plans_scope_requires_direct_acceptance_criterion(self):
        repo = Path("/fixture/repository")
        incidental_path = "docs/plans/[G-030] Boundary.md"
        explicit_path = "docs/plans/[G-031] Contract.md"
        conjunctive_path = "docs/plans/[G-032] Maintenance Contract.md"
        plan_sha = "a" * 40
        current_revision_sha = "b" * 40
        shown = mock.Mock(side_effect=[
            subprocess.CompletedProcess([], 0,
                                        "## Acceptance Criteria\n\n"
                                        "- 후속 계획의 공통 `docs/PLANS.md` 변경을 구분하는 경계를 제공한다.\n"),
            subprocess.CompletedProcess([], 0,
                                        "## Acceptance Criteria\n\n"
                                        "- 공통 `docs/PLANS.md`에 guidance 계약을 문서화한다.\n"),
            subprocess.CompletedProcess([], 0,
                                        "## Acceptance Criteria\n\n"
                                        "- 공통 `docs/PLANS.md`와 Planner prompt는 유지보수 예외를 설명한다.\n"),
            subprocess.CompletedProcess([], 0,
                                        "## Acceptance Criteria\n\n"
                                        "- 공통 `docs/PLANS.md`에 guidance 계약을 문서화한다.\n"),
            subprocess.CompletedProcess([], 128, "fatal: unknown revision\n"),
        ])
        repository_type = mock.Mock(side_effect=[True, True, True, True, True, False])

        with mock.patch.object(loop_manager, "git", shown), \
             mock.patch.object(loop_manager, "is_loop_manager_repository", repository_type), \
             mock.patch("subprocess.Popen", side_effect=AssertionError("unexpected Git subprocess")) as process:
            self.assertFalse(loop_manager._published_plan_allows_common_plans(
                repo, {"plan_sha": plan_sha, "path": incidental_path}))
            self.assertTrue(loop_manager._published_plan_allows_common_plans(
                repo, {"plan_sha": plan_sha, "path": explicit_path}))
            self.assertTrue(loop_manager._published_plan_allows_common_plans(
                repo, {"plan_sha": plan_sha, "path": conjunctive_path}))
            self.assertTrue(loop_manager._published_plan_allows_common_plans(
                repo, {"current_revision_sha": current_revision_sha,
                       "plan_sha": "0" * 40, "path": explicit_path}))
            self.assertFalse(loop_manager._published_plan_allows_common_plans(
                repo, {"plan_sha": plan_sha, "path": explicit_path}))
            self.assertFalse(loop_manager._published_plan_allows_common_plans(
                repo, {"plan_sha": plan_sha, "path": explicit_path}))

        self.assertEqual(repository_type.call_args_list, [mock.call(repo)] * 6)
        self.assertEqual(shown.call_args_list, [
            mock.call(repo, "show", f"{plan_sha}:{incidental_path}", check=False),
            mock.call(repo, "show", f"{plan_sha}:{explicit_path}", check=False),
            mock.call(repo, "show", f"{plan_sha}:{conjunctive_path}", check=False),
            mock.call(repo, "show", f"{current_revision_sha}:{explicit_path}", check=False),
            mock.call(repo, "show", f"{plan_sha}:{explicit_path}", check=False),
        ])
        process.assert_not_called()


class GuidanceGitAdapterTest(unittest.TestCase):
    def test_git_result_reuses_isolated_environment_and_preserves_result_contract(self):
        repo = Path("/fixture/repository")
        result = subprocess.CompletedProcess(["git"], 1, "fatal: missing revision\n")
        with mock.patch("tests.support.guidance.subprocess.run", return_value=result) as run:
            self.assertIs(git_result(repo, "show", "missing:path", check=False), result)
        run.assert_called_once_with(
            ["git", "-C", str(repo), "show", "missing:path"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=git_test_environment(),
        )

    def test_git_result_preserves_checked_subprocess_error(self):
        error = subprocess.CalledProcessError(128, ["git"], output="fatal: failure\n")
        with mock.patch("tests.support.guidance.subprocess.run", side_effect=error):
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                git_result(Path("/fixture/repository"), "show", "missing:path")
        self.assertIs(raised.exception, error)
