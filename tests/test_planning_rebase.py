"""Small stateful Git double for the detached planning rebase boundary."""
from pathlib import Path
from types import SimpleNamespace
import hashlib
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from loop_manager_internal.planning.planning_rebase import (
    PlanningRebaseError, accept_resolved_stash_conflict, complete_planning_rebase,
    rebase_planning_worktree,
)
from loop_manager_internal.planning.planning_document_relevance import relevant_document_changes
from tests.support.catalog import write_execplan


class PlanningGitDouble:
    def __init__(self):
        self.head = "old"
        self.dirty = True
        self.stash_message = None
        self.stash_oid = "stash-oid"
        self.conflict = False
        self.calls = []
        self.interrupt_apply = False

    def __call__(self, _repo, *args, check=True):
        self.calls.append(args)
        result = lambda output="", code=0: SimpleNamespace(stdout=output, returncode=code)
        if args[:2] == ("stash", "list"):
            if self.stash_message is None:
                return result()
            return result(f"stash@{{0}}\0{self.stash_oid}\0On (no branch): {self.stash_message}\n")
        if args[:2] == ("stash", "push"):
            self.stash_message = args[-1]
            self.dirty = False
            return result("Saved working directory")
        if args[:2] == ("stash", "apply"):
            if self.interrupt_apply:
                self.interrupt_apply = False
                raise RuntimeError("process interrupted before stash apply")
            self.dirty = True
            return result()
        if args[:2] == ("stash", "drop"):
            self.stash_message = None
            return result()
        if args[:2] == ("merge-base", "--is-ancestor"):
            return result(code=0 if args[2] == "old" or args[3] == "new" else 1)
        if args[:2] == ("rev-parse", "HEAD"):
            return result(self.head)
        if args[:2] == ("rev-parse", "--git-path"):
            return result(f".git/{args[2]}")
        if args[:2] == ("rev-parse", "--verify"):
            return result(code=0 if self.conflict else 1)
        if args[:2] == ("rev-list", "--count"):
            return result("0")
        if args[:2] == ("status", "--porcelain"):
            return result("MM docs/plans/Plan.md\n?? docs/plans/New.md\n" if self.dirty else "")
        if args[:2] == ("diff", "--binary"):
            return result("saved diff" if self.dirty else "")
        if args[:2] == ("ls-files", "--others"):
            return result()
        if args[:2] == ("switch", "--detach"):
            self.head = args[2]
            return result()
        raise AssertionError(f"unexpected Git command: {args}")


