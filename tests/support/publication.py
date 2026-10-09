import json
import sqlite3
import subprocess
from pathlib import Path
from unittest import mock
import loop_manager
from loop_manager_internal.execution.execution_revision import DefinitionSnapshot
from loop_manager_internal.agents.common_guidance import plan_content_hash
from loop_manager_internal.storage import planning_input_state
from tests.support.catalog import write_execplan
from tests.support.git import git_command, make_remote_repository, commit_and_push
from tests.support.worker import worker_result
from tests.support.lifecycle import register_connection_cleanup
from tests.support.fixed_samples import PROJECT_FILES


def seed_validated_candidate(
    connection: sqlite3.Connection, repo: Path, worktree: Path, plan_id: str,
) -> DefinitionSnapshot:
    """Seed exact candidate evidence; return the definition for publication tokens."""

    row = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
        (str(repo), plan_id),
    ).fetchone()
    base_sha = git_command(repo, "rev-parse", "origin/main")
    candidate_head = git_command(worktree, "rev-parse", "HEAD")
    body = loop_manager._revision_execplan_body_hash(repo, base_sha, str(row["path"]))
    with mock.patch('loop_manager_internal.agents.common_guidance.RUNTIME_ROOT', PROJECT_FILES):
        guidance_hash = plan_content_hash(connection, str(repo), plan_id)
    snapshot = DefinitionSnapshot(
        revision_sha=base_sha,
        path=str(row["path"]),
        title=str(row["title"]),
        sources=tuple(json.loads(str(row["sources"]))),
        depends_on=tuple(json.loads(str(row["depends_on"]))),
        complexity=str(row["current_complexity"]),
        risk=str(row["current_risk"]),
        priority=str(row["priority"]),
        body_hash=str(body),
        body_text=getattr(body, 'body_text', None),
        source_fingerprint=loop_manager._revision_execplan_source_fingerprint(
            repo, base_sha, row,
        ),
        archive_candidates=(),
        guidance_hash=guidance_hash,
    )
    verify_output = str(row['verify_result'] or 'validated candidate: OK')
    values = (
        snapshot.serialize(), snapshot.worker_token, snapshot.archive_token,
        verify_output,
        snapshot.verify_token(base_sha, candidate_head, verify_output),
        snapshot.review_token(base_sha, candidate_head),
    )
    with connection:
        connection.execute(
            "UPDATE exec_plans SET definition_snapshot = ?, worker_input_token = ?, "
            "archive_token = ?, verify_result = ?, verify_token = ?, review_token = ? "
            "WHERE repo = ? AND plan_id = ?",
            (*values, str(repo), plan_id),
        )
        connection.execute(
            "UPDATE projects SET definition_snapshot = ?, worker_input_token = ?, "
            "archive_token = ?, verify_result = ?, verify_token = ?, review_token = ? WHERE repo = ?",
            (*values, str(repo)),
        )
    return snapshot

def matching_branch_pr_lookup(responses):
    """Model a PR on the current branch when a publication fixture omits HEAD.

    Resolve lazily: the Worker may commit or rebase before GitHub is queried.
    Explicit head values remain untouched for stale/external-head scenarios.
    """
    iterator = None if callable(responses) else iter(responses)

    def lookup(repo, branch, *args, **kwargs):
        response = (responses(repo, branch, *args, **kwargs)
                    if callable(responses) else next(iterator))
        if response is None or 'headRefOid' in response:
            return response
        return {
            **response,
            'headRefName': response.get('headRefName', branch),
            'headRefOid': git_command(repo, 'rev-parse', f'refs/heads/{branch}'),
        }

    return lookup

def publishing_branch_pr_lookup(connection, url):
    """Expose an existing PR throughout publication, independent of query count."""
    def response(repo, branch, *_args, **_kwargs):
        row = connection.execute(
            "SELECT phase FROM projects WHERE repo = ? AND branch = ?",
            (str(repo), branch),
        ).fetchone()
        if row is not None and row[0] in {'PUBLISHING', 'PR_OPEN'}:
            return {'url': url, 'state': 'OPEN'}
        return None

    return matching_branch_pr_lookup(response)


class PublicationFixture:
    """Shared scenario setup; inheriting this fixture collects no tests."""

    def _publication_state(self, directory: str):
        repo = make_remote_repository(directory)
        state_dir = Path(directory) / "state"
        state_dir.mkdir()
        (repo / "docs/plans").mkdir()
        plan = write_execplan(
            repo,
            "G-014",
            "Latest Base Publication",
            complexity="high",
            risk="high",
            priority="critical",
        )
        commit_and_push(repo, "Add latest-base publication plan")
        plan_sha = git_command(repo, "rev-parse", "origin/main")
        connection = sqlite3.connect(":memory:")
        close_connection = register_connection_cleanup(self, connection)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
                (str(repo),),
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
                state_dir, str(repo), "G-014"
            )
            loop_manager.make_worktree(repo, worktree, branch, "main")
            (worktree / "src/app.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )
            git_command(worktree, "add", "src/app.py")
            git_command(worktree, "commit", "-m", "Change application value")
            result = worker_result(
                "Publish verified application value",
                "Updates the application value after latest-base validation.",
                "LatestBasePublicationTest: PASS",
            )
            with connection:
                connection.execute(
                    """
                    UPDATE exec_plans
                    SET status = 'PUBLISHING', active_sha = ?,
                        validated_base_sha = ?, branch = ?, worktree = ?,
                        workspace_owned = 1, worker_result = ?,
                        verify_result = 'verify: PASS',
                        review_result = 'VERDICT: PASS'
                    WHERE repo = ? AND plan_id = 'G-014'
                    """,
                    (
                        plan_sha,
                        plan_sha,
                        branch,
                        str(worktree),
                        result,
                        str(repo),
                    ),
                )
                connection.execute(
                    """
                    UPDATE projects
                    SET active_sha = ?, validated_base_sha = ?, plan_sha = ?,
                        phase = 'PUBLISHING', branch = ?, worktree = ?,
                        plan_path = ?, worker_result = ?,
                        verify_result = 'verify: PASS',
                        review_result = 'VERDICT: PASS'
                    WHERE repo = ?
                    """,
                    (
                        plan_sha,
                        plan_sha,
                        plan_sha,
                        branch,
                        str(worktree),
                        relative_plan,
                        result,
                        str(repo),
                    ),
                )
        except BaseException:
            close_connection()
            raise
        return repo, state_dir, connection, plan, branch, worktree

    def _command_stub(self, created_urls, git_calls=None):
        original = loop_manager.command

        def run(arguments, **kwargs):
            if arguments[:3] == ["gh", "pr", "create"]:
                created_urls.append(arguments)
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    stdout="https://example.test/pr/14\n",
                )
            if git_calls is not None and arguments[:3] == ["git", "-C", str(kwargs.get("cwd"))]:
                git_calls.append(arguments)
            return original(arguments, **kwargs)

        return run
