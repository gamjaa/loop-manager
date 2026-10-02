import unittest
from dataclasses import replace
from pathlib import Path

import agent_scope_decision as decision
from loop_manager import GitSnapshot, ManagerGitCheckpoint, AgentScopeError, validate_agent_scope


WORKTREE = "/managed"
REPO = "/source"
PLAN = "docs/plans/[G-051] Plan.md"


def snapshot(**changes):
    base = GitSnapshot(
        head="a", branch="loop-manager/G-051", index={}, dirty={},
        refs={"refs/heads/loop-manager/G-051": ("a", "commit")},
        worktrees={WORKTREE: ("HEAD=a", "branch=refs/heads/loop-manager/G-051")},
        config={}, operations={},
    )
    return replace(base, **changes)


def decide(before, current, *, role="worker", phase="IMPLEMENTING", result=None,
           checkpoint=None, planner_correction=False, cleanup=frozenset(),
           manager_runtime=None, runtime_recreation_proven=False, manager_refs=frozenset()):
    return decision.decide_persisted_scope(
        role=role, phase=phase, base_branch="main", selected_plan_path=PLAN,
        before=before, current=current, result=result, checkpoint=checkpoint,
        worktree_key=WORKTREE, repo_key=REPO,
        planner_correction=planner_correction, manager_cleanup_worktrees=cleanup,
        manager_runtime=manager_runtime, runtime_recreation_proven=runtime_recreation_proven,
        manager_refs=manager_refs,
    )


class PersistedScopeDecisionTests(unittest.TestCase):
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
        for after in (replace(self.after, index={**self.after.index, 'AGENTS.md': ('H 100644 foreign 0',)}),
                      replace(self.after, refs={**self.after.refs, 'refs/heads/other': ('foreign', '')}),
                      replace(self.after, operations={'rebase-merge': 'different'})):
            with self.subTest(after=after), self.assertRaises(AgentScopeError):
                self.validate(after)


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
        for after in (replace(self.before, dirty={'AGENTS.md': (('unstaged',), 'new')}),
                      replace(self.before, index={'AGENTS.md': ('H oid 0',)}),
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
            self.assertEqual(resources.refs, frozenset({'refs/remotes/origin/develop'}))
        self.assertIsNone(decision.used_git_resources(role='worker', phase='unknown',
            base_branch='develop', branch=self.before.branch, worktree_key=WORKTREE))
        scoped = replace(self.before, scope_id='invocation-scope', scope_start_digest='digest')
        restored = GitSnapshot.deserialize(scoped.serialize())
        self.assertEqual(restored, scoped)
        self.assertEqual((restored.scope_id, restored.scope_start_digest), ('invocation-scope', 'digest'))
        self.assertIsNone(GitSnapshot.deserialize(self.before.serialize()).scope_id)


if __name__ == "__main__":
    unittest.main()
