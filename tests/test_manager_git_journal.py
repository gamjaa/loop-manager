import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from loop_manager_internal.git import manager_git_journal as journal


OLD = "a" * 40
NEW = "b" * 40
REF = "refs/remotes/origin/main"


class ManagerGitTransitionTests(unittest.TestCase):
    def test_sealed_equal_receipt_survives_interruption_before_active_marker_removal(self):
        for recover_only in (False, True):
            with self.subTest(recover_only=recover_only), tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                observed = {'oid': OLD}
                apply = mock.Mock()
                arguments = dict(repo='/repo', kind='project_fetch', target=REF,
                                 observe=lambda: observed.copy(), marker=lambda _: False)
                self.assertEqual(journal.execute(state, owner='project', before=observed,
                    expected=observed, apply=apply, **arguments), 'equal')
                slot = journal._slot(state, '/repo', 'project_fetch', REF)
                intent = next(slot.glob('*.intent.json'))
                result = next(slot.glob('*.result.json'))
                sealed = result.read_bytes()
                (slot / 'active.json').write_bytes(intent.read_bytes())

                if recover_only:
                    self.assertFalse(journal.recover_active(state, **arguments))
                else:
                    self.assertEqual(journal.execute(state, owner='project', before=observed,
                        expected=observed, apply=apply, **arguments), 'recovered')

                self.assertFalse((slot / 'active.json').exists())
                self.assertEqual(result.read_bytes(), sealed)
                apply.assert_not_called()

    def test_equal_recovery_keeps_conflicting_sealed_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            observed = {'oid': OLD}
            arguments = dict(repo='/repo', kind='project_fetch', target=REF,
                             observe=lambda: observed.copy(), marker=lambda _: False)
            apply = mock.Mock()
            journal.execute(state, owner='project', before=observed, expected=observed,
                            apply=apply, **arguments)
            slot = journal._slot(state, '/repo', 'project_fetch', REF)
            (slot / 'active.json').write_bytes(next(slot.glob('*.intent.json')).read_bytes())
            result = next(slot.glob('*.result.json'))
            damaged = journal._read(result)
            damaged['observed'] = {'oid': NEW}
            result.unlink()
            journal._write_new(result, damaged)
            sealed = result.read_bytes()

            with self.assertRaises(journal.MutationEvidenceError):
                journal.recover_active(state, **arguments)

            self.assertTrue((slot / 'active.json').exists())
            self.assertEqual(result.read_bytes(), sealed)
            apply.assert_not_called()

    def test_sealed_rebase_without_branch_is_refused_as_mutation_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            token = journal.begin_rebase(
                state, owner='plan:G-099', repo='/repo', target='/worktree',
                before={'head': OLD}, expected={'run_id': 'saved-run'},
            )
            journal.complete_rebase(
                state, repo='/repo', target='/worktree',
                observe=lambda: {'head': NEW}, validate=lambda *_: {},
            )
            slot = journal._slot(state, '/repo', 'managed_rebase', '/worktree')
            intent_path = slot / f'{token}.intent.json'
            result_path = slot / f'{token}.result.json'
            original_intent, original_result = intent_path.read_bytes(), result_path.read_bytes()
            with self.assertRaises(journal.MutationEvidenceError):
                journal.verified_managed_transitions(state, '/repo', 'plan:G-099')
            self.assertEqual(intent_path.read_bytes(), original_intent)
            self.assertEqual(result_path.read_bytes(), original_result)

    def test_damaged_nested_intent_refuses_replay_without_git_write(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            observed = {'oid': OLD}
            slot = journal._slot(state, '/repo', 'project_fetch', REF)
            slot.mkdir(parents=True)
            damaged = {'version': 1, 'id': 'a' * 32, 'owner': 'project',
                       'kind': 'project_fetch', 'repo': '/repo', 'target': REF,
                       'before': {'oid': OLD}, 'expected': {'oid': {'forged': NEW}}}
            journal._write_new(slot / 'active.json', damaged)
            apply = mock.Mock()
            with self.assertRaises(journal.MutationEvidenceError):
                journal.execute(state, owner='project', kind='project_fetch', repo='/repo',
                                target=REF, before=observed, expected={'oid': NEW},
                                observe=lambda: observed.copy(), apply=apply,
                                marker=lambda _: True)
            apply.assert_not_called()
            self.assertEqual(observed, {'oid': OLD})
            self.assertTrue((slot / 'active.json').exists())

    def test_damaged_nested_result_never_offers_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            slot = journal._slot(state, '/repo', 'project_fetch', REF)
            slot.mkdir(parents=True)
            journal._write_new(slot / ('a' * 32 + '.result.json'), {
                'id': 'a' * 32, 'status': 'complete',
                'observed': {'oid': NEW}, 'candidates': {'refs': []},
            })
            with self.assertRaises(journal.MutationEvidenceError):
                journal.verified_candidates(
                    state, '/repo', baseline_refs={REF: OLD},
                    baseline_worktrees={}, live_refs={REF: NEW}, live_worktrees={},
                    marker=lambda _: True)

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


class CurrentRebaseAuditTests(unittest.TestCase):
    def test_unused_journal_does_not_observe_and_active_failure_preserves_intent(self):
        from tests.support.rebase_observations import decision_for, observation
        from loop_manager_internal.git.managed_rebase_validation import journal_observation
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            observe = mock.Mock(return_value=journal_observation(observation()))
            arguments = dict(repo='/repo', target='/owned', owner='plan:G-108',
                             decision=decision_for(), observed=observe)
            self.assertFalse(journal.record_current_rebase_result(state, **arguments))
            observe.assert_not_called()
            journal.begin_rebase(state, owner='plan:G-108', repo='/repo', target='/owned',
                before={'head': 'before'}, expected={'branch': 'loop-manager/G-108'})
            self.assertFalse(journal.record_current_rebase_result(state, **{**arguments, 'owner': 'other'}))
            observe.assert_not_called()
            observe.side_effect = RuntimeError('observation failed')
            with self.assertRaisesRegex(RuntimeError, 'observation failed'):
                journal.record_current_rebase_result(state, **arguments)
            self.assertTrue((journal._slot(state, '/repo', 'managed_rebase', '/owned') / 'active.json').exists())
            observe.side_effect = None
            self.assertTrue(journal.record_current_rebase_result(state, **arguments))
            self.assertEqual(observe.call_count, 2)
            self.assertFalse(journal.record_current_rebase_result(state, **arguments))
            self.assertEqual(observe.call_count, 2)

    def test_current_observation_preserves_old_result_without_fabricating_receipts(self):
        from tests.support.rebase_observations import decision_for, observation
        from loop_manager_internal.git.managed_rebase_validation import journal_observation
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            token = journal.begin_rebase(state, owner='plan:G-108', repo='/repo',
                target='/owned', before={'head': 'before'}, expected={'branch': 'loop-manager/G-108'})
            slot = journal._slot(state, '/repo', 'managed_rebase', '/owned')
            old_intent = (slot / f'{token}.intent.json').read_bytes()
            observed = journal_observation(observation())
            self.assertTrue(journal.record_current_rebase_result(state, repo='/repo',
                target='/owned', owner='plan:G-108', decision=decision_for(), observed=observed))
            self.assertEqual((slot / f'{token}.intent.json').read_bytes(), old_intent)
            result = journal._read(slot / f'{token}.result.json')
            self.assertEqual(result['status'], 'current_state')
            self.assertEqual(journal.verified_managed_transitions(state, '/repo', 'plan:G-108'), [])
            self.assertFalse(journal.record_current_rebase_result(state, repo='/repo',
                target='/owned', owner='plan:G-108', decision=decision_for(), observed=observed))
            self.assertFalse(list(slot.glob('*.invocation.json')))

    def test_continuation_retains_incomplete_old_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            token = journal.begin_rebase(state, owner='plan:G-108', repo='/repo', target='/owned',
                before={'head': 'before'}, expected={'branch': 'loop-manager/G-108'})
            slot = journal._slot(state, '/repo', 'managed_rebase', '/owned')
            old = (slot / 'active.json').read_bytes()
            replacement = journal.begin_current_rebase(state, owner='plan:G-108', repo='/repo',
                target='/owned', before={'head': 'partial'}, expected={'action': 'continue'})
            self.assertNotEqual(token, replacement)
            self.assertEqual(next(slot.glob('*.superseded-active.json')).read_bytes(), old)
            self.assertTrue((slot / f'{token}.intent.json').exists())


if __name__ == '__main__':
    unittest.main()


class PendingProjectMutationTest(unittest.TestCase):
    def test_rebase_workspace_separation_checks_refs_registration_and_observation_races(self):
        from types import SimpleNamespace
        scenarios = ('independent', 'continued', 'continued-wrong-branch', 'continued-wrong-head',
                     'continued-wrong-base', 'continued-missing-head', 'continued-missing-base',
                     'detached-without-metadata', 'detached-without-action',
                     'same-worktree', 'same-local-ref', 'same-published-ref',
                     'duplicate-branch', 'missing-registration', 'registration-failed',
                     'changed-active', 'changed-immutable', 'replaced-during-observation')
        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                repo, target, requested = state / 'repo', state / 'other', state / 'planning'
                branch = 'loop-manager/G-165'
                before = {'head': OLD, 'branch': branch}
                expected = {'branch': branch}
                if scenario.startswith(('continued', 'detached')):
                    before.update(branch=None, metadata={
                        'head-name': f'refs/heads/{branch}', 'orig-head': OLD, 'onto': NEW})
                    expected.update(action='continue', before_head=OLD, target_base_oid=NEW)
                    if scenario == 'continued-wrong-branch':
                        before['metadata']['head-name'] = 'refs/heads/loop-manager/G-999'
                    if scenario == 'continued-wrong-head':
                        before['metadata']['orig-head'] = NEW
                    if scenario == 'continued-wrong-base':
                        before['metadata']['onto'] = OLD
                    if scenario == 'continued-missing-head':
                        del expected['before_head']
                    if scenario == 'continued-missing-base':
                        del expected['target_base_oid']
                    if scenario == 'detached-without-metadata':
                        del before['metadata']
                    if scenario == 'detached-without-action':
                        del expected['action']
                identity = journal.begin_rebase(state, owner='plan:G-165', repo=str(repo),
                    target=str(target), before=before, expected=expected)
                slot = journal._slot(state, str(repo), 'managed_rebase', str(target))
                intent = journal._read(slot / 'active.json')
                if scenario == 'changed-active':
                    (slot / 'active.json').write_text(json.dumps(dict(intent, id='d' * 32)))
                if scenario == 'changed-immutable':
                    saved = dict(intent, owner='plan:G-999')
                    (slot / f'{identity}.intent.json').write_text(json.dumps(saved))
                preserved = {path.name: path.read_bytes() for path in slot.iterdir()}

                def git(repository, *arguments):
                    self.assertEqual(repository, repo)
                    self.assertEqual(arguments, ('worktree', 'list', '--porcelain', '-z'))
                    if scenario == 'replaced-during-observation':
                        replacement = dict(intent, id='c' * 32)
                        (slot / 'active.json').write_text(json.dumps(replacement))
                        preserved['active.json'] = (slot / 'active.json').read_bytes()
                    output = f'worktree {target}\0HEAD {OLD}\0detached\0\0'
                    if scenario == 'duplicate-branch':
                        output += f'worktree {requested}\0HEAD {OLD}\0branch refs/heads/{branch}\0\0'
                    return SimpleNamespace(returncode=int(scenario == 'registration-failed'),
                        stdout='' if scenario == 'missing-registration' else output)

                refs = ((f'refs/heads/{branch}',) if scenario == 'same-local-ref' else
                        (f'refs/remotes/origin/{branch}',) if scenario == 'same-published-ref' else ())
                result = journal.rebase_uses_separate_workspace(state, str(repo), intent,
                    worktrees=(target if scenario == 'same-worktree' else requested,),
                    refs=refs, git=git)
                self.assertEqual(result, scenario in ('independent', 'continued'))
                self.assertEqual({path.name: path.read_bytes() for path in slot.iterdir()}, preserved)

    def test_stage_scope_preserves_intents_and_rejects_shared_writer_and_ownership_races(self):
        from contextlib import nullcontext
        from types import SimpleNamespace
        from tests.support.database import runtime_connection
        from loop_manager_internal.runtime.resource_lock import resource_locks
        scenarios = ('independent', 'publication-ref', 'configuration-race', 'common-repository',
                     'writer-unknown', 'writer-stopped', 'lease-busy', 'owner-race', 'intent-race',
                     'registration-failed', 'shared-configuration-operation', 'checkpoint-race', 'symbolic-branch', 'missing-branch-ref')
        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                repo, target, requested = str(state / 'repo'), state / 'owned', state / 'planning'
                connection = runtime_connection(repo)
                self.addCleanup(connection.close)
                connection.execute("UPDATE exec_plans SET branch='loop-manager/G-047',worktree=?", (str(target),))
                if scenario.startswith('writer-'):
                    connection.execute("""INSERT INTO agent_invocations
                        (invocation_id,repo,plan_id,role,provider,model,effort,transition,attempt,started_at,
                         finished_at,execution_started_at,execution_finish_evidence,execution_status)
                        VALUES ('writer',?,'G-047','worker','codex','model','medium','new',1,'start',
                                'finish','start',?,'executed')""",
                        (repo, 'provider_process_finished' if scenario == 'writer-stopped' else None))
                connection.commit()
                identity = journal.begin_rebase(state, repo=repo, owner='plan:G-047', target=str(target),
                    before={'head': OLD, 'branch': 'loop-manager/G-047'}, expected={'branch': 'loop-manager/G-047'})
                slot = journal._slot(state, repo, 'managed_rebase', str(target))
                if scenario == 'shared-configuration-operation':
                    intent = journal._read(slot / 'active.json')
                    intent['kind'] = 'shared_configuration'
                    other = journal._slot(state, repo, intent['kind'], str(target))
                    other.mkdir()
                    (slot / 'active.json').unlink()
                    journal._write_new(other / 'active.json', intent)
                    journal._write_new(other / f'{identity}.intent.json', intent)
                    slot = other
                preserved = {path.name: path.read_bytes() for path in slot.iterdir()}
                config_reads = 0
                def git(context, *args, **kwargs):
                    nonlocal config_reads
                    self.assertFalse(connection.in_transaction)
                    if args[0] == 'symbolic-ref':
                        return SimpleNamespace(stdout='refs/heads/main', returncode=0 if scenario == 'symbolic-branch' else 1)
                    if args[0] == 'show-ref':
                        return SimpleNamespace(stdout=OLD, returncode=int(scenario == 'missing-branch-ref'))
                    if args[0] == 'config':
                        config_reads += 1
                        value = 'changed' if scenario == 'configuration-race' and config_reads > 1 else 'core.bare=false'
                    elif args[0] == 'rev-parse':
                        value = '/other-repository' if scenario == 'common-repository' and context == target else '/shared'
                    else:
                        self.assertEqual(args, ('worktree', 'list', '--porcelain', '-z'))
                        if scenario == 'owner-race':
                            connection.execute("UPDATE exec_plans SET branch='changed'")
                            connection.commit()
                        if scenario == 'checkpoint-race':
                            connection.execute('INSERT INTO execution_checkpoints(repo,plan_id,generation) VALUES (?, ?, 1)',
                                               (repo, 'G-047'))
                            connection.commit()
                        if scenario == 'intent-race':
                            replacement = journal._read(slot / 'active.json')
                            replacement['id'] = 'd' * 32
                            (slot / 'active.json').write_text(json.dumps(replacement))
                            preserved['active.json'] = (slot / 'active.json').read_bytes()
                        value = f'worktree {target}\0HEAD {OLD}\0detached\0\0'
                    return SimpleNamespace(stdout=value, returncode=int(scenario == 'registration-failed' and args[0] == 'worktree'))
                lease = (resource_locks(state, repo, ['plan:G-047'], owner='another-worker')
                         if scenario == 'lease-busy' else nullcontext())
                with lease, mock.patch('loop_manager_internal.runtime.operation_events.emit') as event:
                    blocked = journal.has_conflicting_project_mutation(state, repo, stage='planning_release',
                        connection=connection, git=git, worktrees=(requested,),
                        refs=('refs/heads/loop-manager/G-047',) if scenario == 'publication-ref' else ())
                self.assertEqual(blocked, scenario not in ('independent', 'writer-stopped'))
                self.assertEqual(event.call_args.kwargs['decision'], 'hold' if blocked else 'allow')
                for key in ('owner', 'operation', 'intent_id', 'stage', 'resource', 'next_condition'):
                    self.assertTrue(event.call_args.kwargs[key])
                self.assertEqual({path.name: path.read_bytes() for path in slot.iterdir()}, preserved)

    def test_optional_source_intents_preserve_evidence_without_gating_remote_planning(self):
        for kind in ('source_fast_forward', 'source_rebase'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                before = {'head': OLD, 'branch': 'main', 'ref': OLD, 'remote': NEW,
                          'origin': 'origin', 'head_reflog': 'original', 'branch_reflog': 'original',
                          'index': {}, 'changes': [], 'operations': [], 'files': {}}
                expected = (dict(before, head=NEW) if kind == 'source_fast_forward'
                            else {'base': 'main', 'remote': NEW, 'affected': []})
                slot = journal._slot(state, '/repo', kind, '/repo')
                slot.mkdir(parents=True)
                intent = {'version': 1, 'id': 'a' * 32, 'owner': 'project', 'kind': kind, 'repo': '/repo',
                          'target': '/repo', 'before': before, 'expected': expected}
                journal._write_new(slot / ('a' * 32 + '.intent.json'), intent)
                journal._write_new(slot / 'active.json', intent)
                saved = (slot / 'active.json').read_bytes()
                self.assertFalse(journal.has_pending_project_mutation(state, '/repo'))
                self.assertEqual((slot / 'active.json').read_bytes(), saved)
                (slot / 'active.json').write_text('{interrupted source intent')
                self.assertFalse(journal.has_pending_project_mutation(state, '/repo'))
                self.assertFalse(journal.has_pending_project_mutation(state, '/other'))

    def test_unsealed_mutation_blocks_only_owning_project_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            self.assertFalse(journal.has_pending_project_mutation(state, '/repo'))
            journal.begin_rebase(
                state, owner='plan:G-165', repo='/repo', target='/planning',
                before={'head': OLD}, expected={'run_id': 'saved-run', 'branch': 'planning'},
            )
            slot = journal._slot(state, '/repo', 'managed_rebase', '/planning')
            original = (slot / 'active.json').read_bytes()
            self.assertTrue(journal.has_pending_project_mutation(state, '/repo'))
            self.assertFalse(journal.has_pending_project_mutation(state, '/other'))
            self.assertEqual((slot / 'active.json').read_bytes(), original)
            journal.complete_rebase(
                state, repo='/repo', target='/planning',
                observe=lambda: {'head': NEW}, validate=lambda *_: {},
            )
            self.assertFalse(journal.has_pending_project_mutation(state, '/repo'))

    def test_damaged_active_intent_is_not_a_release_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            slot = state / 'manager-git-journal' / 'damaged'
            slot.mkdir(parents=True)
            (slot / 'active.json').write_text('{}')
            with self.assertRaises(journal.MutationEvidenceError):
                journal.has_pending_project_mutation(state, '/repo')
