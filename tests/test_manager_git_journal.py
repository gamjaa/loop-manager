import tempfile
import unittest
from pathlib import Path
from unittest import mock

import manager_git_journal as journal


OLD = "a" * 40
NEW = "b" * 40
REF = "refs/remotes/origin/main"


class ManagerGitTransitionTests(unittest.TestCase):
    def test_legacy_resolution_uses_approved_commit_before_later_same_file_change(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            slot = journal._slot(state, '/repo', 'managed_rebase', '/owned')
            slot.mkdir(parents=True)
            expected = {
                'branch': 'loop-manager/G-100', 'before_head': OLD,
                'target_base_oid': NEW, 'remote_oid': OLD,
                'agent_session_id': 'worker-1',
            }
            initial = {
                'id': 'initial', 'repo': '/repo', 'target': '/owned',
                'owner': 'plan:G-100', 'before': {
                    'head': OLD, 'metadata': None,
                }, 'expected': expected,
            }
            final = {
                **initial, 'id': 'resolution', 'before': {
                    'head': NEW, 'dirty': {'same.txt': [['staged'], 'approved']},
                }, 'expected': {
                    **expected, 'action': 'continue',
                    'staged_tree_oid': 'c' * 40,
                    'accepted_files': {'same.txt': 'approved'},
                },
            }
            journal._write_new(slot / 'initial.intent.json', initial)
            journal._write_new(slot / 'initial.result.json', {
                'id': 'initial', 'status': 'complete', 'candidates': {},
                'observed': {'metadata': {'onto': NEW}},
            })
            replay_oids = ['d' * 40, 'e' * 40]
            facts = {
                replay_oids[0]: {'oid': replay_oids[0], 'parent': NEW,
                                 'tree': 'c' * 40},
                replay_oids[1]: {'oid': replay_oids[1],
                                 'parent': replay_oids[0], 'tree': 'f' * 40},
            }
            proof = [{'source': '1' * 40, 'replayed': replay_oids[0]},
                     {'source': '2' * 40, 'replayed': replay_oids[1]}]
            with (mock.patch.object(journal.replay_evidence, 'source_commits',
                                    return_value=(OLD, ['1' * 40, '2' * 40])),
                  mock.patch.object(journal.replay_evidence, 'replay_commits',
                                    return_value=replay_oids),
                  mock.patch.object(journal.replay_evidence, 'commit_fact',
                                    side_effect=lambda _git, _repo, oid: facts.get(oid, {})),
                  mock.patch.object(journal.replay_evidence, 'verify_replay',
                                    return_value=proof) as verify):
                self.assertEqual(journal.legacy_rebase_replay_proof(
                    mock.Mock(), state, final, {'head': replay_oids[1]},
                ), proof)
                self.assertEqual(verify.call_args.kwargs['corrections']['1' * 40]['tree'],
                                 'c' * 40)
                other_session = {**initial, 'expected': {
                    **expected, 'agent_session_id': 'worker-2',
                }}
                (slot / 'initial.intent.json').write_text(
                    journal.json.dumps(other_session), encoding='utf-8',
                )
                with self.assertRaises(journal.MutationEvidenceError):
                    journal.legacy_rebase_replay_proof(
                        mock.Mock(), state, final, {'head': replay_oids[1]},
                    )

    def test_sealed_rebase_completion_rechecks_identity_generation_and_live_result(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            before = {'head': OLD}
            observed = {'head': NEW, 'metadata': None}
            expected = {
                'branch': 'loop-manager/G-100', 'before_head': OLD,
                'target_base_oid': NEW, 'remote_oid': OLD,
                'agent_session_id': 'worker-1', 'run_id': 'run-1',
                'checkpoint_generation': 3,
            }
            identity = journal.begin_rebase(
                state, owner='plan:G-100', repo='/repo', target='/owned',
                before=before, expected=expected,
            )
            validate = lambda _intent, _observed: {'selected': {'head': NEW}}
            proof = lambda _intent, _observed: [{'source': OLD, 'replayed': NEW}]
            self.assertTrue(journal.complete_rebase(
                state, repo='/repo', target='/owned', observe=lambda: observed,
                validate=validate, replay_proof=proof,
            ))
            arguments = dict(
                state_dir=state, repo='/repo', target='/owned',
                owner='plan:G-100', branch='loop-manager/G-100',
                before_head=OLD, target_base_oid=NEW, remote_oid=OLD,
                agent_session_id='worker-1', run_id='run-1',
                checkpoint_generation=4, observe=lambda: observed,
                validate=validate, replay_proof=proof,
            )
            self.assertEqual(journal.verified_rebase_completion(**arguments)['intent']['id'], identity)
            for changed in (
                {'agent_session_id': 'worker-2'},
                {'run_id': 'run-2'},
                {'checkpoint_generation': 2},
                {'observe': lambda: {'head': OLD, 'metadata': None}},
                {'replay_proof': lambda *_: []},
            ):
                with self.subTest(changed=list(changed)), self.assertRaises(journal.MutationEvidenceError):
                    journal.verified_rebase_completion(**{**arguments, **changed})

    def test_sealed_autostash_result_requires_verified_later_correction(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            sealed = {'head': NEW, 'metadata': None, 'unmerged': True}
            live = {'head': NEW, 'metadata': None, 'unmerged': False}
            identity = journal.begin_rebase(
                state, owner='plan:G-100', repo='/repo', target='/owned',
                before={'head': OLD},
                expected={'branch': 'loop-manager/G-100',
                          'before_head': OLD, 'target_base_oid': NEW,
                          'remote_oid': OLD, 'agent_session_id': 'worker-1',
                          'run_id': 'run-1', 'checkpoint_generation': 1},
            )
            validate = lambda _intent, observed: {'unmerged': observed['unmerged']}
            proof = lambda _intent, observed: [{'head': observed['head']}]
            self.assertTrue(journal.complete_rebase(
                state, repo='/repo', target='/owned', observe=lambda: sealed,
                validate=validate, replay_proof=proof,
            ))
            arguments = dict(
                state_dir=state, repo='/repo', target='/owned',
                owner='plan:G-100', branch='loop-manager/G-100',
                before_head=OLD, target_base_oid=NEW, remote_oid=OLD,
                agent_session_id='worker-1', run_id='run-1',
                checkpoint_generation=2, observe=lambda: live,
                validate=validate, replay_proof=proof,
            )
            with self.assertRaises(journal.MutationEvidenceError):
                journal.verified_rebase_completion(**arguments)
            accepted = lambda _intent, old, now: old['unmerged'] and not now['unmerged']
            self.assertEqual(journal.verified_rebase_completion(
                **arguments, later_correction=accepted,
            )['intent']['id'], identity)
            with self.assertRaises(journal.MutationEvidenceError):
                journal.verified_rebase_completion(
                    **arguments, later_correction=lambda *_: False,
                )

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
            "operations": [], "index": {},
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

    def test_source_rejects_legacy_evidence_without_index(self):
        before = {"head": OLD, "branch": "main", "changes": [], "operations": []}
        intent = {
            "kind": "source_fast_forward", "target": "/source",
            "before": before, "expected": {**before, "head": NEW},
        }
        with self.assertRaises(journal.MutationEvidenceError):
            journal.verified_transition(intent, intent["expected"], True)

    def test_runtime_removal_requires_owned_registration_and_receipt(self):
        intent = {
            "kind": "runtime_remove", "target": "/runtime",
            "before": {"exists": True, "registration": {
                "head": OLD, "detached": True, "locked": "loop-manager-runtime:" + "a" * 32,
            }},
            "expected": {"exists": False, "registration": None},
        }
        self.assertEqual(journal.verified_transition(intent, intent["expected"], True),
                         {"refs": {}, "worktrees": {"/runtime": None}})
        with self.assertRaises(journal.MutationEvidenceError):
            journal.verified_transition(intent, intent["expected"], False)
        intent["before"]["registration"]["locked"] = "external"
        with self.assertRaises(journal.MutationEvidenceError):
            journal.verified_transition(intent, intent["expected"], True)

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