class PlanningRebaseTest(unittest.TestCase):
    def test_damaged_correction_journal_refuses_before_git_mutation(self):
        from loop_manager_internal.planning import planning_rebase
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / 'planning'
            worktree.mkdir()
            path = planning_rebase._journal_path(root, '/repo', worktree)
            planning_rebase._write(path, {
                'repo': '/repo', 'worktree': str(worktree.resolve()),
                'old': 'old', 'target': 'new', 'head': 'old', 'nonce': 'nonce',
                'dirty': True, 'phase': 'rebasing',
                'correction': {'paths': 'docs/plans/a.md', 'was_rebase': True,
                               'head': 'old', 'digest': 'hash'},
            })
            git = mock.Mock()
            with self.assertRaises(PlanningRebaseError):
                rebase_planning_worktree(git, root, root, '/repo', worktree, 'old', 'new')
            git.assert_not_called()
            record = planning_rebase.json.loads(path.read_text())
            record['phase'] = 'rebasing'
            record['correction'] = {
                'paths': ['docs/plans/..\\..\\AGENTS.md'],
                'was_rebase': True, 'head': 'old', 'digest': 'hash',
            }
            planning_rebase._write(path, record)
            with self.assertRaises(PlanningRebaseError):
                rebase_planning_worktree(git, root, root, '/repo', worktree, 'old', 'new')
            git.assert_not_called()
            record.pop('correction')
            planning_rebase._write(path, record)
            with self.assertRaises(PlanningRebaseError):
                planning_rebase.save_planning_correction(
                    git, root, '/repo', worktree, 'new',
                    ('docs/plans/..\\..\\AGENTS.md',), True)
            git.assert_not_called()
            self.assertTrue(path.exists())
            record['phase'] = ['rebasing']
            planning_rebase._write(path, record)
            with self.assertRaises(PlanningRebaseError):
                rebase_planning_worktree(git, root, root, '/repo', worktree, 'old', 'new')
            git.assert_not_called()

    def test_nul_git_path_output_keeps_leading_whitespace(self):
        from loop_manager_internal.planning import planning_rebase
        name = ' leading\tline\nback\\slash'
        git = mock.Mock(return_value=SimpleNamespace(stdout=name + '\0', returncode=0))
        self.assertEqual(planning_rebase._git_ok(
            git, Path('/repo'), 'ls-files', '--others', '-z'), name + '\0')
        git.assert_called_once_with(
            Path('/repo'), 'ls-files', '--others', '-z', check=False)

    @unittest.skipIf(os.name == 'nt', 'raw non-UTF-8 filename fixture requires POSIX')
    def test_rebase_fingerprints_read_raw_nul_paths_without_text_git(self):
        from loop_manager_internal.agents import scope_observation
        from loop_manager_internal.planning import planning_rebase
        from loop_manager_internal.planning.git_ports import _PlanningGitPorts
        raw_name = b' leading\r\xff\tback\\slash'
        name = os.fsdecode(raw_name)
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            (worktree / name).write_bytes(b'content')
            text_git = mock.Mock(return_value=SimpleNamespace(stdout='', returncode=0))
            raw_effects = SimpleNamespace(git_bytes=mock.Mock(return_value=raw_name + b'\0'))
            paths = lambda repo, *args: scope_observation._nul_git_paths(
                repo, *args, effects=raw_effects)
            port = _PlanningGitPorts(
                SimpleNamespace(git=text_git, _nul_git_paths=paths),
                None, worktree, worktree)
            git = port._rebase_git
            fingerprint = planning_rebase._restored_fingerprint(git, worktree)
            self.assertEqual(fingerprint[2], ((name, 'file', b'content', 0),))
            digest = planning_rebase._correction_digest(git, worktree, ())
            expected = hashlib.sha256(repr(([(name, ('file', b'content', 0))], '')).encode()).hexdigest()
            self.assertEqual(digest, expected)
            self.assertTrue(all('-z' not in call.args for call in text_git.call_args_list))
            self.assertTrue(all('-z' in call.args for call in raw_effects.git_bytes.call_args_list))
            raw_effects.git_bytes.side_effect = RuntimeError('Git read failed')
            with self.assertRaises(PlanningRebaseError):
                planning_rebase._git_ok(git, worktree, 'ls-files', '--others', '-z')

    def test_missing_stash_untracked_parent_is_empty_but_git_lock_is_preserved(self):
        from loop_manager_internal.planning import planning_rebase
        from loop_manager_internal.planning.git_ports import _RebaseGit
        from loop_manager_internal.runtime.contracts import GitLockError
        text_git = mock.Mock(side_effect=AssertionError('NUL output reached text Git'))
        paths = mock.Mock(side_effect=RuntimeError('no untracked parent'))
        git = _RebaseGit(text_git, paths)
        self.assertEqual(planning_rebase._stash_untracked_collisions(
            git, Path('/repo'), 'stash'), ())
        paths.side_effect = GitLockError('locked')
        with self.assertRaises(GitLockError):
            planning_rebase._stash_untracked_collisions(git, Path('/repo'), 'stash')
        text_git.assert_not_called()

    def test_windows_journal_skips_directory_open(self):
        from loop_manager_internal.planning import planning_rebase
        from loop_manager_internal.git import manager_git_journal
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.json"
            sync = manager_git_journal._sync_directory

            def windows_sync(parent):
                with mock.patch.object(manager_git_journal, "os") as windows_os:
                    windows_os.name = "nt"
                    sync(parent)
                    windows_os.open.assert_not_called()

            with mock.patch.object(planning_rebase, "_sync_directory",
                                   side_effect=windows_sync) as synced:
                planning_rebase._write(path, {"phase": "stashed"})
            synced.assert_called_once_with(path.parent)
            self.assertEqual(path.read_text(), '{"phase": "stashed"}')

    def test_same_base_does_not_stash_or_move_candidate(self):
        git = PlanningGitDouble()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "planning"
            worktree.mkdir()

            result = rebase_planning_worktree(git, root, root, "/repo", worktree, "old", "old")

            self.assertFalse(result.changed)
            self.assertEqual(git.calls, [])
            self.assertTrue(git.dirty)

    def test_remote_rewrite_preserves_dirty_candidate_without_stashing(self):
        original = PlanningGitDouble()

        def git(repo, *args, check=True):
            if args == ("merge-base", "--is-ancestor", "old", "rewritten"):
                return SimpleNamespace(stdout="", returncode=1)
            return original(repo, *args, check=check)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "planning"
            worktree.mkdir()

            with self.assertRaisesRegex(PlanningRebaseError, "재작성"):
                rebase_planning_worktree(git, root, root, "/repo", worktree, "old", "rewritten")

            self.assertEqual(original.calls, [])
            self.assertTrue(original.dirty)

    def test_candidate_drift_keeps_preservation_stash(self):
        git = PlanningGitDouble()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "planning"
            worktree.mkdir()
            rebase_planning_worktree(git, root, root, "/repo", worktree, "old", "new")
            git.dirty = False

            with self.assertRaisesRegex(PlanningRebaseError, "작업물이 달라졌습니다"):
                complete_planning_rebase(git, root, "/repo", worktree, "new")
            self.assertIsNotNone(git.stash_message)

    def test_preserves_staged_unstaged_and_untracked_until_state_commit(self):
        git = PlanningGitDouble()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "planning"
            worktree.mkdir()
            result = rebase_planning_worktree(
                git, root, root, "/repo", worktree, "old", "new",
            )
            self.assertTrue(result.changed)
            self.assertFalse(result.conflict)
            self.assertEqual(result.target, "new")
            self.assertTrue(git.dirty)
            self.assertIsNotNone(git.stash_message)
            self.assertIn(("stash", "push", "--include-untracked", "-m", git.stash_message), git.calls)
            self.assertIn(("stash", "apply", "--index", git.stash_oid), git.calls)
            complete_planning_rebase(git, root, "/repo", worktree, "new")
            self.assertIsNone(git.stash_message)

    def test_interrupted_restore_reuses_preserved_stash(self):
        git = PlanningGitDouble()
        git.interrupt_apply = True
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "planning"
            worktree.mkdir()
            with self.assertRaisesRegex(RuntimeError, "process interrupted"):
                rebase_planning_worktree(
                    git, root, root, "/repo", worktree, "old", "new",
                )
            result = rebase_planning_worktree(
                git, root, root, "/repo", worktree, "old", "new",
            )
            self.assertFalse(result.conflict)
            self.assertEqual(
                sum(call[:2] == ("stash", "push") for call in git.calls), 1,
            )
            self.assertEqual(
                sum(call[:2] == ("stash", "apply") for call in git.calls), 2,
            )

    def test_stash_dropped_after_state_commit_can_finish_journal_cleanup(self):
        git = PlanningGitDouble()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "planning"
            worktree.mkdir()
            rebase_planning_worktree(git, root, root, "/repo", worktree, "old", "new")
            git.stash_message = None  # interrupted after the successful drop
            result = rebase_planning_worktree(
                git, root, root, "/repo", worktree, "new", "new",
            )
            self.assertTrue(result.changed)
            complete_planning_rebase(git, root, "/repo", worktree, "new")


