"""Small stateful Git double for the detached planning rebase boundary."""
from pathlib import Path
from types import SimpleNamespace
import subprocess
import tempfile
import unittest

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

            def git(_repo, *args, check=True):
                if args[:2] == ("diff", "--name-only"):
                    return SimpleNamespace(stdout="\n".join(changes), returncode=0)
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
                    return SimpleNamespace(stdout=source + "\n" + archive, returncode=0)
                revision, path = args[2].split(":", 1)
                value = "same-blob" if (revision, path) in {
                    ("old", source), ("new", archive),
                } else None
                return SimpleNamespace(stdout=value or "", returncode=0 if value else 1)

            result = relevant_document_changes(
                git, root, root, "old", "new", [str(current.relative_to(root))], (),
            )
            self.assertEqual(result, ())


class PlanningRebaseGitBoundaryTest(unittest.TestCase):
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
