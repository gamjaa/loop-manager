import sqlite3
from pathlib import Path
import loop_manager
from loop_manager_internal.storage import planning_input_state
from tests.support.catalog import write_execplan
from tests.support.git import (
    git_command,
    make_remote_repository,
    commit_and_push,
    advance_remote_file,
)
from tests.support.publication import seed_validated_candidate
from tests.support.worker import worker_result


def observe_live_pull_request(repo: Path, pull_request):
    """Keep GitHub head observations aligned with successful local-remote pushes."""
    if pull_request is None:
        return None
    ref = f"refs/heads/{pull_request['headRefName']}"
    result = git_command(repo, 'ls-remote', 'origin', ref)
    entries = [line.split() for line in result.splitlines()]
    heads = [entry[0] for entry in entries if len(entry) == 2 and entry[1] == ref]
    if len(heads) != 1:
        raise AssertionError('PR fixture branch must have exactly one live remote head')
    return {**pull_request, 'headRefOid': heads[0]}


class PrRecoveryFixture:
    """Shared scenario setup; inheriting this fixture collects no tests."""

    def _pr_open_state(self, directory: str):
        repo = make_remote_repository(directory)
        state_dir = Path(directory) / "state"
        state_dir.mkdir()
        (repo / "docs/plans").mkdir()
        plan = write_execplan(
            repo,
            "G-026",
            "Automatic Owned PR Conflict Recovery",
            complexity="high",
            risk="critical",
            priority="critical",
        )
        commit_and_push(repo, "Add conflict recovery plan")
        plan_sha = git_command(repo, "rev-parse", "origin/main")
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        connection.execute(
            "INSERT INTO projects (repo, base_branch, specification_fingerprint) "
            "VALUES (?, 'main', ?)",
            (
                str(repo),
                loop_manager.specification_input_fingerprint(repo, "main"),
            ),
        )
        connection.commit()
        relative_plan = plan.relative_to(repo).as_posix()
        loop_manager.register_execplans(
            connection,
            str(repo),
            repo,
            [(relative_plan, loop_manager._parse_execplan(repo, plan))],
            plan_sha,
            loop_manager.planning_input_fingerprint(repo, "main"),
        )
        with connection:
            planning_input_state.record_general(
                connection, str(repo), plan_sha,
                loop_manager.plan_declarations(repo, plan_sha),
            )
        branch, worktree = loop_manager.expected_execplan_workspace(
            state_dir, str(repo), "G-026"
        )
        loop_manager.make_worktree(repo, worktree, branch, "main")
        (worktree / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
        git_command(worktree, "add", "src/app.py")
        git_command(worktree, "commit", "-m", "Implement owned PR")
        git_command(
            worktree,
            "push",
            "-u",
            "origin",
            f"HEAD:refs/heads/{branch}",
        )
        head_oid = git_command(worktree, "rev-parse", "HEAD")
        pr_url = "https://example.test/pr/26"
        with connection:
            connection.execute(
                """
                UPDATE exec_plans
                SET status = 'PR_OPEN', active_sha = ?,
                    validated_base_sha = ?, publication_remote_oid = ?,
                    branch = ?, worktree = ?, workspace_owned = 1,
                    pr_url = ?, pr_head_oid = ?,
                    worker_session_id = 'worker-existing',
                    reviewer_session_id = 'reviewer-existing',
                    worker_result = ?, verify_result = 'verify: PASS',
                    review_result = 'VERDICT: PASS'
                WHERE repo = ? AND plan_id = 'G-026'
                """,
                (
                    plan_sha,
                    plan_sha,
                    head_oid,
                    branch,
                    str(worktree),
                    pr_url,
                    head_oid,
                    worker_result(
                        "Implement owned PR",
                        "Creates the owned PR fixture.",
                        "AutomaticOwnedPrConflictRecoveryTest: PASS",
                    ),
                    str(repo),
                ),
            )
            connection.execute(
                """
                UPDATE projects
                SET active_sha = ?, validated_base_sha = ?,
                    publication_remote_oid = ?, plan_sha = ?,
                    phase = 'PR_OPEN', branch = ?, worktree = ?,
                    plan_path = ?, pr_url = ?, pr_head_oid = ?,
                    worker_session_id = 'worker-existing',
                    reviewer_session_id = 'reviewer-existing',
                    worker_result = ?, verify_result = 'verify: PASS',
                    review_result = 'VERDICT: PASS'
                WHERE repo = ?
                """,
                (
                    plan_sha,
                    plan_sha,
                    head_oid,
                    plan_sha,
                    branch,
                    str(worktree),
                    relative_plan,
                    pr_url,
                    head_oid,
                    worker_result(
                        "Implement owned PR",
                        "Creates the owned PR fixture.",
                        "AutomaticOwnedPrConflictRecoveryTest: PASS",
                    ),
                    str(repo),
                ),
            )
        seed_validated_candidate(connection, repo, worktree, 'G-026')
        return (
            repo,
            state_dir,
            connection,
            branch,
            worktree,
            pr_url,
            head_oid,
        )

    def _advance_base(self, directory: str, repo: Path, relative_path: str,
                      contents: str, message: str) -> str:
        return advance_remote_file(directory, repo, relative_path, contents, message)

    def _pull_request(
        self,
        branch: str,
        pr_url: str,
        head_oid: str,
        mergeable: str,
        *,
        state: str = "OPEN",
    ) -> dict[str, object]:
        return {
            "url": pr_url,
            "body": "",
            "state": state,
            "mergedAt": None,
            "headRefOid": head_oid,
            "headRefName": branch,
            "baseRefName": "main",
            "mergeable": mergeable,
        }

    def _make_source_checkout_dirty_and_ahead(self, repo: Path) -> None:
        (repo / "local-ahead.txt").write_text(
            "local main commit\n", encoding="utf-8"
        )
        git_command(repo, "add", "local-ahead.txt")
        git_command(repo, "commit", "-m", "Keep local main ahead")
        (repo / "README.md").write_text(
            "# Staged source change\n", encoding="utf-8"
        )
        git_command(repo, "add", "README.md")
        (repo / "config/settings.ini").write_text(
            "setting=source-dirty\n", encoding="utf-8"
        )
        (repo / "source-untracked.txt").write_text(
            "preserve me\n", encoding="utf-8"
        )

    def _source_checkout_snapshot(self, repo: Path) -> tuple[object, ...]:
        paths = (
            "README.md",
            "config/settings.ini",
            "local-ahead.txt",
            "source-untracked.txt",
        )
        return (
            git_command(repo, "rev-parse", "HEAD"),
            git_command(repo, "symbolic-ref", "--short", "HEAD"),
            git_command(repo, "rev-parse", "refs/heads/main"),
            git_command(repo, "write-tree"),
            git_command(
                repo, "status", "--porcelain=v1", "--untracked-files=all"
            ),
            tuple((path, (repo / path).read_bytes()) for path in paths),
        )