class PlanningDocumentRelevanceTest(unittest.TestCase):
    def test_general_target_rechecks_new_active_requirement_outside_its_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs/plans").mkdir(parents=True)
            plan = write_execplan(root, "G-118", "Current")
            plan_path = plan.relative_to(root).as_posix()
            requirement = "docs/requirements/[R-053] New Goal.md"

            def git(_repo, *args, check=True):
                if args[:2] == ("diff", "--name-only"):
                    return SimpleNamespace(stdout=requirement + "\0", returncode=0)
                if args[:2] == ("ls-tree", "-r"):
                    return SimpleNamespace(stdout=plan_path + "\0", returncode=0)
                if args[0] == "show":
                    return SimpleNamespace(stdout=plan.read_text(), returncode=0)
                revision, path = args[2].split(":", 1)
                value = "same-plan" if path == plan_path else (
                    "new-requirement" if revision == "new" and path == requirement else None
                )
                return SimpleNamespace(stdout=value or "", returncode=0 if value else 1)

            arguments = (git, root, root, "old", "new", [], (), ("G-118",))
            self.assertEqual(relevant_document_changes(*arguments), ())
            self.assertEqual(relevant_document_changes(
                *arguments, include_active_requirements=True,
            ), (requirement,))
            self.assertEqual(relevant_document_changes(
                git, root, root, "old", "new", [], (), (),
            ), ())
            self.assertEqual(relevant_document_changes(
                git, root, root, "old", "new", [], (), (),
                include_active_requirements=True,
            ), (requirement,))

    def test_only_current_plan_sources_and_dependencies_require_recheck(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs/plans").mkdir(parents=True)
            source = "docs/requirements/[R-051] Relevant.md"
            current = write_execplan(root, "G-118", "Current", sources=(source,),
                                     depends_on=("G-100",))
            unrelated = "docs/requirements/[R-052] Unrelated.md"
            dependency = "docs/plans/[G-100] Dependency.md"
            product = "docs/PRODUCT.md"
            changes = (source, unrelated, dependency, product,
                       "docs/plans/[G-101] Other.md")
            objects = {("old", path): path + "-old" for path in changes}
            objects.update({("new", path): path + "-new" for path in changes})
            current_path = str(current.relative_to(root))
            for revision in ("old", "new"):
                objects[(revision, current_path)] = "same-current"

            def git(_repo, *args, check=True):
                if args[:2] == ("diff", "--name-only"):
                    return SimpleNamespace(stdout="\0".join(changes) + "\0", returncode=0)
                if args[:2] == ("ls-tree", "-r"):
                    return SimpleNamespace(stdout=current_path + "\0" + dependency + "\0",
                                           returncode=0)
                if args[0] == "show":
                    return SimpleNamespace(stdout=current.read_text(), returncode=0)
                assert args[:2] == ("rev-parse", "--verify")
                revision, path = args[2].split(":", 1)
                value = objects.get((revision, path))
                return SimpleNamespace(stdout=value or "", returncode=0 if value else 1)

            rows = [{"plan_id": "G-100", "path": dependency}]
            result = relevant_document_changes(
                git, root, root, "old", "new", [str(current.relative_to(root))], rows,
            )
            self.assertEqual(result, (product, dependency, source))

    def test_content_preserving_requirement_archive_needs_no_recheck(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs/plans").mkdir(parents=True)
            source = "docs/requirements/[R-051] Relevant.md"
            current = write_execplan(root, "G-118", "Current", sources=(source,))
            archive = "docs/requirements/archive/[R-051] Relevant.md"

            def git(_repo, *args, check=True):
                if args[:2] == ("diff", "--name-only"):
                    return SimpleNamespace(stdout=source + "\0" + archive + "\0", returncode=0)
                if args[:2] == ("ls-tree", "-r"):
                    return SimpleNamespace(stdout=str(current.relative_to(root)) + "\0", returncode=0)
                if args[0] == "show":
                    return SimpleNamespace(stdout=current.read_text(), returncode=0)
                revision, path = args[2].split(":", 1)
                value = "same-blob" if (revision, path) in {
                    ("old", source), ("new", archive),
                } else None
                return SimpleNamespace(stdout=value or "", returncode=0 if value else 1)

            result = relevant_document_changes(
                git, root, root, "old", "new", [str(current.relative_to(root))], (),
            )
            self.assertEqual(result, ())
            self.assertEqual(relevant_document_changes(
                git, root, root, "old", "new", [str(current.relative_to(root))], (),
                include_active_requirements=True,
            ), ())

    def test_content_preserving_dependency_archive_needs_no_recheck(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs/plans").mkdir(parents=True)
            target = write_execplan(root, "G-118", "Current", depends_on=("G-100",))
            target_path = str(target.relative_to(root))
            dependency = "docs/plans/[G-100] Dependency.md"
            archived = "docs/plans/archive/[G-100] Dependency.md"

            def git(_repo, *args, check=True):
                if args[:2] == ("diff", "--name-only"):
                    return SimpleNamespace(stdout=dependency + "\0" + archived + "\0",
                                           returncode=0)
                if args[:2] == ("ls-tree", "-r"):
                    other = dependency if args[4] == "old" else archived
                    return SimpleNamespace(stdout=target_path + "\0" + other + "\0",
                                           returncode=0)
                if args[0] == "show":
                    return SimpleNamespace(stdout=target.read_text(), returncode=0)
                revision, path = args[2].split(":", 1)
                value = ("target" if path == target_path else "same" if
                         (revision, path) in (("old", dependency), ("new", archived))
                         else None)
                return SimpleNamespace(stdout=value or "", returncode=0 if value else 1)

            self.assertEqual(relevant_document_changes(
                git, root, root, "old", "new", [], (), ("G-118",),
            ), ())

    def test_unchanged_checkout_rechecks_remote_target_and_both_relation_sets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_path = "docs/plans/[G-118] Current.md"
            new_path = "docs/plans/[G-118] Renamed.md"
            old_source = "docs/requirements/[R-051] Old.md"
            new_source = "docs/requirements/[R-052] New.md"
            old_dependency = "docs/plans/[G-100] Old dependency.md"
            new_dependency = "docs/plans/[G-101] New dependency.md"
            (root / "docs/plans").mkdir(parents=True)
            old_text = write_execplan(root, "G-118", "Current",
                                      sources=(old_source,),
                                      depends_on=("G-100",)).read_text()
            new_text = write_execplan(root, "G-118", "Renamed",
                                      sources=(new_source,),
                                      depends_on=("G-101",)).read_text()
            trees = {
                "old": {old_path: old_text, old_source: "old source",
                        old_dependency: "old dependency"},
                "new": {new_path: new_text, new_source: "new source",
                        new_dependency: "new dependency"},
            }
            changed = (old_path, new_path, old_source, new_source,
                       old_dependency, new_dependency)

            def git(_repo, *args, check=True):
                if args[:2] == ("diff", "--name-only"):
                    return SimpleNamespace(stdout="\0".join(changed) + "\0", returncode=0)
                if args[:2] == ("ls-tree", "-r"):
                    return SimpleNamespace(stdout="\0".join(trees[args[4]]) + "\0", returncode=0)
                if args[0] == "show":
                    revision, path = args[1].split(":", 1)
                    return SimpleNamespace(stdout=trees[revision][path], returncode=0)
                revision, path = args[2].split(":", 1)
                value = trees[revision].get(path)
                return SimpleNamespace(stdout=value or "", returncode=0 if value else 1)

            result = relevant_document_changes(
                git, root, root, "old", "new", [], (), ("G-118",),
            )
            self.assertEqual(result, tuple(sorted((new_path, old_source,
                                                   new_source, old_dependency,
                                                   new_dependency))))

    def test_explicit_target_tracks_unicode_source_and_policy_without_local_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docs/plans").mkdir(parents=True)
            source = "docs/requirements/[R-051] 관련.md"
            unrelated = "docs/requirements/[R-052] 무관.md"
            plan = write_execplan(root, "G-118", "Current", sources=(source,))
            path = str(plan.relative_to(root))
            content = plan.read_text()
            changes = (source, unrelated, "AGENTS.md")

            def git(_repo, *args, check=True):
                if args[:2] == ("diff", "--name-only"):
                    return SimpleNamespace(stdout="\0".join(changes) + "\0", returncode=0)
                if args[:2] == ("ls-tree", "-r"):
                    return SimpleNamespace(stdout=path + "\0", returncode=0)
                if args[0] == "show":
                    return SimpleNamespace(stdout=content, returncode=0)
                revision, candidate = args[2].split(":", 1)
                value = "same" if candidate == path else (
                    "old" if revision == "old" else "new"
                )
                return SimpleNamespace(stdout=value, returncode=0)

            self.assertEqual(relevant_document_changes(
                git, root, root, "old", "new", [], (), ("G-118",),
            ), ("AGENTS.md", source))
            changes = (unrelated,)
            self.assertEqual(relevant_document_changes(
                git, root, root, "old", "new", [], (), ("G-118",),
            ), ())


class PlanningRebaseGitBoundaryTest(unittest.TestCase):
    def test_correction_resumes_after_stage_or_reset_without_repeating_planner(self):
        from loop_manager_internal.planning import planning_rebase
        from loop_manager_internal.planning.workspace_service import PlanningWorkspaceService
        from tests.support.planning_case import FakePorts, planning_snapshot
        for committed in (False, True, "prepared"):
            for drift in (False, True):
                with self.subTest(committed=committed, drift=drift), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    repo = root / "repo"
                    repo.mkdir()
                    armed = False

                    def git(cwd, *args, check=True):
                        nonlocal armed
                        result = subprocess.run(
                            ["git", *args], cwd=cwd, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        )
                        if check and result.returncode:
                            raise RuntimeError(result.stdout)
                        if armed and args[:2] == (("reset", "--soft") if committed == "prepared" else ("add", "--")):
                            armed = False
                            raise RuntimeError("interrupted after correction")
                        return result

                    git(repo, "init", "-b", "main")
                    git(repo, "config", "user.name", "Test")
                    git(repo, "config", "user.email", "test@example.com")
                    name = "docs/plans/Plan.md"
                    (repo / name).parent.mkdir(parents=True)
                    (repo / name).write_text("base\n")
                    git(repo, "add", ".")
                    git(repo, "commit", "-m", "base")
                    old = git(repo, "rev-parse", "HEAD").stdout.strip()
                    tree = root / "planning"
                    git(repo, "worktree", "add", "--detach", str(tree), old)
                    if committed == "prepared":
                        (tree / "prepared.txt").write_text("prepared\n")
                        git(tree, "add", "prepared.txt")
                        git(tree, "commit", "-m", "prepared")
                    (tree / name).write_text("local\n")
                    git(tree, "add", name)
                    if committed is True:
                        git(tree, "commit", "-m", "candidate")
                    (repo / name).write_text("remote\n")
                    git(repo, "add", name)
                    git(repo, "commit", "-m", "remote")
                    target = git(repo, "rev-parse", "HEAD").stdout.strip()
                    args = (git, repo, root / "state", str(repo), tree, old, target)
                    first = rebase_planning_worktree(*args)
                    self.assertTrue(first.conflict)
                    ports = FakePorts(planning_snapshot(planner_session_id="saved-session"))
                    ports.git.run = git
                    ports.git.save_planning_correction = lambda *a: planning_rebase.save_planning_correction(
                        git, root / "state", *a,
                    )
                    ports.git.planning_correction = lambda *a: planning_rebase.planning_correction(
                        git, root / "state", *a,
                    )
                    ports.git.accept_resolved_stash_conflict = lambda *a: accept_resolved_stash_conflict(
                        git, root / "state", *a,
                    )
                    invoke = ports.agent.run_planner

                    def resolve(tree, prompt, session, callback):
                        (tree / name).write_text("remote\nlocal\n")
                        return invoke(tree, prompt, session, callback)

                    ports.agent.run_planner = resolve
                    run = SimpleNamespace(
                        maintenance_planning=False, row=ports.state.snapshot,
                        context=SimpleNamespace(repo=str(repo)),
                    )
                    armed = True
                    service = PlanningWorkspaceService(ports)
                    with self.assertRaisesRegex(RuntimeError, "interrupted after correction"):
                        service._resolve_rebase_conflict(
                            run, tree, target, first.detail, first.conflict_paths, first.stash_sha,
                        )
                    self.assertEqual(git(tree, "ls-files", "-u").stdout, "")
                    if committed == "prepared":
                        self.assertEqual(git(tree, "rev-parse", "HEAD").stdout.strip(), target)
                        if drift:
                            (tree / name).write_text("unapproved change\n")
                            with self.assertRaisesRegex(PlanningRebaseError, "작업물이 달라"):
                                rebase_planning_worktree(*args)
                        else:
                            self.assertFalse(rebase_planning_worktree(*args).conflict)
                            self.assertEqual((tree / name).read_text(), "remote\nlocal\n")
                            self.assertEqual((tree / "prepared.txt").read_text(), "prepared\n")
                            complete_planning_rebase(git, root / "state", str(repo), tree, target)
                        self.assertEqual(ports.agent.run_count, 1)
                        continue
                    resumed = rebase_planning_worktree(*args)
                    self.assertTrue(resumed.conflict)
                    if drift:
                        (tree / name).write_text("unapproved change\n")
                        with self.assertRaisesRegex(PlanningRebaseError, "작업물이 달라"):
                            service._resolve_rebase_conflict(run, tree, target, resumed.detail)
                        self.assertEqual((tree / name).read_text(), "unapproved change\n")
                    else:
                        self.assertTrue(service._resolve_rebase_conflict(run, tree, target, resumed.detail))
                        self.assertFalse(rebase_planning_worktree(*args).conflict)
                        self.assertEqual((tree / name).read_text(), "remote\nlocal\n")
                        complete_planning_rebase(git, root / "state", str(repo), tree, target)
                    self.assertEqual(ports.agent.run_count, 1)

    def test_staged_overlap_becomes_resumable_conflict(self):
        for interrupted in (False, True, "legacy"):
            with self.subTest(interrupted=interrupted), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repository = root / "repo"
                repository.mkdir()
                armed = interrupted is True

                def git(cwd, *args, check=True):
                    nonlocal armed
                    result = subprocess.run(
                        ["git", *args], cwd=cwd, text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    )
                    if check and result.returncode:
                        raise RuntimeError(result.stdout)
                    if armed and args[:2] == ("stash", "apply") and "--index" not in args:
                        armed = False
                        raise RuntimeError("interrupted after conflict")
                    return result

                git(repository, "init", "-b", "main")
                git(repository, "config", "user.name", "Test")
                git(repository, "config", "user.email", "test@example.com")
                plan = "docs/plans/Plan.md"
                (repository / plan).parent.mkdir(parents=True)
                (repository / plan).write_text("base\n")
                git(repository, "add", ".")
                git(repository, "commit", "-m", "base")
                old = git(repository, "rev-parse", "HEAD").stdout.strip()
                worktree = root / "planning"
                git(repository, "worktree", "add", "--detach", str(worktree), old)
                (worktree / plan).write_text("local staged\n")
                git(worktree, "add", plan)
                (worktree / "untracked.txt").write_text("preserved\n")
                (repository / plan).write_text("remote\n")
                git(repository, "add", ".")
                git(repository, "commit", "-m", "remote")
                target = git(repository, "rev-parse", "HEAD").stdout.strip()
                arguments = (git, repository, root / "state", str(repository), worktree, old, target)
                if interrupted == "legacy":
                    from loop_manager_internal.planning import planning_rebase
                    with mock.patch.object(
                        planning_rebase, "_apply_stash",
                        side_effect=lambda runner, tree, sha: runner(
                            tree, "stash", "apply", "--index", sha, check=False,
                        ),
                    ):
                        self.assertTrue(rebase_planning_worktree(*arguments).conflict)
                    self.assertEqual(git(worktree, "status", "--porcelain").stdout, "")
                elif interrupted:
                    with self.assertRaisesRegex(RuntimeError, "interrupted after conflict"):
                        rebase_planning_worktree(*arguments)
                result = rebase_planning_worktree(*arguments)
                self.assertTrue(result.conflict)
                self.assertIn(plan, git(worktree, "diff", "--name-only", "--diff-filter=U").stdout)
                self.assertEqual(git(worktree, "show", result.stash_sha + "^2:" + plan).stdout,
                                 "local staged\n")
                from tests.support.planning_case import FakePorts, planning_snapshot
                from loop_manager_internal.planning.workspace_service import PlanningWorkspaceService
                ports = FakePorts(planning_snapshot(planner_session_id="saved-session"))
                ports.git.run = git
                ports.git.accept_resolved_stash_conflict = lambda repo, tree, revision: (
                    accept_resolved_stash_conflict(git, root / "state", repo, tree, revision)
                )
                invoke = ports.agent.run_planner

                def resolve(tree, prompt, session, callback):
                    self.assertIn(result.stash_sha + "^2:", prompt)
                    (tree / plan).write_text("remote\nlocal staged\n")
                    return invoke(tree, prompt, session, callback)

                ports.agent.run_planner = resolve
                run = SimpleNamespace(
                    maintenance_planning=False, row=ports.state.snapshot,
                    context=SimpleNamespace(repo=str(repository)),
                )
                self.assertTrue(PlanningWorkspaceService(ports)._resolve_rebase_conflict(
                    run, worktree, target, result.detail, result.conflict_paths, result.stash_sha,
                ))
                self.assertEqual(ports.agent.resumed_session, "saved-session")
                self.assertFalse(rebase_planning_worktree(*arguments).conflict)
                self.assertEqual((worktree / "untracked.txt").read_text(), "preserved\n")
                complete_planning_rebase(git, root / "state", str(repository), worktree, target)
                self.assertEqual(git(worktree, "stash", "list").stdout, "")

    def test_prepared_commit_and_dirty_index_survive_apply_and_reset_interruptions(self):
        for boundary in ("apply", "reset"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repository = root / "repo"
                repository.mkdir()
                armed = False

                def git(cwd, *args, check=True):
                    nonlocal armed
                    result = subprocess.run(
                        ["git", *args], cwd=cwd, text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    )
                    if check and result.returncode:
                        raise RuntimeError(result.stdout)
                    interrupted = (args[:2] == ("stash", "apply") if boundary == "apply"
                                   else args[:2] == ("reset", "--soft"))
                    if armed and interrupted and cwd == worktree:
                        armed = False
                        raise RuntimeError("interrupted after " + boundary)
                    return result

                git(repository, "init", "-b", "main")
                git(repository, "config", "user.name", "Test")
                git(repository, "config", "user.email", "test@example.com")
                (repository / "plan.txt").write_text("base\n", encoding="utf-8")
                git(repository, "add", "plan.txt")
                git(repository, "commit", "-m", "base")
                old = git(repository, "rev-parse", "HEAD").stdout.strip()
                worktree = root / "planning"
                git(repository, "worktree", "add", "--detach", str(worktree), old)
                (worktree / "plan.txt").write_text("prepared\n", encoding="utf-8")
                git(worktree, "add", "plan.txt")
                git(worktree, "commit", "-m", "prepared")
                (worktree / "plan.txt").write_text("staged\n", encoding="utf-8")
                git(worktree, "add", "plan.txt")
                (worktree / "plan.txt").write_text("unstaged\n", encoding="utf-8")
                (worktree / "untracked.txt").write_text("untracked\n", encoding="utf-8")
                (repository / "remote.txt").write_text("remote\n", encoding="utf-8")
                git(repository, "add", "remote.txt")
                git(repository, "commit", "-m", "advance")
                target = git(repository, "rev-parse", "HEAD").stdout.strip()
                armed = True

                with self.assertRaisesRegex(RuntimeError, "interrupted after " + boundary):
                    rebase_planning_worktree(
                        git, repository, root / "state", str(repository), worktree, old, target,
                    )
                resumed = rebase_planning_worktree(
                    git, repository, root / "state", str(repository), worktree, old, target,
                )

                self.assertFalse(resumed.conflict, resumed.detail)
                self.assertEqual(git(worktree, "rev-parse", "HEAD").stdout.strip(), target)
                self.assertEqual(git(worktree, "show", ":plan.txt").stdout, "staged\n")
                self.assertEqual((worktree / "plan.txt").read_text(), "unstaged\n")
                self.assertEqual((worktree / "untracked.txt").read_text(), "untracked\n")
                self.assertEqual((repository / "plan.txt").read_text(), "base\n")
                complete_planning_rebase(git, root / "state", str(repository), worktree, target)
                self.assertEqual(git(worktree, "stash", "list").stdout, "")

    def test_untracked_plan_name_collision_keeps_local_stash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repo"
            repository.mkdir()
            interrupt_after_apply = False

            def git(cwd, *args, check=True):
                nonlocal interrupt_after_apply
                result = subprocess.run(
                    ["git", *args], cwd=cwd, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                )
                if check and result.returncode:
                    raise RuntimeError(result.stdout)
                if args[:2] == ("stash", "apply") and interrupt_after_apply:
                    interrupt_after_apply = False
                    raise RuntimeError("interrupted during stash collision")
                return result

            git(repository, "init", "-b", "main")
            git(repository, "config", "user.name", "Test")
            git(repository, "config", "user.email", "test@example.com")
            (repository / "base.txt").write_text("base\n", encoding="utf-8")
            git(repository, "add", "base.txt")
            git(repository, "commit", "-m", "base")
            old = git(repository, "rev-parse", "HEAD").stdout.strip()
            worktree = root / "planning"
            git(repository, "worktree", "add", "--detach", str(worktree), old)
            local = worktree / "docs/plans/[G-120] Candidate.md"
            local.parent.mkdir(parents=True)
            local.write_text("local plan\n", encoding="utf-8")
            remote = repository / "docs/plans/[G-120] Candidate.md"
            remote.parent.mkdir(parents=True)
            remote.write_text("remote plan\n", encoding="utf-8")
            git(repository, "add", "docs/plans")
            git(repository, "commit", "-m", "advance")
            target = git(repository, "rev-parse", "HEAD").stdout.strip()

            interrupt_after_apply = True
            with self.assertRaisesRegex(RuntimeError, "interrupted during stash collision"):
                rebase_planning_worktree(
                    git, repository, root / "state", str(repository), worktree,
                    old, target,
                )
            result = rebase_planning_worktree(
                git, repository, root / "state", str(repository), worktree,
                old, target,
            )
            self.assertTrue(result.conflict)
            self.assertEqual(result.conflict_paths,
                             ("docs/plans/[G-120] Candidate.md",))
            self.assertEqual(local.read_text(), "remote plan\n")
            self.assertEqual(
                git(worktree, "show", f"{result.stash_sha}^3:docs/plans/[G-120] Candidate.md").stdout,
                "local plan\n",
            )

    def test_dirty_stash_conflict_can_be_corrected_and_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repo"
            repository.mkdir()

            def git(cwd, *args, check=True):
                result = subprocess.run(
                    ["git", *args], cwd=cwd, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                )
                if check and result.returncode:
                    raise RuntimeError(result.stdout)
                return result

            git(repository, "init", "-b", "main")
            git(repository, "config", "user.name", "Test")
            git(repository, "config", "user.email", "test@example.com")
            (repository / "plan.txt").write_text("base\n", encoding="utf-8")
            git(repository, "add", "plan.txt")
            git(repository, "commit", "-m", "base")
            old = git(repository, "rev-parse", "HEAD").stdout.strip()
            worktree = root / "planning"
            git(repository, "worktree", "add", "--detach", str(worktree), old)
            (worktree / "plan.txt").write_text("local\n", encoding="utf-8")
            (repository / "plan.txt").write_text("remote\n", encoding="utf-8")
            git(repository, "add", "plan.txt")
            git(repository, "commit", "-m", "advance")
            target = git(repository, "rev-parse", "HEAD").stdout.strip()

            first = rebase_planning_worktree(
                git, repository, root / "state", str(repository), worktree,
                old, target,
            )
            self.assertTrue(first.conflict)
            self.assertIn("plan.txt", git(worktree, "ls-files", "-u").stdout)
            (worktree / "plan.txt").write_text("remote and local\n", encoding="utf-8")
            git(worktree, "add", "plan.txt")
            accept_resolved_stash_conflict(
                git, root / "state", str(repository), worktree, target,
            )

            resumed = rebase_planning_worktree(
                git, repository, root / "state", str(repository), worktree,
                old, target,
            )
            self.assertFalse(resumed.conflict)
            self.assertEqual((worktree / "plan.txt").read_text(), "remote and local\n")
            complete_planning_rebase(git, root / "state", str(repository), worktree, target)
            self.assertNotIn("loop-manager-planning-rebase:", git(worktree, "stash", "list").stdout)

    def test_conflicted_prepared_commit_resumes_after_file_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repo"
            repository.mkdir()

            def git(cwd, *args, check=True):
                result = subprocess.run(
                    ["git", *args], cwd=cwd, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                )
                if check and result.returncode:
                    raise RuntimeError(result.stdout)
                return result

            git(repository, "init", "-b", "main")
            git(repository, "config", "user.name", "Test")
            git(repository, "config", "user.email", "test@example.com")
            (repository / "plan.txt").write_text("base\n", encoding="utf-8")
            git(repository, "add", "plan.txt")
            git(repository, "commit", "-m", "base")
            old = git(repository, "rev-parse", "HEAD").stdout.strip()
            worktree = root / "planning"
            git(repository, "worktree", "add", "--detach", str(worktree), old)
            (worktree / "plan.txt").write_text("local\n", encoding="utf-8")
            git(worktree, "add", "plan.txt")
            git(worktree, "commit", "-m", "planning")
            (repository / "plan.txt").write_text("remote\n", encoding="utf-8")
            git(repository, "add", "plan.txt")
            git(repository, "commit", "-m", "advance")
            target = git(repository, "rev-parse", "HEAD").stdout.strip()

            first = rebase_planning_worktree(
                git, repository, root / "state", str(repository), worktree,
                old, target,
            )
            self.assertTrue(first.conflict)
            self.assertEqual(git(worktree, "rev-parse", "--verify", "REBASE_HEAD").returncode, 0)
            (worktree / "plan.txt").write_text("remote and local\n", encoding="utf-8")
            git(worktree, "add", "plan.txt")
            git(worktree, "-c", "core.editor=true", "rebase", "--continue")

            resumed = rebase_planning_worktree(
                git, repository, root / "state", str(repository), worktree,
                old, target,
            )
            self.assertFalse(resumed.conflict, resumed.detail)
            self.assertEqual(git(worktree, "rev-parse", "HEAD").stdout.strip(), target)
            self.assertEqual((worktree / "plan.txt").read_text(), "remote and local\n")
            complete_planning_rebase(git, root / "state", str(repository), worktree, target)

    def test_prepared_planning_commit_returns_to_unpublished_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repo"
            repository.mkdir()

            def git(cwd, *args, check=True):
                result = subprocess.run(
                    ["git", *args], cwd=cwd, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                )
                if check and result.returncode:
                    raise RuntimeError(result.stdout)
                return result

            git(repository, "init", "-b", "main")
            git(repository, "config", "user.name", "Test")
            git(repository, "config", "user.email", "test@example.com")
            (repository / "base.txt").write_text("base\n", encoding="utf-8")
            git(repository, "add", "base.txt")
            git(repository, "commit", "-m", "base")
            old = git(repository, "rev-parse", "HEAD").stdout.strip()
            worktree = root / "planning"
            git(repository, "worktree", "add", "--detach", str(worktree), old)
            (worktree / "plan.txt").write_text("planner output\n", encoding="utf-8")
            git(worktree, "add", "plan.txt")
            git(worktree, "commit", "-m", "prepared planning")
            (repository / "remote.txt").write_text("remote\n", encoding="utf-8")
            git(repository, "add", "remote.txt")
            git(repository, "commit", "-m", "advance")
            target = git(repository, "rev-parse", "HEAD").stdout.strip()

            result = rebase_planning_worktree(
                git, repository, root / "state", str(repository), worktree,
                old, target,
            )

            self.assertFalse(result.conflict)
            self.assertEqual(git(worktree, "rev-parse", "HEAD").stdout.strip(), target)
            self.assertEqual((worktree / "plan.txt").read_text(), "planner output\n")
            self.assertIn("plan.txt", git(worktree, "status", "--short").stdout)
            complete_planning_rebase(git, root / "state", str(repository), worktree, target)

    def test_git_preserves_index_worktree_and_untracked_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repo"
            repository.mkdir()
            interrupt_after_apply = False

            def git(cwd, *args, check=True):
                nonlocal interrupt_after_apply
                result = subprocess.run(
                    ["git", *args], cwd=cwd, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                )
                if check and result.returncode:
                    raise RuntimeError(result.stdout)
                if args[:2] == ("stash", "apply") and interrupt_after_apply:
                    interrupt_after_apply = False
                    raise RuntimeError("interrupted after stash apply")
                return result

            git(repository, "init", "-b", "main")
            git(repository, "config", "user.name", "Test")
            git(repository, "config", "user.email", "test@example.com")
            (repository / "shared.txt").write_text("original\n", encoding="utf-8")
            git(repository, "add", "shared.txt")
            git(repository, "commit", "-m", "base")
            old = git(repository, "rev-parse", "HEAD").stdout.strip()
            worktree = root / "planning"
            git(repository, "worktree", "add", "--detach", str(worktree), old)
            (worktree / "staged.txt").write_text("staged\n", encoding="utf-8")
            git(worktree, "add", "staged.txt")
            (worktree / "shared.txt").write_text("original\nunstaged\n", encoding="utf-8")
            (worktree / "untracked.txt").write_text("untracked\n", encoding="utf-8")
            (repository / "remote.txt").write_text("remote\n", encoding="utf-8")
            git(repository, "add", "remote.txt")
            git(repository, "commit", "-m", "advance")
            target = git(repository, "rev-parse", "HEAD").stdout.strip()

            interrupt_after_apply = True
            with self.assertRaisesRegex(RuntimeError, "interrupted after stash apply"):
                rebase_planning_worktree(
                    git, repository, root / "state", str(repository), worktree,
                    old, target,
                )
            result = rebase_planning_worktree(
                git, repository, root / "state", str(repository), worktree,
                old, target,
            )

            self.assertFalse(result.conflict)
            self.assertEqual(git(worktree, "rev-parse", "HEAD").stdout.strip(), target)
            self.assertIn("staged.txt", git(worktree, "diff", "--cached", "--name-only").stdout)
            self.assertIn("shared.txt", git(worktree, "diff", "--name-only").stdout)
            self.assertEqual((worktree / "untracked.txt").read_text(), "untracked\n")
            self.assertEqual((repository / "shared.txt").read_text(), "original\n")
            complete_planning_rebase(git, root / "state", str(repository), worktree, target)
