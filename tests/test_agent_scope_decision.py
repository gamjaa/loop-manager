import unittest
from dataclasses import replace
from pathlib import Path

from loop_manager_internal.agents import agent_scope_decision as decision
from loop_manager import GitSnapshot, ManagerGitCheckpoint, AgentScopeError, validate_agent_scope
from tests.support.scope import snapshot


WORKTREE = "/managed"
REPO = "/source"
PLAN = "docs/plans/[G-051] Plan.md"


def decide(before, current, *, role="worker", phase="IMPLEMENTING", result=None,
           checkpoint=None, planner_correction=False, cleanup=frozenset(),
           manager_runtime=None, runtime_recreation_proven=False, manager_refs=frozenset(),
           base_branch="main"):
    return decision.decide_persisted_scope(
        role=role, phase=phase, base_branch=base_branch, selected_plan_path=PLAN,
        before=before, current=current, result=result, checkpoint=checkpoint,
        worktree_key=WORKTREE, repo_key=REPO,
        planner_correction=planner_correction, manager_cleanup_worktrees=cleanup,
        manager_runtime=manager_runtime, runtime_recreation_proven=runtime_recreation_proven,
        manager_refs=manager_refs,
    )


class PersistedScopeDecisionTests(unittest.TestCase):
    def test_invocation_scope_digest_tracks_selected_inputs_and_ignores_unrelated_checkout_state(self):
        ref, head = 'refs/remotes/origin/main', 'refs/remotes/origin/HEAD'
        own = 'refs/heads/loop-manager/G-051'
        before = snapshot(
            refs={ref: ('a', ''), head: ('a', ref), own: ('a', ''),
                  'refs/heads/main': ('local', ''), 'refs/heads/unrelated': ('other', '')},
            worktrees={WORKTREE: ('HEAD=a', 'branch=' + own),
                       REPO: ('HEAD=local', 'state=clean'),
                       '/outside': ('HEAD=outside',)},
            config={'local:core.hooksPath': ('secure',),
                    'local:branch.unrelated.remote': ('old',)},
        )
        digest = decision.invocation_scope_digest(
            before, role='worker', phase='IMPLEMENTING', base_branch='main',
            branch='loop-manager/G-051', worktree_key=WORKTREE, default_worktree=REPO,
        )
        self.assertNotEqual(decision.invocation_scope_digest(
            before, role='worker', phase='IMPLEMENTING', base_branch='main',
            branch='loop-manager/other', worktree_key=WORKTREE, default_worktree=REPO,
        ), digest)
        unrelated = replace(before,
            refs={**before.refs, 'refs/heads/main': ('new-local', ''),
                  'refs/heads/unrelated': ('new-other', '')},
            worktrees={**before.worktrees, REPO: ('HEAD=changed', 'state=dirty'),
                       '/outside': ('HEAD=other',)},
            config={'local:core.hooksPath': ('secure',),
                    'local:branch.unrelated.remote': ('new',)},
        )
        self.assertEqual(decision.invocation_scope_digest(
            unrelated, role='worker', phase='IMPLEMENTING', base_branch='main',
            branch='loop-manager/G-051', worktree_key=WORKTREE, default_worktree=REPO,
        ), digest)
        for changed in (
            replace(before, refs={**before.refs, ref: ('b', '')}),
            replace(before, dirty={'src/app.py': (('unstaged',), 'changed')}),
            replace(before, config={'local:core.hooksPath': ('unsafe',)}),
        ):
            with self.subTest(changed=changed):
                self.assertNotEqual(decision.invocation_scope_digest(
                    changed, role='worker', phase='IMPLEMENTING', base_branch='main',
                    branch='loop-manager/G-051', worktree_key=WORKTREE, default_worktree=REPO,
                ), digest)

    def test_blocked_handoff_accepts_current_local_input_without_rewriting_history(self):
        branch = 'loop-manager/G-051'
        ref = f'refs/heads/{branch}'
        before = snapshot(
            head='old', branch=branch, refs={ref: ('old', '')},
            worktrees={WORKTREE: ('HEAD=old', f'branch={ref}', 'locked=owner')},
        )
        result = replace(before, dirty={'src/agent.py': (('unstaged',), 'agent result')})
        current = replace(
            result, head='user-commit',
            index={'src/user.py': ('H 100644 new-blob 0',)},
            dirty={'src/user.py': (('staged',), 'user input')},
            refs={ref: ('user-commit', '')},
            worktrees={WORKTREE: ('HEAD=user-commit', f'branch={ref}', 'locked=owner')},
        )
        original = before.serialize()
        accepted = decision.blocked_agent_local_baseline(
            before, result, current, WORKTREE, accept_current_input=True,
        )
        self.assertEqual((accepted.head, accepted.index, accepted.dirty),
                         (current.head, current.index, current.dirty))
        self.assertEqual(accepted.refs[ref], current.refs[ref])
        self.assertEqual(accepted.worktrees[WORKTREE], current.worktrees[WORKTREE])
        self.assertEqual(before.serialize(), original)
        for invalid in (
            replace(current, branch='other'),
            replace(current, refs={ref: ('wrong', '')}),
            replace(current, operations={'rebase-merge': 'operation'}),
            replace(current, index={'src/user.py': ('S 100644 new-blob 0',)}),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(decision.ScopeDecisionError):
                decision.blocked_agent_local_baseline(
                    before, result, invalid, WORKTREE, accept_current_input=True,
                )

    def test_selected_runtime_and_proven_current_base_are_distinct_operands(self):
        ref, head, runtime = 'refs/remotes/origin/develop', 'refs/remotes/origin/HEAD', '/state/runtime/b'
        before = snapshot(refs={ref: ('a', ''), head: ('a', ref)})
        current = replace(before, refs={ref: ('c', ''), head: ('c', ref)},
                          worktrees={**before.worktrees, runtime: ('HEAD=b', 'detached=True')})
        kwargs = dict(role='worker', phase='WORKING', base_branch='develop', selected_plan_path=PLAN,
            before=before, current=current, result=before, checkpoint=ManagerGitCheckpoint(before, frozenset()),
            worktree_key=WORKTREE, repo_key=REPO, planner_correction=False,
            manager_cleanup_worktrees=frozenset(), manager_runtime=(runtime, 'b'))
        with self.assertRaises(decision.ScopeDecisionError):
            decision.decide_persisted_scope(**kwargs)
        accepted = decision.decide_persisted_scope(**kwargs, runtime_base_oid='c')
        self.assertEqual(accepted.reconciled_before.worktrees[runtime], ('HEAD=b', 'detached=True'))
        self.assertEqual(accepted.reconciled_before.refs[ref], ('c', ''))
        with self.assertRaises(decision.ScopeDecisionError):
            decision.decide_persisted_scope(**{**kwargs, 'current': replace(current,
                worktrees={**current.worktrees, runtime: ('HEAD=c', 'detached=True')})}, runtime_base_oid='c')

    def test_verified_runtime_reconciles_only_selected_registration_and_origin(self):
        before = snapshot()
        runtime = "/state/runtimes/source/b"
        registration = ("HEAD=b", "detached=True", "locked=loop-manager-runtime:id")
        current = replace(
            before,
            refs={**before.refs, "refs/remotes/origin/main": ("b", "")},
            worktrees={**before.worktrees, runtime: registration},
        )
        result = decide(before, current, manager_runtime=(runtime, "b"))
        check, = result.checks
        self.assertEqual(check.before.refs, current.refs)
        self.assertEqual(check.before.worktrees, current.worktrees)
        self.assertEqual(result.reconciled_before, check.before)
        self.assertEqual(decide(check.before, current, manager_runtime=(runtime, "b")).reconciled_before, check.before)

        extra = replace(current, worktrees={**current.worktrees, "/external": ("HEAD=c",)})
        check, = decide(before, extra, manager_runtime=(runtime, "b")).checks
        self.assertNotIn("/external", check.before.worktrees)
        with self.assertRaisesRegex(decision.ScopeDecisionError, "runtime 기준선"):
            decide(before, current, manager_runtime=(runtime, "other"))

    def test_registered_base_reconciles_results_and_checkpoint_without_adopting_other_refs(self):
        runtime = "/state/runtime/b"
        for base, main_oid in (("main", None), ("develop", None), ("develop", "unrelated")):
            with self.subTest(base=base, main=main_oid):
                ref = f"refs/remotes/origin/{base}"
                refs = {ref: ("a", ""), "refs/remotes/origin/HEAD": ("a", ref)}
                if main_oid:
                    refs["refs/remotes/origin/main"] = (main_oid, "")
                before = snapshot(refs=refs)
                current = replace(before, refs={**refs, ref: ("b", ""),
                    "refs/remotes/origin/HEAD": ("b", ref)},
                    worktrees={**before.worktrees, runtime: ("HEAD=b", "detached=True")})
                saved = replace(before, dirty={"app.py": (("unstaged",), "work")})
                outcome = decide(before, current, base_branch=base, result=saved,
                    checkpoint=ManagerGitCheckpoint(saved, frozenset()), manager_runtime=(runtime, "b"))
                first, second = outcome.checks
                self.assertEqual(first.before.refs, current.refs)
                self.assertEqual(first.after.refs, current.refs)
                self.assertEqual(second.before.refs, current.refs)
                self.assertEqual(first.after.dirty, saved.dirty)
                self.assertEqual(decision.reconcile_runtime_snapshot(saved, before, current, runtime, base).refs,
                                 current.refs)
                # A changed result/checkpoint base ref or symbolic target is Agent evidence.
                for key in (ref, "refs/remotes/origin/HEAD", "refs/heads/foreign"):
                    violation = replace(saved, refs={**saved.refs, key: ("foreign", "refs/heads/foreign")})
                    reconciled = decision.reconcile_runtime_snapshot(violation, before, current, runtime, base)
                    self.assertEqual(reconciled.refs[key], violation.refs[key])
                    failed = decide(before, current, base_branch=base,
                        checkpoint=ManagerGitCheckpoint(violation, frozenset()), manager_runtime=(runtime, "b"))
                    self.assertEqual(failed.checks[0].after.refs[key], violation.refs[key])
                    with self.assertRaises(AgentScopeError):
                        validate_agent_scope("worker", Path(WORKTREE), failed.checks[0].before,
                                             failed.checks[0].after, selected_plan_path=PLAN)

    def test_registered_base_rejects_wrong_oid_and_symbolic_head_target(self):
        runtime = "/state/runtime/b"
        ref = "refs/remotes/origin/develop"
        before = snapshot(refs={ref: ("a", ""), "refs/remotes/origin/HEAD": ("a", ref)})
        current = replace(before, refs={ref: ("b", ""), "refs/remotes/origin/HEAD": ("b", ref)},
                          worktrees={**before.worktrees, runtime: ("HEAD=b", "detached=True")})
        for changed in ({ref: ("wrong", "")},
                        {"refs/remotes/origin/HEAD": ("b", "refs/remotes/origin/main")}):
            with self.subTest(changed=changed), self.assertRaises(decision.ScopeDecisionError):
                decide(before, replace(current, refs={**current.refs, **changed}),
                       base_branch="develop", manager_runtime=(runtime, "b"))

    def test_runtime_lock_replacement_requires_proof_and_preserves_other_changes(self):
        runtime = "/state/runtime"
        old = ("HEAD=b", "detached=True", "locked=old", "state=same")
        new = ("HEAD=b", "detached=True", "locked=new", "state=same")
        before = snapshot(
            refs={"refs/remotes/origin/main": ("b", "")},
            worktrees={**snapshot().worktrees, runtime: old},
        )
        current = replace(before, worktrees={**before.worktrees, runtime: new,
                                             "/other": ("HEAD=c",)})
        with self.assertRaisesRegex(decision.ScopeDecisionError, "runtime 기준선"):
            decide(before, current, manager_runtime=(runtime, "b"))
        check, = decide(before, current, manager_runtime=(runtime, "b"),
                        runtime_recreation_proven=True).checks
        self.assertEqual(check.before.worktrees[runtime], new)
        self.assertNotIn("/other", check.before.worktrees)
        self.assertEqual(check.after.worktrees["/other"], ("HEAD=c",))
        changed = replace(current, worktrees={**current.worktrees,
                                              runtime: new[:-1] + ("state=changed",)})
        with self.assertRaisesRegex(decision.ScopeDecisionError, "runtime 기준선"):
            decide(before, changed, manager_runtime=(runtime, "b"),
                   runtime_recreation_proven=True)

    def test_blocked_false_positive_reconciles_from_original_start_snapshot(self):
        before = snapshot()
        runtime = "/state/runtimes/source/b"
        current = replace(
            before,
            refs={**before.refs, "refs/remotes/origin/main": ("b", "")},
            worktrees={**before.worktrees, runtime: ("HEAD=b", "detached=True")},
        )
        check, = decide(before, current, phase="PLAN_BLOCKED", result=current,
                        manager_runtime=(runtime, "b")).checks
        self.assertEqual(check.before.worktrees, current.worktrees)
        self.assertEqual(check.before.refs, current.refs)

    def test_runtime_recreation_preserves_checkpoint_and_original_agent_metadata(self):
        runtime = '/state/runtime'
        old = ('HEAD=b', 'detached=True', 'locked=old')
        new = ('HEAD=b', 'detached=True', 'locked=new')
        before = snapshot(
            worktrees={**snapshot().worktrees, runtime: old},
            refs={**snapshot().refs, 'refs/remotes/origin/main': ('b', '')},
            scope_id='original-scope', scope_start_digest='original-digest')
        saved = replace(before, dirty={'app.py': (('unstaged',), 'agent-work')})
        checkpoint = ManagerGitCheckpoint(saved, frozenset({'/cleanup'}))
        current = replace(saved, worktrees={**saved.worktrees, runtime: new})
        original_before = before.serialize()
        original_checkpoint = checkpoint.serialize()
        outcome = decide(before, current, checkpoint=checkpoint,
                         manager_runtime=(runtime, 'b'), runtime_recreation_proven=True)
        first, second = outcome.checks
        expected_before = replace(before, worktrees={**before.worktrees, runtime: new})
        self.assertEqual(outcome.reconciled_before, expected_before)
        self.assertEqual(first.before, expected_before)
        self.assertEqual(first.after, current)
        self.assertEqual(second.before, current)
        self.assertEqual(second.allowed_worktrees, checkpoint.cleanup_worktrees)
        self.assertEqual(before.serialize(), original_before)
        self.assertEqual(checkpoint.serialize(), original_checkpoint)

    def test_runtime_recreation_keeps_agent_result_and_checkpoint_registration_violations(self):
        runtime = '/state/runtime'
        old = ('HEAD=b', 'detached=True', 'locked=old')
        new = ('HEAD=b', 'detached=True', 'locked=new')
        before = snapshot(worktrees={**snapshot().worktrees, runtime: old},
                          refs={**snapshot().refs, 'refs/remotes/origin/main': ('b', '')})
        current = replace(before, worktrees={**before.worktrees, runtime: new})
        for registration in (('HEAD=foreign', 'detached=True', 'locked=old'),
                             ('HEAD=b', 'detached=True', 'locked=agent')):
            with self.subTest(registration=registration):
                saved = replace(before, worktrees={**before.worktrees, runtime: registration},
                                refs={**before.refs, 'refs/heads/other': ('foreign', '')})
                # The same helper carries runtime evidence into stored Agent results.
                reconciled = decision.reconcile_runtime_snapshot(saved, before, current, runtime)
                self.assertEqual(reconciled, saved)
                outcome = decide(before, current, checkpoint=ManagerGitCheckpoint(saved, frozenset()),
                                 manager_runtime=(runtime, 'b'), runtime_recreation_proven=True)
                first, second = outcome.checks
                self.assertEqual(first.after, saved)
                self.assertEqual(second.before, saved)
                with self.assertRaises(AgentScopeError):
                    validate_agent_scope(first.role, Path(WORKTREE), first.before, first.after,
                                         selected_plan_path=PLAN)

    def test_worker_and_reviewer_preserve_unallowed_shared_changes(self):
        before = snapshot()
        current = replace(before, refs={**before.refs, "refs/heads/unowned": ("b", "commit")})
        for role in ("worker", "reviewer"):
            with self.subTest(role=role):
                check, = decide(before, current, role=role).checks
                self.assertEqual(check.role, role)
                self.assertEqual(check.before, before)
                self.assertEqual(check.after, current)
                self.assertEqual(check.allowed_refs, frozenset())
                self.assertEqual(check.allowed_worktrees, frozenset())

    def test_planner_correction_allows_only_diagnosed_later_metadata(self):
        before = snapshot()
        result = replace(before, head="b", refs={**before.refs, "refs/heads/main": ("b", "commit")})
        current = replace(result, refs={**result.refs, "refs/heads/main": ("c", "commit"),
                                        "refs/heads/unowned": ("z", "commit")})
        check, = decide(before, current, role="planner", result=result,
                        planner_correction=True).checks
        self.assertEqual(check.before.head, "b")
        self.assertEqual(check.before.refs, before.refs)
        self.assertEqual(check.allowed_refs, frozenset())
        self.assertEqual(check.allowed_worktrees, frozenset())

    def test_blocked_result_allows_manager_changes_and_exact_diagnosed_commit(self):
        before = snapshot()
        result = snapshot(
            head="b",
            refs={"refs/heads/loop-manager/G-051": ("b", "commit")},
            worktrees={WORKTREE: ("HEAD=b", "branch=refs/heads/loop-manager/G-051")},
        )
        current = replace(result, refs={**result.refs, "refs/remotes/origin/main": ("c", "commit")},
                          worktrees={**result.worktrees, REPO: ("HEAD=c",)})
        check, = decide(before, current, phase="PLAN_BLOCKED", result=result,
                        manager_refs=frozenset({'refs/remotes/origin/main'})).checks
        self.assertEqual(check.before.head, "b")
        self.assertEqual(check.allowed_refs, frozenset((
            "refs/heads/loop-manager/G-051", "refs/remotes/origin/main",
        )))
        self.assertEqual(check.allowed_worktrees, frozenset((WORKTREE,)))

    def test_manager_checkpoint_requires_agent_then_read_only_manager_check(self):
        before = snapshot()
        checkpoint = ManagerGitCheckpoint(before, frozenset(("/cleanup",)))
        current = replace(before, refs={**before.refs, "refs/tags/release": ("b", "tag")})
        first, second = decide(before, current, checkpoint=checkpoint,
                               cleanup=frozenset(("/other-cleanup",)),
                               manager_refs=frozenset({'refs/tags/release'})).checks
        self.assertEqual((first.role, first.before, first.after), ("worker", before, before))
        self.assertEqual(second.role, "reviewer")
        self.assertEqual(second.allowed_refs, frozenset(("refs/tags/release",)))
        self.assertEqual(second.allowed_worktrees, frozenset(("/cleanup", "/other-cleanup")))
        self.assertTrue(second.checkpoint_result)

    def test_blocked_checkpoint_only_allows_selected_plan_edit_and_primary_cleanup(self):
        before = snapshot()
        current = replace(before, dirty={PLAN: (("unstaged",), "changed")})
        checkpoint = ManagerGitCheckpoint(before, frozenset())
        check, = decide(before, current, phase="PLAN_BLOCKED", checkpoint=checkpoint).checks
        self.assertEqual(check.before.dirty, current.dirty)
        self.assertEqual(check.allowed_worktrees, frozenset())

    def test_unknown_role_and_incomplete_result_get_no_allowance(self):
        before = snapshot()
        with self.assertRaisesRegex(decision.ScopeDecisionError, "역할이 유효하지"):
            decide(before, before, role="unknown")
        check, = decide(before, before, phase="PLAN_BLOCKED",
                        result=replace(before, shared_metadata_complete=False)).checks
        self.assertEqual(check.before, before)
        self.assertEqual(check.allowed_refs, frozenset())
        with self.assertRaisesRegex(decision.ScopeDecisionError, "관리 worktree 등록"):
            decide(before, replace(before, worktrees={}), phase="PLAN_BLOCKED", result=before)

    def test_new_blocked_local_change_is_rejected_without_expanding_allowance(self):
        before = snapshot()
        current = replace(before, dirty={"src/unrelated.py": (("unstaged",), "new")})
        with self.assertRaisesRegex(decision.ScopeDecisionError, "src/unrelated.py"):
            decide(before, current, phase="PLAN_BLOCKED", result=before)

    def test_observed_fetch_drift_without_operation_proof_gets_no_allowance(self):
        before = snapshot()
        current = replace(before, refs={**before.refs, 'refs/remotes/origin/main': ('other', '')})
        for checkpoint in (None, ManagerGitCheckpoint(before, frozenset())):
            check = decide(before, current, phase='PLAN_BLOCKED', result=before,
                           checkpoint=checkpoint).checks[-1]
            self.assertFalse(check.allowed_refs)
            with self.assertRaises(AgentScopeError):
                validate_agent_scope(check.role, Path(WORKTREE), check.before, check.after,
                                     selected_plan_path=PLAN if check.role == 'worker' else None)

    def test_proven_metadata_advances_only_matching_used_resources_and_keeps_agent_work(self):
        before = snapshot(dirty={'app.py': (('unstaged',), 'work')})
        current = replace(before, refs={**before.refs, 'refs/remotes/origin/main': ('base', ''),
                                       'refs/heads/other': ('foreign', '')},
                          worktrees={**before.worktrees, '/cleanup': ('HEAD=other',)})
        effective = decision.reconcile_manager_metadata(before, current,
            refs=frozenset({'refs/remotes/origin/main'}), worktrees=frozenset())
        self.assertEqual(effective.dirty, before.dirty)
        self.assertNotIn('refs/heads/other', effective.refs)
        self.assertNotIn('/cleanup', effective.worktrees)
        self.assertNotIn('refs/remotes/origin/main', before.refs)
        resources = decision.used_git_resources(role='worker', phase='WORKING',
            base_branch='main', branch=before.branch, worktree_key=WORKTREE)
        isolated = decision.reconcile_manager_metadata(before, current,
            refs=frozenset(current.refs), worktrees=frozenset(current.worktrees), resources=resources)
        self.assertNotIn('refs/heads/other', isolated.refs)
        self.assertNotIn('/cleanup', isolated.worktrees)

    def test_proven_fetch_updates_derived_symbolic_oid_but_never_symbolic_target(self):
        ref, alias = 'refs/remotes/origin/main', 'refs/remotes/origin/HEAD'
        before = snapshot(refs={ref: ('a', ''), alias: ('a', ref)})
        current = replace(before, refs={ref: ('b', ''), alias: ('b', ref)})
        effective = decision.reconcile_manager_metadata(before, current,
            refs=frozenset({ref}), worktrees=frozenset())
        self.assertEqual(effective.refs, current.refs)
        runtime = '/runtime'
        current = replace(current, worktrees={**current.worktrees,
            runtime: ('HEAD=b', 'detached=True')})
        self.assertEqual(decide(effective, current, manager_runtime=(runtime, 'b')).checks[0].before.refs,
                         current.refs)
        for symbolic in (('b', 'refs/remotes/origin/other'), ('foreign', ref)):
            changed = replace(current, refs={**current.refs, alias: symbolic})
            effective = decision.reconcile_manager_metadata(before, changed,
                refs=frozenset({ref}), worktrees=frozenset())
            self.assertEqual(effective.refs[alias], before.refs[alias])

    def test_blocked_current_flags_must_be_restored_before_local_baseline_adoption(self):
        before = snapshot(index={'app.py': ('H 100644 blob 0',)})
        flagged = replace(before, index={'app.py': ('S 100644 blob 0',)})
        with self.assertRaisesRegex(decision.ScopeDecisionError, 'index flags'):
            decide(before, flagged, phase='PLAN_BLOCKED', result=flagged)
        check, = decide(before, before, phase='PLAN_BLOCKED', result=flagged).checks
        self.assertEqual(check.before.index, before.index)

    def test_post_agent_checkpoint_refs_are_allowed_only_by_explicit_manager_proof(self):
        before = snapshot()
        ref = 'refs/remotes/origin/loop-manager/G-051'
        current = replace(before, refs={**before.refs, ref: ('a', '')})
        checkpoint = ManagerGitCheckpoint(before, frozenset())
        checks = decide(before, current, checkpoint=checkpoint,
                        manager_refs=frozenset({ref})).checks
        self.assertFalse(checks[0].allowed_refs)
        self.assertEqual(checks[-1].allowed_refs, frozenset({ref}))

    def test_resumed_manager_metadata_start_is_bound_to_original_scope_and_role(self):
        import hashlib
        ref = 'refs/remotes/origin/main'
        before = snapshot(refs={ref: ('a', '')})
        effective = replace(before, refs={ref: ('b', '')}, dirty={'app.py': (('unstaged',), 'ignored-local')})
        saved = {'manager_metadata_agent_baseline': {
            'original': hashlib.sha256(before.serialize().encode()).hexdigest(),
            'role': 'worker', 'snapshot': effective.serialize()}}
        comparison = decision.manager_comparison_baseline(before, before, saved, 'worker')
        self.assertEqual(comparison.refs, effective.refs)
        self.assertEqual(comparison.dirty, before.dirty)
        self.assertEqual(decision.manager_comparison_baseline(before, before, saved, 'reviewer'), before)
        changed = replace(before, scope_id='other-invocation')
        self.assertEqual(decision.manager_comparison_baseline(changed, changed, saved, 'worker'), changed)
        saved['manager_metadata_agent_baseline']['snapshot'] = 'malformed'
        with self.assertRaisesRegex(decision.ScopeDecisionError, 'snapshot'):
            decision.manager_comparison_baseline(before, before, saved, 'worker')


class ConflictIndexScopeTests(unittest.TestCase):
    def setUp(self):
        self.before = snapshot(branch=None, index={'app.py': tuple(
            f'M 100644 blob-{stage} {stage}' for stage in (1, 2, 3))},
            worktrees={WORKTREE: ('HEAD=a', 'detached=True')},
            operations={'rebase-merge': 'same-metadata'})
        self.after = replace(self.before, index={'app.py': ('H 100644 resolved 0',)},
                             dirty={'app.py': (('staged',), 'resolved-file')})

    def validate(self, after, *, owned=True):
        validate_agent_scope('worker', Path(WORKTREE), self.before, after,
                             selected_plan_path=PLAN, owned_rebase=owned)

    def test_owned_stage_resolution_is_an_index_change_without_security_flags(self):
        from loop_manager import agent_snapshot_changes
        changes = agent_snapshot_changes(Path(WORKTREE), self.before, self.after)
        self.assertNotIn('index flags', changes[0].kinds)
        self.validate(self.after)
        with self.assertRaisesRegex(AgentScopeError, '소유 rebase'):
            self.validate(self.after, owned=False)

    def test_real_flags_set_or_removed_with_resolution_still_block(self):
        for tag in ('h', 'S', 's'):
            with self.subTest(tag=tag), self.assertRaisesRegex(AgentScopeError, 'index flags'):
                self.validate(replace(self.after, index={'app.py': (f'{tag} 100644 resolved 0',)}))
        for tag in ('m', 'S', 's'):
            flagged = replace(self.before, index={'app.py': tuple(
                f'{tag} 100644 blob-{stage} {stage}' for stage in (1, 2, 3))})
            with self.subTest(removed=tag), self.assertRaisesRegex(AgentScopeError, 'index flags'):
                validate_agent_scope('worker', Path(WORKTREE), flagged, self.after,
                                     selected_plan_path=PLAN, owned_rebase=True)

    def test_resolution_cannot_absorb_protected_path_or_shared_metadata(self):
        for after in (replace(self.after, index={**self.after.index, 'docs/requirements/Protected.md': ('H 100644 foreign 0',)}),
                      replace(self.after, refs={**self.after.refs, 'refs/heads/other': ('foreign', '')}),
                      replace(self.after, operations={'rebase-merge': 'different'})):
            with self.subTest(after=after), self.assertRaises(AgentScopeError):
                self.validate(after)


class WorkerPlanDocumentScopeTests(unittest.TestCase):
    related_plan = "docs/plans/[G-060] Project Root Placement Policy and Inventory.md"

    def test_related_plan_updates_are_allowed_in_worker_result(self):
        before = snapshot()
        for kinds in (("unstaged", "worktree"), ("staged",)):
            with self.subTest(kinds=kinds):
                after = replace(before, dirty={path: (kinds, "inventory-update") for path in
                    (self.related_plan, "AGENTS.md", "docs/PRODUCT.md", "docs/ARCHITECTURE.md")})
                validate_agent_scope("worker", Path(WORKTREE), before, after,
                                     selected_plan_path=PLAN)

    def test_saved_worker_result_can_be_revalidated_after_scope_block(self):
        before = snapshot()
        after = replace(before, dirty={self.related_plan: (("unstaged", "worktree"), "inventory-update")})
        checks = decide(before, after, phase="PLAN_BLOCKED", result=after).checks
        self.assertTrue(checks)
        for check in checks:
            validate_agent_scope(check.role, Path(WORKTREE), check.before, check.after,
                                 selected_plan_path=PLAN, allowed_ref_changes=check.allowed_refs,
                                 allowed_worktree_changes=check.allowed_worktrees)

    def test_reviewer_still_rejects_related_plan_updates(self):
        before = snapshot()
        after = replace(before, dirty={self.related_plan: (("unstaged",), "inventory-update")})
        with self.assertRaises(AgentScopeError):
            validate_agent_scope("reviewer", Path(WORKTREE), before, after)

    def test_protected_documents_remain_blocked_with_related_plan_update(self):
        before = snapshot()
        for path in ("docs/PLANS.md", "docs/requirements/Active.md"):
            with self.subTest(path=path):
                after = replace(before, dirty={
                    self.related_plan: (("unstaged",), "inventory-update"),
                    path: (("unstaged",), "unapproved-update"),
                })
                with self.assertRaises(AgentScopeError) as raised:
                    validate_agent_scope("worker", Path(WORKTREE), before, after,
                                         selected_plan_path=PLAN)
                self.assertIn(path, str(raised.exception))
                self.assertNotIn(self.related_plan, str(raised.exception))

    def test_related_plan_update_does_not_allow_index_flags_or_foreign_refs(self):
        before = snapshot(index={self.related_plan: ("H 100644 inventory 0",)})
        after = replace(before, dirty={self.related_plan: (("unstaged",), "inventory-update")})
        for unsafe in (
            replace(after, index={self.related_plan: ("S 100644 inventory 0",)}),
            replace(after, refs={**after.refs, "refs/heads/unowned": ("foreign", "commit")}),
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(AgentScopeError):
                validate_agent_scope("worker", Path(WORKTREE), before, unsafe,
                                     selected_plan_path=PLAN)


class TaskRelevantScopeTests(unittest.TestCase):
    def setUp(self):
        self.before = snapshot(refs={
            'refs/heads/loop-manager/G-051': ('a', ''),
            'refs/remotes/origin/develop': ('base', ''),
            'refs/remotes/origin/loop-manager/G-051': ('published', ''),
        })
        self.resources = decision.used_git_resources(
            role='worker', phase='WORKING', base_branch='develop',
            branch=self.before.branch, worktree_key=WORKTREE)

    def validate(self, after, *, role='worker', resources=True):
        validate_agent_scope(role, Path(WORKTREE), self.before, after,
            selected_plan_path=PLAN, resources=self.resources if resources else None)

    def test_independent_refs_and_worktrees_do_not_change_audit_snapshots(self):
        after = replace(self.before,
            refs={**self.before.refs, 'refs/tags/new': ('other', ''), 'refs/heads/unused': ('other', '')},
            worktrees={**self.before.worktrees, '/source': ('HEAD=changed', 'state=dirty'),
                       '/other': ('HEAD=changed',)})
        self.validate(after)
        first, second = decision.scoped_metadata(self.before, after, self.resources)
        self.assertEqual(first.refs, second.refs)
        self.assertIn('refs/tags/new', after.refs)
        self.assertIn('/other', after.worktrees)
        with self.assertRaises(AgentScopeError):
            self.validate(after, resources=False)

    def test_each_used_ref_registration_operation_and_unknown_config_remains_protected(self):
        cases = [replace(self.before, refs={**self.before.refs, ref: ('unexpected', '')})
                 for ref in self.resources.refs]
        cases += [replace(self.before, worktrees={}),
                  replace(self.before, operations={'MERGE_HEAD': 'new'}),
                  replace(self.before, config={'local:unknown.option': ('new',)})]
        for after in cases:
            with self.subTest(after=after), self.assertRaises(AgentScopeError):
                self.validate(after)

    def test_allowance_does_not_expand_worker_paths_or_reviewer_contract(self):
        allowed = replace(self.before, dirty={'app.py': (('unstaged',), 'new')})
        self.validate(allowed)
        with self.assertRaises(AgentScopeError):
            self.validate(allowed, role='reviewer')
        for after in (replace(self.before, dirty={'docs/requirements/Protected.md': (('unstaged',), 'new')}),
                      replace(self.before, index={'docs/requirements/Protected.md': ('H oid 0',)}),
                      replace(self.before, index={'app.py': ('h oid 0',)})):
            with self.subTest(after=after), self.assertRaises(AgentScopeError):
                self.validate(after)

    def test_only_known_unrelated_branch_config_is_excluded(self):
        self.validate(replace(self.before, config={'local:branch.unused.remote': ('new',)}))
        for key in ('local:branch.loop-manager/G-051.remote', 'local:remote.origin.url',
                    'local:branch.unused.unknown', 'global:include.path', 'local:core.hooksPath'):
            with self.subTest(key=key), self.assertRaises(AgentScopeError):
                self.validate(replace(self.before, config={key: ('new',)}))

    def test_role_stage_resources_and_legacy_serialization(self):
        for role in ('planner', 'maintenance_planner'):
            resources = decision.used_git_resources(role=role, phase='PLANNING',
                base_branch='develop', branch=None, worktree_key=WORKTREE)
            self.assertEqual(resources.refs, frozenset({'refs/remotes/origin/develop', 'refs/remotes/origin/HEAD'}))
        self.assertIsNone(decision.used_git_resources(role='worker', phase='unknown',
            base_branch='develop', branch=self.before.branch, worktree_key=WORKTREE))
        scoped = replace(self.before, scope_id='invocation-scope', scope_start_digest='digest')
        restored = GitSnapshot.deserialize(scoped.serialize())
        self.assertEqual(restored, scoped)
        self.assertEqual((restored.scope_id, restored.scope_start_digest), ('invocation-scope', 'digest'))
        self.assertIsNone(GitSnapshot.deserialize(self.before.serialize()).scope_id)


if __name__ == "__main__":
    unittest.main()
