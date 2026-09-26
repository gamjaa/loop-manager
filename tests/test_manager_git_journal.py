import tempfile
import unittest
from pathlib import Path

import manager_git_journal as journal


OLD = "a" * 40
NEW = "b" * 40
REF = "refs/remotes/origin/main"


class ManagerGitTransitionTests(unittest.TestCase):
    def test_fetch_requires_owned_marker_and_exact_live_result(self):
        intent = {
            "kind": "project_fetch", "target": REF,
            "before": {"oid": OLD}, "expected": {"oid": NEW},
        }
        with self.assertRaises(journal.MutationEvidenceError):
            journal.verified_transition(intent, {"oid": NEW}, False)
        with self.assertRaises(journal.MutationEvidenceError):
            journal.verified_transition(intent, {"oid": "c" * 40}, True)
        self.assertEqual(
            journal.verified_transition(intent, {"oid": NEW}, True),
            {"refs": {REF: NEW}, "worktrees": {}},
        )

    def test_source_local_delta_cannot_become_agent_baseline(self):
        before = {
            "head": OLD, "branch": "main", "changes": [["untracked", "user.txt", "sig"]],
            "operations": [],
        }
        intent = {
            "kind": "source_fast_forward", "target": "/source",
            "before": before, "expected": {**before, "head": NEW},
        }
        self.assertEqual(
            journal.verified_transition(intent, intent["expected"], True),
            {"refs": {}, "worktrees": {}},
        )
        with self.assertRaises(journal.MutationEvidenceError):
            journal.verified_transition(
                intent, {**intent["expected"], "changes": []}, True,
            )

    def test_pending_mixed_state_blocks_and_preserves_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            observed = {"oid": OLD}

            def apply(_token):
                observed["oid"] = "c" * 40

            arguments = dict(
                owner="project", kind="project_fetch", repo="/source", target=REF,
                before={"oid": OLD}, expected={"oid": NEW},
                observe=lambda: observed.copy(), apply=apply,
                marker=lambda _intent: True,
            )
            with self.assertRaises(journal.MutationEvidenceError):
                journal.execute(state, **arguments)
            with self.assertRaises(journal.MutationEvidenceError):
                journal.execute(state, **arguments)
            self.assertEqual(observed["oid"], "c" * 40)
            self.assertEqual(
                len(list((state / "manager-git-journal").glob("*/active.json"))), 1,
            )

    def test_candidates_require_an_unbroken_chain_to_live_ref(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            observed = {"oid": OLD}
            for target in (NEW, "c" * 40):
                previous = observed.copy()
                journal.execute(
                    state, owner="project", kind="project_fetch", repo="/source",
                    target=REF, before=previous, expected={"oid": target},
                    observe=lambda: observed.copy(),
                    apply=lambda _token, value=target: observed.update(oid=value),
                    marker=lambda _intent: True,
                )
            arguments = dict(
                baseline_refs={REF: OLD}, baseline_worktrees={}, live_worktrees={},
                marker=lambda _intent: True,
            )
            self.assertEqual(
                journal.verified_candidates(
                    state, "/source", live_refs={REF: "c" * 40}, **arguments,
                )["refs"], {REF: "c" * 40},
            )
            self.assertEqual(
                journal.verified_candidates(
                    state, "/source", live_refs={REF: NEW}, **arguments,
                )["refs"], {},
            )
            self.assertEqual(
                journal.verified_candidates(
                    state, "/source", live_refs={REF: "d" * 40}, **arguments,
                )["refs"], {},
            )


if __name__ == "__main__":
    unittest.main()
