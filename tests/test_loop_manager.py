import hashlib
import json
import io
import os
import datetime
import contextlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import loop_manager
import scoped_blocking
import execplan_catalog
from execution_revision import DefinitionSnapshot
from worker_outcome import parse_worker_outcome
from worker_session import SessionBinding


_real_agent_profile = loop_manager._agent_profile
_real_execution_ports = loop_manager._ExecutionLifecyclePorts
_dummy_profile_patch = None
_saved_session_patch = None


def _profile_for_dummy_worker_config(config_path, provider, role_name, level, worktree):
    """Agent fakes use unused.ini; give their new preflight a resolved identity."""
    if role_name == "worker" and Path(config_path).name == "unused.ini" and not Path(config_path).exists():
        return loop_manager.AgentProfile("fixture-worker-model", "medium")
    return _real_agent_profile(config_path, provider, role_name, level, worktree)


def _ports_with_bound_dummy_sessions(connection, repository, **identity):
    # Earlier runtime fixtures predate G-041 and store only the fake session
    # ID. Bind that ID to the fake provider profile when exercising a resume.
    if Path(identity["config_path"]).name == "unused.ini":
        saved = repository.projected(identity["repo"])
        if saved and saved.worker_session_id and saved.worker_session_binding is None:
            repository.update_project(identity["repo"], {
                "worker_session_binding": SessionBinding(
                    identity["provider"], "fixture-worker-model", "medium",
                ).serialize(),
            }, enqueue=False)
    return _real_execution_ports(connection, repository, **identity)


def setUpModule():
    global _dummy_profile_patch, _saved_session_patch
    _dummy_profile_patch = mock.patch.object(
        loop_manager, "_agent_profile", side_effect=_profile_for_dummy_worker_config,
    )
    _dummy_profile_patch.start()
    _saved_session_patch = mock.patch.object(
        loop_manager, "_ExecutionLifecyclePorts", side_effect=_ports_with_bound_dummy_sessions,
    )
    _saved_session_patch.start()


def tearDownModule():
    _saved_session_patch.stop()
    _dummy_profile_patch.stop()


def make_catalog_root(directory: str) -> Path:
    root = Path(directory)
    (root / "docs/plans").mkdir(parents=True)
    (root / "docs/PRODUCT.md").write_text("# Product\n", encoding="utf-8")
    return root


def write_execplan(
    root: Path,
    plan_id: str,
    title: str,
    *,
    sources: tuple[str, ...] = ("docs/PRODUCT.md",),
    depends_on: tuple[str, ...] = (),
    complexity: str = "low",
    risk: str = "low",
    priority: str | None = "normal",
) -> Path:
    source_lines = "sources: []" if not sources else "sources:\n" + "".join(
        f"  - {source}\n" for source in sources
    ).rstrip("\n")
    dependency_lines = "depends_on: []" if not depends_on else "depends_on:\n" + "".join(
        f"  - {dependency}\n" for dependency in depends_on
    ).rstrip("\n")
    priority_line = f"priority: {priority}\n" if priority is not None else ""
    plan = root / "docs/plans" / f"[{plan_id}] {title}.md"
    plan.write_text(
        f"""---
id: {plan_id}
title: {title}
{source_lines}
{dependency_lines}
complexity: {complexity}
risk: {risk}
{priority_line}---

## Purpose

Test plan.
""",
        encoding="utf-8",
    )
    return plan



def worker_result(
    title: str = "Preserve worker test evidence",
    summary: str = "Stores the Worker result for review.",
    tests: str = "targeted test: PASS",
) -> str:
    return (
        'WORKER_OUTCOME: {"kind":"ready_for_validation"}\n'
        f"COMMIT_TITLE: {title}\nPR_SUMMARY: {summary}\n"
        f"TARGETED_TESTS:\n{tests}"
    )

def git_command(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return result.stdout.strip()


def seed_validated_candidate(
    connection: sqlite3.Connection, repo: Path, worktree: Path, plan_id: str,
) -> None:
    """Give a publication fixture evidence for its exact base and HEAD."""

    row = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
        (str(repo), plan_id),
    ).fetchone()
    base_sha = git_command(repo, "rev-parse", "origin/main")
    candidate_head = git_command(worktree, "rev-parse", "HEAD")
    snapshot = DefinitionSnapshot(
        revision_sha=base_sha,
        path=str(row["path"]),
        title=str(row["title"]),
        sources=tuple(json.loads(str(row["sources"]))),
        depends_on=tuple(json.loads(str(row["depends_on"]))),
        complexity=str(row["current_complexity"]),
        risk=str(row["current_risk"]),
        priority=str(row["priority"]),
        body_hash=loop_manager._revision_execplan_body_hash(
            repo, base_sha, str(row["path"]),
        ),
        source_fingerprint=loop_manager._revision_execplan_source_fingerprint(
            repo, base_sha, row,
        ),
        archive_candidates=(),
    )
    values = (
        snapshot.serialize(), snapshot.worker_token, snapshot.archive_token,
        snapshot.verify_token(base_sha, candidate_head),
        snapshot.review_token(base_sha, candidate_head),
    )
    with connection:
        connection.execute(
            "UPDATE exec_plans SET definition_snapshot = ?, worker_input_token = ?, "
            "archive_token = ?, verify_token = ?, review_token = ? "
            "WHERE repo = ? AND plan_id = ?",
            (*values, str(repo), plan_id),
        )
        connection.execute(
            "UPDATE projects SET definition_snapshot = ?, worker_input_token = ?, "
            "archive_token = ?, verify_token = ?, review_token = ? WHERE repo = ?",
            (*values, str(repo)),
        )


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


class PublicationFixtureTest(unittest.TestCase):
    def test_matching_pr_reads_head_only_when_queried(self):
        with mock.patch(__name__ + '.git_command', side_effect=['first', 'rebased']) as git:
            lookup = matching_branch_pr_lookup([None, {'url': 'pr'}, {'url': 'pr'}])
            git.assert_not_called()
            self.assertIsNone(lookup(Path('/repo'), 'feature'))
            git.assert_not_called()
            self.assertEqual(lookup(Path('/repo'), 'feature')['headRefOid'], 'first')
            self.assertEqual(lookup(Path('/repo'), 'feature')['headRefOid'], 'rebased')
            self.assertEqual(git.call_args_list, [
                mock.call(Path('/repo'), 'rev-parse', 'refs/heads/feature'),
                mock.call(Path('/repo'), 'rev-parse', 'refs/heads/feature'),
            ])

    def test_callable_fixture_preserves_explicit_stale_head(self):
        response = {'url': 'pr', 'headRefName': 'other', 'headRefOid': 'stale'}
        source = mock.Mock(return_value=response)
        with mock.patch(__name__ + '.git_command') as git:
            lookup = matching_branch_pr_lookup(source)
            self.assertIs(lookup(Path('/repo'), 'feature', 'saved-url'), response)
            source.assert_called_once_with(Path('/repo'), 'feature', 'saved-url')
            git.assert_not_called()


def make_remote_repository(directory: str) -> Path:
    parent = Path(directory)
    remote = parent / "remote.git"
    repo = parent / "repo"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    repo.mkdir()
    git_command(repo, "init", "--initial-branch=main")
    git_command(repo, "config", "user.email", "loop-manager@example.com")
    git_command(repo, "config", "user.name", "Loop Manager Test")
    git_command(repo, "remote", "add", "origin", str(remote))

    files = {
        "AGENTS.md": "# Agents\n",
        "README.md": "# Readme\n",
        "config/settings.ini": "setting=true\n",
        "docs/ARCHITECTURE.md": "# Architecture\n",
        "docs/PLANS.md": "# Plans\n",
        "docs/PRODUCT.md": "# Product\n",
        "docs/notes.md": "# Notes\n",
        "docs/requirements/Active.md": "# Active\n",
        "docs/requirements/archive/Old.md": "# Old\n",
        "src/app.py": "VALUE = 1\n",
        "tests/test_app.py": "def test_app(): pass\n",
    }
    for relative_path, contents in files.items():
        path = repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    git_command(repo, "add", "-A")
    git_command(repo, "commit", "-m", "Initial")
    git_command(repo, "push", "-u", "origin", "main")
    return repo


def commit_and_push(repo: Path, message: str) -> None:
    git_command(repo, "add", "-A")
    git_command(repo, "commit", "-m", message)
    git_command(repo, "push", "origin", "HEAD:main")


class PinnedPublishedPlanRegistrationTest(unittest.TestCase):
    def test_registration_uses_pinned_tree_when_checkout_is_absent(self):
        sha = "a" * 40
        path = "docs/plans/[G-001] Remote.md"
        text = (
            "---\nid: G-001\ntitle: Remote\nsources:\n"
            "  - docs/PRODUCT.md\ndepends_on: []\ncomplexity: high\n"
            "risk: critical\npriority: critical\n---\n\n## Purpose\n\nRemote.\n"
        )
        snapshot = execplan_catalog.snapshot_from_documents(
            {path: text}, revision_sha=sha,
        )
        tree = (
            b"100644 blob " + b"1" * 40 + b"\tdocs/PRODUCT.md\0"
            + b"100644 blob " + b"2" * 40 + b"\t" + path.encode() + b"\0"
        )
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES ('/missing-checkout', 'main')"
        )
        port = loop_manager._GitCatalogDefinitionPort(
            Path("/missing-checkout"), "refs/remotes/origin/main",
            lambda: frozenset(), pinned_revision=sha,
        )
        with (
            mock.patch.object(loop_manager, "revision_execplan_snapshot", return_value=snapshot),
            mock.patch.object(loop_manager, "git_bytes", return_value=tree),
            mock.patch.object(loop_manager, "git", return_value=mock.Mock(stdout=sha)),
        ):
            result = loop_manager.execplan_catalog_state.CatalogReconciliationService(
                loop_manager.execplan_catalog_state.DefinitionStateRepository(connection),
                port,
            ).reconcile("/missing-checkout")
        self.assertEqual(
            result.kind, loop_manager.execplan_catalog_state.ReconciliationKind.RECONCILED
        )
        row = connection.execute(
            "SELECT content_hash, publication_sha, initial_worker_level, status "
            "FROM exec_plans WHERE plan_id = 'G-001'"
        ).fetchone()
        self.assertEqual(row["content_hash"], execplan_catalog.plan_content_hash(text))
        self.assertEqual(row["publication_sha"], sha)
        self.assertEqual(row["initial_worker_level"], 7)
        self.assertEqual(row["status"], "READY")


class AutomationFirstCatalogTest(unittest.TestCase):
    def _connection(self, repo: str = "repo") -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
            (repo,),
        )
        connection.commit()
        return connection

    def _insert_plan(
        self,
        connection: sqlite3.Connection,
        plan_id: str,
        status: str,
        *,
        priority: str = "normal",
        depends_on: tuple[str, ...] = (),
        repo: str = "repo",
        title: str | None = None,
        plan_sha: str = "plan-sha",
    ) -> None:
        title = title or f"Plan {plan_id}"
        connection.execute(
            """
            INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on, priority,
                content_hash, status, plan_sha
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                repo,
                plan_id,
                f"docs/plans/[{plan_id}] {title}.md",
                title,
                '["docs/PRODUCT.md"]',
                json.dumps(depends_on),
                priority,
                f"hash-{plan_id}",
                status,
                plan_sha,
            ),
        )
        connection.commit()

    def test_priority_parser_supports_legacy_default_and_rejects_invalid_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_catalog_root(directory)
            legacy = write_execplan(root, "G-001", "Legacy", priority=None)
            invalid = write_execplan(root, "G-002", "Invalid", priority="urgent")

            self.assertEqual(
                loop_manager._parse_execplan(root, legacy)["priority"],
                "normal",
            )
            with self.assertRaisesRegex(RuntimeError, "priority"):
                loop_manager._parse_execplan(root, legacy, require_priority=True)
            with self.assertRaisesRegex(RuntimeError, "priority"):
                loop_manager._parse_execplan(root, invalid)

    def test_execplan_catalog_is_deterministic_and_indexed_by_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_catalog_root(directory)
            write_execplan(root, "G-003", "Active")
            archived = write_execplan(root, "G-001", "Historical")
            archive_path = root / "docs/plans/archive" / archived.name
            archive_path.parent.mkdir()
            archived.rename(archive_path)

            catalog = loop_manager.load_execplan_catalog(root)
            snapshot = loop_manager.load_execplan_snapshot(root)

        self.assertEqual(list(catalog), ["G-003"])
        self.assertEqual(list(snapshot.all), ["G-001", "G-003"])
        self.assertEqual(snapshot.next_plan_id, "G-004")

    def test_legacy_database_migration_reconciles_only_confirmed_merge_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_remote_repository(directory)
            (root / "docs/plans").mkdir()
            write_execplan(root, "G-001", "Completed", priority=None)
            write_execplan(root, "G-002", "Unconfirmed")
            active = write_execplan(root, "G-003", "Active", priority="high")
            commit_and_push(root, "Add legacy plans")
            plan_sha = git_command(root, "rev-parse", "HEAD")
            write_execplan(root, "G-004", "Local Only")
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            database = sqlite3.connect(state_dir / "loop-manager.db")
            database.execute(loop_manager.SCHEMA)
            database.execute(
                """
                INSERT INTO projects (
                    repo, base_branch, phase, plan_path, plan_sha, active_sha,
                    branch, worktree, pr_url, worker_session_id
                ) VALUES (?, 'main', 'PR_OPEN', ?, ?, 'base-sha',
                          'loop-manager/G-003', 'managed', 'https://pr/3', 'worker-session')
                """,
                (str(root), active.relative_to(root).as_posix(), plan_sha),
            )
            database.commit()
            database.close()

            with loop_manager.connect_database(state_dir) as connection:
                loop_manager.migrate_legacy_execplans(connection, target_repo=str(root))
                before_reconciliation = connection.execute(
                    "SELECT plan_id, status FROM exec_plans WHERE repo = ? ORDER BY plan_id",
                    (str(root),),
                ).fetchall()
                looked_up = []

                def history(_repo, branch, _url=None):
                    looked_up.append(branch)
                    if branch == "loop-manager/G-001":
                        return {
                            "url": "https://pr/1",
                            "state": "MERGED",
                            "mergedAt": "2026-01-01T00:00:00Z",
                        }
                    return None

                with mock.patch("loop_manager.pr_for_branch", side_effect=history):
                    loop_manager.migrate_legacy_execplans(
                        connection,
                        reconcile_history=True,
                        target_repo=str(root),
                    )
                rows = connection.execute(
                    """
                    SELECT plan_id, status, priority, plan_sha, pr_url, worker_session_id
                    FROM exec_plans WHERE repo = ? ORDER BY plan_id
                    """,
                    (str(root),),
                ).fetchall()

        self.assertEqual(
            [tuple(row) for row in before_reconciliation],
            [("G-003", "PR_OPEN")],
        )
        self.assertEqual(
            looked_up,
            ["loop-manager/G-001", "loop-manager/G-002"],
        )
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                ("G-001", "MERGED", "normal", plan_sha, "https://pr/1", None),
                ("G-002", "READY", "normal", plan_sha, None, None),
                ("G-003", "PR_OPEN", "high", plan_sha, "https://pr/3", "worker-session"),
            ],
        )

    def test_legacy_migration_preserves_existing_active_publication_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_remote_repository(directory)
            (root / "docs/plans").mkdir()
            write_execplan(root, "G-001", "Stable Active")
            commit_and_push(root, "Add stable active plan")
            connection = self._connection(str(root))
            self._insert_plan(
                connection,
                "G-001",
                "READY",
                repo=str(root),
                title="Stable Active",
                plan_sha="stored-publication-sha",
            )

            with mock.patch("loop_manager.pr_for_branch") as lookup:
                loop_manager.migrate_legacy_execplans(
                    connection,
                    reconcile_history=True,
                    target_repo=str(root),
                )

            row = connection.execute(
                "SELECT path, content_hash, plan_sha, status "
                "FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(root),),
            ).fetchone()
            connection.close()

        lookup.assert_not_called()
        self.assertEqual(row["path"], "docs/plans/[G-001] Stable Active.md")
        self.assertEqual(row["content_hash"], "hash-G-001")
        self.assertEqual(row["plan_sha"], "stored-publication-sha")
        self.assertEqual(row["status"], "READY")

    def test_restored_active_plan_is_not_hidden_by_stale_archive_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_remote_repository(directory)
            (root / "docs/plans").mkdir()
            active = write_execplan(root, "G-001", "Restored Active")
            commit_and_push(root, "Add active plan")
            publication_sha = git_command(root, "rev-parse", "HEAD")
            archive = root / "docs/plans/archive"
            archive.mkdir()
            archived = archive / active.name
            active.rename(archived)
            commit_and_push(root, "Archive active plan")
            archived_content = archived.read_text(encoding="utf-8")
            archived.rename(active)
            commit_and_push(root, "Restore active plan")
            connection = self._connection(str(root))
            archived_entry = loop_manager.execplan_catalog.snapshot_from_documents(
                {
                    "docs/plans/archive/[G-001] Restored Active.md":
                        archived_content
                }
            ).archived["G-001"]
            repository = (
                loop_manager.execplan_catalog_state.CatalogStateRepository(
                    connection
                )
            )
            repository.record(
                str(root),
                archived_entry,
                original_path="docs/plans/[G-001] Restored Active.md",
                original_content_hash=archived_entry.content_hash,
                plan_sha=None,
                proof=loop_manager.execplan_catalog_state.MergeProof(False),
                diagnostic=loop_manager.execplan_catalog_state.CatalogDiagnostic(
                    "G-001",
                    archived_entry.path,
                    "merge-proof-missing",
                    "missing",
                ),
                identity_verified=False,
            )
            connection.execute(
                "INSERT INTO execplan_catalog_revisions "
                "(repo, revision_sha, catalog_fingerprint) VALUES (?, ?, ?)",
                (str(root), publication_sha, "initialized-archive"),
            )
            connection.commit()

            with mock.patch("loop_manager.pr_for_branch", return_value=None) as lookup:
                loop_manager.migrate_legacy_execplans(
                    connection,
                    reconcile_history=True,
                    target_repo=str(root),
                )

            history = repository.historical_row(str(root), "G-001")
            compact = json.loads(
                loop_manager.compact_execplan_catalog(connection, str(root))
            )
            decision = loop_manager.catalog_schedule_decision(
                connection, str(root)
            )
            runtime = connection.execute(
                "SELECT status, plan_sha FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(root),),
            ).fetchone()
            diagnostics = repository.diagnostics(str(root))
            diagnostic_count = connection.execute(
                "SELECT COUNT(*) FROM execplan_catalog_diagnostics "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(root),),
            ).fetchone()[0]
            with mock.patch(
                "loop_manager._run_project_step",
                return_value=loop_manager.PROJECT_STEP_PLANNING_PENDING,
            ):
                project_result = loop_manager.run_project_result(
                    connection,
                    Path(directory) / "state",
                    str(root),
                    Path(directory) / "config.json",
                    None,
                    False,
                )
            connection.close()

        lookup.assert_called_once_with(root, "loop-manager/G-001")
        self.assertEqual(history["currently_archived"], 0)
        self.assertEqual(
            history["observed_path"],
            "docs/plans/[G-001] Restored Active.md",
        )
        self.assertEqual([item["id"] for item in compact], ["G-001"])
        self.assertEqual(runtime["status"], "READY")
        self.assertEqual(runtime["plan_sha"], publication_sha)
        self.assertEqual(decision.runnable_ready["plan_id"], "G-001")
        self.assertEqual(project_result.kind, loop_manager.ProjectRunKind.WAITING)
        self.assertEqual(diagnostics, ())
        self.assertEqual(diagnostic_count, 0)

    def test_legacy_migration_recovers_archived_identity_only_with_merge_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_remote_repository(directory)
            (root / "docs/plans").mkdir()
            first = write_execplan(root, "G-001", "Confirmed")
            second = write_execplan(root, "G-002", "Unconfirmed")
            commit_and_push(root, "Add historical plans")
            publication_sha = git_command(root, "rev-parse", "HEAD")
            archive = root / "docs/plans/archive"
            archive.mkdir()
            first.rename(archive / first.name)
            second.rename(archive / second.name)
            commit_and_push(root, "Archive historical plans")

            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            loop_manager._ensure_execplans_schema(connection)
            connection.execute(
                "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
                (str(root),),
            )
            connection.commit()

            def proof(_repo, branch, _url=None):
                if branch == "loop-manager/G-001":
                    return {
                        "url": "https://pr/1",
                        "state": "MERGED",
                        "mergedAt": "2026-01-01T00:00:00Z",
                        "mergeCommit": None,
                    }
                return None

            with mock.patch("loop_manager.pr_for_branch", side_effect=proof):
                loop_manager.migrate_legacy_execplans(
                    connection,
                    reconcile_history=True,
                    target_repo=str(root),
                )

            rows = connection.execute(
                "SELECT plan_id, path, status, plan_sha FROM exec_plans "
                "WHERE repo = ? ORDER BY plan_id",
                (str(root),),
            ).fetchall()
            history = connection.execute(
                "SELECT plan_id, original_path, observed_path, merge_confirmed, "
                "diagnostic_code FROM execplan_catalog_history "
                "WHERE repo = ? ORDER BY plan_id",
                (str(root),),
            ).fetchall()
            connection.close()

        self.assertEqual(
            [tuple(row) for row in rows],
            [
                (
                    "G-001",
                    "docs/plans/[G-001] Confirmed.md",
                    "MERGED",
                    publication_sha,
                )
            ],
        )
        self.assertEqual(history[0]["merge_confirmed"], 1)
        self.assertEqual(history[0]["observed_path"], "docs/plans/archive/[G-001] Confirmed.md")
        self.assertEqual(history[1]["merge_confirmed"], 0)
        self.assertEqual(history[1]["diagnostic_code"], "merge-proof-missing")

    def test_fresh_recovery_rejects_archive_tampered_before_first_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_remote_repository(directory)
            (root / "docs/plans").mkdir()
            active = write_execplan(root, "G-001", "Tampered Archive")
            original_hash = hashlib.sha256(active.read_bytes()).hexdigest()
            commit_and_push(root, "Publish plan")
            publication_sha = git_command(root, "rev-parse", "HEAD")
            archive = root / "docs/plans/archive"
            archive.mkdir()
            archived = archive / active.name
            active.rename(archived)
            archived.write_text(
                archived.read_text(encoding="utf-8")
                + "\nTampered before first recovery.\n",
                encoding="utf-8",
            )
            tampered_hash = hashlib.sha256(archived.read_bytes()).hexdigest()
            commit_and_push(root, "Archive tampered plan")

            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            loop_manager._ensure_execplans_schema(connection)
            connection.execute(
                "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
                (str(root),),
            )
            connection.commit()
            pull_request = {
                "url": "https://pr/1",
                "state": "MERGED",
                "mergedAt": "2026-01-01T00:00:00Z",
                "mergeCommit": None,
            }

            with mock.patch(
                "loop_manager.pr_for_branch", return_value=pull_request
            ):
                loop_manager.migrate_legacy_execplans(
                    connection,
                    reconcile_history=True,
                    target_repo=str(root),
                )

            runtime = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(root),),
            ).fetchone()
            history = connection.execute(
                "SELECT original_content_hash, final_content_hash, plan_sha, "
                "identity_verified, merge_confirmed, diagnostic_code "
                "FROM execplan_catalog_history "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(root),),
            ).fetchone()
            connection.close()

        self.assertIsNone(runtime)
        self.assertEqual(history["original_content_hash"], original_hash)
        self.assertEqual(history["final_content_hash"], tampered_hash)
        self.assertEqual(history["plan_sha"], publication_sha)
        self.assertEqual(history["identity_verified"], 1)
        self.assertEqual(history["merge_confirmed"], 0)
        self.assertEqual(
            history["diagnostic_code"],
            "archived-content-hash-mismatch",
        )

    def test_archived_merged_row_recovers_from_stored_state_and_git_ancestry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_remote_repository(directory)
            (root / "docs/plans").mkdir()
            active = write_execplan(root, "G-001", "Stored Merge")
            commit_and_push(root, "Add stored merge plan")
            publication_sha = git_command(root, "rev-parse", "HEAD")
            publication_hash = hashlib.sha256(active.read_bytes()).hexdigest()
            active.write_text(
                active.read_text(encoding="utf-8")
                + "\nReconciled before archive.\n",
                encoding="utf-8",
            )
            commit_and_push(root, "Revise stored merge plan")
            current_hash = hashlib.sha256(active.read_bytes()).hexdigest()
            archive = root / "docs/plans/archive"
            archive.mkdir()
            archived = archive / active.name
            active.rename(archived)
            commit_and_push(root, "Archive stored merge plan")
            self.assertEqual(
                hashlib.sha256(archived.read_bytes()).hexdigest(),
                current_hash,
            )

            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            loop_manager._ensure_execplans_schema(connection)
            connection.execute(
                "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
                (str(root),),
            )
            connection.execute(
                """
                INSERT INTO exec_plans (
                    repo, plan_id, path, title, sources, depends_on, priority,
                    content_hash, publication_path,
                    publication_content_hash, publication_sha,
                    status, plan_sha
                ) VALUES (?, 'G-001', ?, 'Stored Merge',
                          '["docs/PRODUCT.md"]', '[]', 'normal', ?, ?, ?, ?,
                          'MERGED', ?)
                """,
                (
                    str(root),
                    "docs/plans/[G-001] Stored Merge.md",
                    current_hash,
                    "docs/plans/[G-001] Stored Merge.md",
                    publication_hash,
                    publication_sha,
                    publication_sha,
                ),
            )
            connection.commit()

            with mock.patch("loop_manager.pr_for_branch", return_value=None):
                loop_manager.migrate_legacy_execplans(
                    connection,
                    reconcile_history=True,
                    target_repo=str(root),
                )

            history = connection.execute(
                "SELECT merge_confirmed, proof_kind, plan_sha "
                "FROM execplan_catalog_history WHERE repo = ? AND plan_id = 'G-001'",
                (str(root),),
            ).fetchone()
            connection.close()

        self.assertEqual(history["merge_confirmed"], 1)
        self.assertEqual(history["proof_kind"], "stored-and-git")
        self.assertEqual(history["plan_sha"], publication_sha)

    def test_unverified_archived_active_runtime_is_quarantined(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_remote_repository(directory)
            (root / "docs/plans").mkdir()
            active = write_execplan(root, "G-001", "Interrupted")
            commit_and_push(root, "Add interrupted plan")
            publication_sha = git_command(root, "rev-parse", "HEAD")
            archive = root / "docs/plans/archive"
            archive.mkdir()
            archived = archive / active.name
            active.rename(archived)
            commit_and_push(root, "Archive interrupted plan")
            content_hash = hashlib.sha256(archived.read_bytes()).hexdigest()
            active_path = "docs/plans/[G-001] Interrupted.md"

            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            loop_manager._ensure_execplans_schema(connection)
            connection.execute(
                """
                INSERT INTO projects (
                    repo, base_branch, phase, plan_path, plan_sha,
                    branch, worktree
                ) VALUES (?, 'main', 'WORKING', ?, ?,
                          'loop-manager/G-001', '/tmp/interrupted')
                """,
                (str(root), active_path, publication_sha),
            )
            connection.execute(
                """
                INSERT INTO exec_plans (
                    repo, plan_id, path, title, sources, depends_on, priority,
                    content_hash, status, plan_sha, branch, worktree
                ) VALUES (?, 'G-001', ?, 'Interrupted',
                          '["docs/PRODUCT.md"]', '[]', 'normal', ?,
                          'IMPLEMENTING', ?, 'loop-manager/G-001', '/tmp/interrupted')
                """,
                (str(root), active_path, content_hash, publication_sha),
            )
            connection.commit()

            with mock.patch("loop_manager.pr_for_branch", return_value=None):
                loop_manager.migrate_legacy_execplans(
                    connection,
                    reconcile_history=True,
                    target_repo=str(root),
                )

            plan = connection.execute(
                "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(root),),
            ).fetchone()
            project = connection.execute(
                "SELECT phase, plan_path FROM projects WHERE repo = ?",
                (str(root),),
            ).fetchone()
            history = connection.execute(
                "SELECT merge_confirmed, diagnostic_code "
                "FROM execplan_catalog_history WHERE repo = ? AND plan_id = 'G-001'",
                (str(root),),
            ).fetchone()
            connection.close()

        self.assertEqual(plan["status"], "PLAN_BLOCKED")
        self.assertIsNone(project["phase"])
        self.assertIsNone(project["plan_path"])
        self.assertEqual(history["merge_confirmed"], 0)
        self.assertEqual(history["diagnostic_code"], "merge-proof-missing")

    def test_compact_catalog_excludes_merged_bodies_and_has_only_allowed_fields(self):
        connection = self._connection()
        self._insert_plan(connection, "G-010", "MERGED", priority="critical")
        self._insert_plan(
            connection,
            "G-002",
            "READY",
            priority="high",
            depends_on=("G-001",),
        )

        catalog = json.loads(loop_manager.compact_execplan_catalog(connection, "repo"))
        prompt = loop_manager.planner_prompt(None, json.dumps(catalog))

        self.assertEqual([item["id"] for item in catalog], ["G-002"])
        self.assertEqual(
            set(catalog[0]),
            {"id", "title", "status", "sources", "depends_on", "priority"},
        )
        self.assertNotIn("G-010", prompt)
        self.assertIn("G-002", prompt)
        self.assertIn("기존 docs/plans 전체를 기본 입력으로 읽지 마라", prompt)

    def test_batch_validation_requires_new_consecutive_priority_plans(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            (repo / "docs/plans").mkdir()
            write_execplan(repo, "G-001", "Existing", priority=None)
            commit_and_push(repo, "Add existing plan")
            second = write_execplan(repo, "G-002", "Second", priority="high")
            third = write_execplan(
                repo,
                "G-003",
                "Third",
                priority="critical",
                depends_on=("G-002",),
            )

            plans = loop_manager.validate_planner_result(
                repo,
                git_command(repo, "rev-parse", "HEAD"),
                [
                    second.relative_to(repo).as_posix(),
                    third.relative_to(repo).as_posix(),
                ],
            )

            self.assertEqual([plan["id"] for _path, plan in plans.plans], ["G-002", "G-003"])

        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            (repo / "docs/plans").mkdir()
            write_execplan(repo, "G-001", "Existing")
            commit_and_push(repo, "Add existing plan")
            gap = write_execplan(repo, "G-003", "Gap")

            with self.assertRaisesRegex(loop_manager.PlannerValidationError, "연속"):
                loop_manager.validate_planner_result(
                    repo,
                    git_command(repo, "rev-parse", "HEAD"),
                    [gap.relative_to(repo).as_posix()],
                )

    def test_batch_validation_allocates_after_archived_maximum_id(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            (repo / "docs/plans/archive").mkdir(parents=True)
            archived = repo / "docs/plans/archive/[G-009] Historical.md"
            archived.write_text(
                write_execplan(repo, "G-009", "Historical").read_text(
                    encoding="utf-8"
                ),
                encoding="utf-8",
            )
            (repo / "docs/plans/[G-009] Historical.md").unlink()
            commit_and_push(repo, "Add archived catalog maximum")
            planned = write_execplan(repo, "G-010", "Next Active")

            result = loop_manager.validate_planner_result(
                repo,
                git_command(repo, "rev-parse", "HEAD"),
                [planned.relative_to(repo).as_posix()],
            )

        self.assertEqual(
            [str(plan["id"]) for _path, plan in result.plans],
            ["G-010"],
        )

    def test_batch_registration_is_atomic_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_catalog_root(directory)
            second_path = write_execplan(root, "G-002", "Second", priority="high")
            third_path = write_execplan(root, "G-003", "Third", priority="normal")
            plans = [
                (second_path.relative_to(root).as_posix(), loop_manager._parse_execplan(root, second_path)),
                (third_path.relative_to(root).as_posix(), loop_manager._parse_execplan(root, third_path)),
            ]
            connection = self._connection(str(root))
            connection.execute("UPDATE projects SET phase = 'PLANNING' WHERE repo = ?", (str(root),))
            self._insert_plan(connection, "G-003", "MERGED", repo=str(root))

            with self.assertRaisesRegex(RuntimeError, "충돌"):
                loop_manager.register_execplans(
                    connection,
                    str(root),
                    root,
                    plans,
                    "published-sha",
                    "fingerprint",
                )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM exec_plans WHERE repo = ? AND plan_id = 'G-002'",
                    (str(root),),
                ).fetchone()
            )

            connection.execute(
                "DELETE FROM exec_plans WHERE repo = ? AND plan_id = 'G-003'",
                (str(root),),
            )
            connection.commit()
            loop_manager.register_execplans(
                connection,
                str(root),
                root,
                plans,
                "published-sha",
                "fingerprint",
            )
            loop_manager.register_execplans(
                connection,
                str(root),
                root,
                plans,
                "published-sha",
                "fingerprint",
            )
            rows = connection.execute(
                "SELECT plan_id, status FROM exec_plans WHERE repo = ? ORDER BY plan_id",
                (str(root),),
            ).fetchall()
            project = connection.execute(
                "SELECT phase, planning_fingerprint FROM projects WHERE repo = ?",
                (str(root),),
            ).fetchone()

        self.assertEqual([tuple(row) for row in rows], [("G-002", "READY"), ("G-003", "READY")])
        self.assertEqual(tuple(project), (None, "fingerprint"))

    def test_dependency_gate_then_priority_and_active_plan_preemption(self):
        connection = self._connection()
        self._insert_plan(connection, "G-001", "MERGED")
        self._insert_plan(connection, "G-002", "READY", priority="low")
        self._insert_plan(
            connection,
            "G-003",
            "READY",
            priority="critical",
            depends_on=("G-004",),
        )
        self._insert_plan(connection, "G-004", "READY", priority="high")
        self._insert_plan(connection, "G-005", "READY", priority="high")

        decision = loop_manager.catalog_schedule_decision(connection, "repo")
        self.assertIsNone(decision.active)
        self.assertTrue(decision.has_ready)
        self.assertEqual(decision.runnable_ready["plan_id"], "G-004")
        connection.execute(
            "UPDATE exec_plans SET status = 'MERGED' WHERE repo = 'repo' AND plan_id = 'G-004'"
        )
        connection.commit()
        self.assertEqual(loop_manager.select_ready_execplan(connection, "repo")["plan_id"], "G-003")

        connection.execute(
            "UPDATE exec_plans SET status = 'IMPLEMENTING' WHERE repo = 'repo' AND plan_id = 'G-005'"
        )
        connection.commit()
        decision = loop_manager.catalog_schedule_decision(connection, "repo")
        self.assertEqual(decision.active["plan_id"], "G-005")
        self.assertTrue(decision.has_ready)
        self.assertIsNone(decision.runnable_ready)

    def test_dependency_blocked_ready_is_not_an_empty_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blocked_parent = root / "blocked"
            runnable_parent = root / "runnable"
            blocked_parent.mkdir()
            runnable_parent.mkdir()
            blocked_repo = make_remote_repository(str(blocked_parent))
            runnable_repo = make_remote_repository(str(runnable_parent))
            (blocked_repo / "docs/plans").mkdir()
            (runnable_repo / "docs/plans").mkdir()

            historical = write_execplan(
                blocked_repo, "G-009", "Unverified Historical"
            )
            archive = blocked_repo / "docs/plans/archive"
            archive.mkdir()
            historical.rename(archive / historical.name)

            blocked_plan = write_execplan(
                blocked_repo,
                "G-010",
                "Dependency Blocked",
                depends_on=("G-009",),
                priority="critical",
            )
            runnable_plan = write_execplan(
                runnable_repo,
                "G-001",
                "Runnable",
                priority="high",
            )
            commit_and_push(blocked_repo, "Add dependency-blocked plan")
            commit_and_push(runnable_repo, "Add runnable plan")
            blocked_plan_sha = git_command(blocked_repo, "rev-parse", "origin/main")
            runnable_plan_sha = git_command(runnable_repo, "rev-parse", "origin/main")
            preserved_fingerprint = loop_manager.planning_input_fingerprint(
                blocked_repo, "main"
            )
            preserved_specification = (
                loop_manager.specification_input_fingerprint(
                    blocked_repo, "main"
                )
            )

            (blocked_repo / "docs/PRODUCT.md").write_text(
                "# Changed product input\n",
                encoding="utf-8",
            )
            commit_and_push(blocked_repo, "Change blocked project input")

            state_dir = root / "state"
            state_dir.mkdir()
            connection = loop_manager.connect_database(state_dir, create=True)
            connection.executemany(
                "INSERT INTO projects (repo, base_branch, "
                "specification_fingerprint, planning_fingerprint) "
                "VALUES (?, 'main', ?, ?)",
                (
                    (
                        str(blocked_repo),
                        preserved_specification,
                        preserved_fingerprint,
                    ),
                    (
                        str(runnable_repo),
                        loop_manager.specification_input_fingerprint(
                            runnable_repo, "main"
                        ),
                        loop_manager.planning_input_fingerprint(
                            runnable_repo, "main"
                        ),
                    ),
                ),
            )
            for repo, plan_id, plan_path, title, dependencies, priority, plan_sha in (
                (
                    blocked_repo,
                    "G-010",
                    blocked_plan,
                    "Dependency Blocked",
                    ("G-009",),
                    "critical",
                    blocked_plan_sha,
                ),
                (
                    runnable_repo,
                    "G-001",
                    runnable_plan,
                    "Runnable",
                    (),
                    "high",
                    runnable_plan_sha,
                ),
            ):
                connection.execute(
                    """
                    INSERT INTO exec_plans (
                        repo, plan_id, path, title, sources, depends_on, priority,
                        content_hash, status, plan_sha
                    ) VALUES (?, ?, ?, ?, '["docs/PRODUCT.md"]', ?, ?,
                              ?, 'READY', ?)
                    """,
                    (
                        str(repo),
                        plan_id,
                        plan_path.relative_to(repo).as_posix(),
                        title,
                        json.dumps(dependencies),
                        priority,
                        loop_manager.file_hash(plan_path),
                        plan_sha,
                    ),
                )
            maintenance_repository = (
                loop_manager.maintenance_state.MaintenanceRepository(connection)
            )
            for current_repo in (blocked_repo, runnable_repo):
                current_sha = git_command(
                    current_repo, "rev-parse", "origin/main"
                )
                cycle, _ = maintenance_repository.begin_assessment_if_due(
                    str(current_repo), 5, current_sha, self_repository=True,
                )
                maintenance_repository.advance_success_baseline(
                    str(current_repo),
                    expected_phase=cycle.phase,
                    expected_revision=cycle.revision,
                    remote_commit=current_sha,
                )
            connection.commit()
            config = loop_manager.ManagedConfig(
                path=root / "unused.ini",
                max_workers=2,
                telegram_token="",
                telegram_chat_id="",
            )
            roles: list[str] = []

            def agent(_worktree, _state, _config, role_name, _prompt, **_kwargs):
                roles.append(role_name)
                return loop_manager.CodexResult(
                    1, "stop runnable worker", "worker-session"
                )

            try:
                with mock.patch(
                    "loop_manager.validate_repository",
                    side_effect=lambda path, **_kwargs: Path(path).resolve(),
                ), mock.patch(
                    "loop_manager.run_codex",
                    side_effect=agent,
                ):
                    outcomes = loop_manager.schedule_registered_projects(
                        connection,
                        state_dir,
                        config,
                    )
                blocked_project = connection.execute(
                    "SELECT phase, planning_fingerprint FROM projects WHERE repo = ?",
                    (str(blocked_repo),),
                ).fetchone()
                blocked_catalog = connection.execute(
                    "SELECT plan_id, status FROM exec_plans WHERE repo = ?",
                    (str(blocked_repo),),
                ).fetchall()
                runnable_status = connection.execute(
                    "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                    (str(runnable_repo),),
                ).fetchone()["status"]
            finally:
                connection.close()

        results = {item.repo: item.result.kind for item in outcomes}
        self.assertEqual(
            results[str(blocked_repo)],
            loop_manager.ProjectRunKind.OPERATIONAL_ERROR,
        )
        self.assertEqual(
            results[str(runnable_repo)],
            loop_manager.ProjectRunKind.OPERATIONAL_ERROR,
        )
        self.assertCountEqual(
            roles, ["planner", "worker"],
            msg=[(item.repo, item.result.detail) for item in outcomes],
        )
        self.assertEqual(
            tuple(blocked_project), ("PLANNING", preserved_fingerprint)
        )
        self.assertEqual(
            [tuple(row) for row in blocked_catalog],
            [("G-010", "READY")],
        )
        self.assertEqual(runnable_status, "IMPLEMENTING")


    def test_merge_marks_only_current_plan_and_unlocks_next_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = self._connection()
            self._insert_plan(connection, "G-001", "PR_OPEN", priority="high")
            self._insert_plan(
                connection,
                "G-002",
                "READY",
                priority="critical",
                depends_on=("G-001",),
            )
            state_dir = Path(directory) / "state"
            _branch, missing_worktree = loop_manager.expected_execplan_workspace(
                state_dir, "repo", "G-001"
            )
            connection.execute(
                """
                UPDATE projects
                SET phase = 'PR_OPEN', branch = 'loop-manager/G-001',
                    worktree = ?, plan_path = 'docs/plans/[G-001] Plan G-001.md',
                    pr_url = 'https://pr/1'
                WHERE repo = 'repo'
                """,
                (str(missing_worktree),),
            )
            connection.commit()

            assessment = loop_manager.complete_merged_worktree(
                connection,
                Path(directory),
                "repo",
                state_dir,
                missing_worktree,
                "https://pr/1",
                None,
            )

            statuses = dict(
                connection.execute(
                    "SELECT plan_id, status FROM exec_plans WHERE repo = 'repo'"
                ).fetchall()
            )
            selected = loop_manager.select_ready_execplan(connection, "repo")

        self.assertEqual(assessment.status, "missing")
        self.assertEqual(statuses, {"G-001": "MERGED", "G-002": "READY"})
        self.assertEqual(selected["plan_id"], "G-002")

    def test_last_merge_preserves_successful_planning_baseline(self):
        for completion in ("projected", "catalog"):
            with self.subTest(completion=completion), tempfile.TemporaryDirectory() as directory:
                connection = self._connection()
                self.addCleanup(connection.close)
                self._insert_plan(connection, "G-001", "PR_OPEN")
                state_dir = Path(directory) / "state"
                branch, worktree = loop_manager.expected_execplan_workspace(
                    state_dir, "repo", "G-001"
                )
                plan_path = "docs/plans/[G-001] Plan G-001.md"
                connection.execute(
                    "UPDATE exec_plans SET branch = ?, worktree = ? "
                    "WHERE repo = 'repo' AND plan_id = 'G-001'",
                    (branch, str(worktree)),
                )
                connection.execute(
                    "UPDATE projects SET phase = 'PR_OPEN', branch = ?, "
                    "worktree = ?, plan_path = ?, processed_sha = 'baseline-sha', "
                    "specification_fingerprint = 'baseline-spec', "
                    "planning_fingerprint = 'baseline-full' WHERE repo = 'repo'",
                    (branch, str(worktree), plan_path),
                )
                connection.commit()

                if completion == "projected":
                    assessment = loop_manager.complete_merged_worktree(
                        connection, Path(directory), "repo", state_dir,
                        worktree, "https://pr/1", None,
                    )
                else:
                    plan = connection.execute(
                        "SELECT * FROM exec_plans WHERE repo = 'repo' "
                        "AND plan_id = 'G-001'"
                    ).fetchone()
                    assessment = loop_manager.complete_merged_execplan_worktree(
                        connection, Path(directory), "repo", state_dir,
                        plan, "https://pr/1", None,
                    )

                row = connection.execute(
                    "SELECT phase, processed_sha, specification_fingerprint, "
                    "planning_fingerprint FROM projects WHERE repo = 'repo'"
                ).fetchone()
                self.assertEqual(assessment.status, "missing")
                self.assertEqual(
                    tuple(row),
                    (None, "baseline-sha", "baseline-spec", "baseline-full"),
                )

    def test_planner_error_with_ready_does_not_repeat_project_step(self):
        for phase, planning_error in (("PLANNING", None), (None, "planner error")):
            with self.subTest(phase=phase, planning_error=planning_error):
                connection = self._connection()
                self.addCleanup(connection.close)
                self._insert_plan(connection, "G-001", "READY")

                def failed_planner_step(*_args, **_kwargs):
                    connection.execute(
                        "UPDATE projects SET phase = ?, planning_error = ?, "
                        "last_error = 'planner stopped' WHERE repo = 'repo'",
                        (phase, planning_error),
                    )
                    connection.commit()
                    return 1

                with mock.patch(
                    "loop_manager._checkpoint_dispatch_ready", return_value=False
                ), mock.patch(
                    "loop_manager._run_project_step", side_effect=failed_planner_step
                ) as step:
                    result = loop_manager._run_project_result(
                        connection, Path("/unused"), "repo",
                        Path("/unused.ini"), "main", False,
                    )

                self.assertEqual(result.kind, loop_manager.ProjectRunKind.OPERATIONAL_ERROR)
                step.assert_called_once()

    def test_remote_input_change_after_planner_defers_published_ready_worker(self):
        for publishes_plan in (False, True):
            with self.subTest(publishes_plan=publishes_plan), tempfile.TemporaryDirectory() as directory:
                repo = make_remote_repository(directory)
                (repo / "docs/plans").mkdir()
                write_execplan(repo, "G-001", "Queued", priority="high")
                commit_and_push(repo, "Add queued plan")
                external_repo = Path(directory) / "external"
                git_command(
                    repo,
                    "clone",
                    str(Path(directory) / "remote.git"),
                    str(external_repo),
                )
                git_command(external_repo, "config", "user.email", "user@example.com")
                git_command(external_repo, "config", "user.name", "External User")
                state_dir = Path(directory) / "state"
                connection = self._connection(str(repo))
                baseline_sha = git_command(repo, "rev-parse", "origin/main")
                self._insert_plan(
                    connection,
                    "G-001",
                    "MERGED",
                    priority="high",
                    repo=str(repo),
                    title="Queued",
                    plan_sha=baseline_sha,
                )
                started_fingerprint = loop_manager.planning_input_fingerprint(repo, "main")
                roles = []
                remote_advanced = False

                def advance_remote():
                    nonlocal remote_advanced
                    if remote_advanced:
                        return
                    git_command(external_repo, "fetch", "origin")
                    git_command(external_repo, "merge", "--ff-only", "origin/main")
                    (external_repo / "src/app.py").write_text(
                        "VALUE = 2\n",
                        encoding="utf-8",
                    )
                    commit_and_push(external_repo, "Change input during planning")
                    remote_advanced = True

                def agent(worktree, _state, _config, role_name, _prompt, **_kwargs):
                    roles.append(role_name)
                    if role_name == "planner":
                        if publishes_plan and roles.count("planner") == 1:
                            write_execplan(
                                worktree,
                                "G-002",
                                "Deferred",
                                priority="low",
                            )
                        elif not publishes_plan:
                            advance_remote()
                        return loop_manager.CodexResult(0, "", "planner-session")
                    return loop_manager.CodexResult(1, "worker must not start", "worker-session")

                original_publish = loop_manager.publish_general_planning_batch

                def publish(*args, **kwargs):
                    original_publish(*args, **kwargs)
                    if publishes_plan and not remote_advanced:
                        git_command(repo, "merge", "--ff-only", "origin/main")
                        advance_remote()

                with mock.patch(
                    "loop_manager.validate_repository",
                    return_value=repo,
                ), mock.patch(
                    "loop_manager.remote_base",
                    return_value="main",
                ), mock.patch(
                    "loop_manager.ensure_safe_base_worktree",
                ), mock.patch(
                    "loop_manager.run_codex",
                    side_effect=agent,
                ), mock.patch(
                    "loop_manager.publish_general_planning_batch",
                    side_effect=publish,
                ):
                    result = loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )

                project = connection.execute(
                    "SELECT phase, planning_fingerprint FROM projects WHERE repo = ?",
                    (str(repo),),
                ).fetchone()
                plans = connection.execute(
                    "SELECT plan_id, status FROM exec_plans WHERE repo = ? ORDER BY plan_id",
                    (str(repo),),
                ).fetchall()

                self.assertEqual(result, 1 if publishes_plan else 0)
                self.assertEqual(
                    roles,
                    ["planner", "planner", "worker"] if publishes_plan
                    else ["planner", "planner"],
                )
                self.assertEqual(
                    project["phase"], "WORKING" if publishes_plan else None
                )
                self.assertEqual(
                    project["planning_fingerprint"],
                    loop_manager.planning_input_fingerprint(repo, "main"),
                )
                self.assertNotEqual(project["planning_fingerprint"], started_fingerprint)
                self.assertEqual(
                    [tuple(plan) for plan in plans],
                    [("G-001", "MERGED")]
                    + ([('G-002', 'IMPLEMENTING')] if publishes_plan else []),
                )

    def test_changed_input_plans_before_ready_and_unchanged_input_runs_worker(self):
        for changed in (True, False):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                repo = make_remote_repository(directory)
                (repo / "docs/plans").mkdir()
                plan = write_execplan(repo, "G-001", "Queued", priority="high")
                commit_and_push(repo, "Add queued plan")
                state_dir = Path(directory) / "state"
                connection = self._connection(str(repo))
                baseline_sha = git_command(repo, "rev-parse", "origin/main")
                self._insert_plan(
                    connection,
                    "G-001",
                    "READY",
                    priority="high",
                    repo=str(repo),
                    title="Queued",
                    plan_sha=baseline_sha,
                )
                fingerprint = loop_manager.planning_input_fingerprint(repo, "main")
                specification = loop_manager.specification_input_fingerprint(
                    repo, "main"
                )
                if changed:
                    (repo / "src/app.py").write_text(
                        "VALUE = 2\n", encoding="utf-8"
                    )
                    commit_and_push(repo, "Change source before READY selection")
                connection.execute(
                    """
                    UPDATE projects
                    SET specification_fingerprint = ?, planning_fingerprint = ?,
                        processed_sha = ?
                    WHERE repo = ?
                    """,
                    (
                        specification,
                        fingerprint,
                        baseline_sha,
                        str(repo),
                    ),
                )
                connection.commit()
                roles = []

                def agent(_worktree, _state, _config, role_name, _prompt, **_kwargs):
                    roles.append(role_name)
                    if role_name == "planner":
                        return loop_manager.CodexResult(0, "", "planner-session")
                    return loop_manager.CodexResult(1, "stop", f"{role_name}-session")

                with mock.patch(
                    "loop_manager.validate_repository",
                    return_value=repo,
                ), mock.patch(
                    "loop_manager.remote_base",
                    return_value="main",
                ), mock.patch(
                    "loop_manager.ensure_safe_base_worktree",
                ), mock.patch(
                    "loop_manager.run_codex",
                    side_effect=agent,
                ):
                    result = loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )

                self.assertEqual(result, 1)
                self.assertEqual(
                    roles, ["planner", "worker"] if changed else ["worker"]
                )

    def test_planning_and_ready_selection_pin_fingerprint_to_resolved_sha(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            (repo / "docs/plans").mkdir()
            write_execplan(repo, "G-001", "Queued", priority="high")
            commit_and_push(repo, "Add queued plan")
            selected_sha = git_command(repo, "rev-parse", "origin/main")
            selected_fingerprint = loop_manager._planning_input_fingerprint(
                repo,
                selected_sha,
            )

            state_dir = Path(directory) / "state"
            connection = self._connection(str(repo))
            self._insert_plan(
                connection,
                "G-001",
                "READY",
                priority="high",
                repo=str(repo),
                title="Queued",
                plan_sha=selected_sha,
            )
            connection.execute(
                """
                UPDATE projects
                SET planning_fingerprint = ?, processed_sha = ?
                WHERE repo = ?
                """,
                (selected_fingerprint, selected_sha, str(repo)),
            )
            connection.commit()

            (repo / "docs/PRODUCT.md").write_text(
                "# Product moved after SHA resolve\n",
                encoding="utf-8",
            )
            git_command(repo, "add", "docs/PRODUCT.md")
            git_command(repo, "commit", "-m", "Move planning input locally")
            moved_sha = git_command(repo, "rev-parse", "HEAD")

            real_git = loop_manager.git
            real_fingerprint = loop_manager._planning_input_fingerprint
            exact_reads = 0
            fingerprint_revisions = []

            def move_ref_after_resolve(path, *arguments, **kwargs):
                nonlocal exact_reads
                result = real_git(path, *arguments, **kwargs)
                if arguments in {
                    (
                        "rev-parse",
                        "refs/remotes/origin/main^{commit}",
                    ),
                    (
                        "rev-parse",
                        "--verify",
                        "refs/remotes/origin/main^{commit}",
                    ),
                }:
                    exact_reads += 1
                    if exact_reads == 1:
                        git_command(
                            repo,
                            "update-ref",
                            "refs/remotes/origin/main",
                            moved_sha,
                        )
                return result

            def record_fingerprint(path, revision):
                fingerprint_revisions.append(revision)
                return real_fingerprint(path, revision)

            roles = []

            def agent(_worktree, _state, _config, role_name, _prompt, **_kwargs):
                roles.append(role_name)
                return loop_manager.CodexResult(1, "stop", "worker-session")

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.git", side_effect=move_ref_after_resolve
            ), mock.patch(
                "loop_manager._planning_input_fingerprint",
                side_effect=record_fingerprint,
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch("loop_manager.run_codex", side_effect=agent):
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT active_sha, worktree FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(result, 1)
            self.assertEqual(roles, ["worker"])
            self.assertGreaterEqual(exact_reads, 2)
            self.assertEqual(fingerprint_revisions, [selected_sha])
            self.assertEqual(project["active_sha"], selected_sha)
            self.assertEqual(
                git_command(Path(project["worktree"]), "rev-parse", "HEAD"),
                selected_sha,
            )
            self.assertEqual(
                git_command(repo, "rev-parse", "refs/remotes/origin/main"),
                selected_sha,
            )



class PlannerFutureIdeasTest(unittest.TestCase):
    def test_prompt_separates_future_ideas_from_current_execplans(self):
        prompt = loop_manager.planner_prompt(None)

        self.assertIn("docs/FUTURE_IDEAS.md", prompt)
        self.assertIn("같은 계획 주기의 ExecPlan 범위나 미충족 작업의 근거로 사용하지 말고", prompt)
        self.assertIn(
            "mutable READY snapshot의 계획은 같은 ID를 보존한 채",
            prompt,
        )
        self.assertIn(
            "snapshot 밖의 기존 ExecPlan, 실행된 계획, MERGED·archive 계획은 "
            "수정·rename·삭제하지 마라",
            prompt,
        )
        plans_contract = (loop_manager.ROOT / "docs/PLANS.md").read_text(encoding="utf-8")
        self.assertIn("### Agent Guidance Proposal: guidance-example", plans_contract)
        self.assertIn("### Agent Guidance User Decision: guidance-example", plans_contract)
        self.assertIn("`approve`", plans_contract)
        self.assertIn("`reject`", plans_contract)
        self.assertIn("`replace`", plans_contract)

    def test_validation_and_scope_separate_future_ideas_from_new_plans(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            (repo / "docs/plans").mkdir()
            write_execplan(repo, "G-001", "Existing")
            future_ideas = repo / "docs/FUTURE_IDEAS.md"
            future_ideas.write_text("# Future Ideas\n", encoding="utf-8")
            commit_and_push(repo, "Add planning fixtures")
            active_sha = git_command(repo, "rev-parse", "HEAD")
            before = loop_manager.capture_git_snapshot(repo)

            future_ideas.write_text(
                "# Future Ideas\n\n- Deferred idea\n",
                encoding="utf-8",
            )
            second = write_execplan(repo, "G-002", "Current Work")
            after = loop_manager.capture_git_snapshot(repo)

            loop_manager.validate_agent_scope("planner", repo, before, after)
            result = loop_manager.validate_planner_result(
                repo,
                active_sha,
                [
                    future_ideas.relative_to(repo).as_posix(),
                    second.relative_to(repo).as_posix(),
                ],
            )

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(
                [plan["id"] for _path, plan in result.plans],
                ["G-002"],
            )
            self.assertEqual(
                result.publication_paths,
                ("docs/FUTURE_IDEAS.md", "docs/plans/[G-002] Current Work.md"),
            )

            second.unlink()
            future_only = loop_manager.validate_planner_result(
                repo,
                active_sha,
                ["docs/FUTURE_IDEAS.md"],
            )
            self.assertIsNotNone(future_only)
            assert future_only is not None
            self.assertEqual(future_only.plans, ())

    def test_validation_rejects_protected_files_and_existing_plan_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            (repo / "docs/plans").mkdir()
            existing = write_execplan(repo, "G-001", "Existing")
            commit_and_push(repo, "Add existing plan")
            active_sha = git_command(repo, "rev-parse", "HEAD")

            notes = repo / "docs/notes.md"
            notes.write_text("# Planner changed protected notes\n", encoding="utf-8")
            with self.assertRaisesRegex(
                loop_manager.PlannerValidationError,
                "FUTURE_IDEAS",
            ):
                loop_manager.validate_planner_result(
                    repo,
                    active_sha,
                    ["docs/notes.md"],
                )

            notes.write_text("# Notes\n", encoding="utf-8")
            existing.write_text(
                existing.read_text(encoding="utf-8") + "\nchanged\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                loop_manager.PlannerValidationError,
                "기존 ExecPlan을 수정",
            ):
                loop_manager.validate_planner_result(
                    repo,
                    active_sha,
                    [existing.relative_to(repo).as_posix()],
                )

    def test_validation_rejects_future_idea_promoted_to_new_execplan(self):
        for future_idea_state in ("new", "existing", "removed"):
            with self.subTest(
                future_idea_state=future_idea_state
            ), tempfile.TemporaryDirectory() as directory:
                repo = make_remote_repository(directory)
                (repo / "docs/plans").mkdir()
                future_ideas = repo / "docs/FUTURE_IDEAS.md"
                if future_idea_state in {"existing", "removed"}:
                    future_ideas.write_text(
                        "# Future Ideas\n\n- Speculative deployment dashboard\n",
                        encoding="utf-8",
                    )
                    commit_and_push(repo, "Add deferred idea")
                active_sha = git_command(repo, "rev-parse", "HEAD")
                if future_idea_state == "new":
                    future_ideas.write_text(
                        "# Future Ideas\n\n- Speculative deployment dashboard\n",
                        encoding="utf-8",
                    )
                elif future_idea_state == "removed":
                    future_ideas.write_text("# Future Ideas\n", encoding="utf-8")
                plan = write_execplan(
                    repo,
                    "G-001",
                    "Speculative deployment dashboard",
                )
                changed = [plan.relative_to(repo).as_posix()]
                if future_idea_state != "existing":
                    changed.append(future_ideas.relative_to(repo).as_posix())

                with self.assertRaisesRegex(
                    loop_manager.PlannerValidationError,
                    "FUTURE_IDEAS.*같은 계획 주기",
                ):
                    loop_manager.validate_planner_result(
                        repo,
                        active_sha,
                        changed,
                    )

    def test_validation_uses_token_boundaries_for_future_ideas(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            (repo / "docs/plans").mkdir()
            future_ideas = repo / "docs/FUTURE_IDEAS.md"
            future_ideas.write_text("# Future Ideas\n\n- CI\n", encoding="utf-8")
            plan = write_execplan(repo, "G-001", "Decision Log Cleanup")
            active_sha = git_command(repo, "rev-parse", "HEAD")

            result = loop_manager.validate_planner_result(
                repo,
                active_sha,
                [
                    future_ideas.relative_to(repo).as_posix(),
                    plan.relative_to(repo).as_posix(),
                ],
            )

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual([item[1]["id"] for item in result.plans], ["G-001"])

    def test_future_ideas_publish_with_and_without_execplans(self):
        for with_plan in (False, True):
            with self.subTest(with_plan=with_plan), tempfile.TemporaryDirectory() as directory:
                repo = make_remote_repository(directory)
                state_dir = Path(directory) / "state"
                connection = sqlite3.connect(":memory:")
                connection.row_factory = sqlite3.Row
                connection.execute(loop_manager.SCHEMA)
                original_fingerprint = loop_manager.planning_input_fingerprint(repo, "main")

                def agent(worktree, _state, _config, role_name, _prompt, **_kwargs):
                    if role_name == "planner":
                        (worktree / "docs/FUTURE_IDEAS.md").write_text(
                            "# Future Ideas\n\n- Deferred idea\n",
                            encoding="utf-8",
                        )
                        if with_plan:
                            (worktree / "docs/plans").mkdir(parents=True, exist_ok=True)
                            write_execplan(worktree, "G-001", "Current Work")
                        return loop_manager.CodexResult(0, "", "planner-session")
                    return loop_manager.CodexResult(1, "stop after planning", "worker-session")

                with mock.patch(
                    "loop_manager.validate_repository",
                    return_value=repo,
                ), mock.patch(
                    "loop_manager.remote_base",
                    return_value="main",
                ), mock.patch(
                    "loop_manager.ensure_safe_base_worktree"
                ), mock.patch(
                    "loop_manager.run_codex",
                    side_effect=agent,
                ):
                    result = loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )

                self.assertEqual(result, 1 if with_plan else 0)
                self.assertEqual(
                    git_command(repo, "show", "origin/main:docs/FUTURE_IDEAS.md"),
                    "# Future Ideas\n\n- Deferred idea",
                )
                plans = connection.execute(
                    "SELECT plan_id, plan_sha FROM exec_plans ORDER BY plan_id"
                ).fetchall()
                if with_plan:
                    self.assertEqual(
                        [tuple(row) for row in plans],
                        [("G-001", git_command(repo, "rev-parse", "origin/main"))],
                    )
                    self.assertEqual(
                        git_command(
                            repo,
                            "show",
                            "origin/main:docs/plans/[G-001] Current Work.md",
                        ).splitlines()[1],
                        "id: G-001",
                    )
                else:
                    self.assertEqual(plans, [])
                    project = connection.execute(
                        "SELECT phase, planning_fingerprint FROM projects WHERE repo = ?",
                        (str(repo),),
                    ).fetchone()
                    self.assertIsNone(project["phase"])
                    self.assertEqual(project["planning_fingerprint"], original_fingerprint)


class LoopManagerTest(unittest.TestCase):
    def test_codex_uses_mutually_exclusive_sandbox_options(self):
        self.assertEqual(loop_manager.codex_sandbox_arguments(False), ["--approve-for-me"])
        self.assertEqual(loop_manager.codex_sandbox_arguments(True), ["--sandbox", "read-only"])

    def test_schema_and_project_update(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        connection.execute("INSERT INTO projects (repo, base_branch) VALUES ('repo', 'main')")

        loop_manager.update_project(
            connection,
            "repo",
            phase="WORKING",
            active_sha="abc",
            planning_fingerprint="fingerprint",
            planning_retry_count=2,
            planner_session_id="session-1",
        )

        row = connection.execute("SELECT * FROM projects WHERE repo = 'repo'").fetchone()
        self.assertEqual(
            (
                row["phase"],
                row["active_sha"],
                row["planning_fingerprint"],
                row["planning_retry_count"],
                row["planner_session_id"],
            ),
            ("WORKING", "abc", "fingerprint", 2, "session-1"),
        )

    def test_existing_database_adds_planning_fingerprint_without_losing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            database = sqlite3.connect(state_dir / "loop-manager.db")
            database.execute(
                """
                CREATE TABLE projects (
                    repo TEXT PRIMARY KEY,
                    base_branch TEXT NOT NULL,
                    processed_sha TEXT,
                    active_sha TEXT,
                    phase TEXT,
                    branch TEXT,
                    worktree TEXT,
                    pr_url TEXT,
                    last_error TEXT
                )
                """
            )
            database.execute(
                "INSERT INTO projects (repo, base_branch, phase) VALUES ('repo', 'main', 'WORKING')"
            )
            database.commit()
            database.close()

            with loop_manager.connect_database(state_dir) as connection:
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(projects)")
                }
                row = connection.execute(
                    "SELECT * FROM projects WHERE repo = 'repo'"
                ).fetchone()

        self.assertIn("plan_path", columns)
        self.assertIn("specification_fingerprint", columns)
        self.assertIn("planning_fingerprint", columns)
        self.assertIn("planning_attempt_fingerprint", columns)
        self.assertIn("planning_retry_count", columns)
        self.assertIn("planner_session_id", columns)
        self.assertIn("planning_validation_error", columns)
        self.assertIn("planning_error", columns)
        self.assertIn("planning_result_pending_validation", columns)
        self.assertIn("planning_result_completed", columns)
        self.assertIn("planning_error_fingerprint", columns)
        self.assertEqual((row["base_branch"], row["phase"]), ("main", "WORKING"))
        self.assertIsNone(row["specification_fingerprint"])
        self.assertIsNone(row["planning_fingerprint"])
        self.assertEqual(row["planning_retry_count"], 0)
        self.assertEqual(row["planning_result_pending_validation"], 0)
        self.assertIsNone(row["planning_error"])

    def test_planning_input_path_boundary(self):
        cases = {
            b"AGENTS.md": True,
            b"docs/PRODUCT.md": True,
            b"docs/ARCHITECTURE.md": True,
            b"docs/requirements/Active.md": True,
            b"README.md": True,
            b"docs/requirements/.md": True,
            b".github/workflows/test.yml": True,
            b"src/app.py": True,
            b"docs/PLANS.md": False,
            b"docs/FUTURE_IDEAS.md": False,
            b"docs/plans/[G-999] Ignored.md": False,
            b"docs/requirements/archive/Old.md": False,
            b"docs/requirements/nested/Requirement.md": False,
            b"docs/requirements/NotMarkdown.txt": False,
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(loop_manager._is_planning_input(path), expected)

    def test_planning_input_digest_is_ordered_and_unambiguous(self):
        entries = [
            (b"src/b.py", b"blob", b"two"),
            (b"src/a.py", b"blob", b"one"),
        ]

        fingerprint = loop_manager._planning_input_digest(entries)

        self.assertEqual(fingerprint, loop_manager._planning_input_digest(list(reversed(entries))))
        self.assertEqual(len(fingerprint), 64)
        self.assertNotEqual(
            loop_manager._planning_input_digest([(b"a", b"blob", b"bc")]),
            loop_manager._planning_input_digest([(b"ab", b"blob", b"c")]),
        )
        self.assertNotEqual(
            loop_manager._planning_input_digest([(b"src/a.py", b"blob", b"one")]),
            loop_manager._planning_input_digest(
                [(b"src/renamed.py", b"blob", b"one")]
            ),
        )
        self.assertNotEqual(
            loop_manager._planning_input_digest([(b"vendor/lib", b"blob", b"abc")]),
            loop_manager._planning_input_digest([(b"vendor/lib", b"commit", b"abc")]),
        )

    def test_planning_input_fingerprint_uses_origin_tree_and_input_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            initial = loop_manager.planning_input_fingerprint(repo, "main")

            (repo / "src/app.py").write_text("VALUE = 99\n", encoding="utf-8")
            self.assertEqual(loop_manager.planning_input_fingerprint(repo, "main"), initial)
            (repo / "src/app.py").write_text("VALUE = 1\n", encoding="utf-8")

            (repo / "docs/notes.md").write_text("# Changed notes\n", encoding="utf-8")
            commit_and_push(repo, "Change excluded documentation")
            self.assertEqual(loop_manager.planning_input_fingerprint(repo, "main"), initial)

            (repo / "docs/requirements/archive/Old.md").write_text(
                "# Changed archived requirement\n", encoding="utf-8"
            )
            commit_and_push(repo, "Change archived requirement")
            self.assertEqual(loop_manager.planning_input_fingerprint(repo, "main"), initial)

            (repo / "docs/PRODUCT.md").write_text("# Changed product\n", encoding="utf-8")
            commit_and_push(repo, "Change product")
            changed = loop_manager.planning_input_fingerprint(repo, "main")
            self.assertNotEqual(changed, initial)

            git_command(repo, "checkout", "--detach", "HEAD^")
            self.assertNotEqual(
                git_command(repo, "rev-parse", "HEAD"),
                git_command(repo, "rev-parse", "origin/main"),
            )
            self.assertEqual(loop_manager.planning_input_fingerprint(repo, "main"), changed)

            git_command(repo, "checkout", "main")
            (repo / "src/app.py").rename(repo / "src/renamed.py")
            commit_and_push(repo, "Rename source")
            self.assertNotEqual(loop_manager.planning_input_fingerprint(repo, "main"), changed)

    def test_planning_input_fingerprint_includes_gitlink_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            first_target = git_command(repo, "rev-parse", "HEAD")
            git_command(
                repo,
                "update-index",
                "--add",
                "--cacheinfo",
                f"160000,{first_target},vendor/library",
            )
            git_command(repo, "commit", "-m", "Add tracked submodule commit")
            git_command(repo, "push", "origin", "HEAD:main")
            first = loop_manager.planning_input_fingerprint(repo, "main")

            second_target = git_command(repo, "rev-parse", "HEAD")
            git_command(
                repo,
                "update-index",
                "--cacheinfo",
                f"160000,{second_target},vendor/library",
            )
            git_command(repo, "commit", "-m", "Update tracked submodule commit")
            git_command(repo, "push", "origin", "HEAD:main")

            self.assertNotEqual(
                loop_manager.planning_input_fingerprint(repo, "main"), first
            )

    def test_planner_no_change_saves_fingerprint_and_skip_gate_uses_it(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            logs = []

            connection.execute(loop_manager.SCHEMA)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.run_codex", return_value=(0, "")
            ) as planner, mock.patch("loop_manager.operation_log", side_effect=lambda message, **kwargs: logs.append((message, kwargs))):
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    0,
                )
                first = connection.execute(
                    "SELECT planning_fingerprint FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()["planning_fingerprint"]
                self.assertIsNotNone(first)
                self.assertEqual(planner.call_count, 1)
                self.assertEqual(logs[0], ("Planner 시작: 다음 ExecPlan 계획을 생성합니다.", {}))

                planner.reset_mock()
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    0,
                )
                planner.assert_not_called()

                (repo / "docs/notes.md").write_text("# Other notes\n", encoding="utf-8")
                commit_and_push(repo, "Change excluded docs again")
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    0,
                )
                planner.assert_not_called()

                (repo / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
                commit_and_push(repo, "Change source")
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    0,
                )
                self.assertEqual(planner.call_count, 1)

            second = connection.execute(
                "SELECT planning_fingerprint FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()["planning_fingerprint"]
            self.assertNotEqual(second, first)

    def test_requirement_archive_move_does_not_run_planner(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.run_codex", return_value=(0, "")
            ) as planner:
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    0,
                )
                completed_fingerprint = connection.execute(
                    "SELECT planning_fingerprint FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()["planning_fingerprint"]
                planner.reset_mock()

                active = repo / "docs/requirements/Active.md"
                archived = repo / "docs/requirements/archive/Active.md"
                active.rename(archived)
                commit_and_push(repo, "Archive completed requirement")

                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    0,
                )
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    0,
                )
                planner.assert_not_called()

            row = connection.execute(
                "SELECT processed_sha, planning_fingerprint FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(row["processed_sha"], git_command(repo, "rev-parse", "origin/main"))
            self.assertEqual(row["planning_fingerprint"], completed_fingerprint)
            self.assertNotEqual(
                row["planning_fingerprint"],
                loop_manager.planning_input_fingerprint(repo, "main"),
            )

    def test_planner_failure_and_invalid_result_do_not_save_fingerprint(self):
        def fail_planner(_worktree, *_args, **_kwargs):
            return 1, "planner failed"

        def write_invalid_file(worktree, *_args, **_kwargs):
            (worktree / "unexpected.txt").write_text("invalid\n", encoding="utf-8")
            return 0, ""

        for name, planner_effect in (
            ("failure", fail_planner),
            ("invalid-result", write_invalid_file),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                repo = make_remote_repository(directory)
                state_dir = Path(directory) / "state"
                connection = sqlite3.connect(":memory:")
                connection.row_factory = sqlite3.Row
                connection.execute(loop_manager.SCHEMA)

                with mock.patch(
                    "loop_manager.validate_repository", return_value=repo
                ), mock.patch(
                    "loop_manager.remote_base", return_value="main"
                ), mock.patch(
                    "loop_manager.ensure_safe_base_worktree"
                ), mock.patch(
                    "loop_manager.run_codex", side_effect=planner_effect
                ) as planner:
                    for _attempt in range(2):
                        self.assertEqual(
                            loop_manager.run_project(
                                connection,
                                state_dir,
                                str(repo),
                                Path("unused.ini"),
                                "main",
                                False,
                            ),
                            1,
                        )

                row = connection.execute(
                    "SELECT phase, planning_fingerprint FROM projects WHERE repo = ?",
                    (str(repo),),
                ).fetchone()
                self.assertEqual(planner.call_count, 2)
                self.assertEqual(row["phase"], "PLANNING")
                self.assertIsNone(row["planning_fingerprint"])

    def test_first_planner_validation_retry_resumes_session_and_clears_state(self):
        planner_calls = []

        def agent(worktree, _state, _config, role_name, prompt, **kwargs):
            if role_name == "planner":
                planner_calls.append((kwargs["session_id"], prompt))
                (worktree / "docs/plans").mkdir(parents=True, exist_ok=True)
                if len(planner_calls) == 1:
                    write_execplan(
                        worktree,
                        "G-001",
                        "Generated",
                        depends_on=("G-999",),
                    )
                else:
                    write_execplan(worktree, "G-001", "Generated")
                return loop_manager.CodexResult(0, "", "planner-session")
            return loop_manager.CodexResult(1, "stop after planning")

        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.run_codex", side_effect=agent
            ):
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )
                first = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                self.assertEqual(first["planning_retry_count"], 0)
                self.assertEqual(first["planner_session_id"], "planner-session")
                self.assertIn("존재하지 않는 depends_on ID", first["planning_validation_error"])
                self.assertIsNone(first["planning_error"])

                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )

            final = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(planner_calls[0][0], None)
            self.assertEqual(planner_calls[1][0], "planner-session")
            self.assertIn(first["planning_validation_error"], planner_calls[1][1])
            self.assertEqual(final["phase"], "WORKING")
            self.assertEqual(final["planning_retry_count"], 0)
            self.assertIsNone(final["planner_session_id"])
            self.assertIsNone(final["planning_validation_error"])
            self.assertIsNone(final["planning_error"])
    def test_planner_commit_failure_preserves_valid_result_without_retry_penalty(self):
        roles = []

        def agent(worktree, _state, _config, role_name, _prompt, **_kwargs):
            roles.append(role_name)
            if role_name == "planner":
                (worktree / "docs/plans").mkdir(parents=True, exist_ok=True)
                write_execplan(worktree, "G-001", "Generated")
                return loop_manager.CodexResult(0, "", "planner-session")
            return loop_manager.CodexResult(1, "stop after planning")

        original_git = loop_manager.git

        def reject_planning_commit(worktree, *arguments, **kwargs):
            if arguments[:1] == ("commit",) and arguments[-1].startswith("Add ExecPlan"):
                raise RuntimeError("commit hook rejected")
            return original_git(worktree, *arguments, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.run_codex", side_effect=agent
            ):
                with mock.patch("loop_manager.git", side_effect=reject_planning_commit):
                    with self.assertRaisesRegex(RuntimeError, "commit hook rejected"):
                        loop_manager.run_project(
                            connection,
                            state_dir,
                            str(repo),
                            Path("unused.ini"),
                            "main",
                            False,
                        )

                failed_commit = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                self.assertEqual(failed_commit["planning_retry_count"], 0)
                self.assertEqual(failed_commit["planning_result_pending_validation"], 1)
                self.assertIsNone(failed_commit["planning_validation_error"])
                self.assertIsNone(failed_commit["planning_error"])

                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )

            resumed = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(roles, ["planner", "worker"])
            self.assertEqual(resumed["phase"], "WORKING")
            self.assertEqual(resumed["planning_result_pending_validation"], 0)
            self.assertIsNone(resumed["planning_validation_error"])
            self.assertIsNone(resumed["planning_error"])

    def test_interrupted_successful_planner_result_is_validated_before_resume(self):
        roles = []

        def agent(worktree, _state, _config, role_name, _prompt, **_kwargs):
            roles.append(role_name)
            if role_name == "planner":
                (worktree / "docs/plans").mkdir(parents=True, exist_ok=True)
                write_execplan(worktree, "G-001", "Generated")
                return loop_manager.CodexResult(0, "", "planner-session")
            return loop_manager.CodexResult(1, "stop after planning")

        original_changed_files = loop_manager.changed_files
        changed_files_calls = 0

        def interrupt_after_result_is_persisted(*args, **kwargs):
            nonlocal changed_files_calls
            changed_files_calls += 1
            if changed_files_calls == 2:
                raise RuntimeError("process interrupted after Planner result")
            return original_changed_files(*args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.run_codex", side_effect=agent
            ):
                with mock.patch(
                    "loop_manager.changed_files",
                    side_effect=interrupt_after_result_is_persisted,
                ):
                    with self.assertRaisesRegex(RuntimeError, "process interrupted"):
                        loop_manager.run_project(
                            connection,
                            state_dir,
                            str(repo),
                            Path("unused.ini"),
                            "main",
                            False,
                        )

                interrupted = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                self.assertEqual(interrupted["planner_session_id"], "planner-session")
                self.assertEqual(interrupted["planning_result_pending_validation"], 1)
                self.assertIsNone(interrupted["planning_validation_error"])

                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )

            resumed = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(roles, ["planner", "worker"])
            self.assertEqual(resumed["phase"], "WORKING")
            self.assertEqual(resumed["planning_result_pending_validation"], 0)
            self.assertIsNone(resumed["planning_validation_error"])
            self.assertIsNone(resumed["planning_error"])

    def test_interrupted_first_planner_with_result_resumes_before_validation(self):
        planner_sessions = []
        planner_calls = 0

        def agent(worktree, _state, _config, role_name, _prompt, **kwargs):
            nonlocal planner_calls
            if role_name != "planner":
                return loop_manager.CodexResult(1, "stop after planning")
            planner_calls += 1
            planner_sessions.append(kwargs["session_id"])
            (worktree / "docs/plans").mkdir(parents=True, exist_ok=True)
            write_execplan(worktree, "G-001", "Generated")
            if planner_calls == 1:
                # The CLI starts a session and changes the worktree, then the
                # process dies before it can report a completed result.
                loop_manager.update_project(
                    connection,
                    str(repo),
                    planner_session_id="planner-session",
                )
                raise RuntimeError("process interrupted after Planner edit")
            return loop_manager.CodexResult(0, "", "planner-session")

        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch("loop_manager.run_codex", side_effect=agent):
                with self.assertRaisesRegex(RuntimeError, "process interrupted"):
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )

                interrupted = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                self.assertEqual(interrupted["planning_result_pending_validation"], 1)
                self.assertEqual(interrupted["planning_result_completed"], 0)
                self.assertEqual(interrupted["planner_session_id"], "planner-session")
                self.assertTrue(
                    (Path(interrupted["worktree"]) / "docs/plans/[G-001] Generated.md").is_file()
                )

                self.assertEqual(
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    ),
                    1,
                )

            resumed = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(planner_sessions, [None, "planner-session"])
            self.assertEqual(resumed["phase"], "WORKING")



    def test_interrupted_planner_correction_before_edit_resumes_with_invalid_diff(self):
        planner_calls = 0

        def agent(worktree, _state, _config, role_name, _prompt, **_kwargs):
            nonlocal planner_calls
            self.assertEqual(role_name, "planner")
            planner_calls += 1
            if planner_calls == 1:
                (worktree / "unexpected.txt").write_text(
                    "invalid\n", encoding="utf-8"
                )
                return loop_manager.CodexResult(0, "", "planner-session")
            if planner_calls == 2:
                saved = connection.execute(
                    "SELECT planning_result_pending_validation FROM projects WHERE repo = ?",
                    (str(repo),),
                ).fetchone()
                self.assertEqual(saved["planning_result_pending_validation"], 1)
                raise RuntimeError("process interrupted before Planner correction")
            self.assertEqual(_kwargs["session_id"], "planner-session")
            return loop_manager.CodexResult(1, "planner timeout", "planner-session")

        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.run_codex", side_effect=agent
            ):
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )
                with self.assertRaisesRegex(RuntimeError, "process interrupted"):
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    )

                interrupted = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                self.assertEqual(interrupted["planning_result_pending_validation"], 1)
                self.assertEqual(interrupted["planning_retry_count"], 0)
                self.assertTrue((Path(interrupted["worktree"]) / "unexpected.txt").is_file())
                plan_sha = git_command(repo, "rev-parse", "origin/main")
                git_command(
                    Path(directory) / "remote.git",
                    "update-ref",
                    "refs/heads/planner-correction-update",
                    plan_sha,
                )

                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    0,
                )

            resumed = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(planner_calls, 6)
            self.assertEqual(resumed["planning_retry_count"], 0)
            self.assertEqual(resumed["planning_result_pending_validation"], 0)
            self.assertIsNone(resumed["planning_error"])
            self.assertIsNotNone(resumed["planning_validation_error"])
            self.assertIn(
                loop_manager.TRANSIENT_AGENT_ERROR_PREFIX,
                resumed["last_error"],
            )
            self.assertEqual(
                git_command(
                    repo,
                    "rev-parse",
                    "refs/remotes/origin/planner-correction-update",
                ),
                plan_sha,
            )

    def test_planner_cli_failure_does_not_consume_validation_retry(self):
        planner_calls = 0

        def agent(worktree, _state, _config, role_name, _prompt, **_kwargs):
            nonlocal planner_calls
            if role_name != "planner":
                return loop_manager.CodexResult(1, "stop after planning")
            planner_calls += 1
            invalid = worktree / "unexpected.txt"
            if planner_calls == 1:
                invalid.write_text("invalid\n", encoding="utf-8")
                return loop_manager.CodexResult(0, "", "planner-session")
            if 2 <= planner_calls <= 5:
                saved_retry_count = connection.execute(
                    "SELECT planning_retry_count FROM projects WHERE repo = ?",
                    (str(repo),),
                ).fetchone()["planning_retry_count"]
                self.assertEqual(saved_retry_count, 0)
                return loop_manager.CodexResult(1, "planner timeout", "planner-session")
            invalid.unlink()
            (worktree / "docs/plans").mkdir(parents=True, exist_ok=True)
            write_execplan(worktree, "G-001", "Generated")
            return loop_manager.CodexResult(0, "", "planner-session")

        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.run_codex", side_effect=agent
            ):
                self.assertEqual(
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    ),
                    1,
                )
                self.assertEqual(
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    ),
                    0,
                )
                failed = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                self.assertEqual(failed["planning_retry_count"], 0)
                self.assertIsNotNone(failed["planning_validation_error"])
                self.assertIsNone(failed["planning_error"])

                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )

            final = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(planner_calls, 6)
            self.assertEqual(final["phase"], "WORKING")
            self.assertEqual(final["planning_retry_count"], 0)
            self.assertIsNone(final["planning_validation_error"])

    def test_saved_third_retry_is_validated_without_a_fourth_planner_call(self):
        def invalid_planner(worktree, *_args, **_kwargs):
            (worktree / "unexpected.txt").write_text("invalid\n", encoding="utf-8")
            return loop_manager.CodexResult(0, "", "planner-session")

        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.run_codex", side_effect=invalid_planner
            ) as planner:
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )
                loop_manager.update_project(
                    connection,
                    str(repo),
                    planning_retry_count=loop_manager.MAX_PLANNER_VALIDATION_RETRIES,
                )
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )

            row = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(planner.call_count, 1)
            self.assertEqual(row["planning_retry_count"], 3)
            self.assertIsNotNone(row["planning_error"])

    def test_planning_error_blocks_same_input_and_changed_input_starts_fresh(self):
        planner_sessions = []

        def invalid_planner(worktree, _state, _config, role_name, _prompt, **kwargs):
            self.assertEqual(role_name, "planner")
            planner_sessions.append(kwargs["session_id"])
            (worktree / "unexpected.txt").write_text(
                f"invalid {len(planner_sessions)}\n",
                encoding="utf-8",
            )
            git_command(worktree, "add", "unexpected.txt")
            git_command(worktree, "commit", "-m", "Invalid Planner output")
            session_id = kwargs["session_id"] or "planner-session"
            return loop_manager.CodexResult(0, "", session_id)

        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.run_codex", side_effect=invalid_planner
            ) as planner:
                for _attempt in range(4):
                    self.assertEqual(
                        loop_manager.run_project(
                            connection,
                            state_dir,
                            str(repo),
                            Path("unused.ini"),
                            "main",
                            False,
                        ),
                        1,
                    )

                blocked = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                old_worktree = Path(blocked["worktree"])
                old_fingerprint = blocked["planning_error_fingerprint"]
                self.assertEqual(blocked["planning_retry_count"], 3)
                self.assertEqual(
                    blocked["planning_error"],
                    blocked["planning_validation_error"],
                )
                self.assertEqual(
                    blocked["planning_attempt_fingerprint"],
                    old_fingerprint,
                )
                self.assertEqual(blocked["phase"], "PLANNING")
                self.assertIsNone(blocked["branch"])
                self.assertIsNone(blocked["plan_path"])
                self.assertTrue(old_worktree.is_dir())
                self.assertEqual(planner_sessions, [None] + ["planner-session"] * 3)

                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )
                self.assertEqual(planner.call_count, 4)

                (repo / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
                commit_and_push(repo, "Change planning input")
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )

            restarted = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            diagnostic = loop_manager.planning_diagnostics_path(
                state_dir, str(repo), old_fingerprint
            )
            self.assertEqual(planner.call_count, 5)
            self.assertEqual(planner_sessions[-1], None)
            self.assertFalse(old_worktree.exists())
            self.assertNotEqual(Path(restarted["worktree"]), old_worktree)
            self.assertNotEqual(
                restarted["planning_attempt_fingerprint"],
                old_fingerprint,
            )
            self.assertEqual(restarted["planning_retry_count"], 0)
            self.assertIsNone(restarted["planning_error"])
            self.assertIsNone(restarted["planning_error_fingerprint"])
            self.assertTrue(diagnostic.is_file())
            diagnostic_text = diagnostic.read_text(encoding="utf-8")
            self.assertIn(blocked["planning_error"], diagnostic_text)
            self.assertIn("unexpected.txt", diagnostic_text)
            self.assertIn("+invalid 4", diagnostic_text)

    def test_planning_error_gate_precedes_dirty_base_worktree_check(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            preserved_worktree = state_dir / "preserved-planning-worktree"
            preserved_worktree.mkdir(parents=True)
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            error = "Planner는 docs/plans의 ExecPlan 하나만 변경해야 합니다."
            fingerprint = loop_manager.planning_input_fingerprint(repo, "main")
            connection.execute(
                """
                INSERT INTO projects (
                    repo, base_branch, planning_error, planning_error_fingerprint,
                    phase, worktree
                ) VALUES (?, 'main', ?, ?, 'PLANNING', ?)
                """,
                (str(repo), error, fingerprint, str(preserved_worktree)),
            )
            connection.commit()
            (repo / "local-uncommitted-change.txt").write_text("dirty\n", encoding="utf-8")
            stderr = io.StringIO()

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch("loop_manager.run_codex") as planner, redirect_stderr(stderr):
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )

            self.assertIn(error, stderr.getvalue())
            self.assertIn(str(preserved_worktree), stderr.getvalue())
            planner.assert_not_called()

    def test_changed_remote_input_restarts_failed_planning_from_latest_origin(self):
        seen_source_contents = []

        def fail_planner(worktree, *_args, **_kwargs):
            seen_source_contents.append(
                (worktree / "src/app.py").read_text(encoding="utf-8")
            )
            return 1, "planner failed"

        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.run_codex", side_effect=fail_planner
            ):
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )
                first = connection.execute(
                    "SELECT active_sha, worktree FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()

                (repo / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
                commit_and_push(repo, "Change source while planning is pending")

                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )

            second = connection.execute(
                "SELECT active_sha, worktree, phase, planning_fingerprint FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(seen_source_contents, ["VALUE = 1\n", "VALUE = 2\n"])
            self.assertNotEqual(second["active_sha"], first["active_sha"])
            self.assertNotEqual(second["worktree"], first["worktree"])
            self.assertFalse(Path(first["worktree"]).exists())
            self.assertEqual(second["active_sha"], git_command(repo, "rev-parse", "origin/main"))
            self.assertEqual(second["phase"], "PLANNING")
            self.assertIsNone(second["planning_fingerprint"])

    def test_new_execplan_saves_cataloged_input_fingerprint(self):
        roles = []

        def agent(worktree, _state, _config, role_name, _prompt, **_kwargs):
            roles.append(role_name)
            if role_name == "planner":
                (worktree / "docs/plans").mkdir(parents=True, exist_ok=True)
                write_execplan(worktree, "G-001", "Generated")
                return 0, ""
            return 1, "stop after planning"

        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            expected_fingerprint = loop_manager.planning_input_fingerprint(repo, "main")

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.run_codex", side_effect=agent
            ):
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )

            row = connection.execute(
                "SELECT phase, planning_fingerprint FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(roles, ["planner", "worker"])
            self.assertEqual(row["phase"], "WORKING")
            self.assertEqual(row["planning_fingerprint"], expected_fingerprint)

    def test_execplan_frontmatter_parser(self):
        values = loop_manager.parse_frontmatter(
            """---
id: G-002
title: Small Task
sources:
  - docs/PRODUCT.md
depends_on:
  - G-001
complexity: low
risk: medium
---
"""
        )

        self.assertEqual(values["id"], "G-002")
        self.assertEqual(values["sources"], ["docs/PRODUCT.md"])
        self.assertEqual(values["depends_on"], ["G-001"])

    def test_current_execplan_is_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_catalog_root(directory)
            plan = write_execplan(root, "G-001", "Current Plan")

            values = loop_manager.validate_execplan(
                root, plan.relative_to(root).as_posix()
            )

            self.assertEqual(values["id"], "G-001")

    def test_execplan_catalog_is_deterministic_and_indexed_by_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_catalog_root(directory)
            requirement = root / "docs/requirements/[R-001] Active.md"
            requirement.parent.mkdir()
            requirement.write_text("# Requirement\n", encoding="utf-8")
            write_execplan(root, "G-003", "Third", depends_on=("G-002",))
            write_execplan(root, "G-001", "First")
            write_execplan(
                root,
                "G-002",
                "Second",
                sources=("docs/requirements/[R-001] Active.md",),
                depends_on=("G-001",),
            )
            nested = root / "docs/plans/archive/nested/[G-999] Ignored.md"
            nested.parent.mkdir(parents=True)
            nested.write_text("not an active plan", encoding="utf-8")

            catalog = loop_manager.load_execplan_catalog(root)

        self.assertEqual(list(catalog), ["G-001", "G-002", "G-003"])
        self.assertEqual(catalog["G-002"]["title"], "Second")

    def test_execplan_catalog_rejects_duplicate_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_catalog_root(directory)
            write_execplan(root, "G-001", "First")
            write_execplan(root, "G-001", "Duplicate")

            with self.assertRaisesRegex(RuntimeError, "중복 ExecPlan id.*G-001"):
                loop_manager.load_execplan_catalog(root)

    def test_execplan_catalog_restricts_sources_to_active_product_inputs(self):
        invalid_sources = (
            (),
            ("/docs/PRODUCT.md",),
            ("../PRODUCT.md",),
            ("docs/requirements/archive/Old.md",),
            ("README.md",),
            ("docs/requirements/Missing.md",),
        )
        for sources in invalid_sources:
            with self.subTest(sources=sources), tempfile.TemporaryDirectory() as directory:
                root = make_catalog_root(directory)
                (root / "README.md").write_text("# Readme\n", encoding="utf-8")
                archived = root / "docs/requirements/archive/Old.md"
                archived.parent.mkdir(parents=True)
                archived.write_text("# Old\n", encoding="utf-8")
                write_execplan(root, "G-001", "Invalid Source", sources=sources)

                with self.assertRaisesRegex(RuntimeError, "ExecPlan source"):
                    loop_manager.load_execplan_catalog(root)

    def test_execplan_catalog_validates_fixed_metadata(self):
        cases = (
            ("id", "BAD", "Plan", "low", "low", "id는 G-000"),
            ("filename", "G-001", "Plan", "low", "low", "파일명"),
            ("complexity", "G-001", "Plan", "tiny", "low", "complexity"),
            ("risk", "G-001", "Plan", "low", "extreme", "risk"),
            ("keys", "G-001", "Plan", "low", "low", "frontmatter key"),
        )
        for name, plan_id, title, complexity, risk, message in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = make_catalog_root(directory)
                plan = write_execplan(
                    root, plan_id, title, complexity=complexity, risk=risk
                )
                if name == "filename":
                    plan.rename(plan.with_name("[G-001] Wrong.md"))
                elif name == "keys":
                    contents = plan.read_text(encoding="utf-8")
                    plan.write_text(
                        contents.replace("risk: low\n", "risk: low\nstatus: READY\n"),
                        encoding="utf-8",
                    )

                with self.assertRaisesRegex(RuntimeError, message):
                    loop_manager.load_execplan_catalog(root)

    def test_execplan_catalog_rejects_invalid_and_missing_dependencies(self):
        cases = (("G-1", "depends_on ID 형식"), ("G-999", "존재하지 않는 depends_on"))
        for dependency, message in cases:
            with self.subTest(dependency=dependency), tempfile.TemporaryDirectory() as directory:
                root = make_catalog_root(directory)
                write_execplan(root, "G-001", "Plan", depends_on=(dependency,))

                with self.assertRaisesRegex(RuntimeError, message):
                    loop_manager.load_execplan_catalog(root)

    def test_execplan_catalog_rejects_self_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_catalog_root(directory)
            write_execplan(root, "G-001", "Plan", depends_on=("G-001",))

            with self.assertRaisesRegex(RuntimeError, r"G-001 -> G-001"):
                loop_manager.load_execplan_catalog(root)

    def test_execplan_catalog_rejects_multistep_cycle_with_all_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_catalog_root(directory)
            write_execplan(root, "G-001", "First", depends_on=("G-003",))
            write_execplan(root, "G-002", "Second", depends_on=("G-001",))
            write_execplan(root, "G-003", "Third", depends_on=("G-002",))

            with self.assertRaises(RuntimeError) as raised:
                loop_manager.load_execplan_catalog(root)

        message = str(raised.exception)
        self.assertIn("순환 ExecPlan 의존성", message)
        for plan_id in ("G-001", "G-002", "G-003"):
            self.assertIn(plan_id, message)

    def test_selected_execplan_requires_valid_whole_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_catalog_root(directory)
            selected = write_execplan(root, "G-001", "Selected")
            write_execplan(root, "G-002", "Broken", depends_on=("G-999",))
            relative_path = selected.relative_to(root).as_posix()

            with self.assertRaisesRegex(RuntimeError, "G-002.*G-999"):
                loop_manager.validate_execplan(root, relative_path)

    def test_role_resolves_model_alias(self):
        config = loop_manager.ROOT / "config/config.ini.example"

        with mock.patch(
            "loop_manager.codex_model_efforts",
            return_value={"gpt-5.6-sol": frozenset({"xhigh"})},
        ):
            self.assertEqual(loop_manager.role(config, "planner"), ("gpt-5.6-sol", "xhigh"))

    def test_worker_level_boundaries_and_cap(self):
        cases = [
            ("low", "low", 1),
            ("low", "medium", 2),
            ("low", "high", 3),
            ("low", "critical", 4),
            ("medium", "low", 3),
            ("medium", "medium", 4),
            ("medium", "high", 5),
            ("medium", "critical", 6),
            ("high", "low", 5),
            ("high", "medium", 6),
            ("high", "high", 7),
            ("high", "critical", 7),
        ]

        for complexity, risk, expected in cases:
            with self.subTest(complexity=complexity, risk=risk):
                plan = {"complexity": complexity, "risk": risk}
                self.assertEqual(loop_manager.worker_level(plan), expected)

    def test_worker_profile_resolves_level_alias(self):
        config = loop_manager.ROOT / "config/config.ini.example"

        model_efforts = {
            "gpt-5.6-luna": frozenset({"medium"}),
            "gpt-5.6-terra": frozenset({"high"}),
            "gpt-5.6-sol": frozenset({"xhigh"}),
        }
        with mock.patch("loop_manager.codex_model_efforts", return_value=model_efforts):
            self.assertEqual(loop_manager.worker_profile(config, 1), ("gpt-5.6-luna", "medium"))
            self.assertEqual(loop_manager.worker_profile(config, 4), ("gpt-5.6-terra", "high"))
            self.assertEqual(loop_manager.worker_profile(config, 7), ("gpt-5.6-sol", "xhigh"))

    def test_worker_profile_rejects_invalid_level(self):
        config = loop_manager.ROOT / "config/config.ini.example"

        for level in (0, 9, True):
            with self.subTest(level=level):
                with self.assertRaisesRegex(RuntimeError, "Worker level"):
                    loop_manager.worker_profile(config, level)

    def test_worker_profile_rejects_invalid_configuration(self):
        cases = [
            (
                "missing-level",
                "[codex.models]\nfast = model-fast\n[codex.worker]\nlevel2 = fast,medium\n",
                "level1 설정이 없습니다",
            ),
            (
                "empty-level",
                "[codex.models]\nfast = model-fast\n[codex.worker]\nlevel1 =\n",
                "Model alias/Effort 설정이 비어",
            ),
            (
                "malformed-profile",
                "[codex.models]\nfast = model-fast\n[codex.worker]\nlevel1 = fast\n",
                "Model alias,Effort",
            ),
            (
                "empty-alias",
                "[codex.models]\nfast = model-fast\n[codex.worker]\nlevel1 = ,medium\n",
                "Model alias 설정이 비어",
            ),
            (
                "empty-effort",
                "[codex.models]\nfast = model-fast\n[codex.worker]\nlevel1 = fast,\n",
                "Effort 설정이 비어",
            ),
            (
                "unsupported-effort",
                "[codex.models]\nfast = gpt-5.6-sol\n[codex.worker]\nlevel1 = fast,minimal\n",
                "Model/Effort 조합을 Codex CLI가 지원하지 않습니다: gpt-5.6-sol,minimal",
            ),
            (
                "unsupported-model",
                "[codex.models]\nfast = unavailable\n[codex.worker]\nlevel1 = fast,medium\n",
                "Model을 Codex CLI가 지원하지 않습니다: unavailable",
            ),
            (
                "missing-alias",
                "[codex.models]\nfast = model-fast\n[codex.worker]\nlevel1 = strong,high\n",
                "Model alias가 .* 없습니다",
            ),
            (
                "empty-model",
                "[codex.models]\nfast =\n[codex.worker]\nlevel1 = fast,medium\n",
                "Model 설정이 비어",
            ),
            (
                "malformed-ini",
                "codex.models]\nfast = model-fast\n",
                "설정 파일 형식이 올바르지 않습니다",
            ),
            (
                "malformed-profile-interpolation",
                "[codex.models]\nfast = model-fast\n[codex.worker]\nlevel1 = fast,%\n",
                r"설정 값 형식이 올바르지 않습니다: \[codex.worker\] level1",
            ),
            (
                "malformed-model-interpolation",
                "[codex.models]\nfast = model-%\n[codex.worker]\nlevel1 = fast,medium\n",
                r"설정 값 형식이 올바르지 않습니다: \[codex.models\] fast",
            ),
        ]

        with tempfile.TemporaryDirectory() as directory:
            for name, contents, message in cases:
                with self.subTest(name=name):
                    config = Path(directory) / f"{name}.ini"
                    config.write_text(contents, encoding="utf-8")
                    with mock.patch(
                        "loop_manager.codex_model_efforts",
                        return_value={
                            "gpt-5.6-sol": frozenset(
                                {"low", "medium", "high", "xhigh", "max", "ultra"}
                            )
                        },
                    ):
                        with self.assertRaisesRegex(RuntimeError, message):
                            loop_manager.worker_profile(config, 1)

    def test_codex_model_efforts_reads_effective_catalog_for_worktree(self):
        catalog = {
            "models": [
                {
                    "slug": "gpt-reserve",
                    "supported_reasoning_levels": [
                        {"effort": "max"}, {"effort": "ultra"}
                    ],
                }
            ]
        }
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(catalog))
        worktree = Path("/project/worktree")
        with mock.patch("loop_manager.command", return_value=completed) as command:
            efforts = loop_manager.codex_model_efforts(worktree)

        self.assertEqual(efforts, {"gpt-reserve": frozenset({"max", "ultra"})})
        self.assertEqual(command.call_args.args[0], ["codex", "debug", "models"])
        self.assertEqual(command.call_args.kwargs["cwd"], worktree)

    def test_run_codex_validates_profile_in_worker_catalog(self):
        config = loop_manager.ROOT / "config/config.ini.example"
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "worktree"
            worktree.mkdir()
            state_dir = Path(directory) / "state"
            completed = subprocess.CompletedProcess([], 0)
            with mock.patch(
                "loop_manager.codex_model_efforts",
                return_value={"gpt-5.6-terra": frozenset({"high"})},
            ) as model_efforts, mock.patch(
                "loop_manager.subprocess.run", return_value=completed
            ):
                loop_manager.run_codex(
                    worktree,
                    state_dir,
                    config,
                    "worker",
                    "prompt",
                    read_only=False,
                    level=4,
                )

        model_efforts.assert_called_once_with(worktree)

    def test_profiles_validate_model_specific_efforts(self):
        model_efforts = {
            "gpt-5.6-sol": frozenset({"low", "medium", "high", "xhigh", "max", "ultra"}),
            "gpt-5.6-luna": frozenset({"low", "medium", "high", "xhigh", "max"}),
        }
        contents = (
            "[codex.models]\n"
            "strong = gpt-5.6-sol\n"
            "fast = gpt-5.6-luna\n"
            "[codex.roles]\n"
            "planner = strong,max\n"
            "reviewer = strong,ultra\n"
            "[codex.worker]\n"
            "level1 = fast,max\n"
            "level2 = strong,minimal\n"
            "level3 = fast,ultra\n"
        )

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.ini"
            config.write_text(contents, encoding="utf-8")
            with mock.patch(
                "loop_manager.codex_model_efforts",
                return_value=model_efforts,
            ):
                self.assertEqual(loop_manager.role(config, "planner"), ("gpt-5.6-sol", "max"))
                self.assertEqual(loop_manager.role(config, "reviewer"), ("gpt-5.6-sol", "ultra"))
                self.assertEqual(loop_manager.worker_profile(config, 1), ("gpt-5.6-luna", "max"))
                for level in (2, 3):
                    with self.subTest(level=level):
                        with self.assertRaisesRegex(RuntimeError, "Model/Effort 조합"):
                            loop_manager.worker_profile(config, level)

    def test_worker_codex_uses_level_model_and_effort(self):
        config = loop_manager.ROOT / "config/config.ini.example"
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "worktree"
            worktree.mkdir()
            state_dir = Path(directory) / "state"
            completed = subprocess.CompletedProcess([], 0)
            with mock.patch(
                "loop_manager.codex_model_efforts",
                return_value={"gpt-5.6-terra": frozenset({"high"})},
            ), mock.patch("loop_manager.subprocess.run", return_value=completed) as run:
                code, message = loop_manager.run_codex(
                    worktree,
                    state_dir,
                    config,
                    "worker",
                    "prompt",
                    read_only=False,
                    level=4,
                )

        self.assertEqual((code, message), (0, ""))
        arguments = run.call_args.args[0]
        self.assertEqual(arguments[arguments.index("-m") + 1], "gpt-5.6-terra")
        self.assertIn('model_reasoning_effort="high"', arguments)

    def test_planner_codex_persists_thread_id_and_resumes_exact_session(self):
        config = loop_manager.ROOT / "config/config.ini.example"
        calls = []
        call_options = []

        thread_event = (
            json.dumps(
                {
                    "type": "thread.started",
                    "thread_id": "0199a213-81c0-7800-8aa1-bbab2a035a53",
                }
            )
            + "\n"
        )

        class StartedOutput:
            def __iter__(self):
                return iter((thread_event,))

            def close(self):
                pass

        class StartedProcess:
            def __init__(self, arguments, **kwargs):
                calls.append(arguments)
                call_options.append(kwargs)
                self.stdin = mock.Mock()
                self.stdout = StartedOutput()

            def wait(self):
                return 0

            def kill(self):
                raise AssertionError("completed Planner process must not be killed")

        def codex_run(arguments, **kwargs):
            calls.append(arguments)
            call_options.append(kwargs)
            return subprocess.CompletedProcess(arguments, 0)

        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "worktree"
            worktree.mkdir()
            state_dir = Path(directory) / "state"
            with mock.patch(
                "loop_manager.codex_model_efforts",
                return_value={"gpt-5.6-sol": frozenset({"xhigh"})},
            ), mock.patch(
                "loop_manager.subprocess.Popen", side_effect=StartedProcess
            ), mock.patch("loop_manager.subprocess.run", side_effect=codex_run):
                started = loop_manager.run_codex(
                    worktree,
                    state_dir,
                    config,
                    "planner",
                    "plan",
                    read_only=False,
                    persist_session=True,
                )
                resumed = loop_manager.run_codex(
                    worktree,
                    state_dir,
                    config,
                    "planner",
                    "repair",
                    read_only=False,
                    persist_session=True,
                    session_id=started.session_id,
                )

        self.assertEqual(started.code, 0)
        self.assertEqual(
            started.session_id,
            "0199a213-81c0-7800-8aa1-bbab2a035a53",
        )
        self.assertEqual(resumed.session_id, started.session_id)
        self.assertEqual(calls[0][:3], ["codex", "exec", "--json"])
        self.assertNotIn("--ephemeral", calls[0])
        self.assertEqual(
            calls[1][:6],
            ["codex", "exec", "--approve-for-me", "-C", str(worktree), "resume"],
        )
        self.assertIn("--json", calls[1])
        self.assertIn(started.session_id, calls[1])
        self.assertEqual(call_options[0]["cwd"], worktree)
        self.assertEqual(call_options[1]["cwd"], worktree)
        self.assertNotIn("PYTHONDONTWRITEBYTECODE", call_options[0]["env"])
        self.assertNotIn("PYTHONDONTWRITEBYTECODE", call_options[1]["env"])

    def test_planner_environment_does_not_override_parent_bytecode_policy(self):
        parent_environment = {
            "HOME": "/home/test",
            "PATH": "/bin",
            "PYTHONDONTWRITEBYTECODE": "0",
            "PYTHONPYCACHEPREFIX": "/parent-cache",
        }
        with mock.patch.dict(os.environ, parent_environment, clear=True):
            planner_codex = loop_manager.agent_environment("codex", "planner")
            planner_claude = loop_manager.agent_environment("claude", "planner")
            worker_codex = loop_manager.agent_environment("codex", "worker")
            self.assertEqual(os.environ["PYTHONDONTWRITEBYTECODE"], "0")

        self.assertNotIn("PYTHONDONTWRITEBYTECODE", planner_codex)
        self.assertNotIn("PYTHONDONTWRITEBYTECODE", planner_claude)
        self.assertNotIn("PYTHONPYCACHEPREFIX", planner_codex)
        self.assertNotIn("PYTHONDONTWRITEBYTECODE", worker_codex)

    def test_resumed_read_only_codex_keeps_managed_worktree_boundary(self):
        session_id = "0199a213-81c0-7800-8aa1-bbab2a035a53"
        observed = {}

        def resumed(arguments, **kwargs):
            observed["arguments"] = arguments
            observed["cwd"] = kwargs["cwd"]
            (kwargs["cwd"] / "resume-output.txt").write_text("managed\n", encoding="utf-8")
            return subprocess.CompletedProcess(arguments, 0)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "base"
            base.mkdir()
            worktree = root / "managed"
            worktree.mkdir()
            state_dir = root / "state"
            with mock.patch(
                "loop_manager.role", return_value=("gpt-5.6-sol", "high")
            ), mock.patch("loop_manager.subprocess.run", side_effect=resumed):
                result = loop_manager.run_codex(
                    worktree,
                    state_dir,
                    root / "unused.ini",
                    "reviewer",
                    "review",
                    read_only=True,
                    session_id=session_id,
                )

            self.assertEqual(result.code, 0)
            self.assertEqual(observed["cwd"], worktree)
            self.assertTrue((worktree / "resume-output.txt").is_file())
            self.assertFalse((base / "resume-output.txt").exists())

        arguments = observed["arguments"]
        self.assertEqual(
            arguments[:7],
            ["codex", "exec", "--sandbox", "read-only", "-C", str(worktree), "resume"],
        )
        self.assertIn(session_id, arguments)

    def test_interrupted_planner_persists_started_session_and_resumes(self):
        session_id = "0199a213-81c0-7800-8aa1-bbab2a035a53"
        thread_event = json.dumps(
            {"type": "thread.started", "thread_id": session_id}
        ) + "\n"

        class InterruptedOutput:
            def __iter__(self):
                yield thread_event
                raise RuntimeError("Planner process interrupted")

            def close(self):
                pass

        class InterruptedProcess:
            def __init__(self):
                self.stdin = mock.Mock()
                self.stdout = InterruptedOutput()
                self.killed = False

            def wait(self):
                return -9

            def kill(self):
                self.killed = True

        process = InterruptedProcess()
        real_popen = subprocess.Popen

        def start_process(arguments, *args, **kwargs):
            if arguments[0] == "codex":
                return process
            return real_popen(arguments, *args, **kwargs)

        resumed_sessions = []

        def stopped_resume(_worktree, _state, _config, role_name, _prompt, **kwargs):
            self.assertEqual(role_name, "planner")
            resumed_sessions.append(kwargs["session_id"])
            return loop_manager.CodexResult(
                1, "stop after persisted session check", kwargs["session_id"]
            )

        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            config = loop_manager.ROOT / "config/config.ini.example"
            connection = loop_manager.connect_database(state_dir, create=True)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.codex_model_efforts",
                return_value={"gpt-5.6-sol": frozenset({"xhigh"})},
            ), mock.patch(
                "loop_manager.subprocess.Popen", side_effect=start_process
            ):
                with self.assertRaisesRegex(RuntimeError, "Planner process interrupted"):
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        config,
                        "main",
                        False,
                    )
            connection.close()

            with loop_manager.connect_database(state_dir) as connection:
                interrupted = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                self.assertEqual(interrupted["phase"], "PLANNING")
                self.assertEqual(interrupted["planner_session_id"], session_id)
                self.assertTrue(Path(interrupted["worktree"]).is_dir())
                self.assertTrue(process.killed)

                with mock.patch(
                    "loop_manager.validate_repository", return_value=repo
                ), mock.patch(
                    "loop_manager.remote_base", return_value="main"
                ), mock.patch(
                    "loop_manager.ensure_safe_base_worktree"
                ), mock.patch(
                    "loop_manager.run_codex", side_effect=stopped_resume
                ):
                    self.assertEqual(
                        loop_manager.run_project(
                            connection,
                            state_dir,
                            str(repo),
                            config,
                            "main",
                            False,
                        ),
                        1,
                    )

        self.assertEqual(resumed_sessions, [session_id])

    def test_unsupported_worker_effort_fails_before_codex_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.ini"
            config.write_text(
                "[codex.models]\nstrong = gpt-5.6-sol\n[codex.worker]\nlevel1 = strong,minimal\n",
                encoding="utf-8",
            )
            with mock.patch(
                "loop_manager.codex_model_efforts",
                return_value={
                    "gpt-5.6-sol": frozenset({"low", "medium", "high", "xhigh", "max", "ultra"})
                },
            ), mock.patch("loop_manager.subprocess.run") as run:
                with self.assertRaisesRegex(RuntimeError, "Model/Effort 조합"):
                    loop_manager.run_codex(
                        root / "worktree",
                        root / "state",
                        config,
                        "worker",
                        "prompt",
                        read_only=False,
                        level=1,
                    )
            run.assert_not_called()

    def _make_active_worker_project(self, directory: str):
        repo = make_remote_repository(directory)
        state_dir = Path(directory) / "state"
        state_dir.mkdir()
        (repo / "docs/plans").mkdir()
        plan = write_execplan(repo, "G-001", "Worker Boundary")
        commit_and_push(repo, "Add worker plan")
        plan_sha = git_command(repo, "rev-parse", "origin/main")
        branch, worktree = loop_manager.expected_execplan_workspace(
            state_dir, str(repo), "G-001"
        )
        loop_manager.make_worktree(repo, worktree, branch, "main")
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        connection.execute(
            """
            INSERT INTO projects (
                repo, base_branch, active_sha, plan_sha, phase, branch,
                worktree, plan_path
            ) VALUES (?, 'main', ?, ?, 'WORKING', ?, ?, ?)
            """,
            (
                str(repo),
                plan_sha,
                plan_sha,
                branch,
                str(worktree),
                plan.relative_to(repo).as_posix(),
            ),
        )
        connection.commit()
        return repo, state_dir, connection

    def test_worker_without_diff_requeues_after_source_change(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection = self._make_active_worker_project(directory)
            plan_sha = git_command(repo, "rev-parse", "origin/main")
            managed = Path(
                connection.execute(
                    "SELECT worktree FROM projects WHERE repo = ?",
                    (str(repo),),
                ).fetchone()["worktree"]
            )
            planning_fingerprint = loop_manager.planning_input_fingerprint(repo, "main")
            connection.execute(
                """
                UPDATE projects
                SET active_sha = ?, plan_sha = ?, worktree = ?,
                    planning_fingerprint = ?, processed_sha = ?
                WHERE repo = ?
                """,
                (
                    plan_sha,
                    plan_sha,
                    str(managed),
                    planning_fingerprint,
                    plan_sha,
                    str(repo),
                ),
            )
            connection.commit()
            roles = []

            def worker(worktree, _state, _config, role_name, _prompt, **_kwargs):
                roles.append(role_name)
                if role_name == "planner":
                    write_execplan(
                        worktree,
                        "G-002",
                        "Deferred",
                        priority="low",
                    )
                    return loop_manager.CodexResult(0, "", "planner-session")
                if roles.count("worker") == 1:
                    return loop_manager.CodexResult(0, worker_result(), "worker-session")
                return loop_manager.CodexResult(1, "stop after source change", "worker-session")

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex", side_effect=worker
            ), mock.patch(
                "loop_manager.verify", return_value=(True, "verify: PASS")
            ) as verify:
                blocked_result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

                blocked_project = connection.execute(
                    """
                    SELECT phase, worktree, pr_url, last_error, verify_result
                    FROM projects WHERE repo = ?
                    """,
                    (str(repo),),
                ).fetchone()
                blocked_plan = connection.execute(
                    """
                    SELECT status, worktree, pr_url, last_error, verify_result,
                           blocked_fingerprint
                    FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'
                    """,
                    (str(repo),),
                ).fetchone()
                worktree_preserved = Path(blocked_plan["worktree"]).exists()
                connection.execute(
                    "UPDATE exec_plans SET retry_count = 2 WHERE repo = ? AND plan_id = 'G-001'",
                    (str(repo),),
                )
                connection.commit()
                git_command(repo, "worktree", "remove", "--force", "--force",
                            str(managed))
                self.assertFalse(managed.exists())

                unchanged_result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )
                unchanged_plan = connection.execute(
                    "SELECT status, retry_count FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                    (str(repo),),
                ).fetchone()
                worktree_restored = managed.is_dir()

                blocked_plan_path = managed / "docs/plans/[G-001] Worker Boundary.md"
                original_plan_text = blocked_plan_path.read_text(encoding="utf-8")
                blocked_plan_path.write_text(
                    original_plan_text.replace("priority: normal", "priority: critical"),
                    encoding="utf-8",
                )
                metadata_result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )
                rejected_metadata = connection.execute(
                    """
                    SELECT status, priority, depends_on, last_error, retry_count
                    FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'
                    """,
                    (str(repo),),
                ).fetchone()
                blocked_plan_path.write_text(original_plan_text, encoding="utf-8")

                (repo / "docs/PRODUCT.md").write_text(
                    "# Product changed to resolve block\n",
                    encoding="utf-8",
                )
                commit_and_push(repo, "Change blocked plan source")
                resumed_result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            resumed_project = connection.execute(
                """
                SELECT active_sha, phase, worktree, pr_url, last_error
                FROM projects WHERE repo = ?
                """,
                (str(repo),),
            ).fetchone()
            resumed_plan = connection.execute(
                """
                SELECT status, pr_url, last_error, blocked_fingerprint, retry_count
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'
                """,
                (str(repo),),
            ).fetchone()
            rebased_source = (managed / "docs/PRODUCT.md").read_text(encoding="utf-8")
            managed_head = git_command(managed, "rev-parse", "HEAD")
            origin_head = git_command(repo, "rev-parse", "origin/main")

        self.assertEqual(
            (blocked_result, unchanged_result, metadata_result, resumed_result),
            (0, 0, 0, 1),
        )
        self.assertEqual(roles, ["worker", "planner", "worker"])
        verify.assert_not_called()
        self.assertIsNone(blocked_project["phase"])
        self.assertEqual(blocked_plan["verify_result"], "")
        self.assertIsNone(blocked_project["pr_url"])
        self.assertTrue(worktree_preserved)
        self.assertIn("PR을 생성할 수 없습니다", blocked_plan["last_error"])
        self.assertEqual(blocked_plan["status"], "PLAN_BLOCKED")
        self.assertIsNotNone(blocked_plan["blocked_fingerprint"])
        self.assertEqual(tuple(unchanged_plan), ("PLAN_BLOCKED", 2))
        self.assertTrue(worktree_restored)
        self.assertEqual(rejected_metadata["status"], "PLAN_BLOCKED")
        self.assertEqual(rejected_metadata["priority"], "normal")
        self.assertEqual(rejected_metadata["depends_on"], "[]")
        self.assertIn(
            "reconciled catalog와 다릅니다",
            rejected_metadata["last_error"],
        )
        self.assertEqual(rejected_metadata["retry_count"], 2)
        self.assertEqual(resumed_project["phase"], "WORKING")
        self.assertEqual(resumed_project["last_error"], "stop after source change")
        self.assertEqual(resumed_project["active_sha"], managed_head)
        self.assertEqual(resumed_project["active_sha"], origin_head)
        self.assertEqual(resumed_plan["status"], "IMPLEMENTING")
        self.assertIsNone(resumed_plan["blocked_fingerprint"])
        self.assertEqual(resumed_plan["retry_count"], 0)
        self.assertEqual(rebased_source, "# Product changed to resolve block\n")

    def test_rebase_that_drops_worker_patch_blocks_before_review(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection = self._make_active_worker_project(directory)
            plan_sha = git_command(repo, "rev-parse", "origin/main")
            managed = Path(
                connection.execute(
                    "SELECT worktree FROM projects WHERE repo = ?",
                    (str(repo),),
                ).fetchone()["worktree"]
            )
            connection.execute(
                """
                UPDATE projects
                SET active_sha = ?, plan_sha = ?, worktree = ?
                WHERE repo = ?
                """,
                (plan_sha, plan_sha, str(managed), str(repo)),
            )
            connection.commit()
            (repo / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            commit_and_push(repo, "Apply worker change upstream")
            roles = []

            def worker(worktree, _state, _config, role_name, _prompt, **_kwargs):
                roles.append(role_name)
                (worktree / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
                return loop_manager.CodexResult(
                    0,
                    worker_result("Apply worker change"),
                    "worker-session",
                )

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex", side_effect=worker
            ), mock.patch(
                "loop_manager.verify", return_value=(True, "verify: PASS"),
            ) as verify:
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan = connection.execute(
                """
                SELECT status, last_error, blocked_fingerprint
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'
                """,
                (str(repo),),
            ).fetchone()
            ahead = git_command(managed, "rev-list", "--count", "origin/main..HEAD")

        self.assertEqual(result, 0)
        self.assertEqual(roles, ["worker"])
        # The revision gate now refreshes the base before Worker dispatch, so
        # it never validates the already-absorbed patch on the stale base.
        verify.assert_not_called()
        self.assertEqual(ahead, "0")
        self.assertIsNone(project["phase"])
        self.assertIn("Worker가 변경을 만들지 않아", plan["last_error"])
        self.assertEqual(plan["status"], "PLAN_BLOCKED")
        self.assertIsNotNone(plan["blocked_fingerprint"])

    def test_worker_and_reviewer_prompts_define_test_execution_boundary(self):
        worker = loop_manager.worker_prompt("docs/plans/[G-001] Worker Boundary.md", None)
        reviewer = loop_manager.reviewer_prompt("main")
        no_test_reviewer = loop_manager.reviewer_prompt(
            "main", "실행 가능한 관련 targeted unit test가 없습니다: 문서 변경만 포함합니다.",
        )

        for phrase in (
            "최소 범위의 targeted unit test만 실행",
            "개별 test case, test class, test module",
            "같은 세션에서 원인을 수정하고 같은 targeted test를 다시 실행",
            "실행하지 못한 구체적인 사유를 최종 결과에 남겨라",
            "전체 test suite, integration test, end-to-end test",
            "프로젝트 전체 lint·build·type check",
            "scripts/verify 실행은 절대 실행하지 마라",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, worker)
        self.assertNotIn("targeted: PASS", reviewer)
        self.assertNotIn("verify: PASS", reviewer)
        self.assertIn("문서 변경만 포함합니다", no_test_reviewer)
        self.assertIn("테스트 command와 scripts/verify를 실행하지 말고", reviewer)
        self.assertIn("ISSUE_ID: relative/path::stable-symbol-or-test::category-slug", reviewer)
        self.assertIn("이전과 정확히 같은 ID", reviewer)
        self.assertIn("RERUN:", worker)
        self.assertIn("FAILED_COMMAND:", worker)
        self.assertIn("공백을 정규화한 뒤 정확히 같아야", worker)
        self.assertIn("성공·실패가 섞인 요약", worker)
        self.assertIn("COMMIT_TITLE:", worker)
        self.assertIn("PR_SUMMARY:", worker)
        self.assertIn("TARGETED_TESTS:", worker)
        self.assertIsNone(
            loop_manager.reviewer_targeted_test_reason("targeted test: PASS")
        )
        self.assertEqual(
            loop_manager.reviewer_targeted_test_reason(
                "실행 가능한 관련 targeted unit test가 없습니다: 문서 변경만 포함합니다."
            ),
            "실행 가능한 관련 targeted unit test가 없습니다: 문서 변경만 포함합니다.",
        )
        self.assertEqual(
            loop_manager.reviewer_targeted_test_reason(
                "문서 바이트 적용만 수행하므로 실행 가능한 관련 unit test가 없습니다."
            ),
            "문서 바이트 적용만 수행하므로 실행 가능한 관련 unit test가 없습니다.",
        )

    def test_existing_database_adds_worker_session_and_result_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            database = sqlite3.connect(state_dir / "loop-manager.db")
            database.execute(
                """
                CREATE TABLE projects (
                    repo TEXT PRIMARY KEY,
                    base_branch TEXT NOT NULL,
                    phase TEXT
                )
                """
            )
            database.execute(
                "INSERT INTO projects (repo, base_branch, phase) VALUES ('repo', 'main', 'WORKING')"
            )
            database.commit()
            database.close()

            with loop_manager.connect_database(state_dir) as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
                row = connection.execute("SELECT * FROM projects WHERE repo = 'repo'").fetchone()

        self.assertTrue({"worker_session_id", "worker_result", "verify_result"} <= columns)
        self.assertIsNone(row["worker_session_id"])
        self.assertIsNone(row["worker_result"])
        self.assertIsNone(row["verify_result"])

    def test_repeated_verify_failure_blocks_in_one_run_without_reviewer(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection = self._make_active_worker_project(directory)
            sessions = []
            prompts = []

            def worker(_worktree, _state, _config, role_name, prompt, **kwargs):
                self.assertEqual(role_name, "worker")
                sessions.append(kwargs["session_id"])
                prompts.append(prompt)
                (_worktree / "src/app.py").write_text(
                    f"VALUE = {len(sessions) + 1}\n", encoding="utf-8",
                )
                return loop_manager.CodexResult(
                    0, worker_result(), "worker-session"
                )

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex", side_effect=worker
            ), mock.patch(
                "loop_manager.verify", return_value=(False, "verify failed")
            ) as verify:
                self.assertEqual(
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    ),
                    0,
                )

            row = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            plan = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            history = json.loads(plan["verify_failure_history"])

        self.assertIsNone(row["phase"])
        self.assertEqual(plan["status"], "PLAN_BLOCKED")
        self.assertEqual(plan["worker_session_id"], "worker-session")
        self.assertEqual(plan["worker_result"], worker_result())
        self.assertEqual(plan["verify_result"], "verify failed")
        self.assertIn("실패 지문:", plan["last_error"])
        self.assertEqual(sessions, [None] + ["worker-session"] * 3)
        fingerprint = next(iter(history))
        self.assertIn(
            f"verify:{fingerprint} occurrence=1 revision=1\n"
            "verify exit code: unavailable\nverify failed",
            prompts[1],
        )
        self.assertNotIn("전체 검증 실패:\nverify failed", prompts[1])
        self.assertIn(
            f"verify:{fingerprint} occurrence=2 revision=2", prompts[2]
        )
        self.assertNotIn("verify failed", prompts[2])
        self.assertEqual(verify.call_count, 4)
        self.assertEqual(
            [record["occurrences"] for record in history.values()], [4]
        )

    def test_rebase_verify_failure_recovers_after_initial_success(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection = self._make_active_worker_project(directory)
            sessions = []
            reviewed = False
            loop_manager.migrate_legacy_execplans(
                connection, target_repo=str(repo)
            )
            origin_sha = git_command(repo, "rev-parse", "origin/main")
            origin_tree = git_command(repo, "rev-parse", "origin/main^{tree}")
            advanced_sha = git_command(
                repo,
                "commit-tree",
                origin_tree,
                "-p",
                origin_sha,
                "-m",
                "Advance base for rebase verification",
            )
            git_command(repo, "push", "origin", f"{advanced_sha}:main")
            managed = Path(
                connection.execute(
                    "SELECT worktree FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()["worktree"]
            )
            for _ in range(3):
                loop_manager.transition_validation_failure(
                    connection, str(repo), managed, "verify", "rebased verify failed"
                )

            def worker(worktree, _state, _config, role_name, _prompt, **kwargs):
                nonlocal reviewed
                if role_name == "reviewer":
                    reviewed = True
                    return loop_manager.CodexResult(
                        0, "VERDICT: PASS", "reviewer-session"
                    )
                self.assertEqual(role_name, "worker")
                sessions.append(kwargs["session_id"])
                (worktree / "src/app.py").write_text(
                    "VALUE = 2\n", encoding="utf-8"
                )
                return loop_manager.CodexResult(
                    0,
                    worker_result("Rebase failure context"),
                    "worker-session",
                )

            verify_results = [(True, "first verify")]
            clear_history = loop_manager.clear_validation_failure_history
            cleared_histories = []

            def track_clear_history(connection, project_repo, stage):
                clear_history(connection, project_repo, stage)
                if stage == "verify":
                    cleared_histories.append(
                        json.loads(
                            connection.execute(
                                "SELECT verify_failure_history FROM exec_plans "
                                "WHERE repo = ? AND plan_id = 'G-001'",
                                (project_repo,),
                            ).fetchone()["verify_failure_history"]
                        )
                    )

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch",
                side_effect=matching_branch_pr_lookup(lambda *_args, **_kwargs: (
                    {"url": "https://example.test/pr/1"}
                    if reviewed else None
                )),
            ), mock.patch(
                "loop_manager.run_codex", side_effect=worker
            ), mock.patch(
                "loop_manager.verify", side_effect=verify_results
            ) as verify, mock.patch(
                "loop_manager.clear_validation_failure_history",
                side_effect=track_clear_history,
            ):
                self.assertEqual(
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    ),
                    0,
                )

            row = connection.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan = connection.execute(
                "SELECT status, last_error, verify_result, verify_failure_history FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'", (str(repo),)
            ).fetchone()

        self.assertIsNone(row["phase"])
        self.assertEqual(plan["status"], "PR_OPEN")
        self.assertIsNone(plan["last_error"])
        self.assertEqual(sessions, [None])
        verify.assert_called_once()
        self.assertEqual(plan["verify_result"], "first verify")
        self.assertEqual(cleared_histories, [{}])
        self.assertEqual(json.loads(plan["verify_failure_history"]), {})
        # The base advances at the pre-Worker gate, so neither a stale Worker
        # result nor a stale verify result is produced or consumed.

    def test_verify_success_keeps_worker_and_verify_logs_out_of_reviewer(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection = self._make_active_worker_project(directory)
            calls = []
            logs = []

            def agent(worktree, _state, _config, role_name, prompt, **kwargs):
                calls.append((role_name, prompt, kwargs))
                if role_name == "worker":
                    (worktree / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
                    return loop_manager.CodexResult(0, worker_result("Persist worker change"), "worker-session")
                self.assertEqual(role_name, "reviewer")
                return 0, "VERDICT: PASS"

            with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch("loop_manager.ensure_safe_base_worktree"), mock.patch(
                "loop_manager.pr_for_branch", side_effect=matching_branch_pr_lookup(
                    [None, {"url": "https://example.test/pr/1"}]
                )
            ), mock.patch("loop_manager.run_codex", side_effect=agent), mock.patch(
                "loop_manager.verify", return_value=(True, "verify: PASS")
            ) as verify, mock.patch("loop_manager.operation_log", side_effect=lambda message, **kwargs: logs.append((message, kwargs))):
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    0,
                )
                verify.assert_called_once()

            self.assertEqual(logs[:3], [("Worker 시작: G-001 Worker Boundary", {}), ("전체 verify 시작: G-001 Worker Boundary", {}), ("Reviewer 시작: G-001 Worker Boundary", {})])
            self.assertEqual([role for role, _prompt, _kwargs in calls], ["worker", "reviewer"])
            reviewer_prompt = calls[1][1]
            self.assertNotIn("targeted test: PASS", reviewer_prompt)
            self.assertNotIn("verify: PASS", reviewer_prompt)
            self.assertIn("테스트 command와 scripts/verify를 실행하지 말고", reviewer_prompt)

            row = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            plan = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(
                git_command(Path(plan["worktree"]), "log", "-1", "--pretty=%s"),
                "Persist worker change",
            )
            self.assertEqual(
                git_command(repo, "log", "-1", "--pretty=%s"),
                "Add worker plan",
            )
            self.assertIsNone(row["phase"])
            self.assertEqual(plan["status"], "PR_OPEN")
            self.assertEqual(plan["worker_session_id"], "worker-session")
            self.assertEqual(plan["verify_result"], "verify: PASS")



class BoundedExecutionRecoveryTest(unittest.TestCase):
    def test_review_occurrence_history_does_not_create_final_block(self):
        with tempfile.TemporaryDirectory() as directory:
            root, connection = self._failure_state(directory)
            self.addCleanup(connection.close)
            reviews = [
                ('ISSUE_ID: app.py::same-issue::blocking\n'
                 f'Changed diagnostic {index}\n'
                 + ('ISSUE_ID: app.py::extra-issue::blocking\n' if index == 3 else '')
                 + 'VERDICT: FAIL')
                for index in range(5)
            ]
            decisions = [
                loop_manager.transition_validation_failure(
                    connection, str(root), root, 'review', review,
                ) for review in reviews
            ]
            row = connection.execute(
                "SELECT status, review_failure_history FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-013'", (str(root),),
            ).fetchone()
            self.assertEqual([item.occurrences for item in decisions], [1, 2, 3, 4, 5])
            self.assertTrue(all(not item.blocked for item in decisions))
            self.assertEqual(row['status'], 'IMPLEMENTING')
            self.assertEqual(json.loads(row['review_failure_history'])[
                decisions[-1].fingerprint]['occurrences'], 5)
            self.assertIsNone(scoped_blocking.plan_block(
                connection, str(root), 'G-013',
            ))

    def test_verify_port_uses_supplied_timeout_without_rereading_config(self):
        from tests.test_runtime_state import RuntimeStateRepositoryTest
        import runtime_state

        connection = RuntimeStateRepositoryTest()._connection()
        self.addCleanup(connection.close)
        repository = runtime_state.RuntimeStateRepository(connection)
        repository.activate(
            'repo', 'G-047', activation_sha='base',
            branch='loop-manager/G-047', worktree='/state/G-047',
        )
        with mock.patch.object(loop_manager, 'verify', return_value=(True, 'OK')) as verify:
            ports = _real_execution_ports(
                connection, repository, repo='repo', repo_path=Path('/repo'),
                state_dir=Path('/state'), base='main', base_sha='base',
                provider='codex', config_path=Path('unused.ini'),
                verify_timeout_seconds=42,
                newly_activated=False,
            )
            self.assertEqual(ports.effects.validation.verify(Path('/state/G-047')), (True, 'OK'))
        verify.assert_called_once_with(Path('/state/G-047'), 42)

    def _failure_state(
        self,
        directory: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[Path, sqlite3.Connection]:
        root = make_catalog_root(directory)
        plan = write_execplan(root, "G-013", "Bounded Recovery")
        connection = connection or sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        connection.execute(
            """
            INSERT INTO projects (
                repo, base_branch, phase, plan_path, worktree,
                worker_session_id, reviewer_session_id
            ) VALUES (?, 'main', 'VERIFYING', ?, ?, 'worker-session', 'reviewer-session')
            """,
            (str(root), plan.relative_to(root).as_posix(), str(root)),
        )
        connection.execute(
            """
            INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on, priority,
                content_hash, status, worktree, worker_session_id,
                reviewer_session_id
            ) VALUES (?, 'G-013', ?, 'Bounded Recovery', ?, '[]', 'critical',
                      'hash', 'VERIFYING', ?, 'worker-session', 'reviewer-session')
            """,
            (
                str(root),
                plan.relative_to(root).as_posix(),
                json.dumps(["docs/PRODUCT.md"]),
                str(root),
            ),
        )
        connection.commit()
        return root, connection

    def _active_worker_project(
        self, directory: str
    ) -> tuple[Path, Path, sqlite3.Connection]:
        repo = make_remote_repository(directory)
        state_dir = Path(directory) / "state"
        state_dir.mkdir()
        (repo / "docs/plans").mkdir()
        plan = write_execplan(repo, "G-001", "Worker Boundary")
        commit_and_push(repo, "Add worker plan")
        plan_sha = git_command(repo, "rev-parse", "origin/main")
        branch, worktree = loop_manager.expected_execplan_workspace(
            state_dir, str(repo), "G-001"
        )
        loop_manager.make_worktree(repo, worktree, branch, "main")
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        connection.execute(
            """
            INSERT INTO projects (
                repo, base_branch, active_sha, plan_sha, phase, branch,
                worktree, plan_path
            ) VALUES (?, 'main', ?, ?, 'WORKING', ?, ?, ?)
            """,
            (
                str(repo),
                plan_sha,
                plan_sha,
                branch,
                str(worktree),
                plan.relative_to(repo).as_posix(),
            ),
        )
        connection.commit()
        return repo, state_dir, connection

    def _run_review_sequence(
        self,
        directory: str,
        review_messages: tuple[str, ...],
        *,
        publish: bool,
    ) -> dict[str, object]:
        repo, state_dir, connection = self._active_worker_project(directory)
        roles = []
        reviewer_sessions = []
        reviewer_prompts = []
        worker_version = 1
        transitions = []
        original_transition = loop_manager.transition_validation_failure

        def record_transition(*args, **kwargs):
            decision = original_transition(*args, **kwargs)
            transitions.append(
                (
                    decision.stage,
                    decision.fingerprint,
                    decision.occurrences,
                    decision.blocked,
                )
            )
            return decision

        def agent(worktree, _state, _config, role_name, _prompt, **kwargs):
            nonlocal worker_version
            roles.append(role_name)
            if role_name == "worker":
                worker_version += 1
                (worktree / "src/app.py").write_text(
                    f"VALUE = {worker_version}\n", encoding="utf-8"
                )
                return loop_manager.CodexResult(
                    0,
                    worker_result(f"Record review sequence {worker_version}"),
                    "worker-session",
                )
            reviewer_sessions.append(kwargs["session_id"])
            reviewer_prompts.append(_prompt)
            return loop_manager.CodexResult(
                0, next(responses), "reviewer-session"
            )

        responses = iter(review_messages)

        def existing_pr(*_args, **_kwargs):
            phase = connection.execute(
                "SELECT phase FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()["phase"]
            if publish and phase == "PUBLISHING":
                return {"url": "https://example.test/pr/13"}
            return None

        with mock.patch(
            "loop_manager.validate_repository", return_value=repo
        ), mock.patch(
            "loop_manager.remote_base", return_value="main"
        ), mock.patch(
            "loop_manager.ensure_safe_base_worktree"
        ), mock.patch(
            "loop_manager.pr_for_branch", side_effect=matching_branch_pr_lookup(existing_pr)
        ), mock.patch(
            "loop_manager.run_codex", side_effect=agent
        ), mock.patch(
            "loop_manager.verify", return_value=(True, "verify: PASS")
        ) as verify, mock.patch(
            "loop_manager.transition_validation_failure",
            side_effect=record_transition,
        ):
            result = loop_manager.run_project_result(
                connection,
                state_dir,
                str(repo),
                Path("unused.ini"),
                "main",
                False,
            )

        project = connection.execute(
            "SELECT phase, worker_session_id, reviewer_session_id, "
            "last_error, review_result FROM projects WHERE repo = ?",
            (str(repo),),
        ).fetchone()
        plan = connection.execute(
            "SELECT status, last_error, review_result, review_failure_history, "
            "review_level_history, effective_worker_level "
            "FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
            (str(repo),),
        ).fetchone()
        return {
            "result": result,
            "roles": roles,
            "reviewer_sessions": reviewer_sessions,
            "reviewer_prompts": reviewer_prompts,
            "transitions": transitions,
            "project": project,
            "plan": plan,
            "verify_calls": verify.call_count,
        }

    def test_failure_fingerprints_normalize_noise_and_preserve_issue_identity(self):
        first_root = Path("/tmp/run-one/worktree")
        second_root = Path("/tmp/run-two/worktree")
        first = "\n".join(
            (
                "2026-09-19T10:20:30Z FAIL tests/test_b.py::test_b",
                f"{first_root}/tests/test_a.py:42: assertion failed",
                "FAIL tests/test_a.py::test_a in 0.23s",
            )
        )
        second = "\n".join(
            (
                "FAIL tests/test_a.py::test_a in 9.91s",
                f"{second_root}/tests/test_a.py:42: assertion failed",
                "2027-01-01 22:11:09 FAIL tests/test_b.py::test_b",
            )
        )

        first_fingerprint, first_normalized = loop_manager.validation_failure_fingerprint(
            "verify", first, first_root
        )
        second_fingerprint, second_normalized = loop_manager.validation_failure_fingerprint(
            "verify", second, second_root
        )
        changed_fingerprint, _ = loop_manager.validation_failure_fingerprint(
            "verify",
            second.replace("test_b", "test_c"),
            second_root,
        )
        review_fingerprint, _ = loop_manager.validation_failure_fingerprint(
            "review",
            "ISSUE_ID: tests/test_a.py::test-a::blocking\nVERDICT: FAIL",
            second_root,
        )
        same_temp_first, _ = loop_manager.validation_failure_fingerprint(
            "verify", "FAIL /tmp/run-one/test_same.py:42"
        )
        same_temp_second, _ = loop_manager.validation_failure_fingerprint(
            "verify", "FAIL /tmp/run-two/test_same.py:42"
        )
        different_temp_file, _ = loop_manager.validation_failure_fingerprint(
            "verify", "FAIL /tmp/run-three/test_other.py:42"
        )
        temp_directory_first, _ = loop_manager.validation_failure_fingerprint(
            "verify", "workspace cleanup failed: /tmp/tmpABC"
        )
        temp_directory_second, _ = loop_manager.validation_failure_fingerprint(
            "verify", "workspace cleanup failed: /tmp/tmpXYZ"
        )
        extensionless_file_first, _ = loop_manager.validation_failure_fingerprint(
            "verify", "FAIL /tmp/inputA"
        )
        extensionless_file_second, _ = loop_manager.validation_failure_fingerprint(
            "verify", "FAIL /tmp/inputB"
        )
        prefixed_file_first, _ = loop_manager.validation_failure_fingerprint(
            "verify", "FAIL /tmp/tmpInputA"
        )
        prefixed_file_second, _ = loop_manager.validation_failure_fingerprint(
            "verify", "FAIL /tmp/tmpInputB"
        )

        self.assertEqual(first_normalized, second_normalized)
        self.assertEqual(first_fingerprint, second_fingerprint)
        self.assertNotEqual(first_fingerprint, changed_fingerprint)
        self.assertNotEqual(first_fingerprint, review_fingerprint)
        self.assertEqual(same_temp_first, same_temp_second)
        self.assertNotEqual(same_temp_first, different_temp_file)
        self.assertEqual(temp_directory_first, temp_directory_second)
        self.assertNotEqual(extensionless_file_first, extensionless_file_second)
        self.assertNotEqual(prefixed_file_first, prefixed_file_second)

    def test_review_fingerprints_use_stable_issue_ids_not_explanatory_prose(self):
        first = "\n".join(
            (
                "ISSUE_ID: loop_manager.py::run_project::session-recovery",
                "[P1] line 120: the saved session was not resumed.",
                "ISSUE_ID: tests/test_loop_manager.py::retry-case::missing-coverage",
                "Add a regression test near line 3000.",
                "VERDICT: FAIL",
            )
        )
        reworded = "\n".join(
            (
                "ISSUE_ID: tests/test_loop_manager.py::retry-case::missing-coverage",
                "The scenario is still untested at a different location.",
                "ISSUE_ID: LOOP_MANAGER.PY::RUN_PROJECT::SESSION-RECOVERY",
                "Session continuation remains broken, phrased differently.",
                "VERDICT: FAIL",
            )
        )
        changed = reworded.replace("SESSION-RECOVERY", "USAGE-LIMIT")

        first_fingerprints = loop_manager.validation_failure_fingerprints(
            "review", first
        )
        reworded_fingerprints = loop_manager.validation_failure_fingerprints(
            "review", reworded
        )
        changed_fingerprints = loop_manager.validation_failure_fingerprints(
            "review", changed
        )
        with self.assertRaisesRegex(
            loop_manager.ReviewerProtocolError, "유효한 ISSUE_ID가 없습니다"
        ):
            loop_manager.validation_failure_fingerprints(
                "review", "first free-form explanation\nVERDICT: FAIL"
            )
        with self.assertRaisesRegex(
            loop_manager.ReviewerProtocolError, "형식이 유효하지 않습니다"
        ):
            loop_manager.validation_failure_fingerprints(
                "review", "ISSUE_ID: app.py:10 prose\nVERDICT: FAIL"
            )

        self.assertEqual(first_fingerprints, reworded_fingerprints)
        self.assertEqual(
            tuple(normalized for _fingerprint, normalized in first_fingerprints),
            (
                "ISSUE_ID:loop_manager.py::run_project::session-recovery",
                "ISSUE_ID:tests/test_loop_manager.py::retry-case::missing-coverage",
            ),
        )
        first_by_issue = {
            normalized: fingerprint
            for fingerprint, normalized in first_fingerprints
        }
        changed_by_issue = {
            normalized: fingerprint
            for fingerprint, normalized in changed_fingerprints
        }
        self.assertEqual(
            first_by_issue[
                "ISSUE_ID:tests/test_loop_manager.py::retry-case::missing-coverage"
            ],
            changed_by_issue[
                "ISSUE_ID:tests/test_loop_manager.py::retry-case::missing-coverage"
            ],
        )
        self.assertEqual(
            set(first_by_issue) & set(changed_by_issue),
            {"ISSUE_ID:tests/test_loop_manager.py::retry-case::missing-coverage"},
        )

    def test_review_issue_id_accepts_actual_execplan_path(self):
        review = (
            "ISSUE_ID: docs/plans/[G-025] Bootstrap Remaining Completed "
            "Requirement Archive.md::Decision-Log::archive-eligibility-evidence\n"
            "eligibility evidence does not match the source plans\n"
            "VERDICT: FAIL"
        )

        verdict, issue_ids = loop_manager.parse_reviewer_result(review)

        self.assertEqual(verdict, "FAIL")
        self.assertEqual(
            issue_ids,
            (
                "ISSUE_ID:docs/plans/[g-025] bootstrap remaining completed "
                "requirement archive.md::decision-log::archive-eligibility-evidence",
            ),
        )

    def test_reviewer_specification_deadlock_requires_structured_evidence(self):
        import json

        evidence = {
            "kind": "conflict",
            "locations": [
                {"path": "docs/PRODUCT.md", "section": "Review policy"},
                {"path": "docs/requirements/[R-025] Review.md", "section": "Acceptance Criteria"},
            ],
            "problem": "The two retention rules conflict",
            "implementation_choice": "Whether to retain review history",
            "question": "Which retention rule should apply?",
        }
        response = f"SPEC_DEADLOCK: {json.dumps(evidence)}\nVERDICT: SPEC_BLOCKED"
        verdict, details = loop_manager.parse_reviewer_result(response)
        self.assertEqual(verdict, "SPEC_BLOCKED")
        self.assertEqual(details.paths, (
            "docs/PRODUCT.md", "docs/requirements/[R-025] Review.md",
        ))
        self.assertEqual(details.question, evidence["question"])
        missing = {
            **evidence,
            "kind": "missing",
            "locations": evidence["locations"][:1],
        }
        missing_verdict, missing_details = loop_manager.parse_reviewer_result(
            f"SPEC_DEADLOCK: {json.dumps(missing)}\nVERDICT: SPEC_BLOCKED"
        )
        self.assertEqual(missing_verdict, "SPEC_BLOCKED")
        self.assertEqual(missing_details.paths, ("docs/PRODUCT.md",))
        for invalid in (
            {**evidence, "locations": evidence["locations"][:1]},
            {**evidence, "question": ""},
            {**evidence, "kind": ["conflict"]},
            {**evidence, "kind": {"value": "conflict"}},
            {**evidence, "locations": [
                {"path": "docs/../PRODUCT.md", "section": "Review policy"},
                evidence["locations"][1],
            ]},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(
                loop_manager.ReviewerProtocolError
            ):
                loop_manager.parse_reviewer_result(
                    f"SPEC_DEADLOCK: {json.dumps(invalid)}\nVERDICT: SPEC_BLOCKED"
                )
        with self.assertRaises(loop_manager.ReviewerProtocolError):
            loop_manager.parse_reviewer_result(
                f"SPEC_DEADLOCK: {json.dumps(evidence)}\nVERDICT: FAIL"
            )

    def test_review_specification_block_requeues_only_after_input_change(self):
        from tests.test_runtime_state import RuntimeStateRepositoryTest

        connection = RuntimeStateRepositoryTest()._connection()
        self.addCleanup(connection.close)
        connection.execute(
            "UPDATE exec_plans SET status = 'PLAN_BLOCKED', "
            "blocked_reason = 'review_specification_deadlock', "
            "blocked_fingerprint = 'old-input', "
            "blocked_review_fingerprint = 'old-document', "
            "worktree = '/state/G-047', "
            "workspace_owned = 0 WHERE repo = 'repo' AND plan_id = 'G-047'"
        )
        connection.commit()
        with (
            mock.patch.object(loop_manager, '_revision_execplan_input_fingerprint',
                              return_value='old-input') as inputs,
            mock.patch.object(loop_manager, '_revision_review_issue_fingerprint',
                              return_value='old-document') as document_input,
            mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
            mock.patch.object(loop_manager, 'git', return_value=mock.Mock(returncode=1)),
            mock.patch.object(loop_manager, '_blocked_execplan_revision_metadata_error',
                              return_value=None),
            mock.patch.object(loop_manager, '_requeue_blocked_execplan') as requeue,
        ):
            self.assertFalse(loop_manager.resume_blocked_execplan_if_inputs_changed(
                connection, Path('/repo'), 'repo', 'main', Path('/state'),
            ))
            requeue.assert_not_called()
            document_input.return_value = 'changed-architecture'
            self.assertTrue(loop_manager.resume_blocked_execplan_if_inputs_changed(
                connection, Path('/repo'), 'repo', 'main', Path('/state'),
            ))
            requeue.assert_called_once_with(connection, 'repo', 'G-047')
            requeue.reset_mock()
            document_input.return_value = 'old-document'
            inputs.return_value = 'changed-source'
            self.assertTrue(loop_manager.resume_blocked_execplan_if_inputs_changed(
                connection, Path('/repo'), 'repo', 'main', Path('/state'),
            ))
            requeue.assert_called_once_with(connection, 'repo', 'G-047')

    def test_specification_document_fingerprint_tracks_architecture_revision(self):
        import json

        evidence = {
            'kind': 'missing',
            'locations': [
                {'path': 'docs/ARCHITECTURE.md', 'section': 'Migration policy'},
            ],
            'problem': 'The migration decision is absent',
            'implementation_choice': 'Which migration to use',
            'question': 'Which migration should be used?',
        }
        review = f"SPEC_DEADLOCK: {json.dumps(evidence)}\nVERDICT: SPEC_BLOCKED"

        def git(_repo, _command, revision_path, **_kwargs):
            revision = revision_path.split(':', 1)[0]
            return mock.Mock(returncode=0, stdout=(
                'Old policy' if revision in {'base', 'unrelated'} else 'New policy'
            ))

        with mock.patch.object(loop_manager, 'git', side_effect=git) as shown:
            base = loop_manager._revision_review_issue_fingerprint(
                Path('/repo'), 'base', review,
            )
            unrelated = loop_manager._revision_review_issue_fingerprint(
                Path('/repo'), 'unrelated', review,
            )
            changed = loop_manager._revision_review_issue_fingerprint(
                Path('/repo'), 'changed', review,
            )
        self.assertEqual(base, unrelated)
        self.assertNotEqual(base, changed)
        self.assertTrue(all(
            call.args[2].endswith(':docs/ARCHITECTURE.md')
            for call in shown.call_args_list
        ))

    def test_remote_checkpoint_reviewer_specification_skips_fail_fingerprint(self):
        from tests.test_runtime_state import RuntimeStateRepositoryTest
        import runtime_state

        connection = RuntimeStateRepositoryTest()._connection()
        self.addCleanup(connection.close)
        repository = runtime_state.RuntimeStateRepository(connection)
        repository.activate(
            'repo', 'G-047', activation_sha='base',
            branch='loop-manager/G-047', worktree='/state/G-047',
        )
        prior = 'ISSUE_ID: app.py::review::blocking\nReview failure\nVERDICT: FAIL'
        repository.block_plan(
            'repo', 'G-047', reason_code='review_retry_exhausted',
            evidence=prior, release_fingerprint='prior-input',
            plan_values={
                'review_result': prior,
                'worker_result': worker_result(),
            }, clear_projection=True,
        )
        blocked = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        evidence = {
            'kind': 'missing',
            'locations': [
                {'path': 'docs/ARCHITECTURE.md', 'section': 'Migration policy'},
            ],
            'problem': 'The decision is absent',
            'implementation_choice': 'Which migration to use',
            'question': 'Which migration should be used?',
        }
        review = f"SPEC_DEADLOCK: {json.dumps(evidence)}\nVERDICT: SPEC_BLOCKED"
        snapshot = mock.Mock()
        with (
            mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=snapshot),
            mock.patch.object(loop_manager, 'verify', return_value=(True, 'OK')),
            mock.patch.object(loop_manager, '_remote_checkpoint_review_revision',
                              return_value=('base', 'head')),
            mock.patch.object(loop_manager, 'validate_agent_scope'),
            mock.patch.object(loop_manager, 'run_agent', return_value=(0, review)),
            mock.patch.object(loop_manager, '_remote_checkpoint_has_new_failure') as fingerprint,
        ):
            result = loop_manager._evaluate_remote_checkpoint_cause(
                connection, Path('/repo'), Path('/state'), 'repo', blocked,
                Path('/config'), 30,
            )
        self.assertEqual(result, (False, review, 'specification'))
        fingerprint.assert_not_called()

    def test_remote_checkpoint_new_review_failure_uses_existing_run_budget(self):
        import blocked_checkpoint_state
        import runtime_state
        from tests.test_runtime_state import RuntimeStateRepositoryTest
        from validation_feedback import normalize_feedback
        from validation_feedback_state import ValidationFeedbackRepository

        connection = RuntimeStateRepositoryTest()._connection()
        self.addCleanup(connection.close)
        runtime = runtime_state.RuntimeStateRepository(connection)
        runtime.activate(
            'repo', 'G-047', activation_sha='base',
            branch='loop-manager/G-047', worktree='/state/G-047',
        )
        old_review = 'ISSUE_ID: app.py::old::blocking\nOld issue\nVERDICT: FAIL'
        runtime.block_plan(
            'repo', 'G-047', reason_code='review_retry_exhausted',
            evidence=old_review, release_fingerprint='prior-input',
            plan_values={'review_result': old_review, 'worker_result': worker_result()},
            clear_projection=True,
        )
        checkpoints = blocked_checkpoint_state.BlockedCheckpointRepository(connection)
        request = checkpoints.get('repo', 'G-047')
        checkpoints.finish_published(request, 'published-oid')
        blocked = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        run_id = loop_manager.recovery_state.begin_external_run(connection, 'same-run')
        feedback = ValidationFeedbackRepository(connection)
        for index in range(3):
            prior = f'ISSUE_ID: app.py::prior-{index}::blocking\nVERDICT: FAIL'
            feedback.observe(
                'repo', 'G-047', 'base', 'review', False, prior,
                normalize_feedback('review', prior),
            )
            result_id = loop_manager.recovery_state.pending_review_correction(
                connection, 'repo', 'G-047', prior,
            )
            self.assertEqual(
                loop_manager.recovery_state.reserve_review_correction(
                    connection, 'repo', 'G-047', result_id,
                ), (True, index + 1, run_id),
            )

        new_review = 'ISSUE_ID: app.py::new::blocking\nNew issue\nVERDICT: FAIL'
        with (
            mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=mock.Mock()),
            mock.patch.object(loop_manager, 'verify', return_value=(True, 'OK')),
            mock.patch.object(loop_manager, '_remote_checkpoint_review_revision',
                              return_value=('base', 'candidate-head')),
            mock.patch.object(loop_manager, 'validate_agent_scope'),
            mock.patch.object(loop_manager, 'run_agent', return_value=(0, new_review)),
            mock.patch.object(loop_manager, '_remote_checkpoint_has_new_failure',
                              return_value=True),
        ):
            resolved = loop_manager._evaluate_remote_checkpoint_cause(
                connection, Path('/repo'), Path('/state'), 'repo', blocked,
                Path('/config'), 30,
            )
        self.assertEqual(resolved, (True, new_review, 'review'))
        loop_manager._resume_remote_corrected_execplan(
            connection, 'repo', blocked, checkpoints, request,
            'candidate-head', 'candidate-input', 'new Review issue',
            new_failure_stage='review', assessment=new_review,
        )
        plan = connection.execute(
            "SELECT status, review_result FROM exec_plans "
            "WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        self.assertEqual(tuple(plan), ('IMPLEMENTING', new_review))
        latest = connection.execute(
            "SELECT id, stage, classification, raw_output FROM validation_results "
            "WHERE repo = 'repo' AND plan_id = 'G-047' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(tuple(latest)[1:], ('review', 'failure', new_review))
        self.assertEqual(
            loop_manager.recovery_state.pending_review_correction(
                connection, 'repo', 'G-047', plan['review_result'],
            ), latest['id'],
        )
        self.assertEqual(
            loop_manager.recovery_state.reserve_review_correction(
                connection, 'repo', 'G-047', latest['id'],
            ), (False, 3, run_id),
        )

    def test_remote_checkpoint_requires_valid_worker_evidence_before_reviewer(self):
        from tests.test_runtime_state import RuntimeStateRepositoryTest
        import runtime_state

        connection = RuntimeStateRepositoryTest()._connection()
        self.addCleanup(connection.close)
        repository = runtime_state.RuntimeStateRepository(connection)
        repository.activate(
            'repo', 'G-047', activation_sha='base',
            branch='loop-manager/G-047', worktree='/state/G-047',
        )
        for evidence in (None, worker_result(tests='none')):
            with self.subTest(evidence=evidence):
                repository.block_plan(
                    'repo', 'G-047', reason_code='review_retry_exhausted',
                    evidence='prior review failure', release_fingerprint='prior-input',
                    plan_values={'worker_result': evidence}, clear_projection=True,
                )
                blocked = connection.execute(
                    "SELECT * FROM exec_plans WHERE repo = 'repo' AND plan_id = 'G-047'"
                ).fetchone()
                with (
                    mock.patch.object(loop_manager, 'capture_git_snapshot',
                                      return_value=mock.Mock()),
                    mock.patch.object(loop_manager, 'verify', return_value=(True, '')),
                    mock.patch.object(loop_manager, '_remote_checkpoint_review_revision',
                                      return_value=('base', 'head')),
                    mock.patch.object(loop_manager, 'validate_agent_scope'),
                    mock.patch.object(loop_manager, 'run_agent') as reviewer,
                ):
                    resolved, message, stage = loop_manager._evaluate_remote_checkpoint_cause(
                        connection, Path('/repo'), Path('/state'), 'repo', blocked,
                        Path('/config'), 30,
                    )
                self.assertFalse(resolved)
                self.assertEqual(stage, 'worker')
                self.assertIn('Worker 결과 검증 실패', message)
                reviewer.assert_not_called()

    def test_remote_checkpoint_base_change_after_verify_skips_reviewer(self):
        from tests.test_runtime_state import RuntimeStateRepositoryTest
        import runtime_state

        connection = RuntimeStateRepositoryTest()._connection()
        self.addCleanup(connection.close)
        repository = runtime_state.RuntimeStateRepository(connection)
        repository.activate(
            'repo', 'G-047', activation_sha='base',
            branch='loop-manager/G-047', worktree='/state/G-047',
        )
        repository.block_plan(
            'repo', 'G-047', reason_code='review_retry_exhausted',
            evidence='prior review failure', release_fingerprint='prior-input',
            plan_values={'worker_result': worker_result()}, clear_projection=True,
        )
        blocked = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        with (
            mock.patch.object(loop_manager, '_remote_checkpoint_review_revision',
                              return_value=None),
            mock.patch.object(loop_manager, 'verify') as verify,
            mock.patch.object(loop_manager, 'run_agent') as reviewer,
        ):
            resolved, _, stage = loop_manager._evaluate_remote_checkpoint_cause(
                connection, Path('/repo'), Path('/state'), 'repo', blocked,
                Path('/config'), 30,
            )
        self.assertFalse(resolved)
        self.assertEqual(stage, 'stale_base')
        verify.assert_not_called()
        reviewer.assert_not_called()
        for revisions, expected_calls in (
            ([('base', 'head'), ('new-base', 'head')], 0),
            ([('base', 'head'), ('base', 'head'), ('new-base', 'head')], 1),
        ):
            with self.subTest(expected_calls=expected_calls):
                with (
                    mock.patch.object(loop_manager, '_remote_checkpoint_review_revision',
                                      side_effect=revisions),
                    mock.patch.object(loop_manager, 'capture_git_snapshot',
                                      return_value=mock.Mock()),
                    mock.patch.object(loop_manager, 'verify', return_value=(True, '')),
                    mock.patch.object(loop_manager, 'validate_agent_scope'),
                    mock.patch.object(loop_manager, 'run_agent',
                                      return_value=(0, 'invalid review')) as reviewer,
                ):
                    resolved, message, stage = loop_manager._evaluate_remote_checkpoint_cause(
                        connection, Path('/repo'), Path('/state'), 'repo', blocked,
                        Path('/config'), 30,
                    )
                self.assertFalse(resolved)
                self.assertEqual(stage, 'revision_drift')
                self.assertIn('base 또는 HEAD', message)
                self.assertEqual(reviewer.call_count, expected_calls)
        audit = connection.execute(
            "SELECT stage, classification, raw_output FROM validation_results "
            "WHERE repo = 'repo' AND plan_id = 'G-047' ORDER BY id"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in audit],
            [('verify', 'success', ''), ('verify', 'success', '')],
        )

    def test_remote_checkpoint_revision_requires_latest_base_ancestry(self):
        responses = [
            SimpleNamespace(stdout='head\n'),
            SimpleNamespace(returncode=1),
        ]
        with mock.patch.object(loop_manager, '_recorded_base_fetch', return_value='base') as fetched, \
             mock.patch.object(loop_manager, 'git', side_effect=responses) as git:
            revision = loop_manager._remote_checkpoint_review_revision(
                Path('/repo'), Path('/worktree'), 'main', Path('/state'),
            )
        self.assertIsNone(revision)
        fetched.assert_called_once_with(
            Path('/repo'), Path('/state'), 'main', owner='project',
        )
        self.assertEqual(
            git.call_args_list[-1].args,
            (Path('/repo'), 'merge-base', '--is-ancestor', 'base', 'head'),
        )

    def test_remote_checkpoint_corrects_invalid_specification_to_pass(self):
        from tests.test_runtime_state import RuntimeStateRepositoryTest
        import runtime_state

        connection = RuntimeStateRepositoryTest()._connection()
        self.addCleanup(connection.close)
        repository = runtime_state.RuntimeStateRepository(connection)
        repository.activate(
            'repo', 'G-047', activation_sha='base',
            branch='loop-manager/G-047', worktree='/state/G-047',
        )
        repository.block_plan(
            'repo', 'G-047', reason_code='review_retry_exhausted',
            evidence='prior review failure', release_fingerprint='prior-input',
            plan_values={'worker_result': worker_result()},
            clear_projection=True,
        )
        blocked = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        invalid = (
            'SPEC_DEADLOCK: {"kind":"missing","locations":'
            '[{"path":"docs/other.md","section":"Policy"}],'
            '"problem":"Missing rule","implementation_choice":"Choice",'
            '"question":"Which rule?"}\nVERDICT: SPEC_BLOCKED'
        )
        raw_verify = 'PRIVATE_SUCCESS_LOG\n' + 'x' * 9000
        snapshot = mock.Mock()
        with (
            mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=snapshot),
            mock.patch.object(loop_manager, 'verify', return_value=(
                True, loop_manager.VerifyOutput(raw_verify),
            )),
            mock.patch.object(loop_manager, '_remote_checkpoint_review_revision',
                              return_value=('base', 'head')),
            mock.patch.object(loop_manager, 'validate_agent_scope'),
            mock.patch.object(loop_manager, 'run_agent', side_effect=[
                (0, invalid), (0, 'VERDICT: PASS'),
            ]) as reviewer,
        ):
            result = loop_manager._evaluate_remote_checkpoint_cause(
                connection, Path('/repo'), Path('/state'), 'repo', blocked,
                Path('/config'), 30,
            )
        self.assertEqual(result, (True, 'VERDICT: PASS', None))
        self.assertEqual(reviewer.call_count, 2)
        self.assertIn('직전 Reviewer 결과가 아래 프로토콜 검증에 실패',
                      reviewer.call_args_list[1].args[5])
        self.assertIn('docs/other.md', reviewer.call_args_list[1].args[5])
        audit = connection.execute(
            "SELECT definition_revision, stage, classification, raw_output "
            "FROM validation_results WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in audit],
            [('base', 'verify', 'success', raw_verify),
             ('base', 'review', 'success', 'VERDICT: PASS')],
        )
        for call in reviewer.call_args_list:
            self.assertNotIn('PRIVATE_SUCCESS_LOG', call.args[5])

    def test_remote_checkpoint_protocol_exhaustion_stops_at_three_attempts(self):
        from tests.test_runtime_state import RuntimeStateRepositoryTest
        import runtime_state
        import blocked_checkpoint_state

        connection = RuntimeStateRepositoryTest()._connection()
        self.addCleanup(connection.close)
        repository = runtime_state.RuntimeStateRepository(connection)
        repository.activate(
            'repo', 'G-047', activation_sha='base',
            branch='loop-manager/G-047', worktree='/state/G-047',
        )
        repository.block_plan(
            'repo', 'G-047', reason_code='review_retry_exhausted',
            evidence='prior review failure', release_fingerprint='prior-input',
            plan_values={'worker_result': worker_result()},
            clear_projection=True,
        )
        checkpoint = blocked_checkpoint_state.BlockedCheckpointRepository(connection)
        checkpoint.finish_published(checkpoint.get('repo', 'G-047'), 'published-oid')
        request = checkpoint.get('repo', 'G-047')
        blocked = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        invalid = (
            'SPEC_DEADLOCK: {"kind":"missing","locations":'
            '[{"path":"docs/other.md","section":"Policy"}],'
            '"problem":"Missing rule","implementation_choice":"Choice",'
            '"question":"Which rule?"}\nVERDICT: SPEC_BLOCKED'
        )
        snapshot = mock.Mock()
        with (
            mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=snapshot),
            mock.patch.object(loop_manager, 'verify', return_value=(True, 'OK')),
            mock.patch.object(loop_manager, '_remote_checkpoint_review_revision',
                              return_value=('base', 'head')),
            mock.patch.object(loop_manager, 'validate_agent_scope'),
            mock.patch.object(loop_manager, 'run_agent', return_value=(0, invalid)) as reviewer,
            mock.patch.object(loop_manager, '_remote_checkpoint_has_new_failure') as fingerprint,
        ):
            resolved, evidence, stage = loop_manager._evaluate_remote_checkpoint_cause(
                connection, Path('/repo'), Path('/state'), 'repo', blocked,
                Path('/config'), 30,
            )
        self.assertFalse(resolved)
        self.assertEqual(stage, 'protocol_exhausted')
        self.assertEqual(reviewer.call_count, loop_manager.MAX_REVIEWER_PROTOCOL_ATTEMPTS)
        self.assertIn('프로토콜 교정 3회 실패', evidence)
        self.assertIn(invalid, evidence)
        fingerprint.assert_not_called()

        with (
            mock.patch.object(loop_manager, '_revision_execplan_input_fingerprint',
                              return_value='source-base'),
            mock.patch.object(loop_manager, '_worktree_execplan_input_fingerprint',
                              return_value='accepted-worktree'),
        ):
            loop_manager._reclassify_remote_checkpoint_block(
                connection, Path('/repo'), 'repo', blocked, checkpoint, request,
                'accepted-oid', Path('/state/G-047'),
                reason=loop_manager.BlockReasonCode.REVIEW_PROTOCOL_EXHAUSTED,
                evidence=evidence, review_fingerprint_source=None,
                user_targets=('docs/plans/[G-047] Runtime.md', 'docs/PRODUCT.md'),
            )
        plan = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        saved = connection.execute(
            "SELECT * FROM blocked_checkpoints WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        self.assertEqual(plan['blocked_reason'], 'review_protocol_exhausted')
        self.assertEqual(plan['review_result'], evidence)
        self.assertEqual(saved['phase'], 'published')
        self.assertEqual(saved['accepted_remote_oid'], 'accepted-oid')
        self.assertIsNone(saved['inspected_remote_oid'])
        next_snapshot = mock.Mock()
        next_snapshot.serialize.return_value = 'saved-worktree'
        with (
            mock.patch.object(loop_manager._BlockedCheckpointAdapter, 'observe_remote',
                              return_value='accepted-oid'),
            mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
            mock.patch.object(loop_manager, 'validate_managed_worktree'),
            mock.patch.object(loop_manager, 'capture_git_snapshot',
                              return_value=next_snapshot),
            mock.patch.object(loop_manager, '_nul_git_paths', return_value=()),
            mock.patch.object(loop_manager, 'run_agent') as repeated_reviewer,
        ):
            next_result = loop_manager._reconcile_published_blocked_checkpoint(
                connection, Path('/repo'), Path('/state'), 'repo', plan,
                saved, checkpoint.get('repo', 'G-047'), checkpoint,
            )
        self.assertEqual(next_result, 'unchanged')
        repeated_reviewer.assert_not_called()

    def test_remote_checkpoint_specification_block_preserves_published_identity(self):
        from tests.test_runtime_state import RuntimeStateRepositoryTest
        import runtime_state
        import blocked_checkpoint_state

        connection = RuntimeStateRepositoryTest()._connection()
        self.addCleanup(connection.close)
        repository = runtime_state.RuntimeStateRepository(connection)
        repository.activate(
            'repo', 'G-047', activation_sha='base',
            branch='loop-manager/G-047', worktree='/state/G-047',
        )
        prior = 'ISSUE_ID: app.py::review::blocking\nReview failure\nVERDICT: FAIL'
        repository.block_plan(
            'repo', 'G-047', reason_code='review_retry_exhausted',
            evidence=prior, release_fingerprint='prior-input',
            plan_values={'review_result': prior, 'review_failure_history':
                         '{"prior": {"occurrences": 4}}'},
            clear_projection=True,
        )
        checkpoint = blocked_checkpoint_state.BlockedCheckpointRepository(connection)
        initial_request = checkpoint.get('repo', 'G-047')
        checkpoint.finish_published(initial_request, 'published-oid')
        request = checkpoint.get('repo', 'G-047')
        blocked = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        evidence = {
            'kind': 'missing',
            'locations': [
                {'path': 'docs/ARCHITECTURE.md', 'section': 'Migration policy'},
            ],
            'problem': 'The decision is absent',
            'implementation_choice': 'Which migration to use',
            'question': 'Which migration should be used?',
        }
        review = f"SPEC_DEADLOCK: {json.dumps(evidence)}\nVERDICT: SPEC_BLOCKED"
        with (
            mock.patch.object(loop_manager, '_revision_execplan_input_fingerprint',
                              return_value='source-base'),
            mock.patch.object(loop_manager, '_revision_review_issue_fingerprint',
                              return_value='architecture-base'),
            mock.patch.object(loop_manager, '_worktree_execplan_input_fingerprint',
                              return_value='accepted-worktree'),
        ):
            loop_manager._reclassify_remote_specification_deadlock(
                connection, Path('/repo'), 'repo', blocked, checkpoint, request,
                'accepted-oid', Path('/state/G-047'), review,
            )
        plan = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        block = connection.execute(
            "SELECT * FROM execplan_blocks WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        saved = connection.execute(
            "SELECT * FROM blocked_checkpoints WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        recovery = connection.execute(
            "SELECT * FROM recovery_records WHERE repo = 'repo' AND subject = 'G-047'"
        ).fetchone()
        self.assertEqual(plan['status'], 'PLAN_BLOCKED')
        self.assertEqual(plan['blocked_reason'], 'review_specification_deadlock')
        self.assertEqual(plan['review_result'], review)
        self.assertEqual(plan['blocked_fingerprint'], 'source-base')
        self.assertEqual(plan['blocked_review_fingerprint'], 'architecture-base')
        self.assertEqual(json.loads(plan['review_failure_history']), {})
        self.assertEqual((block['reason_code'], block['evidence']),
                         ('review_specification_deadlock', review))
        self.assertEqual(saved['phase'], 'published')
        self.assertEqual(saved['published_oid'], 'published-oid')
        self.assertEqual(saved['accepted_remote_oid'], 'accepted-oid')
        self.assertEqual(saved['reason_code'], 'review_specification_deadlock')
        self.assertNotEqual(saved['request_identity'], request.request_identity)
        self.assertIsNone(saved['inspected_remote_oid'])
        self.assertIsNone(saved['inspected_input'])
        self.assertIsNone(saved['inspection_reason'])
        self.assertEqual(json.loads(recovery['final_evidence'])['user_targets'],
                         ['docs/ARCHITECTURE.md'])

        snapshot = mock.Mock()
        snapshot.serialize.return_value = 'saved-worktree'
        with (
            mock.patch.object(loop_manager._BlockedCheckpointAdapter, 'observe_remote',
                              return_value='accepted-oid'),
            mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
            mock.patch.object(loop_manager, 'validate_managed_worktree'),
            mock.patch.object(loop_manager, 'capture_git_snapshot',
                              return_value=snapshot),
            mock.patch.object(loop_manager, '_nul_git_paths', return_value=()),
            mock.patch.object(loop_manager, 'run_agent') as reviewer,
        ):
            next_result = loop_manager._reconcile_published_blocked_checkpoint(
                connection, Path('/repo'), Path('/state'), 'repo', plan,
                saved, checkpoint.get('repo', 'G-047'), checkpoint,
            )
        self.assertEqual(next_result, 'unchanged')
        reviewer.assert_not_called()

    def test_review_specification_block_uses_source_revision_not_worker_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root, connection = self._failure_state(directory)
            self.addCleanup(connection.close)
            connection.execute(
                "UPDATE exec_plans SET status = 'REVIEWING', active_sha = 'base' "
                "WHERE repo = ? AND plan_id = 'G-013'", (str(root),)
            )
            connection.execute(
                "UPDATE projects SET phase = 'REVIEWING', "
                "agent_input_fingerprint = 'worktree-progress' WHERE repo = ?",
                (str(root),),
            )
            connection.commit()
            snapshot = mock.Mock()
            snapshot.serialize.return_value = 'saved-snapshot'
            with (
                mock.patch.object(loop_manager, '_worktree_execplan_input_fingerprint',
                                  return_value='worktree-progress'),
                mock.patch.object(loop_manager, '_revision_execplan_input_fingerprint',
                                  return_value='source-base') as source_input,
                mock.patch.object(loop_manager, '_revision_review_issue_fingerprint',
                                  return_value='spec-document-base'),
                mock.patch.object(loop_manager, 'capture_git_snapshot',
                                  return_value=snapshot),
                mock.patch.object(loop_manager, '_final_block_recovery_decision',
                                  return_value=mock.Mock()),
                mock.patch.object(loop_manager.runtime_state.RuntimeStateRepository,
                                  'block_plan') as block_plan,
            ):
                loop_manager.block_active_execplan(
                    connection, str(root), root, 'Specification missing', 'verify passed',
                    reason=loop_manager.BlockReasonCode.REVIEW_SPECIFICATION_DEADLOCK,
                )
            self.assertEqual(source_input.call_args.args[1], 'base')
            self.assertEqual(block_plan.call_args.kwargs['release_fingerprint'],
                             'source-base')
            self.assertEqual(
                block_plan.call_args.kwargs['plan_values']['blocked_review_fingerprint'],
                'spec-document-base',
            )

    def test_review_issue_id_rejects_unsafe_repo_relative_paths(self):
        invalid_paths = (
            "/docs/plans/plan.md",
            "docs/../plan.md",
            "docs/./plan.md",
            "docs//plan.md",
            "docs\\plans\\plan.md",
            "c:/repo/plan.md",
            "docs/plans/control\x01.md",
            "docs/ plan.md",
            "docs/plans/plan.md ",
        )
        for path in invalid_paths:
            review = (
                f"ISSUE_ID: {path}::stable-symbol::blocking\n"
                "VERDICT: FAIL"
            )
            with self.subTest(path=path), self.assertRaisesRegex(
                loop_manager.ReviewerProtocolError,
                "ISSUE_ID 형식이 유효하지 않습니다",
            ):
                loop_manager.parse_reviewer_result(review)

    def test_verify_fingerprint_uses_full_output_before_feedback_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            scripts = worktree / "scripts"
            scripts.mkdir()
            (scripts / "verify.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            lines = [
                f"FAIL tests/test_{index:04}.py::test_case assertion {index}"
                for index in range(400)
            ]
            ordered = "\n".join(lines)
            reordered = "\n".join(reversed(lines))
            results = [(False, False, ordered), (False, False, reordered)]
            with mock.patch("loop_manager.run_verify_process", side_effect=results):
                first_passed, first_output = loop_manager.verify(worktree)
                second_passed, second_output = loop_manager.verify(worktree)

        self.assertFalse(first_passed)
        self.assertFalse(second_passed)
        self.assertLessEqual(len(first_output), 8000)
        self.assertLessEqual(len(second_output), 8000)
        self.assertNotEqual(first_output, second_output)
        first_fingerprint, _ = loop_manager.validation_failure_fingerprint(
            "verify", first_output, worktree
        )
        second_fingerprint, _ = loop_manager.validation_failure_fingerprint(
            "verify", second_output, worktree
        )
        self.assertEqual(first_fingerprint, second_fingerprint)

    def test_stage_histories_survive_cycles_and_block_only_fourth_occurrence(self):
        with tempfile.TemporaryDirectory() as directory:
            root, connection = self._failure_state(directory)
            outputs = ["failure A", "failure B", "failure C", "failure D"]
            decisions = [
                loop_manager.transition_validation_failure(
                    connection, str(root), root, "verify", output
                )
                for output in outputs
            ]
            for _ in range(2):
                decisions.append(
                    loop_manager.transition_validation_failure(
                        connection, str(root), root, "verify", "failure A"
                    )
                )
            review = loop_manager.transition_validation_failure(
                connection, str(root), root, "review", "ISSUE_ID: app.py::failure-a::blocking\nfailure A\nVERDICT: FAIL"
            )
            final = loop_manager.transition_validation_failure(
                connection, str(root), root, "verify", "failure A"
            )
            row = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-013'",
                (str(root),),
            ).fetchone()
            verify_history = json.loads(row["verify_failure_history"])
            review_history = json.loads(row["review_failure_history"])

        self.assertTrue(all(not decision.blocked for decision in decisions))
        self.assertFalse(review.blocked)
        self.assertTrue(final.blocked)
        self.assertEqual(final.occurrences, 4)
        self.assertEqual(row["status"], "PLAN_BLOCKED")
        self.assertIn(final.fingerprint, row["last_error"])
        self.assertEqual(verify_history[final.fingerprint]["occurrences"], 4)
        self.assertEqual(review_history[review.fingerprint]["occurrences"], 1)
        self.assertEqual(
            sorted(record["occurrences"] for record in verify_history.values()),
            [1, 1, 1, 4],
        )

    def test_legacy_review_block_uses_base_and_worktree_fingerprints_across_rebase(self):
        import runtime_state

        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection = self._active_worker_project(directory)
            loop_manager.migrate_legacy_execplans(
                connection, target_repo=str(repo)
            )
            project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            plan = connection.execute(
                "SELECT * FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            worktree = Path(project["worktree"])
            plan_path = str(plan["path"])
            plan_file = worktree / plan_path
            plan_file.write_text(
                plan_file.read_text(encoding="utf-8")
                + "\n## Progress\n\n- [x] Worker checkpoint\n",
                encoding="utf-8",
            )
            git_command(worktree, "add", plan_path)
            git_command(worktree, "commit", "-m", "Record Worker checkpoint")
            base_sha = str(project["active_sha"])
            with connection:
                connection.execute(
                    """
                    UPDATE exec_plans
                    SET status = 'REVIEWING', active_sha = ?, branch = ?,
                        worktree = ?, workspace_owned = 1,
                        reviewer_session_id = 'reviewer-session'
                    WHERE repo = ? AND plan_id = 'G-001'
                    """,
                    (
                        base_sha,
                        str(project["branch"]),
                        str(worktree),
                        str(repo),
                    ),
                )
                connection.execute(
                    """
                    UPDATE projects
                    SET phase = 'REVIEWING', validated_base_sha = ?,
                        reviewer_session_id = 'reviewer-session'
                    WHERE repo = ?
                    """,
                    (base_sha, str(repo)),
                )

            output = (
                "ISSUE_ID: docs/PLANS.md::stable-contract::blocking\n"
                "Contract is still missing.\nVERDICT: FAIL"
            )

            def record_legacy_review_block() -> None:
                active = connection.execute(
                    "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                    (str(repo),),
                ).fetchone()
                blocked_input, blocked_worktree_input = (
                    loop_manager._blocked_validation_input_fingerprints(
                        connection, repo, str(repo), worktree, active,
                    )
                )
                blocked_revision = loop_manager._blocked_validation_base_revision(
                    connection, str(repo), active,
                )
                runtime_state.RuntimeStateRepository(connection).block_plan(
                    str(repo), "G-001", reason_code="review_retry_exhausted",
                    evidence=output, release_fingerprint=blocked_input,
                    plan_values={
                        "blocked_fingerprint": blocked_input,
                        "blocked_worktree_fingerprint": blocked_worktree_input,
                        "blocked_review_fingerprint": (
                            loop_manager._revision_review_issue_fingerprint(
                                repo, blocked_revision, output,
                            )
                        ),
                        "agent_result_snapshot": (
                            loop_manager.capture_git_snapshot(worktree).serialize()
                        ),
                    },
                    clear_projection=True,
                )

            for _ in range(4):
                decision = loop_manager.transition_validation_failure(
                    connection, str(repo), worktree, "review", output
                )
            self.assertFalse(decision.blocked)
            record_legacy_review_block()

            blocked = connection.execute(
                "SELECT * FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            base_fingerprint = loop_manager._revision_execplan_input_fingerprint(
                repo, base_sha, blocked
            )
            worktree_fingerprint = (
                loop_manager._worktree_execplan_input_fingerprint(
                    worktree, blocked
                )
            )
            self.assertEqual(blocked["blocked_fingerprint"], base_fingerprint)
            self.assertEqual(
                blocked["blocked_worktree_fingerprint"],
                worktree_fingerprint,
            )
            self.assertEqual(
                blocked["blocked_review_fingerprint"],
                loop_manager._revision_review_issue_fingerprint(
                    repo, base_sha, output
                ),
            )
            self.assertNotEqual(base_fingerprint, worktree_fingerprint)

            self.assertFalse(
                loop_manager.resume_blocked_execplan_if_inputs_changed(
                    connection, repo, str(repo), "main", state_dir
                )
            )
            unchanged = connection.execute(
                "SELECT status, review_failure_history, reviewer_session_id "
                "FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(unchanged["status"], "PLAN_BLOCKED")
            self.assertEqual(
                next(iter(json.loads(unchanged["review_failure_history"]).values()))[
                    "occurrences"
                ],
                4,
            )
            self.assertEqual(
                unchanged["reviewer_session_id"], "reviewer-session"
            )

            with connection:
                connection.execute(
                    "UPDATE exec_plans SET blocked_review_fingerprint = NULL "
                    "WHERE repo = ? AND plan_id = 'G-001'",
                    (str(repo),),
                )
            (repo / "README.md").write_text(
                "# Unrelated remote change\n",
                encoding="utf-8",
            )
            commit_and_push(repo, "Change unrelated remote input")
            self.assertFalse(
                loop_manager.resume_blocked_execplan_if_inputs_changed(
                    connection, repo, str(repo), "main", state_dir
                )
            )

            (repo / "docs/PLANS.md").write_text(
                "# Plans\n\nDocument the missing maintenance contract.\n",
                encoding="utf-8",
            )
            commit_and_push(repo, "Resolve structured review input")
            latest_sha = git_command(repo, "rev-parse", "origin/main")
            self.assertTrue(
                loop_manager.resume_blocked_execplan_if_inputs_changed(
                    connection, repo, str(repo), "main", state_dir
                )
            )
            resumed = connection.execute(
                "SELECT * FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(resumed["status"], "READY")
            self.assertIsNone(resumed["blocked_fingerprint"])
            self.assertIsNone(resumed["blocked_worktree_fingerprint"])
            self.assertIsNone(resumed["blocked_review_fingerprint"])
            self.assertEqual(json.loads(resumed["review_failure_history"]), {})
            self.assertEqual(
                git_command(worktree, "merge-base", "--is-ancestor", latest_sha, "HEAD"),
                "",
            )

            with connection:
                connection.execute(
                    """
                    UPDATE exec_plans
                    SET status = 'REVIEWING', active_sha = ?,
                        reviewer_session_id = 'reviewer-session'
                    WHERE repo = ? AND plan_id = 'G-001'
                    """,
                    (latest_sha, str(repo)),
                )
                connection.execute(
                    """
                    UPDATE projects
                    SET phase = 'REVIEWING', active_sha = ?,
                        validated_base_sha = ?, branch = ?, worktree = ?,
                        plan_path = ?, reviewer_session_id = 'reviewer-session'
                    WHERE repo = ?
                    """,
                    (
                        latest_sha,
                        latest_sha,
                        str(project["branch"]),
                        str(worktree),
                        plan_path,
                        str(repo),
                    ),
                )
            for _ in range(4):
                decision = loop_manager.transition_validation_failure(
                    connection, str(repo), worktree, "review", output
                )
            self.assertFalse(decision.blocked)
            record_legacy_review_block()
            refreshed = connection.execute(
                "SELECT * FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(
                refreshed["blocked_fingerprint"],
                loop_manager._revision_execplan_input_fingerprint(
                    repo, latest_sha, refreshed
                ),
            )
            connection.close()

    def test_history_persists_and_stage_success_or_blocked_requeue_clears_it(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            database = sqlite3.connect(state_dir / "loop-manager.db")
            database.row_factory = sqlite3.Row
            root, connection = self._failure_state(
                str(Path(directory) / "catalog"), connection=database
            )
            verify = loop_manager.transition_validation_failure(
                connection, str(root), root, "verify", "failure A"
            )
            review = loop_manager.transition_validation_failure(
                connection, str(root), root, "review", "ISSUE_ID: app.py::issue-r::blocking\nissue R\nVERDICT: FAIL"
            )
            connection.close()

            reopened = sqlite3.connect(state_dir / "loop-manager.db")
            reopened.row_factory = sqlite3.Row
            persisted = reopened.execute(
                "SELECT verify_failure_history, review_failure_history "
                "FROM exec_plans WHERE repo = ? AND plan_id = 'G-013'",
                (str(root),),
            ).fetchone()
            self.assertEqual(
                json.loads(persisted["verify_failure_history"])[verify.fingerprint][
                    "occurrences"
                ],
                1,
            )
            self.assertEqual(
                json.loads(persisted["review_failure_history"])[review.fingerprint][
                    "occurrences"
                ],
                1,
            )

            loop_manager.clear_validation_failure_history(
                reopened, str(root), "verify"
            )
            stage_cleared = reopened.execute(
                "SELECT verify_failure_history, review_failure_history "
                "FROM exec_plans WHERE repo = ? AND plan_id = 'G-013'",
                (str(root),),
            ).fetchone()
            self.assertEqual(json.loads(stage_cleared["verify_failure_history"]), {})
            self.assertNotEqual(json.loads(stage_cleared["review_failure_history"]), {})

            reopened.execute(
                "UPDATE exec_plans SET status = 'BLOCKED' "
                "WHERE repo = ? AND plan_id = 'G-013'",
                (str(root),),
            )
            reopened.execute(
                "UPDATE projects SET phase = 'BLOCKED' WHERE repo = ?",
                (str(root),),
            )
            reopened.commit()
            loop_manager._requeue_blocked_execplan(
                reopened, str(root), "G-013"
            )
            requeued = reopened.execute(
                "SELECT verify_failure_history, review_failure_history, "
                "worker_session_id, reviewer_session_id "
                "FROM exec_plans WHERE repo = ? AND plan_id = 'G-013'",
                (str(root),),
            ).fetchone()
            reopened.close()

        self.assertEqual(json.loads(requeued["verify_failure_history"]), {})
        self.assertEqual(json.loads(requeued["review_failure_history"]), {})
        self.assertIsNone(requeued["worker_session_id"])
        self.assertIsNone(requeued["reviewer_session_id"])

    def test_agent_retry_usage_limit_and_session_fallback_are_bounded(self):
        transient_calls = []

        def transient_then_success(session_id, recovery):
            transient_calls.append((session_id, recovery))
            if len(transient_calls) <= loop_manager.MAX_TRANSIENT_AGENT_RETRIES:
                return loop_manager.CodexResult(1, "service unavailable", session_id)
            return loop_manager.CodexResult(0, "ok", "resumed-session")

        recovered = loop_manager.run_agent_with_retries(
            transient_then_success,
            session_id="resumed-session",
        )
        self.assertEqual(recovered.result_kind, loop_manager.AgentResultKind.SUCCESS)
        self.assertEqual(
            len(transient_calls), loop_manager.MAX_TRANSIENT_AGENT_RETRIES + 1
        )

        timeout_calls = []

        def timeout_then_success(session_id, recovery):
            timeout_calls.append((session_id, recovery))
            if len(timeout_calls) == 1:
                raise subprocess.TimeoutExpired(["codex"], 1)
            return loop_manager.CodexResult(0, "ok", session_id)

        timeout_recovered = loop_manager.run_agent_with_retries(
            timeout_then_success,
            session_id="timeout-session",
        )
        self.assertEqual(
            timeout_recovered.result_kind, loop_manager.AgentResultKind.SUCCESS
        )
        self.assertEqual(len(timeout_calls), 2)

        usage_calls = []
        usage = loop_manager.run_agent_with_retries(
            lambda session_id, recovery: (
                usage_calls.append((session_id, recovery))
                or loop_manager.CodexResult(1, "usage limit reached", session_id)
            ),
            session_id="usage-session",
        )
        self.assertEqual(usage.result_kind, loop_manager.AgentResultKind.USAGE_LIMIT)
        self.assertEqual(len(usage_calls), 1)

        fallback_calls = []

        def fallback(session_id, recovery):
            fallback_calls.append((session_id, recovery))
            if session_id:
                return loop_manager.CodexResult(
                    1, "session not found", session_id
                )
            return loop_manager.CodexResult(0, "fresh", "fresh-session")

        fresh = loop_manager.run_agent_with_retries(
            fallback,
            session_id="missing-session",
        )
        self.assertEqual(fresh.session_id, "fresh-session")
        self.assertEqual(
            fallback_calls,
            [("missing-session", False), (None, True)],
        )
        recovered_prompt = loop_manager.worker_prompt(
            "docs/plans/[G-013] Recovery.md",
            "temporary CLI failure",
            "previous Worker result",
            "verify failure output",
            session_recovery=True,
        )
        self.assertIn("이전 Worker 결과", recovered_prompt)
        self.assertIn("previous Worker result", recovered_prompt)
        self.assertIn("verify failure output", recovered_prompt)

    def test_project_results_distinguish_pause_block_and_operational_error(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = str((Path(directory) / "repo").resolve())
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            loop_manager._ensure_execplans_schema(connection)
            connection.execute(
                "INSERT INTO projects (repo, base_branch, phase, last_error) "
                "VALUES (?, 'main', 'WORKING', 'saved')",
                (repo,),
            )
            connection.commit()

            cases = (
                (
                    loop_manager.PROJECT_STEP_TRANSIENT_PAUSE,
                    "WORKING",
                    loop_manager.ProjectRunKind.TRANSIENT_PAUSE,
                    0,
                ),
                (
                    loop_manager.PROJECT_STEP_USAGE_PAUSE,
                    "REVIEWING",
                    loop_manager.ProjectRunKind.USAGE_LIMIT_PAUSE,
                    0,
                ),
                (1, "PROJECT_BLOCKED", loop_manager.ProjectRunKind.BLOCKED, 1),
                (
                    1,
                    "WORKING",
                    loop_manager.ProjectRunKind.OPERATIONAL_ERROR,
                    1,
                ),
            )
            for step, phase, expected_kind, expected_code in cases:
                with self.subTest(kind=expected_kind):
                    connection.execute(
                        "UPDATE projects SET phase = ? WHERE repo = ?",
                        (phase, repo),
                    )
                    connection.commit()
                    with mock.patch(
                        "loop_manager._run_project_step", return_value=step
                    ):
                        result = loop_manager.run_project_result(
                            connection,
                            Path(directory) / "state",
                            repo,
                            Path("unused.ini"),
                            "main",
                            False,
                        )
                    self.assertEqual(result.kind, expected_kind)
                    self.assertEqual(result.exit_code, expected_code)

    def test_distinct_verify_failures_recover_in_one_run_with_persistent_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection = self._active_worker_project(directory)
            roles = []
            worker_sessions = []
            reviewer_sessions = []
            worker_version = 1

            def agent(worktree, _state, _config, role_name, _prompt, **kwargs):
                nonlocal worker_version
                roles.append(role_name)
                if role_name == "worker":
                    worker_sessions.append(kwargs["session_id"])
                    worker_version += 1
                    (worktree / "src/app.py").write_text(
                        f"VALUE = {worker_version}\n", encoding="utf-8"
                    )
                    return loop_manager.CodexResult(
                        0,
                        worker_result(f"Record recovery {worker_version}"),
                        "worker-session",
                    )
                reviewer_sessions.append(kwargs["session_id"])
                return loop_manager.CodexResult(
                    0, "VERDICT: PASS", "reviewer-session"
                )

            verify_results = [
                (False, "failure A"),
                (False, "failure B"),
                (False, "failure C"),
                (False, "failure D"),
                (True, "verify: PASS"),
            ]
            pr_results = [None] * 5 + [{"url": "https://example.test/pr/13"}]
            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", side_effect=matching_branch_pr_lookup(pr_results)
            ), mock.patch(
                "loop_manager.run_codex", side_effect=agent
            ), mock.patch(
                "loop_manager.verify", side_effect=verify_results
            ) as verify:
                result = loop_manager.run_project_result(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT phase, worker_session_id, reviewer_session_id "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan = connection.execute(
                "SELECT status, worker_session_id, reviewer_session_id, "
                "verify_failure_history, review_failure_history "
                "FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()

        self.assertEqual(result.kind, loop_manager.ProjectRunKind.WAITING)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(verify.call_count, 5)
        self.assertEqual(roles, ["worker"] * 5 + ["reviewer"])
        self.assertEqual(worker_sessions, [None] + ["worker-session"] * 4)
        self.assertEqual(reviewer_sessions, [None])
        self.assertIsNone(project["phase"])
        self.assertEqual(plan["status"], "PR_OPEN")
        self.assertEqual(plan["worker_session_id"], "worker-session")
        self.assertEqual(plan["reviewer_session_id"], "reviewer-session")
        self.assertEqual(json.loads(plan["verify_failure_history"]), {})
        self.assertEqual(json.loads(plan["review_failure_history"]), {})

    def test_distinct_review_issues_pause_before_fourth_worker_correction(self):
        messages = (
            "ISSUE_ID: app.py::issue-a::blocking\nissue A\nVERDICT: FAIL",
            "ISSUE_ID: app.py::issue-b::blocking\nissue B\nVERDICT: FAIL",
            "ISSUE_ID: app.py::issue-c::blocking\nissue C\nVERDICT: FAIL",
            "ISSUE_ID: app.py::issue-d::blocking\nissue D\nVERDICT: FAIL",
            "VERDICT: PASS",
        )
        with tempfile.TemporaryDirectory() as directory:
            data = self._run_review_sequence(
                directory, messages, publish=True
            )

        result = data["result"]
        transitions = data["transitions"]
        project = data["project"]
        plan = data["plan"]
        self.assertEqual(result.kind, loop_manager.ProjectRunKind.TRANSIENT_PAUSE)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(data["verify_calls"], 4)
        self.assertEqual(data["roles"], ["worker", "reviewer"] * 4)
        self.assertEqual(
            data["reviewer_sessions"],
            [None] + ["reviewer-session"] * 3,
        )
        self.assertEqual(
            [
                (stage, occurrences, blocked)
                for stage, _fingerprint, occurrences, blocked in transitions
            ],
            [("review", 1, False)] * 4,
        )
        self.assertEqual(
            len(
                {
                    fingerprint
                    for _stage, fingerprint, _occurrences, _blocked in transitions
                }
            ),
            4,
        )
        self.assertEqual(project["phase"], "WORKING")
        self.assertEqual(plan["status"], "IMPLEMENTING")
        self.assertEqual(len(json.loads(plan["review_failure_history"])), 4)

    def test_review_issue_in_changing_sets_pauses_after_three_corrections(self):
        messages = (
            "ISSUE_ID: app.py::a-shared-issue::blocking\n"
            "ISSUE_ID: app.py::z-extra-a::blocking\nVERDICT: FAIL",
            "ISSUE_ID: app.py::a-shared-issue::blocking\n"
            "ISSUE_ID: app.py::z-extra-b::blocking\nVERDICT: FAIL",
            "ISSUE_ID: app.py::a-shared-issue::blocking\n"
            "ISSUE_ID: app.py::z-extra-c::blocking\nVERDICT: FAIL",
            "ISSUE_ID: app.py::a-shared-issue::blocking\n"
            "ISSUE_ID: app.py::z-extra-d::blocking\nVERDICT: FAIL",
        )
        with tempfile.TemporaryDirectory() as directory:
            data = self._run_review_sequence(
                directory, messages, publish=False
            )

        result = data["result"]
        transitions = data["transitions"]
        project = data["project"]
        plan = data["plan"]
        history = json.loads(plan["review_failure_history"])
        self.assertEqual(result.kind, loop_manager.ProjectRunKind.TRANSIENT_PAUSE)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(data["verify_calls"], 4)
        self.assertEqual(data["roles"], ["worker", "reviewer"] * 4)
        self.assertEqual(
            data["reviewer_sessions"],
            [None] + ["reviewer-session"] * 3,
        )
        self.assertEqual(
            [
                (stage, occurrences, blocked)
                for stage, _fingerprint, occurrences, blocked in transitions
            ],
            [
                ("review", 1, False),
                ("review", 2, False),
                ("review", 3, False),
                ("review", 4, False),
            ],
        )
        self.assertEqual(
            len(
                {
                    fingerprint
                    for _stage, fingerprint, _occurrences, _blocked in transitions
                }
            ),
            1,
        )
        self.assertEqual(project["phase"], "WORKING")
        self.assertEqual(plan["status"], "IMPLEMENTING")
        self.assertEqual(
            sorted(record["occurrences"] for record in history.values()),
            [1, 1, 1, 1, 4],
        )

    def test_review_protocol_errors_do_not_retry_worker_or_consume_history(self):
        invalid_reviews = (
            "blocking issue without an identifier\nVERDICT: FAIL",
            "ISSUE_ID: app.py:42 prose\nblocking issue\nVERDICT: FAIL",
            "ISSUE_ID: app.py::pass-issue::blocking\nVERDICT: PASS",
            "ISSUE_ID: app.py::missing-verdict::blocking\nblocking issue",
            "VERDICT: PASS\nVERDICT: FAIL",
            "ISSUE_ID: app.py::unknown-verdict::blocking\nVERDICT: MAYBE",
            "VERDICT: PASS\ntrailing explanation",
        )
        for review in invalid_reviews:
            with self.subTest(review=review), tempfile.TemporaryDirectory() as directory:
                data = self._run_review_sequence(
                    directory,
                    (review,) * loop_manager.MAX_REVIEWER_PROTOCOL_ATTEMPTS,
                    publish=False,
                )

            result = data["result"]
            project = data["project"]
            plan = data["plan"]
            self.assertEqual(result.kind, loop_manager.ProjectRunKind.WAITING)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(
                data["roles"],
                ["worker"]
                + ["reviewer"] * loop_manager.MAX_REVIEWER_PROTOCOL_ATTEMPTS,
            )
            self.assertEqual(
                data["reviewer_sessions"],
                [None]
                + ["reviewer-session"]
                * (loop_manager.MAX_REVIEWER_PROTOCOL_ATTEMPTS - 1),
            )
            self.assertEqual(data["verify_calls"], 1)
            self.assertEqual(data["transitions"], [])
            self.assertIsNone(project["phase"])
            self.assertIn("Reviewer 결과 프로토콜 오류", plan["last_error"])
            self.assertIn("프로토콜 교정 3회 실패", plan["last_error"])
            self.assertEqual(plan["review_result"], review)
            self.assertEqual(plan["status"], "PLAN_BLOCKED")
            self.assertEqual(json.loads(plan["review_failure_history"]), {})
            self.assertEqual(json.loads(plan["review_level_history"]), {})
            self.assertEqual(plan["effective_worker_level"], 1)
            for prompt in data["reviewer_prompts"][1:]:
                self.assertIn("직전 Reviewer 결과가 아래 프로토콜 검증에 실패", prompt)
                self.assertIn(review, prompt)

    def test_review_protocol_error_is_corrected_to_pass_in_same_session(self):
        invalid = "blocking issue without an identifier\nVERDICT: FAIL"
        with tempfile.TemporaryDirectory() as directory:
            data = self._run_review_sequence(
                directory,
                (invalid, "VERDICT: PASS"),
                publish=True,
            )

        self.assertEqual(data["result"].kind, loop_manager.ProjectRunKind.WAITING)
        self.assertEqual(data["result"].exit_code, 0)
        self.assertEqual(data["roles"], ["worker", "reviewer", "reviewer"])
        self.assertEqual(data["reviewer_sessions"], [None, "reviewer-session"])
        self.assertEqual(data["verify_calls"], 1)
        self.assertEqual(data["transitions"], [])
        self.assertIsNone(data["project"]["phase"])
        self.assertEqual(data["plan"]["status"], "PR_OPEN")
        self.assertIsNone(data["plan"]["last_error"])
        self.assertIn(
            "Reviewer FAIL 결과에 유효한 ISSUE_ID가 없습니다",
            data["reviewer_prompts"][1],
        )
        self.assertIn(invalid, data["reviewer_prompts"][1])

    def test_review_protocol_error_is_corrected_to_fail_without_worker_retry(self):
        invalid = "blocking issue without an identifier\nVERDICT: FAIL"
        valid_fail = (
            "ISSUE_ID: src/app.py::value::incorrect\n"
            "value remains incorrect\nVERDICT: FAIL"
        )
        with tempfile.TemporaryDirectory() as directory:
            data = self._run_review_sequence(
                directory,
                (invalid, valid_fail, "VERDICT: PASS"),
                publish=True,
            )

        self.assertEqual(data["result"].kind, loop_manager.ProjectRunKind.WAITING)
        self.assertEqual(data["result"].exit_code, 0)
        self.assertEqual(
            data["roles"],
            ["worker", "reviewer", "reviewer", "worker", "reviewer"],
        )
        self.assertEqual(data["verify_calls"], 2)
        self.assertEqual(
            [
                (stage, occurrences, blocked)
                for stage, _fingerprint, occurrences, blocked in data["transitions"]
            ],
            [("review", 1, False)],
        )
        self.assertIn(invalid, data["reviewer_prompts"][1])
        self.assertNotIn(
            "직전 Reviewer 결과가 아래 프로토콜 검증에 실패",
            data["reviewer_prompts"][2],
        )

    def test_review_failure_then_verify_failure_keeps_stage_counts_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection = self._active_worker_project(directory)
            roles = []
            worker_sessions = []
            reviewer_sessions = []
            review_messages = iter(
                ("ISSUE_ID: app.py::issue-a::blocking\nblocking issue A\nVERDICT: FAIL", "VERDICT: PASS")
            )
            worker_version = 1
            transitions = []
            original_transition = loop_manager.transition_validation_failure

            def record_transition(*args, **kwargs):
                decision = original_transition(*args, **kwargs)
                transitions.append(
                    (decision.stage, decision.fingerprint, decision.occurrences)
                )
                return decision

            def agent(worktree, _state, _config, role_name, _prompt, **kwargs):
                nonlocal worker_version
                roles.append(role_name)
                if role_name == "worker":
                    worker_sessions.append(kwargs["session_id"])
                    worker_version += 1
                    (worktree / "src/app.py").write_text(
                        f"VALUE = {worker_version}\n", encoding="utf-8"
                    )
                    return loop_manager.CodexResult(
                        0,
                        worker_result(f"Record review recovery {worker_version}"),
                        "worker-session",
                    )
                reviewer_sessions.append(kwargs["session_id"])
                return loop_manager.CodexResult(
                    0, next(review_messages), "reviewer-session"
                )

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch",
                side_effect=matching_branch_pr_lookup([
                    None,
                    None,
                    None,
                    {"url": "https://example.test/pr/13"},
                ]),
            ), mock.patch(
                "loop_manager.run_codex", side_effect=agent
            ), mock.patch(
                "loop_manager.verify",
                side_effect=[
                    (True, "verify: PASS"),
                    (False, "verify issue V"),
                    (True, "verify: PASS"),
                ],
            ), mock.patch(
                "loop_manager.transition_validation_failure",
                side_effect=record_transition,
            ):
                result = loop_manager.run_project_result(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            row = connection.execute(
                "SELECT phase, reviewer_session_id FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan = connection.execute(
                "SELECT status, reviewer_session_id, verify_failure_history, "
                "review_failure_history "
                "FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()

        self.assertEqual(result.kind, loop_manager.ProjectRunKind.WAITING)
        self.assertEqual(
            roles,
            ["worker", "reviewer", "worker", "worker", "reviewer"],
        )
        self.assertEqual(
            worker_sessions, [None, "worker-session", "worker-session"]
        )
        self.assertEqual(
            reviewer_sessions, [None, "reviewer-session"]
        )
        self.assertEqual(
            [(stage, occurrences) for stage, _fingerprint, occurrences in transitions],
            [("review", 1), ("verify", 1)],
        )
        self.assertIsNone(row["phase"])
        self.assertEqual(plan["status"], "PR_OPEN")
        self.assertEqual(plan["reviewer_session_id"], "reviewer-session")
        self.assertEqual(json.loads(plan["verify_failure_history"]), {})
        self.assertEqual(json.loads(plan["review_failure_history"]), {})

    def test_existing_database_migrates_recovery_fields_without_losing_history(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            database = sqlite3.connect(state_dir / "loop-manager.db")
            legacy_projects = loop_manager.SCHEMA.replace(
                "    reviewer_session_id TEXT,\n", ""
            ).replace("    review_result TEXT,\n", "")
            for line in (
                "    pr_conflict_recovery INTEGER NOT NULL DEFAULT 0,\n",
                "    pr_conflict_head_oid TEXT,\n",
                "    pr_conflict_base_oid TEXT,\n",
            ):
                legacy_projects = legacy_projects.replace(line, "")
                "    pr_conflict_recovery INTEGER NOT NULL DEFAULT 0,\n",
                "    pr_conflict_head_oid TEXT,\n",
                "    pr_conflict_base_oid TEXT,\n",
            legacy_execplans = loop_manager.EXEC_PLANS_SCHEMA
            for line in (
                "    reviewer_session_id TEXT,\n",
                "    review_result TEXT,\n",
                "    verify_failure_history TEXT NOT NULL DEFAULT '{}',\n",
                "    review_failure_history TEXT NOT NULL DEFAULT '{}',\n",
                "    initial_worker_level INTEGER,\n",
                "    effective_worker_level INTEGER,\n",
                "    review_level_history TEXT NOT NULL DEFAULT '{}',\n",
                "    blocked_review_fingerprint TEXT,\n",
            ):
                legacy_execplans = legacy_execplans.replace(line, "")
            database.execute(legacy_projects)
            database.execute(legacy_execplans)
            database.execute(
                "INSERT INTO projects (repo, base_branch, phase) "
                "VALUES ('repo', 'main', 'VERIFYING')"
            )
            database.execute(
                """
                INSERT INTO exec_plans (
                    repo, plan_id, path, title, sources, depends_on, priority,
                    content_hash, status, retry_count
                ) VALUES ('repo', 'G-013', 'plan.md', 'Plan', '[]', '[]',
                          'critical', 'hash', 'VERIFYING', 2)
                """
            )
            database.commit()
            database.close()

            with loop_manager.connect_database(state_dir) as migrated:
                project_columns = {
                    row[1] for row in migrated.execute("PRAGMA table_info(projects)")
                }
                plan_columns = {
                    row[1] for row in migrated.execute("PRAGMA table_info(exec_plans)")
                }
                row = migrated.execute(
                    "SELECT retry_count, verify_failure_history, "
                    "review_failure_history, initial_worker_level, "
                    "effective_worker_level, review_level_history FROM exec_plans"
                ).fetchone()

        self.assertTrue(
            {
                "reviewer_session_id",
                "review_result",
                "pr_conflict_recovery",
                "pr_conflict_head_oid",
                "pr_conflict_base_oid",
            } <= project_columns
        )
        self.assertTrue(
            {
                "reviewer_session_id",
                "review_result",
                "verify_failure_history",
                "review_failure_history",
                "pr_conflict_recovery",
                "pr_conflict_head_oid",
                "pr_conflict_base_oid",
                "initial_worker_level",
                "effective_worker_level",
                "review_level_history",
                "blocked_review_fingerprint",
            }
            <= plan_columns
        )
        self.assertEqual(row["retry_count"], 2)
        self.assertEqual(json.loads(row["verify_failure_history"]), {})
        self.assertEqual(json.loads(row["review_failure_history"]), {})
        self.assertIsNone(row["initial_worker_level"])
        self.assertIsNone(row["effective_worker_level"])
        self.assertEqual(json.loads(row["review_level_history"]), {})


class ReviewDrivenWorkerProfileEscalationTest(unittest.TestCase):
    def _level_state(
        self,
        directory: str,
        *,
        initial_level: int,
    ) -> tuple[Path, Path, sqlite3.Connection]:
        root = make_catalog_root(str(Path(directory) / "catalog"))
        plan = write_execplan(root, "G-019", "Review Escalation")
        state_dir = Path(directory) / "state"
        state_dir.mkdir()
        connection = loop_manager.connect_database(state_dir, create=True)
        connection.execute(
            """
            INSERT INTO projects (repo, base_branch, phase, plan_path, worktree)
            VALUES (?, 'main', 'REVIEWING', ?, ?)
            """,
            (str(root), plan.relative_to(root).as_posix(), str(root)),
        )
        connection.execute(
            """
            INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on, priority,
                content_hash, status, worktree, initial_worker_level,
                effective_worker_level
            ) VALUES (?, 'G-019', ?, 'Review Escalation', ?, '[]', 'critical',
                      'hash', 'REVIEWING', ?, ?, ?)
            """,
            (
                str(root),
                plan.relative_to(root).as_posix(),
                json.dumps(["docs/PRODUCT.md"]),
                str(root),
                initial_level,
                initial_level,
            ),
        )
        connection.commit()
        return root, state_dir, connection

    def _active_worker_project(
        self,
        directory: str,
    ) -> tuple[Path, Path, sqlite3.Connection]:
        repo = make_remote_repository(directory)
        state_dir = Path(directory) / "state"
        state_dir.mkdir()
        (repo / "docs/plans").mkdir()
        plan = write_execplan(repo, "G-019", "Review Escalation")
        commit_and_push(repo, "Add escalation plan")
        plan_sha = git_command(repo, "rev-parse", "origin/main")
        branch, worktree = loop_manager.expected_execplan_workspace(
            state_dir, str(repo), "G-019"
        )
        loop_manager.make_worktree(repo, worktree, branch, "main")
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        connection.execute(
            """
            INSERT INTO projects (
                repo, base_branch, active_sha, plan_sha, phase, branch,
                worktree, plan_path
            ) VALUES (?, 'main', ?, ?, 'WORKING', ?, ?, ?)
            """,
            (
                str(repo),
                plan_sha,
                plan_sha,
                branch,
                str(worktree),
                plan.relative_to(repo).as_posix(),
            ),
        )
        connection.commit()
        return repo, state_dir, connection

    def _run_reviews(
        self,
        directory: str,
        reviewer_results: tuple[loop_manager.CodexResult, ...],
        *,
        publish: bool,
    ) -> tuple[
        loop_manager.ProjectRunResult,
        list[int],
        sqlite3.Row,
    ]:
        repo, state_dir, connection = self._active_worker_project(directory)
        worker_levels: list[int] = []
        worker_version = 1
        responses = iter(reviewer_results)

        def agent(worktree, _state, _config, role_name, _prompt, **kwargs):
            nonlocal worker_version
            if role_name == "worker":
                worker_levels.append(kwargs["level"])
                worker_version += 1
                (worktree / "src/app.py").write_text(
                    f"VALUE = {worker_version}\n", encoding="utf-8"
                )
                return loop_manager.CodexResult(
                    0,
                    worker_result(f"Record escalation {worker_version}"),
                    "worker-session",
                )
            return next(responses)

        pr_results: list[dict[str, str] | None] = [None] * len(reviewer_results)
        if publish:
            pr_results.append({"url": "https://example.test/pr/19"})
        else:
            pr_results.extend((None, None))

        with mock.patch(
            "loop_manager.validate_repository", return_value=repo
        ), mock.patch(
            "loop_manager.remote_base", return_value="main"
        ), mock.patch(
            "loop_manager.ensure_safe_base_worktree"
        ), mock.patch(
            "loop_manager.pr_for_branch", side_effect=matching_branch_pr_lookup(pr_results)
        ), mock.patch(
            "loop_manager.run_codex", side_effect=agent
        ), mock.patch(
            "loop_manager.verify", return_value=(True, "verify: PASS")
        ):
            result = loop_manager.run_project_result(
                connection,
                state_dir,
                str(repo),
                Path("unused.ini"),
                "main",
                False,
            )

        plan = connection.execute(
            """
            SELECT status, review_failure_history, initial_worker_level,
                   effective_worker_level, review_level_history
            FROM exec_plans
            WHERE repo = ? AND plan_id = 'G-019'
            """,
            (str(repo),),
        ).fetchone()
        connection.close()
        return result, worker_levels, plan

    def test_new_execplan_stores_capped_initial_and_effective_level(self):
        cases = (
            ("low", "low", 1),
            ("low", "critical", 4),
            ("medium", "critical", 6),
            ("high", "high", 7),
            ("high", "critical", 7),
        )
        for complexity, risk, expected in cases:
            with self.subTest(complexity=complexity, risk=risk):
                self.assertEqual(
                    loop_manager.worker_level(
                        {"complexity": complexity, "risk": risk}
                    ),
                    expected,
                )

        with tempfile.TemporaryDirectory() as directory:
            root = make_catalog_root(directory)
            plan_path = write_execplan(
                root,
                "G-019",
                "Review Escalation",
                complexity="high",
                risk="critical",
            )
            plan = loop_manager.validate_execplan(
                root, plan_path.relative_to(root).as_posix()
            )
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            loop_manager._ensure_execplans_schema(connection)
            connection.execute(
                "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
                (str(root),),
            )
            loop_manager.register_execplans(
                connection,
                str(root),
                root,
                [(plan_path.relative_to(root).as_posix(), plan)],
                "plan-sha",
                "planning-fingerprint",
            )
            stored = connection.execute(
                """
                SELECT initial_worker_level, effective_worker_level,
                       review_level_history
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-019'
                """,
                (str(root),),
            ).fetchone()
            connection.close()

        self.assertEqual(stored["initial_worker_level"], 7)
        self.assertEqual(stored["effective_worker_level"], 7)
        self.assertEqual(json.loads(stored["review_level_history"]), {})

    def test_review_state_transition_escalates_one_level_and_caps_at_eight(self):
        history: dict[str, dict[str, int]] = {}
        effective_level = 1

        for expected_level in range(2, 9):
            fingerprint = f"same-at-{effective_level}"
            first = loop_manager.evaluate_review_worker_level(
                effective_level, history, (fingerprint,)
            )
            self.assertFalse(first.escalated)
            second = loop_manager.evaluate_review_worker_level(
                effective_level, first.review_history, (fingerprint,)
            )
            self.assertTrue(second.escalated)
            self.assertEqual(second.effective_level, expected_level)
            history = second.review_history
            effective_level = second.effective_level

        capped = loop_manager.evaluate_review_worker_level(
            8, history, ("distinct-a", "distinct-b", "distinct-c")
        )
        self.assertFalse(capped.escalated)
        self.assertEqual(capped.effective_level, 8)

        distinct = loop_manager.evaluate_review_worker_level(
            4, {}, ("issue-a", "issue-b", "issue-c")
        )
        self.assertTrue(distinct.escalated)
        self.assertEqual(distinct.effective_level, 5)

    def test_review_level_persists_with_global_history_and_blocked_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            root, state_dir, connection = self._level_state(
                directory, initial_level=7
            )
            output = (
                "ISSUE_ID: app.py::same-issue::blocking\n"
                "Actionable issue\nVERDICT: FAIL"
            )
            first = loop_manager.transition_validation_failure(
                connection, str(root), root, "review", output
            )
            connection.close()

            reopened = loop_manager.connect_database(state_dir)
            before = reopened.execute(
                """
                SELECT effective_worker_level, review_level_history
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-019'
                """,
                (str(root),),
            ).fetchone()
            self.assertEqual(before["effective_worker_level"], 7)
            self.assertEqual(
                json.loads(before["review_level_history"])["7"][first.fingerprint],
                1,
            )

            second = loop_manager.transition_validation_failure(
                reopened, str(root), root, "review", output
            )
            escalated = reopened.execute(
                """
                SELECT effective_worker_level, review_level_history,
                       review_failure_history
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-019'
                """,
                (str(root),),
            ).fetchone()
            self.assertEqual(escalated["effective_worker_level"], 8)
            self.assertEqual(
                json.loads(escalated["review_level_history"])["7"][second.fingerprint],
                2,
            )
            self.assertEqual(
                json.loads(escalated["review_failure_history"])[second.fingerprint][
                    "occurrences"
                ],
                2,
            )

            reopened.execute(
                "UPDATE exec_plans SET status = 'BLOCKED' "
                "WHERE repo = ? AND plan_id = 'G-019'",
                (str(root),),
            )
            reopened.execute(
                "UPDATE projects SET phase = 'BLOCKED' WHERE repo = ?",
                (str(root),),
            )
            reopened.commit()
            loop_manager._requeue_blocked_execplan(
                reopened, str(root), "G-019"
            )
            reset = reopened.execute(
                """
                SELECT initial_worker_level, effective_worker_level,
                       review_level_history, review_failure_history
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-019'
                """,
                (str(root),),
            ).fetchone()
            reopened.close()

        self.assertEqual(reset["initial_worker_level"], 7)
        self.assertEqual(reset["effective_worker_level"], 7)
        self.assertEqual(json.loads(reset["review_level_history"]), {})
        self.assertEqual(json.loads(reset["review_failure_history"]), {})

    def test_verify_and_invalid_review_do_not_change_escalation_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _state_dir, connection = self._level_state(
                directory, initial_level=4
            )
            review = (
                "ISSUE_ID: app.py::review-only::blocking\n"
                "Actionable issue\nVERDICT: FAIL"
            )
            loop_manager.transition_validation_failure(
                connection, str(root), root, "review", review
            )
            before = connection.execute(
                """
                SELECT effective_worker_level, review_level_history
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-019'
                """,
                (str(root),),
            ).fetchone()

            loop_manager.transition_validation_failure(
                connection, str(root), root, "verify", "unit failure"
            )
            with self.assertRaises(loop_manager.ReviewerProtocolError):
                loop_manager.transition_validation_failure(
                    connection,
                    str(root),
                    root,
                    "review",
                    "VERDICT: FAIL",
                )
            after = connection.execute(
                """
                SELECT effective_worker_level, review_level_history
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-019'
                """,
                (str(root),),
            ).fetchone()
            connection.close()

        self.assertEqual(before["effective_worker_level"], 4)
        self.assertEqual(after["effective_worker_level"], 4)
        self.assertEqual(after["review_level_history"], before["review_level_history"])

    def test_worker_uses_escalated_level_only_after_second_matching_review(self):
        issue = (
            "ISSUE_ID: app.py::persistent-issue::blocking\n"
            "Actionable issue\nVERDICT: FAIL"
        )
        reviews = (
            loop_manager.CodexResult(0, issue, "reviewer-session"),
            loop_manager.CodexResult(0, issue, "reviewer-session"),
            loop_manager.CodexResult(0, "VERDICT: PASS", "reviewer-session"),
        )
        with tempfile.TemporaryDirectory() as directory:
            result, worker_levels, plan = self._run_reviews(
                directory, reviews, publish=True
            )

        self.assertEqual(result.kind, loop_manager.ProjectRunKind.WAITING)
        self.assertEqual(worker_levels, [1, 1, 2])
        self.assertEqual(plan["initial_worker_level"], 1)
        self.assertEqual(plan["effective_worker_level"], 2)
        self.assertEqual(json.loads(plan["review_failure_history"]), {})
        level_history = json.loads(plan["review_level_history"])
        self.assertEqual(sorted(level_history["1"].values()), [2])

    def test_reviewer_cli_failure_does_not_escalate_worker(self):
        reviews = (
            loop_manager.CodexResult(
                1,
                "configuration failed",
                "reviewer-session",
                loop_manager.AgentResultKind.PERMANENT_CONFIGURATION,
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            result, worker_levels, plan = self._run_reviews(
                directory, reviews, publish=False
            )

        self.assertEqual(result.kind, loop_manager.ProjectRunKind.OPERATIONAL_ERROR)
        self.assertEqual(worker_levels, [1])
        self.assertEqual(plan["effective_worker_level"], 1)
        self.assertEqual(json.loads(plan["review_level_history"]), {})
        self.assertEqual(json.loads(plan["review_failure_history"]), {})

    def test_codex_and_claude_level_eight_profiles_share_validation_boundary(self):
        config = loop_manager.ROOT / "config/config.ini.example"
        with mock.patch(
            "loop_manager.codex_model_efforts",
            return_value={"gpt-6-astra": frozenset({"low"})},
        ):
            self.assertEqual(
                loop_manager.worker_profile(config, 8),
                ("gpt-6-astra", "low"),
            )
        self.assertEqual(
            loop_manager.provider_worker_profile(
                config,
                "claude",
                8,
                {"claude-opus-5": frozenset({"max"})},
            ),
            ("claude-opus-5", "max"),
        )

        with tempfile.TemporaryDirectory() as directory:
            with mock.patch(
                "loop_manager.codex_model_efforts",
                return_value={"gpt-6-astra": frozenset({"medium"})},
            ), mock.patch("loop_manager.subprocess.run") as subprocess_run:
                with self.assertRaisesRegex(RuntimeError, "지원하지 않습니다"):
                    loop_manager.run_codex(
                        Path(directory) / "worktree",
                        Path(directory) / "state",
                        config,
                        "worker",
                        "prompt",
                        read_only=False,
                        level=8,
                    )
            subprocess_run.assert_not_called()

        invalid_profiles = (
            (
                "codex-alias",
                "[codex.models]\nfrontier = gpt-6-astra\nstrong = gpt-5.6-sol\n"
                "[codex.worker]\nlevel8 = strong,low\n",
                "codex",
                {"gpt-5.6-sol": frozenset({"low"})},
                "frontier,low",
            ),
            (
                "claude-alias",
                "[claude.models]\nstrong = claude-opus-5\nfast = claude-haiku\n"
                "[claude.worker]\nlevel8 = fast,max\n",
                "claude",
                {"claude-haiku": frozenset({"max"})},
                "strong,max",
            ),
            (
                "claude-family",
                "[claude.models]\nstrong = claude-fable-5\n"
                "[claude.worker]\nlevel8 = strong,max\n",
                "claude",
                {"claude-fable-5": frozenset({"max"})},
                "claude-opus-5",
            ),
            (
                "unsupported-effort",
                "[codex.models]\nfrontier = gpt-6-astra\n"
                "[codex.worker]\nlevel8 = frontier,low\n",
                "codex",
                {"gpt-6-astra": frozenset({"medium"})},
                "지원하지 않습니다",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            for name, contents, provider, catalog, message in invalid_profiles:
                with self.subTest(name=name):
                    profile_config = Path(directory) / f"{name}.ini"
                    profile_config.write_text(contents, encoding="utf-8")
                    with self.assertRaisesRegex(RuntimeError, message):
                        loop_manager.provider_worker_profile(
                            profile_config, provider, 8, catalog
                        )


class WorkerPublicationTest(unittest.TestCase):
    plan = {"id": "G-008", "title": "Descriptive Worker Commits"}

    def test_worker_result_fixture_satisfies_new_and_publication_contracts(self):
        message = worker_result(
            "Store immutable plan links", "Adds immutable PR context.",
            "test_one: PASS",
        )
        outcome = parse_worker_outcome(
            message, plan_path="docs/plans/[G-008] Descriptive Worker Commits.md",
            sources=(), documents={},
        )
        publication = loop_manager.parse_worker_result(message, self.plan)
        self.assertEqual(outcome.kind, "ready_for_validation")
        self.assertEqual(publication.commit_title, "Store immutable plan links")

    def test_worker_result_contract_preserves_fields_and_rejects_bad_titles(self):
        publication = loop_manager.parse_worker_result(
            worker_result("Store immutable plan links", "Adds immutable PR context.", "test_one: PASS"),
            self.plan,
        )

        self.assertEqual(publication.commit_title, "Store immutable plan links")
        self.assertEqual(publication.summary, "Adds immutable PR context.")
        self.assertEqual(publication.test_evidence, "test_one: PASS")
        resolved = loop_manager.parse_worker_result(
            worker_result(
                "Record resolved test evidence",
                "Records a corrected targeted test.",
                "FAILED_COMMAND: python3 -m unittest tests.test_one\n"
                "first run: FAIL\n"
                "RERUN: python3 -m unittest tests.test_one\nPASS",
            ),
            self.plan,
        )
        self.assertIn(
            "RERUN: python3 -m unittest tests.test_one", resolved.test_evidence
        )
        document_reason = "문서 바이트 적용만 수행하므로 실행 가능한 관련 unit test가 없습니다."
        document_publication = loop_manager.parse_worker_result(
            worker_result("Apply approved document bytes", "Summary", document_reason),
            self.plan,
        )
        self.assertEqual(
            loop_manager.reviewer_targeted_test_reason(document_publication.test_evidence),
            document_reason,
        )
        freeform_reason = "문서만 변경해 관련 unit test를 실행하지 않았습니다"
        freeform_publication = loop_manager.parse_worker_result(
            worker_result("Explain omitted document tests", "Summary", freeform_reason),
            self.plan,
        )
        self.assertEqual(
            loop_manager.reviewer_targeted_test_reason(freeform_publication.test_evidence),
            freeform_reason,
        )
        alternate_reason = "문서만 변경되어 적용할 unit test가 없어 실행하지 않았습니다"
        alternate_publication = loop_manager.parse_worker_result(
            worker_result("Explain omitted unit tests", "Summary", alternate_reason),
            self.plan,
        )
        self.assertEqual(
            loop_manager.reviewer_targeted_test_reason(alternate_publication.test_evidence),
            alternate_reason,
        )
        configuration_reason = "설정만 바뀌어 unit test를 수행하지 못했습니다"
        configuration_publication = loop_manager.parse_worker_result(
            worker_result("Explain omitted configuration tests", "Summary", configuration_reason),
            self.plan,
        )
        self.assertEqual(
            loop_manager.reviewer_targeted_test_reason(configuration_publication.test_evidence),
            configuration_reason,
        )
        invalid_results = (
            worker_result("", "Summary", "test: PASS"),
            worker_result(
                "Report unresolved targeted failure",
                "Summary",
                "test: FAIL",
            ),
            "COMMIT_TITLE: First line\nsecond line\nPR_SUMMARY: Summary\nTARGETED_TESTS:\ntest: PASS",
            worker_result("Descriptive Worker Commits", "Summary", "test: PASS"),
            worker_result("Implement G-008: Descriptive Worker Commits", "Summary", "test: PASS"),
            worker_result("Update", "Summary", "test: PASS"),
            worker_result("Implement G-008 worker metadata", "Summary", "test: PASS"),
            worker_result("Update.", "Summary", "test: PASS"),
            worker_result("Implement changes", "Summary", "test: PASS"),
            worker_result("Implement code changes", "Summary", "test: PASS"),
            worker_result("Update implementation", "Summary", "test: PASS"),
            worker_result("Implement the changes", "Summary", "test: PASS"),
            worker_result("Fix!", "Summary", "test: PASS"),
            worker_result("Descriptive Worker Commits.", "Summary", "test: PASS"),
            worker_result("Reject missing test disposition", "Summary", "none"),
            worker_result("Reject unexplained no-test claim", "Summary", "실행 가능한 관련 unit test가 없습니다"),
            worker_result("Reject negated pass result", "Summary", "test_api did not pass"),
            worker_result(
                "Reject unexplained mixed test report", "Summary",
                "실행 가능한 관련 unit test가 없습니다; test_api: PASS",
            ),
        )
        for result in invalid_results:
            with self.subTest(result=result):
                with self.assertRaises(loop_manager.WorkerResultError):
                    loop_manager.parse_worker_result(result, self.plan)

    def test_no_test_reason_omits_same_line_success_details_and_commands(self):
        reason = "문서만 변경해 관련 unit test를 실행하지 않았습니다"
        for evidence in (
            f"test_api: PASS; {reason}; python3 -m unittest tests.test_api",
            f"{reason} test_api: PASS python3 -m unittest tests.test_api",
            f"{reason} npm test: 3 tests passed",
        ):
            with self.subTest(evidence=evidence):
                publication = loop_manager.parse_worker_result(
                    worker_result("Explain document test omission", "Summary", evidence),
                    self.plan,
                )
                extracted = loop_manager.reviewer_targeted_test_reason(
                    publication.test_evidence,
                )
                self.assertEqual(extracted, reason)
                prompt = loop_manager.reviewer_prompt("main", extracted)
                self.assertIn(reason, prompt)
                self.assertNotIn("test_api: PASS", prompt)
                self.assertNotIn("python3 -m unittest tests.test_api", prompt)
                self.assertNotIn("npm test: 3 tests passed", prompt)

    def test_targeted_test_evidence_requires_a_clean_explicit_rerun(self):
        valid_evidence = (
            "pytest: 0 failed, 2 passed",
            (
                "FAILED_COMMAND: python3  -m unittest tests.test_case\n"
                "first run: 1 failed, 2 passed\n"
                "RERUN: python3 -m  unittest tests.test_case\n3 passed"
            ),
            (
                "FAILED_COMMAND: python3 -m unittest tests.test_syntax\n"
                "first run: SyntaxError 발생\n"
                "RERUN: python3 -m unittest tests.test_syntax\n1 passed"
            ),
            (
                "FAILED_COMMAND: python3 -m unittest tests.test_api\n"
                "test_api did not pass\n"
                "RERUN: python3 -m unittest tests.test_api\n1 passed"
            ),
        )
        for evidence in valid_evidence:
            with self.subTest(evidence=evidence):
                publication = loop_manager.parse_worker_result(
                    worker_result(
                        "Validate clean targeted reruns", "Summary", evidence
                    ),
                    self.plan,
                )
                self.assertEqual(publication.test_evidence, evidence)

        invalid_evidence = (
            "pytest: 1 failed, 2 passed",
            "unittest: 1 error, 2 passed",
            "RERUN: pytest: 1 failed, 2 passed",
            "SyntaxError 발생, 수정 후 통과",
            "targeted test raised AssertionError: mismatch; then passed",
            "targeted test: FAIL\nunable to run the rerun",
            (
                "FAILED_COMMAND: python3 -m unittest tests.test_a\n"
                "test_a: FAIL\n"
                "RERUN: python3 -m unittest tests.test_b\nPASS"
            ),
            (
                "FAILED_COMMAND: python3 -m unittest tests.test_a\n"
                "test_a: FAIL\n"
                "RERUN: python3 -m unittest tests.test_a\n"
                "FAILED_COMMAND: python3 -m unittest tests.test_b\n"
                "test_b OK"
            ),
        )
        for evidence in invalid_evidence:
            with self.subTest(evidence=evidence):
                with self.assertRaises(loop_manager.WorkerResultError):
                    loop_manager.parse_worker_result(
                        worker_result(
                            "Reject mixed targeted summaries", "Summary", evidence
                        ),
                        self.plan,
                    )

    def test_github_origin_normalization_matches_repository_validation(self):
        self.assertEqual(
            loop_manager.github_repository("HTTPS://WWW.GitHub.Com/Owner/Repo.GIT"),
            "owner/repo",
        )
        url = loop_manager.immutable_github_blob_url(
            "https://www.github.com/Owner/Repo.git",
            "a" * 40,
            "docs/plans/[G-008] 한글.md",
        )
        self.assertIn("github.com/owner/repo/blob/", url)
        self.assertIn("%5BG-008%5D%20%ED%95%9C%EA%B8%80.md", url)

    def test_existing_active_database_backfills_plan_sha_or_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            (repo / "docs/plans").mkdir()
            plan = write_execplan(repo, "G-001", "Worker Boundary")
            git_command(repo, "add", str(plan.relative_to(repo)))
            git_command(repo, "commit", "-m", "Add ExecPlan G-001: Worker Boundary")
            plan_sha = git_command(repo, "rev-parse", "HEAD")
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            database = sqlite3.connect(state_dir / "loop-manager.db")
            database.execute(loop_manager.SCHEMA.replace("    plan_sha TEXT,\n", ""))
            database.execute(
                "INSERT INTO projects (repo, base_branch, active_sha, phase, plan_path) "
                "VALUES (?, 'main', 'rebased', 'WORKING', ?)",
                (str(repo), plan.relative_to(repo).as_posix()),
            )
            database.commit()
            database.close()

            with loop_manager.connect_database(state_dir) as connection:
                loop_manager.backfill_plan_shas(connection, target_repo=str(repo))
                row = connection.execute(
                    "SELECT plan_sha, phase FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                loop_manager.clear_active(connection, str(repo))
                cleared = connection.execute(
                    "SELECT plan_sha FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()

        self.assertEqual(row["plan_sha"], plan_sha)
        self.assertEqual(row["phase"], "WORKING")
        self.assertIsNone(cleared["plan_sha"])

    def test_unrecoverable_plan_sha_migration_stays_blocked_without_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            (repo / "docs/plans").mkdir()
            plan = write_execplan(repo, "G-001", "Worker Boundary")
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            database = sqlite3.connect(state_dir / "loop-manager.db")
            database.execute(loop_manager.SCHEMA.replace("    plan_sha TEXT,\n", ""))
            database.execute(
                "INSERT INTO projects (repo, base_branch, active_sha, phase, plan_path) "
                "VALUES (?, 'main', 'rebased', 'WORKING', ?)",
                (str(repo), plan.relative_to(repo).as_posix()),
            )
            database.commit()
            database.close()

            with loop_manager.connect_database(state_dir) as connection:
                loop_manager.backfill_plan_shas(connection, target_repo=str(repo))
                blocked = connection.execute(
                    "SELECT phase, last_error FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                    "loop_manager.run_codex"
                ) as run:
                    self.assertEqual(
                        loop_manager.run_project(
                            connection, state_dir, str(repo), Path("unused.ini"), "main", False
                        ),
                        1,
                    )
                retained = connection.execute(
                    "SELECT phase, last_error FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()

        self.assertEqual(blocked["phase"], "PROJECT_BLOCKED")
        self.assertEqual(blocked["last_error"], loop_manager.PLAN_SHA_BACKFILL_ERROR)
        self.assertEqual(retained["phase"], "PROJECT_BLOCKED")
        self.assertEqual(retained["last_error"], loop_manager.PLAN_SHA_BACKFILL_ERROR)
        run.assert_not_called()

    def test_dirty_base_worktree_sync_preserves_file_index_and_head(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            (repo / "src/app.py").write_text("VALUE = 99\n", encoding="utf-8")
            git_command(repo, "add", "src/app.py")
            head_before = git_command(repo, "rev-parse", "HEAD")
            index_before = git_command(repo, "diff", "--cached", "--", "src/app.py")

            result = loop_manager.synchronize_base_worktree(repo, "main")

            self.assertEqual(result.status, "equal")
            self.assertEqual(
                (repo / "src/app.py").read_text(encoding="utf-8"),
                "VALUE = 99\n",
            )
            self.assertEqual(
                git_command(repo, "diff", "--cached", "--", "src/app.py"),
                index_before,
            )
            self.assertEqual(git_command(repo, "rev-parse", "HEAD"), head_before)

    def test_equal_base_worktree_sync_records_noop(self):
        result = loop_manager.BaseWorktreeSyncResult(
            "equal", "이미 원격 base와 같음", "example-sha"
        )
        with mock.patch("loop_manager.operation_log") as operation_log:
            loop_manager.log_base_worktree_sync(Path("/source"), result)
        operation_log.assert_called_once_with(
            "기본 worktree 동기화 equal (/source): 이미 원격 base와 같음",
            error=False,
        )

    def test_verify_and_review_retries_keep_latest_worker_metadata_out_of_reviewer_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            (repo / "docs/plans").mkdir()
            plan = write_execplan(repo, "G-001", "Worker Boundary")
            git_command(repo, "add", str(plan.relative_to(repo)))
            git_command(repo, "commit", "-m", "Add ExecPlan G-001: Worker Boundary")
            git_command(repo, "push", "origin", "main")
            plan_sha = git_command(repo, "rev-parse", "origin/main")
            branch, worktree = loop_manager.expected_execplan_workspace(
                state_dir, str(repo), "G-001"
            )
            loop_manager.make_worktree(repo, worktree, branch, "main")
            base_file = (repo / "src/app.py").read_text(encoding="utf-8")
            base_index = git_command(repo, "diff", "--cached")
            base_head = git_command(repo, "rev-parse", "HEAD")
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                """INSERT INTO projects (
                    repo, base_branch, active_sha, plan_sha, phase, branch, worktree, plan_path
                ) VALUES (?, 'main', ?, ?, 'WORKING', 'loop-manager/G-001', ?, ?)""",
                (
                    str(repo),
                    plan_sha,
                    plan_sha,
                    str(worktree),
                    plan.relative_to(repo).as_posix(),
                ),
            )
            connection.commit()
            worker_messages = iter(
                (
                    worker_result("Record initial context", "Initial summary", "test: PASS"),
                    worker_result("Refresh verified context", "Verified summary", "test: PASS"),
                    worker_result("Publish reviewed context", "Final summary", "test: PASS"),
                )
            )
            versions = iter((2, 3, 4))
            review_messages = iter(("ISSUE_ID: src/app.py::value::incorrect\nVERDICT: FAIL", "VERDICT: PASS"))
            reviewer_prompts = []
            logs = []

            def agent(worktree, _state, _config, role_name, prompt, **_kwargs):
                if role_name == "worker":
                    version = next(versions)
                    (worktree / "src/app.py").write_text(
                        f"VALUE = {version}\n", encoding="utf-8"
                    )
                    return loop_manager.CodexResult(0, next(worker_messages), "worker-session")
                reviewer_prompts.append(prompt)
                return 0, next(review_messages)

            gh_commands = []
            original_command = loop_manager.command

            def command_stub(arguments, **kwargs):
                if arguments[-3:] == ["remote", "get-url", "origin"]:
                    return subprocess.CompletedProcess(arguments, 0, stdout="git@github.com:octo/loop-manager.git\n")
                if arguments[:3] == ["gh", "pr", "create"]:
                    gh_commands.append(arguments)
                    return subprocess.CompletedProcess(arguments, 0, stdout="https://example.test/pr/1\n")
                return original_command(arguments, **kwargs)

            with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch("loop_manager.ensure_safe_base_worktree"), mock.patch(
                "loop_manager.pr_for_branch", return_value=None,
            ), mock.patch("loop_manager.run_codex", side_effect=agent), mock.patch(
                "loop_manager.verify",
                side_effect=[(False, "verify failed"), (True, "verify: PASS"), (True, "verify: PASS")],
            ), mock.patch("loop_manager.command", side_effect=command_stub), mock.patch("loop_manager.operation_log", side_effect=lambda message, **kwargs: logs.append((message, kwargs))):
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    0,
                )
                row = connection.execute(
                    "SELECT worker_result, status FROM exec_plans "
                    "WHERE repo = ? AND plan_id = 'G-001'", (str(repo),)
                ).fetchone()

                latest_title = git_command(worktree, "log", "-1", "--pretty=%s")
                latest_body = loop_manager.build_pr_body(
                    loop_manager.parse_worker_result(row["worker_result"], self.plan),
                    {**self.plan, "sources": ["docs/requirements/Active.md"]},
                    "docs/plans/[G-001] Worker Boundary.md",
                    "## Purpose\n\nPreserve immutable context.\n",
                    "git@github.com:octo/loop-manager.git",
                    "a" * 40,
                )

                base_file_after = (repo / "src/app.py").read_text(encoding="utf-8")
                base_index_after = git_command(repo, "diff", "--cached")
                base_head_after = git_command(repo, "rev-parse", "HEAD")
        self.assertEqual(latest_title, "Publish reviewed context")
        self.assertEqual(
            row["worker_result"],
            worker_result("Publish reviewed context", "Final summary", "test: PASS"),
        )
        self.assertNotIn("Publish reviewed context", reviewer_prompts[-1])
        self.assertNotIn("Final summary", reviewer_prompts[-1])
        self.assertNotIn("Initial summary", reviewer_prompts[-1])
        self.assertNotIn("test: PASS", reviewer_prompts[-1])
        self.assertNotIn("verify: PASS", reviewer_prompts[-1])
        self.assertIn("Final summary", latest_body)
        self.assertEqual(row["status"], "PR_OPEN")
        self.assertNotEqual(worktree, repo)
        self.assertEqual(base_file_after, base_file)
        self.assertEqual(base_index_after, base_index)
        self.assertEqual(base_head_after, base_head)
        self.assertEqual(len(gh_commands), 1)
        pr_title = gh_commands[0][gh_commands[0].index("--title") + 1]
        self.assertEqual(pr_title, "Implement G-001: Worker Boundary")
        self.assertNotEqual(pr_title, "Publish reviewed context")
        self.assertIn(("Reviewer 시작: G-001 Worker Boundary", {}), logs)
        self.assertIn(("Worker 반환: G-001 Worker Boundary (Review 실패)", {"error": True}), logs)


    def test_invalid_worker_result_resumes_same_session_without_verify(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            (repo / "docs/plans").mkdir()
            plan = write_execplan(repo, "G-001", "Worker Boundary")
            commit_and_push(repo, "Add worker plan")
            branch, worktree = loop_manager.expected_execplan_workspace(
                state_dir, str(repo), "G-001"
            )
            loop_manager.make_worktree(repo, worktree, branch, "main")
            plan_sha = git_command(repo, "rev-parse", "origin/main")
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                """
                INSERT INTO projects (
                    repo, base_branch, active_sha, plan_sha, phase, branch, worktree, plan_path
                ) VALUES (?, 'main', ?, ?, 'WORKING', 'loop-manager/G-001', ?, ?)
                """,
                (
                    str(repo),
                    plan_sha,
                    plan_sha,
                    str(worktree),
                    plan.relative_to(repo).as_posix(),
                ),
            )
            connection.commit()
            sessions = []
            prompts = []

            def worker(agent_worktree, _state, _config, role_name, prompt, **kwargs):
                if role_name == "reviewer":
                    return loop_manager.CodexResult(
                        0, "VERDICT: PASS", "reviewer-session"
                    )
                sessions.append(kwargs["session_id"])
                prompts.append(prompt)
                if len(sessions) == 1:
                    result = worker_result(
                        "Worker Boundary", "Bad title", "test: PASS"
                    )
                else:
                    (agent_worktree / "src/app.py").write_text(
                        "VALUE = 2\n", encoding="utf-8"
                    )
                    result = worker_result(
                        "Validate worker metadata", "Good title", "test: PASS"
                    )
                return loop_manager.CodexResult(0, result, "worker-session")

            with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch("loop_manager.ensure_safe_base_worktree"), mock.patch(
                "loop_manager.pr_for_branch",
                side_effect=matching_branch_pr_lookup(
                    [None, None, {"url": "https://example.test/pr/1"}]
                ),
            ), mock.patch("loop_manager.run_codex", side_effect=worker), mock.patch(
                "loop_manager.verify",
                side_effect=lambda *_args: (
                    self.assertEqual(len(sessions), 2) or True,
                    "verify: PASS",
                ),
            ) as verify:
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )
                row = connection.execute(
                    "SELECT phase, last_error FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                self.assertEqual(row["phase"], "WORKING")
                self.assertIn("Worker 결과 검증 실패", row["last_error"])
                verify.assert_not_called()

                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    0,
                )

            self.assertEqual(sessions, [None, "worker-session"])
            self.assertIn("Worker 결과 검증 실패", prompts[1])
            verify.assert_called_once()

    def test_plan_sha_migration_and_clear_active(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            database = sqlite3.connect(state_dir / "loop-manager.db")
            database.execute(loop_manager.SCHEMA.replace("    plan_sha TEXT,\n", ""))
            database.execute("INSERT INTO projects (repo, base_branch) VALUES ('repo', 'main')")
            database.commit()
            database.close()

            with loop_manager.connect_database(state_dir) as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
                loop_manager.update_project(connection, "repo", active_sha="current", plan_sha="planned")
                loop_manager.update_project(connection, "repo", active_sha="rebased")
                preserved = connection.execute(
                    "SELECT active_sha, plan_sha FROM projects WHERE repo = 'repo'"
                ).fetchone()
                self.assertEqual(tuple(preserved), ("rebased", "planned"))
                loop_manager.clear_active(connection, "repo")
                row = connection.execute(
                    "SELECT active_sha, plan_sha FROM projects WHERE repo = 'repo'"
                ).fetchone()

        self.assertIn("plan_sha", columns)
        self.assertEqual(tuple(row), (None, None))


    def test_immutable_url_and_pr_body_encode_plan_context(self):
        plan = {
            "id": "G-008",
            "title": "Descriptive Worker Commits",
            "sources": ["docs/requirements/[R-004] 한글 요구사항.md"],
        }
        plan_path = "docs/plans/[G-008] Descriptive Worker Commits.md"
        plan_text = "---\nid: G-008\n---\n\n## Purpose\n\nKeep review context stable.\n"
        published = loop_manager.published_execplan(
            plan_path,
            """---
id: G-008
title: Descriptive Worker Commits
sources:
  - docs/requirements/[R-004] 한글 요구사항.md
depends_on: []
complexity: medium
risk: medium
---
""",
        )
        self.assertEqual(published["sources"], plan["sources"])

        sha = "a" * 40
        body = loop_manager.build_pr_body(
            loop_manager.WorkerPublication("Describe changes", "Adds PR context.", "test: PASS"),
            plan,
            plan_path,
            plan_text,
            "git@github.com:octo/loop-manager.git",
            sha,
        )

        self.assertIn("Adds PR context.", body)
        self.assertIn("Keep review context stable.", body)
        self.assertIn(f"/blob/{sha}/docs/plans/%5BG-008%5D%20Descriptive%20Worker%20Commits.md", body)
        self.assertIn("%5BR-004%5D%20%ED%95%9C%EA%B8%80%20%EC%9A%94%EA%B5%AC%EC%82%AC%ED%95%AD.md", body)

class AgentScopeValidatorTest(unittest.TestCase):
    def test_self_hosted_runtime_preserves_interrupted_worker_and_reviewer_identity(self):
        for role in ("worker", "reviewer"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as directory:
                repo, plan_path = self._repository_with_plan(directory)
                before = loop_manager.capture_git_snapshot(repo)
                state_dir = Path(directory) / "state"
                self._persist_interrupted_worker(state_dir, repo, plan_path, before)
                connection = sqlite3.connect(state_dir / "loop-manager.db")
                connection.row_factory = sqlite3.Row
                connection.execute(
                    "UPDATE projects SET agent_role = ?, worker_session_id = 'worker-existing', "
                    "reviewer_session_id = 'reviewer-existing' WHERE repo = ?",
                    (role, str(repo)),
                )
                connection.commit()

                writer = Path(directory) / "writer"
                subprocess.run(
                    ["git", "clone", str(Path(directory) / "remote.git"), str(writer)],
                    check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                )
                git_command(writer, "config", "user.email", "loop-manager@example.com")
                git_command(writer, "config", "user.name", "Loop Manager Test")
                (writer / "README.md").write_text("# Changed\n", encoding="utf-8")
                commit_and_push(writer, "Advance runtime")
                selected_sha = git_command(writer, "rev-parse", "HEAD")

                with mock.patch("loop_manager.github_repository", return_value="owner/repo"):
                    with loop_manager.process_lock(state_dir) as handle:
                        mutation_id = "d" * 32
                        runtime = loop_manager.prepare_runtime_worktree(
                            repo, state_dir, mutation_id=mutation_id,
                        )
                        loop_manager._write_process_lock_identity(handle, f"runtime:{mutation_id}")
                        with mock.patch.object(loop_manager, "ROOT", runtime.path):
                            identity = loop_manager.validate_runtime_mutation(
                                repo, state_dir, selected_sha, mutation_id,
                            )
                        loop_manager.validate_persisted_agent_scope(
                            connection, repo, state_dir, str(repo),
                            runtime_mutation=identity,
                        )
                project = connection.execute(
                    "SELECT worker_session_id, reviewer_session_id, agent_snapshot "
                    "FROM projects WHERE repo = ?", (str(repo),),
                ).fetchone()
                plan = connection.execute(
                    "SELECT status, retry_count FROM exec_plans WHERE repo = ? "
                    "AND plan_id = 'G-001'", (str(repo),),
                ).fetchone()
                self.assertEqual(project["worker_session_id"], "worker-existing")
                self.assertEqual(project["reviewer_session_id"], "reviewer-existing")
                self.assertEqual(plan["status"], "IMPLEMENTING")
                self.assertEqual(plan["retry_count"], 0)
                self.assertIn(str(runtime.path), loop_manager.GitSnapshot.deserialize(
                    project["agent_snapshot"]
                ).worktrees)
                connection.close()

    def _repository_with_plan(self, directory: str) -> tuple[Path, str]:
        repo = make_remote_repository(directory)
        (repo / "docs/plans").mkdir()
        plan = write_execplan(repo, "G-001", "Scope Safety")
        commit_and_push(repo, "Add scope safety plan")
        return repo, plan.relative_to(repo).as_posix()

    def _persist_interrupted_worker(
        self,
        state_dir: Path,
        repo: Path,
        plan_path: str,
        snapshot: loop_manager.GitSnapshot,
    ) -> None:
        state_dir.mkdir()
        database = sqlite3.connect(state_dir / "loop-manager.db")
        database.row_factory = sqlite3.Row
        database.execute(loop_manager.SCHEMA)
        database.execute(
            "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
            (str(repo),),
        )
        plan_sha = git_command(repo, "rev-parse", "origin/main")
        loop_manager.register_execplans(
            database,
            str(repo),
            repo,
            [(plan_path, loop_manager._parse_execplan(repo, repo / plan_path))],
            plan_sha,
            loop_manager.planning_input_fingerprint(repo, "main"),
        )
        branch = "loop-manager/G-001"
        with database:
            database.execute(
                """
                UPDATE exec_plans
                SET status = 'IMPLEMENTING', active_sha = ?, branch = ?,
                    worktree = ?, workspace_owned = 1
                WHERE repo = ? AND plan_id = 'G-001'
                """,
                (plan_sha, branch, str(repo), str(repo)),
            )
            database.execute(
                """
                UPDATE projects
                SET active_sha = ?, plan_sha = ?, phase = 'WORKING',
                    branch = ?, worktree = ?, plan_path = ?,
                    agent_role = 'worker', agent_snapshot = ?
                WHERE repo = ?
                """,
                (
                    plan_sha,
                    plan_sha,
                    branch,
                    str(repo),
                    plan_path,
                    snapshot.serialize(),
                    str(repo),
                ),
            )
        database.close()

    def test_interrupted_planner_scope_violation_resumes_correction_from_result_snapshot(self):
        planner_sessions = []

        def agent(worktree, _state, _config, role_name, _prompt, **kwargs):
            if role_name != "planner":
                return loop_manager.CodexResult(
                    1,
                    "stop after corrected planning",
                    "worker-session",
                )
            planner_sessions.append(kwargs["session_id"])
            unexpected = worktree / "unexpected.txt"
            if len(planner_sessions) == 1:
                kwargs["on_session_started"]("planner-session")
                unexpected.write_text("invalid\n", encoding="utf-8")
                raise RuntimeError("process interrupted after invalid Planner edit")
            self.assertEqual(kwargs["session_id"], "planner-session")
            unexpected.unlink()
            (worktree / "docs/plans").mkdir(parents=True, exist_ok=True)
            write_execplan(worktree, "G-001", "Recovered")
            return loop_manager.CodexResult(0, "", "planner-session")

        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)

            with mock.patch(
                "loop_manager.validate_repository",
                return_value=repo,
            ), mock.patch(
                "loop_manager.remote_base",
                return_value="main",
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.run_codex",
                side_effect=agent,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "process interrupted",
                ):
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )

                self.assertEqual(
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    ),
                    1,
                )
                blocked = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?",
                    (str(repo),),
                ).fetchone()
                self.assertIn(
                    "unexpected.txt",
                    blocked["planning_validation_error"],
                )
                self.assertEqual(planner_sessions, [None])
                start_snapshot = loop_manager.GitSnapshot.deserialize(
                    blocked["agent_snapshot"]
                )
                result_snapshot = loop_manager.GitSnapshot.deserialize(
                    blocked["agent_result_snapshot"]
                )
                self.assertNotEqual(start_snapshot, result_snapshot)
                self.assertIn("unexpected.txt", result_snapshot.dirty)

                self.assertEqual(
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    ),
                    1,
                )

            resumed = connection.execute(
                "SELECT phase, planning_retry_count, planning_error "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(planner_sessions, [None, "planner-session"])
            self.assertEqual(resumed["phase"], "WORKING")
            self.assertEqual(resumed["planning_retry_count"], 0)
            self.assertIsNone(resumed["planning_error"])

    def test_role_boundaries_reject_protected_and_read_only_changes(self):
        scenarios = (
            (
                "planner-product",
                "planner",
                lambda repo, _plan: (repo / "docs/PRODUCT.md").write_text(
                    "# changed\n", encoding="utf-8"
                ),
                None,
                "docs/PRODUCT.md",
            ),
            (
                "worker-requirement",
                "worker",
                lambda repo, _plan: (
                    repo / "docs/requirements/Active.md"
                ).write_text("# changed\n", encoding="utf-8"),
                "docs/plans/[G-001] Scope Safety.md",
                "docs/requirements/Active.md",
            ),
            (
                "reviewer-staged-and-untracked",
                "reviewer",
                lambda repo, _plan: (
                    (repo / "src/app.py").write_text(
                        "VALUE = 2\n", encoding="utf-8"
                    ),
                    git_command(repo, "add", "src/app.py"),
                    (repo / "review.txt").write_text(
                        "unexpected\n", encoding="utf-8"
                    ),
                ),
                None,
                "review.txt",
            ),
        )
        for name, role_name, mutate, selected_plan, expected in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                repo, plan_path = self._repository_with_plan(directory)
                before = loop_manager.capture_git_snapshot(repo)
                mutate(repo, plan_path)
                after = loop_manager.capture_git_snapshot(repo)

                with self.assertRaisesRegex(
                    loop_manager.AgentScopeError,
                    expected.replace("[", r"\[").replace("]", r"\]"),
                ):
                    loop_manager.validate_agent_scope(
                        role_name,
                        repo,
                        before,
                        after,
                        selected_plan_path=selected_plan,
                    )

    def test_ignored_untracked_creation_change_and_deletion_are_allowed(self):
        scenarios = (
            ("planner", None, "created by planner\n"),
            ("worker", "before work\n", "changed by worker\n"),
            ("reviewer", "before review\n", "changed by reviewer\n"),
            ("reviewer", "before review\n", None),
        )
        for role_name, before_contents, after_contents in scenarios:
            with self.subTest(role=role_name), tempfile.TemporaryDirectory() as directory:
                repo, _plan_path = self._repository_with_plan(directory)
                (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
                commit_and_push(repo, "Ignore diagnostic fixture")
                ignored = repo / "ignored.txt"
                if before_contents is not None:
                    ignored.write_text(before_contents, encoding="utf-8")
                before = loop_manager.capture_git_snapshot(repo)

                if after_contents is None:
                    ignored.unlink()
                else:
                    ignored.write_text(after_contents, encoding="utf-8")
                after = loop_manager.capture_git_snapshot(repo)

                self.assertNotIn("ignored.txt", before.dirty)
                self.assertNotIn("ignored.txt", after.dirty)
                loop_manager.validate_agent_scope(
                    role_name, repo, before, after,
                    selected_plan_path="docs/plans/[G-001] Scope Safety.md"
                    if role_name == "worker" else None,
                )

    def test_legacy_ignored_snapshot_requires_unchanged_tracked_rule(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _plan_path = self._repository_with_plan(directory)
            (repo / ".gitignore").write_text("cache.bin\n", encoding="utf-8")
            commit_and_push(repo, "Ignore cache fixture")
            (repo / "cache.bin").write_bytes(b"old")
            current = loop_manager.capture_git_snapshot(repo)
            legacy = replace(
                current,
                dirty={"cache.bin": (("untracked",), "old-signature")},
                config={
                    key: value for key, value in current.config.items()
                    if not key.startswith("ignore-source:")
                },
            )
            normalized = loop_manager._normalize_legacy_ignored_snapshot(
                repo, legacy, current
            )
            self.assertEqual(normalized.dirty, {})

            (repo / ".gitignore").write_text("cache.bin\nnew.bin\n", encoding="utf-8")
            changed = loop_manager.capture_git_snapshot(repo)
            self.assertEqual(
                loop_manager._normalize_legacy_ignored_snapshot(repo, legacy, changed),
                legacy,
            )

    def test_persisted_worker_snapshot_resumes_with_legacy_ignored_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, plan_path = self._repository_with_plan(directory)
            (repo / ".gitignore").write_text("cache.bin\n", encoding="utf-8")
            commit_and_push(repo, "Ignore cache fixture")
            (repo / "cache.bin").write_bytes(b"old")
            current = loop_manager.capture_git_snapshot(repo)
            legacy = replace(
                current,
                dirty={"cache.bin": (("untracked",), "old-signature")},
                config={
                    key: value for key, value in current.config.items()
                    if not key.startswith("ignore-source:")
                },
            )
            state_dir = Path(directory) / "state"
            self._persist_interrupted_worker(state_dir, repo, plan_path, legacy)
            connection = sqlite3.connect(state_dir / "loop-manager.db")
            connection.row_factory = sqlite3.Row
            try:
                self.assertEqual(
                    loop_manager.validate_persisted_agent_scope(
                        connection, repo, state_dir, str(repo)
                    ),
                    current,
                )
            finally:
                connection.close()

    def test_legacy_snapshot_does_not_trust_unrecorded_exclude_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, plan_path = self._repository_with_plan(directory)
            before = loop_manager.capture_git_snapshot(repo)
            legacy = replace(
                before,
                config={
                    key: value for key, value in before.config.items()
                    if not key.startswith("ignore-source:")
                },
            )
            state_dir = Path(directory) / "state"
            self._persist_interrupted_worker(state_dir, repo, plan_path, legacy)
            exclude = Path(git_command(repo, "rev-parse", "--git-path", "info/exclude"))
            if not exclude.is_absolute():
                exclude = repo / exclude
            exclude.write_text(
                exclude.read_text(encoding="utf-8") + "\nnew-cache.bin\n",
                encoding="utf-8",
            )
            connection = sqlite3.connect(state_dir / "loop-manager.db")
            connection.row_factory = sqlite3.Row
            try:
                with self.assertRaisesRegex(loop_manager.AgentScopeError, "ignore-source"):
                    loop_manager.validate_persisted_agent_scope(
                        connection, repo, state_dir, str(repo)
                    )
            finally:
                connection.close()

    def test_forced_staged_ignored_file_stays_in_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _plan_path = self._repository_with_plan(directory)
            (repo / ".gitignore").write_text("cache.bin\n", encoding="utf-8")
            commit_and_push(repo, "Ignore cache fixture")
            before = loop_manager.capture_git_snapshot(repo)
            (repo / "cache.bin").write_bytes(b"staged")
            git_command(repo, "add", "-f", "cache.bin")
            after = loop_manager.capture_git_snapshot(repo)
            self.assertIn("cache.bin", after.index)
            with self.assertRaisesRegex(loop_manager.AgentScopeError, "cache.bin"):
                loop_manager.validate_agent_scope("reviewer", repo, before, after)

    def test_tracked_file_matching_ignore_rule_stays_in_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _plan_path = self._repository_with_plan(directory)
            (repo / ".gitignore").write_text("src/app.py\n", encoding="utf-8")
            commit_and_push(repo, "Ignore tracked path fixture")
            before = loop_manager.capture_git_snapshot(repo)
            (repo / "src/app.py").write_text("VALUE = 3\n", encoding="utf-8")
            after = loop_manager.capture_git_snapshot(repo)
            self.assertIn("src/app.py", after.dirty)
            with self.assertRaisesRegex(loop_manager.AgentScopeError, "src/app.py"):
                loop_manager.validate_agent_scope("reviewer", repo, before, after)

    def test_new_ignore_rule_cannot_hide_untracked_file(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _plan_path = self._repository_with_plan(directory)
            (repo / ".gitignore").write_text("*.cache\n", encoding="utf-8")
            commit_and_push(repo, "Ignore cache fixture")
            before = loop_manager.capture_git_snapshot(repo)
            (repo / ".gitignore").write_text("*.cache\nhidden.txt\n", encoding="utf-8")
            (repo / "hidden.txt").write_text("new", encoding="utf-8")
            after = loop_manager.capture_git_snapshot(repo)
            self.assertIn("hidden.txt", after.dirty)
            with self.assertRaisesRegex(loop_manager.AgentScopeError, "hidden.txt"):
                loop_manager.validate_agent_scope("reviewer", repo, before, after)

    def test_colon_in_ignore_rule_path_cannot_hide_untracked_file(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _plan_path = self._repository_with_plan(directory)
            rule_directory = repo / "rules:old"
            rule_directory.mkdir()
            rule = rule_directory / ".gitignore"
            rule.write_text("*.cache\n", encoding="utf-8")
            git_command(repo, "add", "rules:old/.gitignore")
            commit_and_push(repo, "Add ignore rule with colon in path")
            before = loop_manager.capture_git_snapshot(repo)
            rule.write_text("*.cache\nhidden.txt\n", encoding="utf-8")
            (rule_directory / "hidden.txt").write_text("new", encoding="utf-8")
            after = loop_manager.capture_git_snapshot(repo)
            self.assertIn("rules:old/hidden.txt", after.dirty)
            with self.assertRaisesRegex(loop_manager.AgentScopeError, "hidden.txt"):
                loop_manager.validate_agent_scope("reviewer", repo, before, after)

    def test_repository_exclude_change_cannot_hide_new_file(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _plan_path = self._repository_with_plan(directory)
            before = loop_manager.capture_git_snapshot(repo)
            exclude = Path(git_command(repo, "rev-parse", "--git-path", "info/exclude"))
            if not exclude.is_absolute():
                exclude = repo / exclude
            exclude.write_text(
                exclude.read_text(encoding="utf-8") + "\nhidden.txt\n",
                encoding="utf-8",
            )
            (repo / "hidden.txt").write_text("new", encoding="utf-8")
            after = loop_manager.capture_git_snapshot(repo)
            self.assertNotIn("hidden.txt", after.dirty)
            with self.assertRaisesRegex(loop_manager.AgentScopeError, "Git config"):
                loop_manager.validate_agent_scope("reviewer", repo, before, after)

    def test_planner_import_bytecode_is_ignored_without_hiding_scope_violations(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _plan_path = self._repository_with_plan(directory)
            for module_name in (
                "agent_scope_decision.py",
                "agent_invocation.py",
                "agent_invocation_state.py",
                "agent_provider_usage.py",
                "blocked_checkpoint.py",
                "blocked_checkpoint_state.py",
                "document_drafts.py",
                "document_publication.py",
                "checkpoint_ownership.py",
                "loop_manager.py",
                "manager_git_journal.py",
                "managed_rebase_evidence.py",
                "rebase_file_evidence.py",
                "execution_checkpoint.py",
                "execution_checkpoint_state.py",
                "execution_runtime.py",
                "execution_handlers.py",
                "execution_revision.py",
                "execution_state.py",
                "guidance_approval.py",
                "execplan_archive.py",
                "execplan_archive_state.py",
                "execplan_catalog.py",
                "execplan_catalog_state.py",
                "managed_config.py",
                "maintenance_file_inventory.py",
                "maintenance_file_ledger.py",
                "maintenance_file_publication.py",
                "maintenance_file_review.py",
                "maintenance_reconciliation.py",
                "maintenance_findings.py",
                "maintenance_git.py",
                "maintenance_orchestration.py",
                "maintenance_planning.py",
                "full_assessment.py",
                "maintenance_state.py",
                "notifications.py",
                "work_exhaustion.py",
                "planning_catalog.py",
                "planning_runtime.py",
                "publication_resume.py",
                "project_registry.py",
                "rebase_correction.py",
                "rebase_correction_state.py",
                "recovery_dispatcher.py",
                "recovery_runtime.py",
                "recovery_state.py",
                "reviewer_specification.py",
                "requirement_archive.py",
                "requirement_archive_recovery.py",
                "requirement_archive_state.py",
                "runtime_state.py",
                "scoped_blocking.py",
                "validation_feedback.py",
                "validation_feedback_state.py",
                "verify_process.py",
                "worker_session.py",
                "worker_outcome.py",
            ):
                shutil.copy(loop_manager.ROOT / module_name, repo / module_name)
            (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
            commit_and_push(repo, "Add planner import fixtures")

            planner_environment = loop_manager.agent_environment("codex", "planner")
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import loop_manager, managed_config, maintenance_git, "
                    "maintenance_planning, maintenance_state, project_registry",
                ],
                cwd=repo,
                env=planner_environment,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertTrue(any(repo.rglob("*.pyc")))

            existing_cache = repo / "__pycache__" / "preserved.pyc"
            existing_cache.parent.mkdir(exist_ok=True)
            existing_cache.write_bytes(b"existing cache")
            before = loop_manager.capture_git_snapshot(repo)
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import loop_manager, managed_config, maintenance_git, "
                    "maintenance_planning, maintenance_state, project_registry",
                ],
                cwd=repo,
                env=planner_environment,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(existing_cache.read_bytes(), b"existing cache")
            write_execplan(repo, "G-002", "Imported Without Bytecode")
            after = loop_manager.capture_git_snapshot(repo)
            loop_manager.validate_agent_scope("planner", repo, before, after)

            explicit_cache = repo / "__pycache__" / "explicit.pyc"
            explicit_cache.write_bytes(b"explicit cache")
            explicit_after = loop_manager.capture_git_snapshot(repo)
            loop_manager.validate_agent_scope("planner", repo, after, explicit_after)
            (repo / "unexpected.txt").write_text("publishable\n", encoding="utf-8")
            with self.assertRaisesRegex(loop_manager.AgentScopeError, "unexpected.txt"):
                loop_manager.validate_agent_scope(
                    "planner", repo, after, loop_manager.capture_git_snapshot(repo)
                )

    def test_planner_new_plan_and_worker_selected_plan_are_allowed(self):
        for role_name in ("planner", "worker"):
            with self.subTest(role=role_name), tempfile.TemporaryDirectory() as directory:
                repo, plan_path = self._repository_with_plan(directory)
                before = loop_manager.capture_git_snapshot(repo)
                selected = plan_path
                if role_name == "planner":
                    write_execplan(repo, "G-002", "New Plan")
                    selected = None
                else:
                    (repo / plan_path).write_text(
                        (repo / plan_path).read_text(encoding="utf-8")
                        + "\n## Progress\n\n- [x] complete\n",
                        encoding="utf-8",
                    )
                after = loop_manager.capture_git_snapshot(repo)

                loop_manager.validate_agent_scope(
                    role_name,
                    repo,
                    before,
                    after,
                    selected_plan_path=selected,
                )

    def test_planner_existing_plan_change_reaches_snapshot_validator(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, plan_path = self._repository_with_plan(directory)
            before = loop_manager.capture_git_snapshot(repo)
            (repo / plan_path).write_text("changed\n", encoding="utf-8")
            after = loop_manager.capture_git_snapshot(repo)

            loop_manager.validate_agent_scope("planner", repo, before, after)

    def test_agent_commit_and_branch_switch_are_rejected(self):
        mutations = (
            (
                "commit",
                lambda repo: (
                    (repo / "src/app.py").write_text(
                        "VALUE = 2\n", encoding="utf-8"
                    ),
                    git_command(repo, "add", "src/app.py"),
                    git_command(repo, "commit", "-m", "Agent commit"),
                ),
                "HEAD 변경",
            ),
            (
                "branch",
                lambda repo: git_command(repo, "switch", "-c", "agent-branch"),
                "branch 전환",
            ),
        )
        for name, mutate, expected in mutations:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                repo, plan_path = self._repository_with_plan(directory)
                before = loop_manager.capture_git_snapshot(repo)
                mutate(repo)
                after = loop_manager.capture_git_snapshot(repo)

                with self.assertRaisesRegex(
                    loop_manager.AgentScopeError, expected
                ):
                    loop_manager.validate_agent_scope(
                        "worker",
                        repo,
                        before,
                        after,
                        selected_plan_path=plan_path,
                    )

    def test_unfinished_merge_is_rejected_even_for_worker_allowed_files(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, plan_path = self._repository_with_plan(directory)
            git_command(repo, "switch", "-c", "merge-source")
            (repo / "src/app.py").write_text(
                "VALUE = 2\n",
                encoding="utf-8",
            )
            git_command(repo, "add", "src/app.py")
            git_command(repo, "commit", "-m", "Prepare merge source")
            git_command(repo, "switch", "main")
            before = loop_manager.capture_git_snapshot(repo)

            git_command(
                repo,
                "merge",
                "--no-ff",
                "--no-commit",
                "merge-source",
            )
            after = loop_manager.capture_git_snapshot(repo)

            self.assertNotIn("MERGE_HEAD", before.operations)
            self.assertIn("MERGE_HEAD", after.operations)
            with self.assertRaisesRegex(
                loop_manager.AgentScopeError,
                r"Git operation 상태 변경 .*MERGE_HEAD",
            ):
                loop_manager.validate_agent_scope(
                    "worker",
                    repo,
                    before,
                    after,
                    selected_plan_path=plan_path,
                )

    def test_auto_merge_tree_ref_alone_is_not_an_active_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, plan_path = self._repository_with_plan(directory)
            before = loop_manager.capture_git_snapshot(repo)
            auto_merge = Path(
                git_command(repo, "rev-parse", "--git-path", "AUTO_MERGE")
            )
            if not auto_merge.is_absolute():
                auto_merge = repo / auto_merge
            auto_merge.write_text(
                git_command(repo, "write-tree") + "\n",
                encoding="ascii",
            )

            after = loop_manager.capture_git_snapshot(repo)

            self.assertTrue(auto_merge.is_file())
            self.assertNotIn("AUTO_MERGE", after.operations)
            loop_manager.validate_agent_scope(
                "worker",
                repo,
                before,
                after,
                selected_plan_path=plan_path,
            )

    def test_shared_git_metadata_changes_are_rejected(self):
        scenarios = (
            (
                "branch",
                lambda repo, _root: git_command(
                    repo, "branch", "agent-created"
                ),
                "refs",
                r"공유 Git refs 변경 .*refs/heads/agent-created",
            ),
            (
                "tag",
                lambda repo, _root: git_command(repo, "tag", "agent-tag"),
                "refs",
                r"공유 Git refs 변경 .*refs/tags/agent-tag",
            ),
            (
                "worktree",
                lambda repo, root: git_command(
                    repo,
                    "worktree",
                    "add",
                    "--detach",
                    str(root / "agent-worktree"),
                    "HEAD",
                ),
                "worktrees",
                r"Git worktree 등록·HEAD·index·파일 상태 변경 .*agent-worktree",
            ),
        )
        for name, mutate, state_field, expected in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repo, plan_path = self._repository_with_plan(directory)
                before = loop_manager.capture_git_snapshot(repo)
                self.assertEqual(
                    loop_manager.GitSnapshot.deserialize(before.serialize()),
                    before,
                )

                mutate(repo, root)
                after = loop_manager.capture_git_snapshot(repo)

                self.assertNotEqual(
                    getattr(before, state_field),
                    getattr(after, state_field),
                )
                with self.assertRaisesRegex(
                    loop_manager.AgentScopeError,
                    expected,
                ):
                    loop_manager.validate_agent_scope(
                        "worker",
                        repo,
                        before,
                        after,
                        selected_plan_path=plan_path,
                    )

    def test_codex_turn_diff_refs_are_ignored_for_persisted_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, plan_path = self._repository_with_plan(directory)
            head = git_command(repo, "rev-parse", "HEAD")
            old_ref = "refs/codex/turn-diffs/captures/old/session/base"
            new_ref = "refs/codex/turn-diffs/captures/new/session/base"
            git_command(repo, "update-ref", old_ref, head)
            before = loop_manager.capture_git_snapshot(repo)

            git_command(repo, "update-ref", "-d", old_ref)
            git_command(repo, "update-ref", new_ref, head)
            after = loop_manager.capture_git_snapshot(repo)

            self.assertIn(old_ref, before.refs)
            self.assertNotIn(old_ref, after.refs)
            self.assertNotIn(new_ref, before.refs)
            self.assertIn(new_ref, after.refs)
            loop_manager.validate_agent_scope(
                "worker",
                repo,
                before,
                after,
                selected_plan_path=plan_path,
            )

    def test_changes_in_another_registered_worktree_are_rejected(self):
        for mutation in ("file", "index"):
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    repo, plan_path = self._repository_with_plan(directory)
                    managed = root / "managed"
                    loop_manager.make_worktree(
                        repo,
                        managed,
                        "loop-manager/G-099",
                        "main",
                    )
                    before = loop_manager.capture_git_snapshot(managed)

                    if mutation == "file":
                        (repo / "src/app.py").write_text(
                            "VALUE = 2\n",
                            encoding="utf-8",
                        )
                    else:
                        git_command(
                            repo,
                            "update-index",
                            "--assume-unchanged",
                            "--",
                            "src/app.py",
                        )
                    after = loop_manager.capture_git_snapshot(managed)

                    external_path = str(repo.resolve())
                    self.assertNotEqual(
                        before.worktrees[external_path],
                        after.worktrees[external_path],
                    )
                    with self.assertRaises(
                        loop_manager.AgentScopeError
                    ) as raised:
                        loop_manager.validate_agent_scope(
                            "worker",
                            managed,
                            before,
                            after,
                            selected_plan_path=plan_path,
                        )
                    self.assertIn(external_path, str(raised.exception))

    def test_primary_worktree_and_base_refs_can_change_during_managed_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, plan_path = self._repository_with_plan(directory)
            managed = root / "managed"
            loop_manager.make_worktree(
                repo,
                managed,
                "loop-manager/G-099",
                "main",
            )
            before = loop_manager.capture_git_snapshot(managed)

            (repo / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            git_command(repo, "add", "src/app.py")
            git_command(repo, "commit", "-m", "Update primary worktree")
            after = loop_manager.capture_git_snapshot(managed)

            primary_path = str(repo.resolve())
            self.assertNotEqual(
                before.worktrees[primary_path],
                after.worktrees[primary_path],
            )
            self.assertNotEqual(
                before.refs["refs/heads/main"],
                after.refs["refs/heads/main"],
            )
            with self.assertRaises(loop_manager.AgentScopeError):
                loop_manager.validate_agent_scope(
                    "worker",
                    managed,
                    before,
                    after,
                    selected_plan_path=plan_path,
                )
            loop_manager.validate_agent_scope(
                "worker",
                managed,
                before,
                after,
                selected_plan_path=plan_path,
                external_worktree=repo,
                base_branch="main",
            )

    def test_primary_worktree_registration_removal_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, plan_path = self._repository_with_plan(directory)
            managed = root / "managed"
            loop_manager.make_worktree(
                repo,
                managed,
                "loop-manager/G-099",
                "main",
            )
            before = loop_manager.capture_git_snapshot(managed)
            primary_path = str(repo.resolve())
            remaining_worktrees = dict(before.worktrees)
            del remaining_worktrees[primary_path]
            after = loop_manager.GitSnapshot(
                head=before.head,
                branch=before.branch,
                index=before.index,
                dirty=before.dirty,
                refs=before.refs,
                worktrees=remaining_worktrees,
                config=before.config,
                operations=before.operations,
            )

            with self.assertRaises(loop_manager.AgentScopeError) as raised:
                loop_manager.validate_agent_scope(
                    "worker",
                    managed,
                    before,
                    after,
                    selected_plan_path=plan_path,
                    external_worktree=repo,
                    base_branch="main",
                )

            self.assertEqual(
                raised.exception.reason_code,
                loop_manager.BlockReasonCode.DEFAULT_WORKTREE_MUTATION,
            )

    def test_default_worktree_project_block_releases_without_source_revert(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, plan_path = self._repository_with_plan(directory)
            state_dir = root / "state"
            state_dir.mkdir()
            branch, worktree = loop_manager.expected_execplan_workspace(
                state_dir,
                str(repo),
                "G-001",
            )
            loop_manager.make_worktree(repo, worktree, branch, "main")
            plan_sha = git_command(repo, "rev-parse", "origin/main")

            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
                (str(repo),),
            )
            loop_manager.register_execplans(
                connection,
                str(repo),
                repo,
                [(plan_path, loop_manager._parse_execplan(repo, repo / plan_path))],
                plan_sha,
                loop_manager.planning_input_fingerprint(repo, "main"),
            )
            loop_manager.scoped_blocking.ensure_schema(connection)

            before = loop_manager.capture_git_snapshot(worktree)
            (worktree / "src/app.py").write_text(
                "VALUE = 2\n",
                encoding="utf-8",
            )
            source_note = repo / "source-note.txt"
            source_note.write_text("preserve me\n", encoding="utf-8")
            after = loop_manager.capture_git_snapshot(worktree)
            self.assertNotEqual(
                before.worktrees[str(repo.resolve())],
                after.worktrees[str(repo.resolve())],
            )

            session_id = "preserved-worker-session"
            with connection:
                connection.execute(
                    """
                    UPDATE exec_plans
                    SET status = 'IMPLEMENTING', active_sha = ?, branch = ?,
                        worktree = ?, workspace_owned = 1,
                        worker_session_id = ?, agent_role = 'worker',
                        agent_snapshot = ?, agent_result_snapshot = ?
                    WHERE repo = ? AND plan_id = 'G-001'
                    """,
                    (
                        plan_sha,
                        branch,
                        str(worktree),
                        session_id,
                        before.serialize(),
                        after.serialize(),
                        str(repo),
                    ),
                )
                connection.execute(
                    """
                    UPDATE projects
                    SET active_sha = ?, plan_sha = ?, phase = 'WORKING',
                        branch = ?, worktree = ?, plan_path = ?,
                        worker_session_id = ?, agent_role = 'worker',
                        agent_snapshot = ?, agent_result_snapshot = ?
                    WHERE repo = ?
                    """,
                    (
                        plan_sha,
                        plan_sha,
                        branch,
                        str(worktree),
                        plan_path,
                        session_id,
                        before.serialize(),
                        after.serialize(),
                        str(repo),
                    ),
                )
                loop_manager.scoped_blocking.record_project_block(
                    connection,
                    str(repo),
                    loop_manager.BlockReasonCode.DEFAULT_WORKTREE_MUTATION,
                    "worker Git 범위 위반: primary checkout changed",
                    "blocked-fingerprint",
                    plan_id="G-001",
                    previous_phase="WORKING",
                )

            self.assertTrue(
                loop_manager.resume_project_block_if_safe(
                    connection,
                    repo,
                    state_dir,
                    str(repo),
                )
            )

            project = connection.execute(
                "SELECT phase, worker_session_id, last_error FROM projects "
                "WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan = connection.execute(
                "SELECT status, worker_session_id FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            self.assertIsNone(
                loop_manager.scoped_blocking.project_block(connection, str(repo))
            )
            self.assertEqual(tuple(project), ("WORKING", session_id, None))
            self.assertEqual(tuple(plan), ("IMPLEMENTING", session_id))
            self.assertEqual(source_note.read_text(encoding="utf-8"), "preserve me\n")

    def test_active_execplan_uses_reconciled_identity_not_publication_equality(self):
        replacements = (
            (
                "sources",
                "sources:\n  - docs/PRODUCT.md",
                "sources:\n  - docs/requirements/Active.md",
            ),
            (
                "depends_on",
                "depends_on: []",
                "depends_on:\n  - G-002",
            ),
            ("complexity", "complexity: low", "complexity: medium"),
            ("risk", "risk: low", "risk: high"),
            ("priority", "priority: normal", "priority: critical"),
            ("body", "Test plan.", "Updated implementation progress."),
        )
        for name, original, replacement in replacements:
            with self.subTest(field=name):
                with tempfile.TemporaryDirectory() as directory:
                    repo, plan_path = self._repository_with_plan(directory)
                    write_execplan(repo, "G-002", "Dependency")
                    commit_and_push(repo, "Add dependency plan")
                    plan_sha = git_command(repo, "rev-parse", "origin/main")
                    connection = sqlite3.connect(":memory:")
                    connection.row_factory = sqlite3.Row
                    connection.execute(loop_manager.SCHEMA)
                    connection.execute(
                        "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
                        (str(repo),),
                    )
                    parsed = loop_manager._parse_execplan(
                        repo,
                        repo / plan_path,
                    )
                    loop_manager.register_execplans(
                        connection,
                        str(repo),
                        repo,
                        [(plan_path, parsed)],
                        plan_sha,
                        loop_manager.planning_input_fingerprint(repo, "main"),
                    )
                    connection.execute(
                        "UPDATE exec_plans SET status = 'IMPLEMENTING' "
                        "WHERE repo = ? AND plan_id = 'G-001'",
                        (str(repo),),
                    )
                    connection.commit()
                    stored = loop_manager.active_execplan(
                        connection,
                        str(repo),
                    )
                    self.assertIsNotNone(stored)
                    assert stored is not None

                    plan_file = repo / plan_path
                    if name == "sources":
                        requirement = repo / "docs/requirements/Active.md"
                        requirement.parent.mkdir(parents=True, exist_ok=True)
                        requirement.write_text("# Active\n", encoding="utf-8")
                    plan_file.write_text(
                        plan_file.read_text(encoding="utf-8").replace(
                            original,
                            replacement,
                        ),
                        encoding="utf-8",
                    )
                    if name != "body":
                        revised = loop_manager.validate_execplan(repo, plan_path)
                        sources, dependencies, priority = (
                            loop_manager._execplan_metadata(revised)
                        )
                        connection.execute(
                            "UPDATE exec_plans SET sources = ?, depends_on = ?, "
                            "priority = ?, content_hash = ?, "
                            "current_complexity = ?, current_risk = ? "
                            "WHERE repo = ? AND plan_id = 'G-001'",
                            (
                                sources,
                                dependencies,
                                priority,
                                loop_manager.file_hash(plan_file),
                                revised["complexity"],
                                revised["risk"],
                                str(repo),
                            ),
                        )
                        connection.commit()
                        stored = loop_manager.active_execplan(
                            connection,
                            str(repo),
                        )
                        self.assertIsNotNone(stored)
                        assert stored is not None

                    current = loop_manager.validate_active_execplan_metadata(
                        repo,
                        repo,
                        plan_path,
                        stored,
                    )
                    self.assertEqual(current["id"], "G-001")

    def test_git_config_changes_are_rejected_without_leaking_values(self):
        scenarios = (
            (
                "remote-url",
                r"Git config 변경 .*local:remote\.origin\.url",
            ),
            (
                "hooks-path",
                r"Git config 변경 .*local:core\.hookspath",
            ),
        )
        for name, expected in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repo, plan_path = self._repository_with_plan(directory)
                before = loop_manager.capture_git_snapshot(repo)
                if name == "remote-url":
                    sensitive_value = "https://secret@attacker.invalid/repo.git"
                    git_command(
                        repo,
                        "remote",
                        "set-url",
                        "origin",
                        sensitive_value,
                    )
                else:
                    sensitive_value = str(root / "agent-hooks")
                    git_command(
                        repo,
                        "config",
                        "core.hooksPath",
                        sensitive_value,
                    )
                after = loop_manager.capture_git_snapshot(repo)

                self.assertNotEqual(before.config, after.config)
                self.assertNotIn(sensitive_value, after.serialize())
                with self.assertRaisesRegex(
                    loop_manager.AgentScopeError,
                    expected,
                ) as violation:
                    loop_manager.validate_agent_scope(
                        "worker",
                        repo,
                        before,
                        after,
                        selected_plan_path=plan_path,
                    )
                self.assertNotIn(sensitive_value, str(violation.exception))

    def test_blocked_config_change_stops_before_git_reentry(self):
        scenarios = (
            (
                "remote-url",
                ("remote", "set-url", "origin"),
                "https://secret@attacker.invalid/repo.git",
                "local:remote.origin.url",
            ),
            (
                "hooks-path",
                ("config", "core.hooksPath"),
                "agent-hooks",
                "local:core.hookspath",
            ),
        )
        for name, arguments, configured_value, expected_key in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repo, _plan_path = self._repository_with_plan(directory)
                before = loop_manager.capture_git_snapshot(repo)
                sensitive_value = (
                    str(root / configured_value)
                    if name == "hooks-path"
                    else configured_value
                )
                git_command(repo, *arguments, sensitive_value)
                blocked_result = loop_manager.capture_git_snapshot(repo)
                connection = sqlite3.connect(":memory:")
                connection.row_factory = sqlite3.Row
                connection.execute(loop_manager.SCHEMA)
                connection.execute(
                    """
                    INSERT INTO projects (
                        repo, base_branch, phase, worktree, agent_role,
                        agent_snapshot, agent_result_snapshot
                    ) VALUES (?, 'main', 'BLOCKED', ?, 'worker', ?, ?)
                    """,
                    (
                        str(repo),
                        str(repo),
                        before.serialize(),
                        blocked_result.serialize(),
                    ),
                )
                connection.commit()

                with mock.patch(
                    "loop_manager.validate_repository",
                    return_value=repo,
                ), mock.patch(
                    "loop_manager.migrate_legacy_execplans"
                ) as migration, mock.patch(
                    "loop_manager.git",
                    side_effect=AssertionError("unsafe Git command before config validation"),
                ) as unsafe_git, mock.patch(
                    "loop_manager.run_codex"
                ) as agent:
                    result = loop_manager.run_project(
                        connection,
                        root / "state",
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )

                project = connection.execute(
                    "SELECT phase, last_error, agent_result_snapshot "
                    "FROM projects WHERE repo = ?",
                    (str(repo),),
                ).fetchone()
                self.assertEqual(result, 1)
                self.assertEqual(project["phase"], "PROJECT_BLOCKED")
                self.assertIn("범위 선검증 실패", project["last_error"])
                self.assertIn(expected_key, project["last_error"])
                self.assertNotIn(sensitive_value, project["last_error"])
                self.assertEqual(
                    project["agent_result_snapshot"],
                    blocked_result.serialize(),
                )
                migration.assert_not_called()
                unsafe_git.assert_not_called()
                agent.assert_not_called()

    def test_interrupted_config_change_stops_before_database_git_migrations(self):
        scenarios = (
            (
                "remote-url",
                ("remote", "set-url", "origin"),
                "https://secret@attacker.invalid/repo.git",
                "local:remote.origin.url",
            ),
            (
                "hooks-path",
                ("config", "core.hooksPath"),
                "agent-hooks",
                "local:core.hookspath",
            ),
        )
        for name, arguments, configured_value, expected_key in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repo, plan_path = self._repository_with_plan(directory)
                before = loop_manager.capture_git_snapshot(repo)
                state_dir = root / "state"
                self._persist_interrupted_worker(state_dir, repo, plan_path, before)

                sensitive_value = (
                    str(root / configured_value)
                    if name == "hooks-path"
                    else configured_value
                )
                git_command(repo, *arguments, sensitive_value)

                with mock.patch.object(
                    loop_manager.sys,
                    "argv",
                    [
                        "loop_manager.py",
                        "--runtime-child",
                        "--runtime-source",
                        str(loop_manager.ROOT),
                        "--runtime-sha",
                        "a" * 40,
                        "--state-dir",
                        str(state_dir),
                        "--config",
                        str(loop_manager.ROOT / "config/config.ini.example"),
                        "run",
                    ],
                ), mock.patch(
                    "loop_manager.validate_runtime_child"
                ), mock.patch(
                    "loop_manager.validate_runtime_mutation", return_value=None
                ), mock.patch(
                    "loop_manager.git",
                    side_effect=AssertionError(
                        "unsafe Git command before config validation"
                    ),
                ) as unsafe_git, mock.patch(
                    "loop_manager.run_codex"
                ) as agent:
                    result = loop_manager.main()
                    database = sqlite3.connect(state_dir / "loop-manager.db")
                    database.row_factory = sqlite3.Row
                    project = database.execute(
                        "SELECT phase, last_error FROM projects WHERE repo = ?",
                        (str(repo),),
                    ).fetchone()
                    plan = database.execute(
                        "SELECT status, blocked_fingerprint FROM exec_plans "
                        "WHERE repo = ? AND plan_id = 'G-001'",
                        (str(repo),),
                    ).fetchone()
                    database.close()

                self.assertEqual(result, 1)
                self.assertEqual(project["phase"], "PROJECT_BLOCKED")
                self.assertEqual(plan["status"], "IMPLEMENTING")
                self.assertIsNotNone(plan["blocked_fingerprint"])
                self.assertIn("범위 선검증 실패", project["last_error"])
                self.assertIn(expected_key, project["last_error"])
                self.assertNotIn(sensitive_value, project["last_error"])
                unsafe_git.assert_not_called()
                agent.assert_not_called()

    def test_interrupted_non_base_ref_change_blocks_before_migration_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, plan_path = self._repository_with_plan(directory)
            before = loop_manager.capture_git_snapshot(repo)
            state_dir = root / "state"
            self._persist_interrupted_worker(state_dir, repo, plan_path, before)
            changed_ref = git_command(repo, "rev-parse", "HEAD^")
            git_command(
                repo,
                "update-ref",
                "refs/heads/agent-created",
                changed_ref,
            )
            real_git = loop_manager.git
            git_calls = []

            def reject_fetch(repo_path, *arguments, **kwargs):
                git_calls.append(arguments)
                if arguments[:2] == ("fetch", "origin"):
                    raise AssertionError("migration fetch ran before scope validation")
                return real_git(repo_path, *arguments, **kwargs)

            with mock.patch.object(
                loop_manager.sys,
                "argv",
                [
                    "loop_manager.py",
                    "--runtime-child",
                    "--runtime-source",
                    str(loop_manager.ROOT),
                    "--runtime-sha",
                    "a" * 40,
                    "--state-dir",
                    str(state_dir),
                    "--config",
                    str(loop_manager.ROOT / "config/config.ini.example"),
                    "run",
                ],
            ), mock.patch(
                "loop_manager.validate_runtime_child"
            ), mock.patch(
                "loop_manager.validate_runtime_mutation", return_value=None
            ), mock.patch(
                "loop_manager.validate_repository",
                return_value=repo,
            ), mock.patch(
                "loop_manager.git",
                side_effect=reject_fetch,
            ), mock.patch(
                "loop_manager.run_codex"
            ) as agent:
                result = loop_manager.main()

            database = sqlite3.connect(state_dir / "loop-manager.db")
            database.row_factory = sqlite3.Row
            project = database.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan = database.execute(
                "SELECT status, blocked_fingerprint FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            database.close()

            self.assertEqual(result, 1)
            self.assertEqual(project["phase"], "PROJECT_BLOCKED")
            self.assertEqual(plan["status"], "IMPLEMENTING")
            self.assertIsNotNone(plan["blocked_fingerprint"])
            self.assertIn("공유 Git refs 변경", project["last_error"])
            self.assertIn("refs/heads/agent-created", project["last_error"])
            self.assertEqual(
                git_command(repo, "rev-parse", "refs/heads/agent-created"),
                changed_ref,
            )
            self.assertFalse(
                any(arguments[:2] == ("fetch", "origin") for arguments in git_calls)
            )
            agent.assert_not_called()

    def test_interrupted_worker_resume_accepts_manager_fetched_remote_ref(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, plan_path = self._repository_with_plan(directory)
            state_dir = root / "state"
            state_dir.mkdir()
            plan_sha = git_command(repo, "rev-parse", "origin/main")
            branch, worktree = loop_manager.expected_execplan_workspace(
                state_dir, str(repo), "G-001"
            )
            loop_manager.make_worktree(repo, worktree, branch, "main")
            cleanup_branch, cleanup_worktree = loop_manager.expected_execplan_workspace(
                state_dir, str(repo), "G-099"
            )
            loop_manager.make_worktree(repo, cleanup_worktree, cleanup_branch, "main")

            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
                (str(repo),),
            )
            loop_manager.register_execplans(
                connection,
                str(repo),
                repo,
                [(plan_path, loop_manager._parse_execplan(repo, repo / plan_path))],
                plan_sha,
                loop_manager.planning_input_fingerprint(repo, "main"),
            )
            loop_manager._ensure_cleanup_pending_schema(connection)
            loop_manager._store_cleanup_pending(
                connection,
                str(repo),
                cleanup_worktree,
                "https://example.test/pr/99",
                plan_sha,
                "pending cleanup",
            )
            connection.commit()
            before = loop_manager.capture_git_snapshot(worktree)
            with connection:
                connection.execute(
                    """
                    UPDATE exec_plans
                    SET status = 'IMPLEMENTING', active_sha = ?, branch = ?,
                        worktree = ?, workspace_owned = 1,
                        worker_session_id = 'worker-session'
                    WHERE repo = ? AND plan_id = 'G-001'
                    """,
                    (plan_sha, branch, str(worktree), str(repo)),
                )
                connection.execute(
                    """
                    UPDATE projects
                    SET active_sha = ?, plan_sha = ?, phase = 'WORKING',
                        branch = ?, worktree = ?, plan_path = ?,
                        worker_session_id = 'worker-session',
                        agent_role = 'worker', agent_snapshot = ?
                    WHERE repo = ?
                    """,
                    (
                        plan_sha,
                        plan_sha,
                        branch,
                        str(worktree),
                        plan_path,
                        before.serialize(),
                        str(repo),
                    ),
                )

            (worktree / "src/app.py").write_text(
                "VALUE = 2\n",
                encoding="utf-8",
            )

            remote = root / "remote.git"
            git_command(
                remote,
                "update-ref",
                "refs/heads/manager-update",
                plan_sha,
            )
            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.refresh_persisted_agent_scope_after_manager_git",
                side_effect=KeyboardInterrupt("interrupt after Manager checkpoint"),
            ), mock.patch(
                "loop_manager.run_codex"
            ) as interrupted_agent:
                with self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "Manager checkpoint",
                ):
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )

            interrupted_checkpoint = connection.execute(
                "SELECT agent_manager_snapshot FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()["agent_manager_snapshot"]
            self.assertIsNotNone(interrupted_checkpoint)
            self.assertFalse(cleanup_worktree.exists())
            self.assertEqual(
                git_command(repo, "rev-parse", "refs/remotes/origin/manager-update"),
                plan_sha,
            )
            interrupted_agent.assert_not_called()

            sessions = []

            def resumed_worker(_worktree, _state, _config, role_name, _prompt, **kwargs):
                self.assertEqual(role_name, "worker")
                sessions.append(kwargs["session_id"])
                return loop_manager.CodexResult(
                    1, "stop after resumed worker", "worker-session"
                )

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex", side_effect=resumed_worker
            ):
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT phase, last_error, agent_snapshot, agent_manager_snapshot "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan_status = connection.execute(
                "SELECT status FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()["status"]

            self.assertEqual(result, 1)
            self.assertEqual(sessions, ["worker-session"])
            self.assertEqual(project["phase"], "WORKING")
            self.assertEqual(plan_status, "IMPLEMENTING")
            self.assertEqual(project["last_error"], "stop after resumed worker")
            self.assertIsNone(project["agent_snapshot"])
            self.assertIsNone(project["agent_manager_snapshot"])
            self.assertEqual(
                git_command(repo, "rev-parse", "refs/remotes/origin/manager-update"),
                plan_sha,
            )
            self.assertFalse(cleanup_worktree.exists())
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM cleanup_pending WHERE repo = ?",
                    (str(repo),),
                ).fetchone()[0],
                0,
            )

    def test_blocked_checkpoint_allows_manager_cleanup_and_later_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, plan_path = self._repository_with_plan(directory)
            state_dir = root / "state"
            state_dir.mkdir()
            plan_sha = git_command(repo, "rev-parse", "origin/main")
            branch, worktree = loop_manager.expected_execplan_workspace(
                state_dir, str(repo), "G-001"
            )
            loop_manager.make_worktree(repo, worktree, branch, "main")
            cleanup_branch, cleanup_worktree = loop_manager.expected_execplan_workspace(
                state_dir, str(repo), "G-099"
            )
            loop_manager.make_worktree(
                repo,
                cleanup_worktree,
                cleanup_branch,
                "main",
            )

            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
                (str(repo),),
            )
            loop_manager.register_execplans(
                connection,
                str(repo),
                repo,
                [(plan_path, loop_manager._parse_execplan(repo, repo / plan_path))],
                plan_sha,
                loop_manager.planning_input_fingerprint(repo, "main"),
            )
            plan = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            input_fingerprint = loop_manager._worktree_execplan_input_fingerprint(
                worktree,
                plan,
            )
            before = loop_manager.capture_git_snapshot(worktree)
            (worktree / "reviewer.txt").write_text(
                "preserve violation\n",
                encoding="utf-8",
            )
            blocked_result = loop_manager.capture_git_snapshot(worktree)
            loop_manager._ensure_cleanup_pending_schema(connection)
            loop_manager._store_cleanup_pending(
                connection,
                str(repo),
                cleanup_worktree,
                "https://example.test/pr/99",
                plan_sha,
                "pending cleanup",
            )
            with connection:
                connection.execute(
                    """
                    UPDATE exec_plans
                    SET status = 'BLOCKED', active_sha = ?, branch = ?,
                        worktree = ?, workspace_owned = 1,
                        blocked_fingerprint = ?,
                        blocked_worktree_fingerprint = ?,
                        last_error = 'scope blocked'
                    WHERE repo = ? AND plan_id = 'G-001'
                    """,
                    (
                        plan_sha,
                        branch,
                        str(worktree),
                        input_fingerprint,
                        input_fingerprint,
                        str(repo),
                    ),
                )
                connection.execute(
                    """
                    UPDATE projects
                    SET active_sha = ?, plan_sha = ?, phase = 'BLOCKED',
                        branch = ?, worktree = ?, plan_path = ?,
                        last_error = 'scope blocked', agent_role = 'reviewer',
                        agent_snapshot = ?, agent_input_fingerprint = ?,
                        agent_result_snapshot = ?
                    WHERE repo = ?
                    """,
                    (
                        plan_sha,
                        plan_sha,
                        branch,
                        str(worktree),
                        plan_path,
                        before.serialize(),
                        input_fingerprint,
                        blocked_result.serialize(),
                        str(repo),
                    ),
                )

            common_patches = (
                mock.patch("loop_manager.validate_repository", return_value=repo),
                mock.patch("loop_manager.remote_base", return_value="main"),
                mock.patch("loop_manager.ensure_safe_base_worktree"),
                mock.patch("loop_manager.pr_for_branch", return_value=None),
            )
            with common_patches[0], common_patches[1], common_patches[2], common_patches[3], mock.patch(
                "loop_manager.run_codex"
            ) as blocked_agent:
                blocked_run = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            blocked_project = connection.execute(
                "SELECT phase, last_error, agent_manager_snapshot "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            blocked_plan = connection.execute(
                "SELECT status, last_error, agent_snapshot "
                "FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(blocked_run, 0)
            self.assertIsNone(blocked_project["phase"])
            self.assertEqual(blocked_plan["status"], "PLAN_BLOCKED")
            self.assertEqual(blocked_plan["last_error"], "scope blocked")
            self.assertIsNotNone(blocked_plan["agent_snapshot"])
            self.assertFalse(cleanup_worktree.exists())
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM cleanup_pending WHERE repo = ?",
                    (str(repo),),
                ).fetchone()[0],
                0,
            )
            blocked_agent.assert_not_called()

            (repo / "docs/PRODUCT.md").write_text(
                "# Product changed after cleanup\n",
                encoding="utf-8",
            )
            commit_and_push(repo, "Change source after blocked cleanup")
            with connection:
                connection.execute(
                    "UPDATE projects SET specification_fingerprint = ? "
                    "WHERE repo = ?",
                    (
                        loop_manager.specification_input_fingerprint(repo, "main"),
                        str(repo),
                    ),
                )
            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex",
                side_effect=[
                    loop_manager.CodexResult(0, "", "planner-session"),
                    loop_manager.CodexResult(
                        1, "stop after cleanup recovery", "worker-session"
                    ),
                ],
            ) as resumed_agent:
                resumed_run = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            resumed_project = connection.execute(
                "SELECT phase, last_error, agent_snapshot, agent_manager_snapshot "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            resumed_plan = connection.execute(
                "SELECT status, blocked_fingerprint, blocked_worktree_fingerprint "
                "FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(resumed_run, 1)
            self.assertEqual(resumed_project["phase"], "WORKING")
            self.assertEqual(
                resumed_project["last_error"],
                "stop after cleanup recovery",
            )
            self.assertIsNone(resumed_project["agent_snapshot"])
            self.assertIsNone(resumed_project["agent_manager_snapshot"])
            self.assertEqual(resumed_plan["status"], "IMPLEMENTING")
            self.assertIsNone(resumed_plan["blocked_fingerprint"])
            self.assertIsNone(resumed_plan["blocked_worktree_fingerprint"])
            self.assertEqual(
                [call.args[3] for call in resumed_agent.call_args_list],
                ["planner", "worker"],
            )

    def test_blocked_resume_does_not_excuse_agent_owned_ref_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, plan_path = self._repository_with_plan(directory)
            git_command(repo, "branch", "loop-manager/G-099", "HEAD")
            worktree = root / "managed"
            loop_manager.make_worktree(
                repo,
                worktree,
                "loop-manager/G-001",
                "main",
            )
            before = loop_manager.capture_git_snapshot(worktree)
            git_command(
                worktree,
                "update-ref",
                "refs/heads/loop-manager/G-099",
                "HEAD^",
            )
            blocked_result = loop_manager.capture_git_snapshot(worktree)
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                """
                INSERT INTO projects (
                    repo, base_branch, agent_role, agent_snapshot,
                    agent_result_snapshot
                ) VALUES (?, 'main', 'reviewer', ?, ?)
                """,
                (
                    str(repo),
                    before.serialize(),
                    blocked_result.serialize(),
                ),
            )
            connection.commit()

            (repo / "docs/PRODUCT.md").write_text(
                "# Product changed after blocked review\n",
                encoding="utf-8",
            )
            commit_and_push(repo, "Change source after base ref mutation")

            with self.assertRaisesRegex(
                loop_manager.AgentScopeError,
                r"refs/heads/loop-manager/G-099",
            ):
                loop_manager.validate_blocked_agent_scope(
                    connection,
                    repo,
                    str(repo),
                    "main",
                    worktree,
                    plan_path,
                )

    def test_agent_commit_isolated_automatically_after_blocked_input_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, plan_path = self._repository_with_plan(directory)
            state_dir = root / "state"
            state_dir.mkdir()
            branch, worktree = loop_manager.expected_execplan_workspace(
                state_dir,
                str(repo),
                "G-001",
            )
            loop_manager.make_worktree(repo, worktree, branch, "main")
            plan_sha = git_command(repo, "rev-parse", "origin/main")
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
                (str(repo),),
            )
            loop_manager.register_execplans(
                connection,
                str(repo),
                repo,
                [(plan_path, loop_manager._parse_execplan(repo, repo / plan_path))],
                plan_sha,
                loop_manager.planning_input_fingerprint(repo, "main"),
            )
            plan = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            blocked_fingerprint = loop_manager._worktree_execplan_input_fingerprint(
                worktree,
                plan,
            )
            with connection:
                connection.execute(
                    """
                    UPDATE exec_plans
                    SET status = ?, active_sha = ?, branch = ?,
                        worktree = ?, workspace_owned = 1
                    WHERE repo = ? AND plan_id = ?
                    """,
                    (
                        "IMPLEMENTING",
                        plan_sha,
                        branch,
                        str(worktree),
                        str(repo),
                        "G-001",
                    ),
                )
                connection.execute(
                    """
                    UPDATE projects
                    SET active_sha = ?, plan_sha = ?, phase = ?,
                        branch = ?, worktree = ?, plan_path = ?
                    WHERE repo = ?
                    """,
                    (
                        plan_sha,
                        plan_sha,
                        "WORKING",
                        branch,
                        str(worktree),
                        plan_path,
                        str(repo),
                    ),
                )
            (worktree / "src/app.py").write_text(
                "VALUE = 2\n",
                encoding="utf-8",
            )
            git_command(worktree, "add", "src/app.py")
            (worktree / "README.md").write_text(
                "# preserved unstaged Worker change\n",
                encoding="utf-8",
            )
            (worktree / "tests/evidence.txt").write_text(
                "targeted tests passed\n",
                encoding="utf-8",
            )
            before = loop_manager.begin_agent_scope(
                connection,
                str(repo),
                worktree,
                "worker",
                input_fingerprint=blocked_fingerprint,
            )
            self.assertTrue(before.dirty)
            self.assertIn("staged", before.dirty["src/app.py"][0])
            (worktree / "docs/requirements/Active.md").write_text(
                "# changed by committing Agent\n",
                encoding="utf-8",
            )
            git_command(worktree, "add", "-A")
            git_command(worktree, "commit", "-m", "Agent commit")
            agent_commit = git_command(worktree, "rev-parse", "HEAD")
            blocked_result = loop_manager.capture_git_snapshot(worktree)
            with connection:
                connection.execute(
                    """
                    UPDATE exec_plans
                    SET status = 'BLOCKED', active_sha = ?, branch = ?,
                        worktree = ?, workspace_owned = 1,
                        blocked_fingerprint = ?,
                        blocked_worktree_fingerprint = ?
                    WHERE repo = ? AND plan_id = 'G-001'
                    """,
                    (
                        plan_sha,
                        branch,
                        str(worktree),
                        blocked_fingerprint,
                        blocked_fingerprint,
                        str(repo),
                    ),
                )
                connection.execute(
                    """
                    UPDATE projects
                    SET active_sha = ?, plan_sha = ?, phase = 'BLOCKED',
                        branch = ?, worktree = ?, plan_path = ?,
                        agent_role = 'worker', agent_snapshot = ?,
                        agent_input_fingerprint = ?,
                        agent_result_snapshot = ?
                    WHERE repo = ?
                    """,
                    (
                        plan_sha,
                        plan_sha,
                        branch,
                        str(worktree),
                        plan_path,
                        before.serialize(),
                        blocked_fingerprint,
                        blocked_result.serialize(),
                        str(repo),
                    ),
                )

            (repo / "docs/PRODUCT.md").write_text(
                "# Product changed after blocked Agent commit\n",
                encoding="utf-8",
            )
            commit_and_push(repo, "Change source after blocked Agent commit")
            with connection:
                connection.execute(
                    "UPDATE projects SET specification_fingerprint = ? "
                    "WHERE repo = ?",
                    (
                        loop_manager.specification_input_fingerprint(repo, "main"),
                        str(repo),
                    ),
                )

            def stop_after_isolation(
                _worktree, _state, _config, role_name, _prompt, **_kwargs
            ):
                if role_name == "planner":
                    return loop_manager.CodexResult(0, "", "planner-session")
                restored = loop_manager.capture_git_snapshot(worktree)
                self.assertIn("staged", restored.dirty["src/app.py"][0])
                self.assertIn("unstaged", restored.dirty["README.md"][0])
                self.assertIn(
                    "untracked",
                    restored.dirty["tests/evidence.txt"][0],
                )
                return loop_manager.CodexResult(
                    1,
                    "stop after automatic commit isolation",
                    "planner-session",
                )

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex",
                side_effect=stop_after_isolation,
            ) as resumed_agent:
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT phase, last_error, agent_snapshot, agent_recovery "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            resumed_plan = connection.execute(
                "SELECT status, blocked_fingerprint FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            diagnostic_refs = git_command(
                worktree,
                "for-each-ref",
                "--format=%(objectname)",
                "refs/loop-manager/violations/",
            ).splitlines()
            self.assertEqual(result, 1)
            self.assertEqual(project["phase"], "WORKING")
            self.assertEqual(
                project["last_error"],
                "stop after automatic commit isolation",
            )
            self.assertIsNone(project["agent_snapshot"])
            self.assertIsNone(project["agent_recovery"])
            self.assertEqual(resumed_plan["status"], "IMPLEMENTING")
            self.assertIsNone(resumed_plan["blocked_fingerprint"])
            self.assertIn(agent_commit, diagnostic_refs)
            self.assertEqual(
                (worktree / "src/app.py").read_text(encoding="utf-8"),
                "VALUE = 2\n",
            )
            self.assertEqual(
                (worktree / "tests/evidence.txt").read_text(encoding="utf-8"),
                "targeted tests passed\n",
            )
            restored = loop_manager.capture_git_snapshot(worktree)
            self.assertIn("staged", restored.dirty["src/app.py"][0])
            self.assertIn("unstaged", restored.dirty["README.md"][0])
            self.assertIn("untracked", restored.dirty["tests/evidence.txt"][0])
            self.assertEqual(
                (worktree / "README.md").read_text(encoding="utf-8"),
                "# preserved unstaged Worker change\n",
            )
            self.assertEqual(
                (worktree / "docs/requirements/Active.md").read_text(
                    encoding="utf-8"
                ),
                "# Active\n",
            )
            self.assertEqual(
                [call.args[3] for call in resumed_agent.call_args_list],
                ["planner", "worker"],
            )

    def test_restored_worker_git_metadata_allows_changed_input_to_requeue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, plan_path = self._repository_with_plan(directory)
            state_dir = root / "state"
            state_dir.mkdir()
            branch, worktree = loop_manager.expected_execplan_workspace(
                state_dir,
                str(repo),
                "G-001",
            )
            loop_manager.make_worktree(repo, worktree, branch, "main")
            plan_sha = git_command(repo, "rev-parse", "origin/main")
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
                (str(repo),),
            )
            loop_manager.register_execplans(
                connection,
                str(repo),
                repo,
                [(plan_path, loop_manager._parse_execplan(repo, repo / plan_path))],
                plan_sha,
                loop_manager.planning_input_fingerprint(repo, "main"),
            )
            plan = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            blocked_fingerprint = loop_manager._worktree_execplan_input_fingerprint(
                worktree,
                plan,
            )
            before = loop_manager.capture_git_snapshot(worktree)

            (worktree / "src/app.py").write_text(
                "VALUE = 2\n",
                encoding="utf-8",
            )
            git_command(worktree, "add", "src/app.py")
            git_command(worktree, "commit", "-m", "Agent commit")
            git_command(worktree, "switch", "-c", "agent-branch")
            git_command(
                worktree,
                "update-ref",
                "refs/tags/agent-tag",
                "HEAD",
            )
            git_command(worktree, "config", "core.hooksPath", ".agent-hooks")
            blocked_result = loop_manager.capture_git_snapshot(worktree)
            with connection:
                connection.execute(
                    """
                    UPDATE exec_plans
                    SET status = 'BLOCKED', active_sha = ?, branch = ?,
                        worktree = ?, workspace_owned = 1,
                        blocked_fingerprint = ?
                    WHERE repo = ? AND plan_id = 'G-001'
                    """,
                    (
                        plan_sha,
                        branch,
                        str(worktree),
                        blocked_fingerprint,
                        str(repo),
                    ),
                )
                connection.execute(
                    """
                    UPDATE projects
                    SET active_sha = ?, plan_sha = ?, phase = 'BLOCKED',
                        branch = ?, worktree = ?, plan_path = ?,
                        agent_role = 'worker', agent_snapshot = ?,
                        agent_result_snapshot = ?
                    WHERE repo = ?
                    """,
                    (
                        plan_sha,
                        plan_sha,
                        branch,
                        str(worktree),
                        plan_path,
                        before.serialize(),
                        blocked_result.serialize(),
                        str(repo),
                    ),
                )

            git_command(worktree, "config", "--unset", "core.hooksPath")
            git_command(worktree, "switch", branch)
            git_command(worktree, "reset", "--hard", plan_sha)
            git_command(worktree, "branch", "-D", "agent-branch")
            git_command(worktree, "update-ref", "-d", "refs/tags/agent-tag")
            (repo / "docs/PRODUCT.md").write_text(
                "# Product changed after Git recovery\n",
                encoding="utf-8",
            )
            commit_and_push(repo, "Change source after Git recovery")
            with connection:
                connection.execute(
                    "UPDATE projects SET specification_fingerprint = ? "
                    "WHERE repo = ?",
                    (
                        loop_manager.specification_input_fingerprint(repo, "main"),
                        str(repo),
                    ),
                )

            with mock.patch(
                "loop_manager.validate_repository",
                return_value=repo,
            ), mock.patch(
                "loop_manager.remote_base",
                return_value="main",
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch",
                return_value=None,
            ), mock.patch(
                "loop_manager.run_codex",
                side_effect=[
                    loop_manager.CodexResult(0, "", "planner-session"),
                    loop_manager.CodexResult(
                        1, "stop after restored metadata", "worker-session"
                    ),
                ],
            ) as resumed_agent:
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT phase, last_error, agent_snapshot, agent_result_snapshot "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan = connection.execute(
                "SELECT status, blocked_fingerprint FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(result, 1)
            self.assertEqual(project["phase"], "WORKING")
            self.assertEqual(project["last_error"], "stop after restored metadata")
            self.assertIsNone(project["agent_snapshot"])
            self.assertIsNone(project["agent_result_snapshot"])
            self.assertEqual(plan["status"], "IMPLEMENTING")
            self.assertIsNone(plan["blocked_fingerprint"])
            self.assertEqual(
                [call.args[3] for call in resumed_agent.call_args_list],
                ["planner", "worker"],
            )

    def test_index_flags_are_rejected_for_worker_allowed_path(self):
        for option in ("--skip-worktree", "--assume-unchanged"):
            with self.subTest(option=option), tempfile.TemporaryDirectory() as directory:
                repo, plan_path = self._repository_with_plan(directory)
                before = loop_manager.capture_git_snapshot(repo)

                git_command(
                    repo,
                    "update-index",
                    option,
                    "--",
                    "src/app.py",
                )
                after = loop_manager.capture_git_snapshot(repo)

                self.assertNotEqual(
                    before.index["src/app.py"],
                    after.index["src/app.py"],
                )
                with self.assertRaisesRegex(
                    loop_manager.AgentScopeError,
                    r"src/app\.py .*index flags",
                ):
                    loop_manager.validate_agent_scope(
                        "worker",
                        repo,
                        before,
                        after,
                        selected_plan_path=plan_path,
                    )

    def test_preexisting_index_flags_do_not_hide_agent_file_changes(self):
        scenarios = (
            ("planner", "README.md"),
            ("worker", "docs/requirements/Active.md"),
            ("reviewer", "src/app.py"),
        )
        for option in ("--skip-worktree", "--assume-unchanged"):
            for role_name, relative_path in scenarios:
                with self.subTest(option=option, role=role_name):
                    with tempfile.TemporaryDirectory() as directory:
                        repo, plan_path = self._repository_with_plan(directory)
                        git_command(
                            repo,
                            "update-index",
                            option,
                            "--",
                            relative_path,
                        )
                        before = loop_manager.capture_git_snapshot(repo)

                        (repo / relative_path).write_text(
                            f"changed by {role_name}\n",
                            encoding="utf-8",
                        )
                        after = loop_manager.capture_git_snapshot(repo)

                        self.assertEqual(
                            before.index[relative_path],
                            after.index[relative_path],
                        )
                        self.assertNotEqual(
                            before.dirty[relative_path],
                            after.dirty[relative_path],
                        )
                        with self.assertRaises(
                            loop_manager.AgentScopeError
                        ) as raised:
                            loop_manager.validate_agent_scope(
                                role_name,
                                repo,
                                before,
                                after,
                                selected_plan_path=(
                                    plan_path if role_name == "worker" else None
                                ),
                            )
                        self.assertIn(relative_path, str(raised.exception))

    def test_existing_database_adds_persistent_agent_scope_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            database = sqlite3.connect(state_dir / "loop-manager.db")
            database.execute(
                """
                CREATE TABLE projects (
                    repo TEXT PRIMARY KEY,
                    base_branch TEXT NOT NULL,
                    phase TEXT
                )
                """
            )
            database.execute(
                "INSERT INTO projects (repo, base_branch) VALUES ('repo', 'main')"
            )
            database.commit()
            database.close()

            with loop_manager.connect_database(state_dir) as connection:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(projects)")
                }
                row = connection.execute(
                    "SELECT agent_role, agent_snapshot, agent_input_fingerprint, agent_result_snapshot, "
                    "planning_workspace_owned "
                    "FROM projects "
                    "WHERE repo = 'repo'"
                ).fetchone()

            self.assertTrue(
                {
                    "agent_role",
                    "agent_snapshot",
                    "agent_input_fingerprint",
                    "agent_result_snapshot",
                    "planning_workspace_owned",
                }
                <= columns
            )
            self.assertEqual(tuple(row), (None, None, None, None, 0))

    def test_reviewer_mutation_blocks_even_when_verdict_passes(self):

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, plan_path = self._repository_with_plan(directory)
            state_dir = root / "state"
            state_dir.mkdir()
            branch, worktree = loop_manager.expected_execplan_workspace(
                state_dir, str(repo), "G-001"
            )
            loop_manager.make_worktree(repo, worktree, branch, "main")
            (worktree / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            git_command(worktree, "add", "src/app.py")
            git_command(worktree, "commit", "-m", "Prepare verified candidate")
            plan_sha = git_command(repo, "rev-parse", "origin/main")
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                """
                INSERT INTO projects (
                    repo, base_branch, active_sha, plan_sha, phase, branch,
                    worktree, plan_path, worker_result, verify_result
                ) VALUES (?, 'main', ?, ?, 'VERIFYING', ?, ?, ?, ?, ?)
                """,
                (
                    str(repo),
                    plan_sha,
                    plan_sha,
                    branch,
                    str(worktree),
                    plan_path,
                    worker_result(),
                    None,
                ),
            )
            connection.commit()
            with connection:
                connection.execute(
                    """
                    UPDATE projects
                    SET planning_fingerprint = ?, specification_fingerprint = ?
                    WHERE repo = ?
                    """,
                    (
                        loop_manager.planning_input_fingerprint(repo, "main"),
                        loop_manager.specification_input_fingerprint(repo, "main"),
                        str(repo),
                    ),
                )

            def reviewer(agent_worktree, *_args, **_kwargs):
                (agent_worktree / "reviewer.txt").write_text(
                    "keep for diagnosis\n", encoding="utf-8"
                )
                (agent_worktree / "docs/PRODUCT.md").write_text(
                    "# Product changed by Reviewer\n", encoding="utf-8"
                )
                return 0, "VERDICT: PASS"

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.verify", return_value=(True, "verify: PASS")
            ), mock.patch(
                "loop_manager.run_codex", side_effect=reviewer
            ):
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT phase, last_error, agent_role, agent_snapshot, "
                "agent_result_snapshot "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan = connection.execute(
                "SELECT * FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(result, 0)
            self.assertIsNone(project["phase"])
            self.assertIn("reviewer Git 범위 위반", plan["last_error"])
            self.assertEqual(plan["status"], "PLAN_BLOCKED")
            self.assertEqual(plan["agent_role"], "reviewer")
            self.assertIsNotNone(plan["agent_snapshot"])
            self.assertIsNotNone(plan["agent_result_snapshot"])
            self.assertNotIn("keep for diagnosis", plan["agent_snapshot"])
            self.assertIsNotNone(plan["blocked_fingerprint"])
            self.assertEqual(
                plan["blocked_fingerprint"],
                loop_manager._revision_execplan_input_fingerprint(
                    repo, "refs/remotes/origin/main", plan
                ),
            )
            self.assertNotEqual(
                plan["blocked_fingerprint"],
                plan["blocked_worktree_fingerprint"],
            )
            self.assertTrue((worktree / "reviewer.txt").is_file())

            blocked_error = plan["last_error"]
            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex"
            ) as unchanged_agent:
                unchanged_result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            unchanged_project = connection.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            unchanged_plan = connection.execute(
                "SELECT status, last_error, blocked_fingerprint FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(unchanged_result, 0)
            self.assertIsNone(unchanged_project["phase"])
            self.assertEqual(unchanged_plan["last_error"], blocked_error)
            self.assertEqual(unchanged_plan["status"], "PLAN_BLOCKED")
            self.assertEqual(
                unchanged_plan["blocked_fingerprint"],
                plan["blocked_fingerprint"],
            )
            self.assertEqual(
                (worktree / "docs/PRODUCT.md").read_text(encoding="utf-8"),
                "# Product changed by Reviewer\n",
            )
            unchanged_agent.assert_not_called()

            (repo / "docs/PRODUCT.md").write_text(
                "# Product changed after blocked review\n",
                encoding="utf-8",
            )
            commit_and_push(repo, "Change source after blocked review")
            with connection:
                connection.execute(
                    "UPDATE projects SET specification_fingerprint = ? "
                    "WHERE repo = ?",
                    (
                        loop_manager.specification_input_fingerprint(repo, "main"),
                        str(repo),
                    ),
                )
            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex",
                side_effect=[
                    loop_manager.CodexResult(0, "", "planner-session"),
                    loop_manager.CodexResult(
                        1, "stop after scope unblock", "worker-session"
                    ),
                ],
            ) as resumed_agent:
                resumed_result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            resumed_project = connection.execute(
                "SELECT phase, last_error, agent_role, agent_snapshot, "
                "agent_result_snapshot "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            resumed_plan = connection.execute(
                "SELECT status, blocked_fingerprint FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(resumed_result, 1)
            self.assertEqual(resumed_project["phase"], "WORKING")
            self.assertEqual(
                resumed_project["last_error"],
                "stop after scope unblock",
            )
            self.assertIsNone(resumed_project["agent_role"])
            self.assertIsNone(resumed_project["agent_snapshot"])
            self.assertIsNone(resumed_project["agent_result_snapshot"])
            self.assertEqual(resumed_plan["status"], "IMPLEMENTING")
            self.assertIsNone(resumed_plan["blocked_fingerprint"])
            self.assertFalse((worktree / "reviewer.txt").exists())
            self.assertIn(
                "loop-manager scope violation G-001",
                git_command(worktree, "stash", "list"),
            )
            self.assertEqual(
                [call.args[3] for call in resumed_agent.call_args_list],
                ["planner", "worker"],
            )


    def test_reviewer_scope_block_records_execplan_body_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, plan_path = self._repository_with_plan(directory)
            worktree = root / "managed"
            loop_manager.make_worktree(
                repo,
                worktree,
                "loop-manager/G-001",
                "main",
            )
            plan_sha = git_command(repo, "rev-parse", "origin/main")
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                """
                INSERT INTO projects (repo, base_branch, plan_path)
                VALUES (?, 'main', ?)
                """,
                (str(repo), plan_path),
            )
            loop_manager.register_execplans(
                connection,
                str(repo),
                repo,
                [(plan_path, loop_manager._parse_execplan(repo, repo / plan_path))],
                plan_sha,
                loop_manager.planning_input_fingerprint(repo, "main"),
            )
            loop_manager.update_project(connection, str(repo), plan_path=plan_path)
            plan = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            input_fingerprint = loop_manager._worktree_execplan_input_fingerprint(
                worktree,
                plan,
            )
            plan_file = worktree / plan_path
            input_plan = plan_file.read_text(encoding="utf-8")
            loop_manager.begin_agent_scope(
                connection,
                str(repo),
                worktree,
                "reviewer",
                input_fingerprint=input_fingerprint,
            )
            result_plan = input_plan.replace("Test plan.", "Reviewer mutation.")
            plan_file.write_text(result_plan, encoding="utf-8")

            loop_manager.block_active_execplan(
                connection,
                str(repo),
                worktree,
                "reviewer scope violation",
                "",
            )

            plan = connection.execute(
                "SELECT agent_input_plan, agent_result_plan "
                "FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(plan["agent_input_plan"], input_plan)
            self.assertEqual(plan["agent_result_plan"], result_plan)

    def test_blocked_scope_resume_rebases_non_source_remote_and_direct_execplan_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, plan_path = self._repository_with_plan(directory)
            state_dir = root / "state"
            state_dir.mkdir()
            branch, worktree = loop_manager.expected_execplan_workspace(
                state_dir,
                str(repo),
                "G-001",
            )
            loop_manager.make_worktree(repo, worktree, branch, "main")
            plan_sha = git_command(repo, "rev-parse", "origin/main")
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
                (str(repo),),
            )
            loop_manager.register_execplans(
                connection,
                str(repo),
                repo,
                [(plan_path, loop_manager._parse_execplan(repo, repo / plan_path))],
                plan_sha,
                loop_manager.planning_input_fingerprint(repo, "main"),
            )
            plan = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            input_fingerprint = loop_manager._worktree_execplan_input_fingerprint(
                worktree,
                plan,
            )
            plan_file = worktree / plan_path
            original_plan_text = plan_file.read_text(encoding="utf-8")
            before = loop_manager.capture_git_snapshot(worktree)
            reviewer_plan_text = original_plan_text.replace(
                "Test plan.", "Reviewer mutation."
            )
            plan_file.write_text(reviewer_plan_text, encoding="utf-8")
            blocked_worktree_fingerprint = (
                loop_manager._worktree_execplan_input_fingerprint(worktree, plan)
            )
            (worktree / "reviewer.txt").write_text(
                "preserve violation\n",
                encoding="utf-8",
            )
            blocked_result = loop_manager.capture_git_snapshot(worktree)
            with connection:
                connection.execute(
                    """
                    UPDATE exec_plans
                    SET status = 'BLOCKED', active_sha = ?, branch = ?,
                        worktree = ?, workspace_owned = 1,
                        blocked_fingerprint = ?,
                        blocked_worktree_fingerprint = ?,
                        last_error = 'scope blocked'
                    WHERE repo = ? AND plan_id = 'G-001'
                    """,
                    (
                        plan_sha,
                        branch,
                        str(worktree),
                        input_fingerprint,
                        blocked_worktree_fingerprint,
                        str(repo),
                    ),
                )
                connection.execute(
                    """
                    UPDATE projects
                    SET active_sha = ?, plan_sha = ?, phase = 'BLOCKED',
                        branch = ?, worktree = ?, plan_path = ?,
                        last_error = 'scope blocked', agent_role = 'reviewer',
                        agent_snapshot = ?, agent_input_fingerprint = ?,
                        agent_result_snapshot = ?, agent_input_plan = ?,
                        agent_result_plan = ?
                    WHERE repo = ?
                    """,
                    (
                        plan_sha,
                        plan_sha,
                        branch,
                        str(worktree),
                        plan_path,
                        before.serialize(),
                        input_fingerprint,
                        blocked_result.serialize(),
                        original_plan_text,
                        reviewer_plan_text,
                        str(repo),
                    ),
                )

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex"
            ) as blocked_agent:
                unchanged_run = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            persisted_block = connection.execute(
                "SELECT phase, agent_manager_snapshot "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            persisted_plan = connection.execute(
                "SELECT status, agent_snapshot FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(unchanged_run, 0)
            self.assertIsNone(persisted_block["phase"])
            self.assertEqual(persisted_plan["status"], "PLAN_BLOCKED")
            self.assertIsNotNone(persisted_plan["agent_snapshot"])
            blocked_agent.assert_not_called()

            (repo / "docs/notes.md").write_text(
                "# Notes\n\nRemote non-input change.\n",
                encoding="utf-8",
            )
            commit_and_push(repo, "Change non-input documentation")
            remote_head = git_command(repo, "rev-parse", "origin/main")
            self.assertEqual(
                loop_manager._revision_execplan_input_fingerprint(
                    repo,
                    "refs/remotes/origin/main",
                    plan,
                ),
                input_fingerprint,
            )

            user_mixed_plan_text = plan_file.read_text(encoding="utf-8") + (
                "\nUser clarification after scope block.\n"
            )
            plan_file.write_text(user_mixed_plan_text, encoding="utf-8")
            user_plan_text = original_plan_text + "\nUser clarification after scope block.\n"

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.os.replace",
                side_effect=KeyboardInterrupt,
            ), self.assertRaises(KeyboardInterrupt):
                loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            interrupted = connection.execute(
                "SELECT phase, agent_recovery FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(interrupted["phase"], "PLAN_BLOCKED")
            self.assertIsNotNone(interrupted["agent_recovery"])
            recovery = json.loads(interrupted["agent_recovery"])
            recovery_temp = worktree / recovery["plan_temp_path"]
            self.assertEqual(
                plan_file.read_text(encoding="utf-8"),
                original_plan_text,
            )
            self.assertTrue(recovery_temp.is_file())
            recovery_temp.write_bytes(b"partial interrupted contents")
            self.assertFalse((worktree / "reviewer.txt").exists())

            def resumed_worker(agent_worktree, *_args, **_kwargs):
                self.assertEqual(_args[2], "worker")
                self.assertEqual(
                    (agent_worktree / plan_path).read_text(encoding="utf-8"),
                    user_plan_text,
                )
                self.assertNotIn(
                    "Reviewer mutation.",
                    (agent_worktree / plan_path).read_text(encoding="utf-8"),
                )
                self.assertEqual(
                    (agent_worktree / "docs/notes.md").read_text(
                        encoding="utf-8"
                    ),
                    "# Notes\n\nRemote non-input change.\n",
                )
                self.assertEqual(
                    git_command(
                        agent_worktree,
                        "merge-base",
                        "--is-ancestor",
                        remote_head,
                        "HEAD",
                    ),
                    "",
                )
                self.assertFalse((agent_worktree / "reviewer.txt").exists())
                return loop_manager.CodexResult(
                    1,
                    "stop after combined input recovery",
                    "worker-session",
                )

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex", side_effect=resumed_worker
            ) as resumed_agent:
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT phase, last_error, agent_snapshot "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            resumed_plan = connection.execute(
                "SELECT status, blocked_fingerprint, blocked_worktree_fingerprint "
                "FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(result, 1)
            self.assertEqual(project["phase"], "WORKING")
            self.assertEqual(
                project["last_error"],
                "stop after combined input recovery",
            )
            self.assertIsNone(project["agent_snapshot"])
            self.assertEqual(resumed_plan["status"], "IMPLEMENTING")
            self.assertIsNone(resumed_plan["blocked_fingerprint"])
            self.assertIsNone(resumed_plan["blocked_worktree_fingerprint"])
            self.assertEqual(plan_file.read_text(encoding="utf-8"), user_plan_text)
            self.assertFalse((worktree / "reviewer.txt").exists())
            self.assertIn(
                "loop-manager scope violation G-001",
                git_command(worktree, "stash", "list"),
            )
            stashed_plan = git_command(
                worktree, "show", f"stash@{{0}}:{plan_path}"
            )
            self.assertIn("Reviewer mutation.", stashed_plan)
            resumed_agent.assert_called_once()
            self.assertEqual(resumed_agent.call_args.args[3], "worker")

    def test_verify_mutation_of_worker_protected_file_blocks_before_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, plan_path = self._repository_with_plan(directory)
            state_dir = root / "state"
            state_dir.mkdir()
            branch, worktree = loop_manager.expected_execplan_workspace(
                state_dir, str(repo), "G-001"
            )
            loop_manager.make_worktree(repo, worktree, branch, "main")
            (worktree / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            git_command(worktree, "add", "src/app.py")
            git_command(worktree, "commit", "-m", "Prepare Worker candidate")
            plan_sha = git_command(repo, "rev-parse", "origin/main")
            initial_head = git_command(worktree, "rev-parse", "HEAD")
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                """
                INSERT INTO projects (
                    repo, base_branch, active_sha, plan_sha, phase, branch,
                    worktree, plan_path, worker_result
                ) VALUES (?, 'main', ?, ?, 'VERIFYING', ?, ?, ?, ?)
                """,
                (
                    str(repo),
                    plan_sha,
                    plan_sha,
                    branch,
                    str(worktree),
                    plan_path,
                    worker_result(),
                ),
            )
            connection.commit()

            def verify_with_protected_mutation(agent_worktree, timeout_seconds):
                self.assertEqual(timeout_seconds, 600)
                (agent_worktree / "docs/requirements/Active.md").write_text(
                    "# changed by verify\n",
                    encoding="utf-8",
                )
                return True, "verify: PASS"

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.verify", side_effect=verify_with_protected_mutation
            ) as verify, mock.patch(
                "loop_manager.run_codex"
            ) as agent:
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT phase, last_error, verify_result, agent_role, "
                "agent_snapshot, agent_result_snapshot "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan = connection.execute(
                "SELECT status, last_error, verify_result, agent_role, "
                "agent_snapshot, agent_result_snapshot FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()

            self.assertEqual(result, 0)
            self.assertIsNone(project["phase"])
            self.assertIn("worker Git 범위 위반", plan["last_error"])
            self.assertIn(
                "docs/requirements/Active.md",
                plan["last_error"],
            )
            self.assertEqual(plan["verify_result"], "verify: PASS")
            self.assertEqual(plan["agent_role"], "worker")
            self.assertIsNotNone(plan["agent_snapshot"])
            self.assertIsNotNone(plan["agent_result_snapshot"])
            self.assertEqual(plan["status"], "PLAN_BLOCKED")
            self.assertEqual(
                git_command(worktree, "rev-parse", "HEAD"),
                initial_head,
            )
            self.assertEqual(
                (worktree / "docs/requirements/Active.md").read_text(
                    encoding="utf-8"
                ),
                "# changed by verify\n",
            )
            verify.assert_called_once_with(worktree, 600)
            agent.assert_not_called()

    def test_rebase_verify_allowed_change_is_committed_before_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, plan_path = self._repository_with_plan(directory)
            state_dir = root / "state"
            state_dir.mkdir()
            branch, worktree = loop_manager.expected_execplan_workspace(
                state_dir, str(repo), "G-001"
            )
            loop_manager.make_worktree(repo, worktree, branch, "main")
            plan_sha = git_command(repo, "rev-parse", "origin/main")
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                """
                INSERT INTO projects (
                    repo, base_branch, active_sha, plan_sha, phase, branch,
                    worktree, plan_path
                ) VALUES (?, 'main', ?, ?, 'WORKING', ?, ?, ?)
                """,
                (
                    str(repo),
                    plan_sha,
                    plan_sha,
                    branch,
                    str(worktree),
                    plan_path,
                ),
            )
            connection.commit()

            (repo / "README.md").write_text(
                "# base advanced\n",
                encoding="utf-8",
            )
            commit_and_push(repo, "Advance base before worker publication")
            roles = []
            verify_calls = []

            def agent(agent_worktree, _state, _config, role_name, _prompt, **_kwargs):
                roles.append(role_name)
                if role_name == "worker":
                    (agent_worktree / "src/app.py").write_text(
                        "VALUE = 2\n",
                        encoding="utf-8",
                    )
                    return loop_manager.CodexResult(
                        0,
                        worker_result("Persist rebased verify output"),
                        "worker-session",
                    )
                self.assertEqual(role_name, "reviewer")
                self.assertEqual(
                    git_command(agent_worktree, "status", "--porcelain"),
                    "",
                )
                self.assertEqual(
                    git_command(
                        agent_worktree,
                        "show",
                        "HEAD:verify-output.txt",
                    ),
                    "created after rebase",
                )
                return 1, "stop after publication check"

            def verify_after_rebase(agent_worktree, timeout_seconds):
                self.assertEqual(timeout_seconds, 600)
                verify_calls.append(agent_worktree)
                if len(verify_calls) == 1:
                    (agent_worktree / "verify-output.txt").write_text(
                        "created after rebase\n",
                        encoding="utf-8",
                    )
                return True, f"verify pass {len(verify_calls)}"

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex", side_effect=agent
            ), mock.patch(
                "loop_manager.verify", side_effect=verify_after_rebase
            ):
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            published_paths = git_command(
                worktree,
                "diff",
                "--name-only",
                "origin/main...HEAD",
            ).splitlines()

            self.assertEqual(result, 1)
            self.assertEqual(roles, ["worker", "reviewer"])
            self.assertEqual(len(verify_calls), 2)
            self.assertEqual(project["phase"], "REVIEWING")
            self.assertIn("stop after publication check", project["last_error"])
            self.assertIn("src/app.py", published_paths)
            self.assertIn("verify-output.txt", published_paths)
            self.assertEqual(
                git_command(worktree, "status", "--porcelain"),
                "",
            )
            self.assertEqual(
                git_command(worktree, "log", "-1", "--pretty=%s"),
                "Persist rebased verify output",
            )



class WorkspaceRecoveryTest(unittest.TestCase):
    def test_planning_git_port_only_translates_workspace_ownership_errors(self):
        connection = sqlite3.connect(":memory:")
        port = loop_manager._PlanningGitPorts(
            connection,
            Path("/repo"),
            Path("/state"),
        )
        arguments = (
            Path("/repo"),
            Path("/planning"),
            "main",
        )
        keywords = {
            "allow_create": True,
            "allow_existing": False,
            "ownership_reserved": False,
            "expected_head": "a" * 40,
            "before_create": lambda: None,
        }

        with mock.patch(
            "loop_manager.make_planning_worktree",
            side_effect=loop_manager.GitLockError("index.lock exists"),
        ), self.assertRaises(loop_manager.GitLockError):
            port.make_planning_worktree(*arguments, **keywords)

        with mock.patch(
            "loop_manager.make_planning_worktree",
            side_effect=loop_manager.WorkspaceOwnershipError("wrong owner"),
        ), self.assertRaisesRegex(
            loop_manager.PlanningWorkspaceOwnershipError,
            "wrong owner",
        ):
            port.make_planning_worktree(*arguments, **keywords)

    def _workspace(
        self, directory: str
    ) -> tuple[Path, Path, str, Path]:
        root = Path(directory)
        repo = make_remote_repository(directory)
        state_dir = root / "state"
        branch, worktree = loop_manager.expected_execplan_workspace(
            state_dir, str(repo), "G-012"
        )
        loop_manager.make_worktree(repo, worktree, branch, "main")
        return repo, state_dir, branch, worktree

    def _ready_execplan(self, directory: str):
        repo = make_remote_repository(directory)
        state_dir = Path(directory) / "state"
        (repo / "docs/plans").mkdir()
        plan = write_execplan(repo, "G-012", "Recovery Safety")
        commit_and_push(repo, "Add recovery safety plan")
        plan_sha = git_command(repo, "rev-parse", "origin/main")
        connection = sqlite3.connect(":memory:")
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
        branch, worktree = loop_manager.expected_execplan_workspace(
            state_dir, str(repo), "G-012"
        )
        return repo, state_dir, connection, plan, plan_sha, branch, worktree

    def test_execplan_reservation_rejects_branch_inserted_before_create(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                _plan,
                plan_sha,
                branch,
                worktree,
            ) = self._ready_execplan(directory)
            real_git = loop_manager.git

            def interrupt_before_worktree_add(target, *arguments, **kwargs):
                if arguments[:2] == ("worktree", "add") and "-b" in arguments:
                    raise KeyboardInterrupt(
                        "interrupt before ExecPlan worktree creation"
                    )
                return real_git(target, *arguments, **kwargs)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.git", side_effect=interrupt_before_worktree_add
            ), mock.patch(
                "loop_manager.run_codex"
            ) as interrupted_agent:
                with self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "before ExecPlan worktree creation",
                ):
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )

            reserved = connection.execute(
                "SELECT workspace_owned FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-012'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(
                reserved["workspace_owned"],
                loop_manager.WORKSPACE_RESERVED,
            )
            self.assertFalse(worktree.exists())
            self.assertEqual(
                loop_manager.git(
                    repo,
                    "show-ref",
                    "--verify",
                    "--quiet",
                    f"refs/heads/{branch}",
                    check=False,
                ).returncode,
                1,
            )
            interrupted_agent.assert_not_called()

            git_command(
                repo,
                "worktree",
                "add",
                "-b",
                branch,
                str(worktree),
                "origin/main",
            )
            (worktree / "external.txt").write_text(
                "not owned by Loop Manager\n",
                encoding="utf-8",
            )
            git_command(worktree, "add", "external.txt")
            git_command(worktree, "commit", "-m", "Create external branch state")
            external_head = git_command(worktree, "rev-parse", "HEAD")
            git_command(repo, "worktree", "remove", str(worktree))
            self.assertFalse(worktree.exists())
            self.assertNotEqual(external_head, plan_sha)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex"
            ) as resumed_agent:
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            blocked = connection.execute(
                "SELECT status, workspace_owned FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-012'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(result, 1)
            self.assertEqual(project["phase"], "PROJECT_BLOCKED")
            self.assertIn(
                "예약 ExecPlan branch가 저장된 기준 commit과 다릅니다",
                project["last_error"],
            )
            self.assertEqual(
                tuple(blocked),
                ("IMPLEMENTING", loop_manager.WORKSPACE_RESERVED),
            )
            self.assertFalse(worktree.exists())
            self.assertEqual(
                git_command(repo, "rev-parse", f"refs/heads/{branch}"),
                external_head,
            )
            self.assertEqual(
                git_command(repo, "show", f"{branch}:external.txt"),
                "not owned by Loop Manager",
            )
            resumed_agent.assert_not_called()

    def test_planning_reservation_preserves_clean_external_path_on_input_change(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            real_git = loop_manager.git

            def interrupt_before_worktree_add(target, *arguments, **kwargs):
                if arguments[:3] == ("worktree", "add", "--detach"):
                    raise KeyboardInterrupt(
                        "interrupt before planning worktree creation"
                    )
                return real_git(target, *arguments, **kwargs)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.git", side_effect=interrupt_before_worktree_add
            ), mock.patch(
                "loop_manager.run_codex"
            ) as interrupted_agent:
                with self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "before planning worktree creation",
                ):
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )

            reserved = connection.execute(
                "SELECT active_sha, phase, worktree, planning_workspace_owned "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            planning_worktree = Path(reserved["worktree"])
            self.assertEqual(reserved["phase"], "PLANNING")
            self.assertEqual(
                reserved["planning_workspace_owned"],
                loop_manager.WORKSPACE_RESERVED,
            )
            self.assertFalse(planning_worktree.exists())
            interrupted_agent.assert_not_called()

            git_command(
                repo,
                "worktree",
                "add",
                "--detach",
                str(planning_worktree),
                reserved["active_sha"],
            )
            self.assertEqual(
                git_command(planning_worktree, "status", "--porcelain"),
                "",
            )
            (repo / "docs/PRODUCT.md").write_text(
                "# changed after planning reservation\n",
                encoding="utf-8",
            )
            commit_and_push(repo, "Change input after planning reservation")

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.run_codex"
            ) as resumed_agent:
                first_result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )
                second_result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            preserved = connection.execute(
                "SELECT phase, planning_error, planning_workspace_owned "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual((first_result, second_result), (1, 1))
            self.assertEqual(preserved["phase"], "PLANNING")
            self.assertIn(
                "planning workspace cleanup 소유권 검증 실패",
                preserved["planning_error"],
            )
            self.assertIn(
                "planning worktree의 Loop Manager 소유권이 확인되지 않았습니다",
                preserved["planning_error"],
            )
            self.assertEqual(
                preserved["planning_workspace_owned"],
                loop_manager.WORKSPACE_RESERVED,
            )
            self.assertTrue(planning_worktree.is_dir())
            self.assertEqual(
                git_command(planning_worktree, "rev-parse", "HEAD"),
                reserved["active_sha"],
            )
            self.assertEqual(
                git_command(planning_worktree, "status", "--porcelain"),
                "",
            )
            resumed_agent.assert_not_called()


    def test_planning_workspace_reservation_survives_post_create_interruption(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            real_make_planning_worktree = loop_manager.make_planning_worktree

            def interrupt_after_create(*args, **kwargs):
                real_make_planning_worktree(*args, **kwargs)
                raise KeyboardInterrupt("interrupt after planning worktree creation")

            with mock.patch(
                "loop_manager.validate_repository",
                return_value=repo,
            ), mock.patch(
                "loop_manager.remote_base",
                return_value="main",
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.make_planning_worktree",
                side_effect=interrupt_after_create,
            ), mock.patch(
                "loop_manager.run_codex"
            ) as interrupted_agent:
                with self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "planning worktree creation",
                ):
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )

            interrupted = connection.execute(
                "SELECT phase, worktree, planning_workspace_owned "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(interrupted["phase"], "PLANNING")
            self.assertEqual(
                interrupted["planning_workspace_owned"],
                loop_manager.WORKSPACE_RESERVED,
            )
            self.assertTrue(Path(interrupted["worktree"]).is_dir())
            interrupted_agent.assert_not_called()

            with mock.patch(
                "loop_manager.validate_repository",
                return_value=repo,
            ), mock.patch(
                "loop_manager.remote_base",
                return_value="main",
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.run_codex",
                return_value=loop_manager.CodexResult(
                    1,
                    "stop after planning reservation recovery",
                    "planner-session",
                ),
            ) as resumed_agent:
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            resumed = connection.execute(
                "SELECT phase, planning_error, planning_workspace_owned "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(result, 1)
            self.assertEqual(resumed["phase"], "PLANNING")
            self.assertIsNone(resumed["planning_error"])
            self.assertEqual(
                resumed["planning_workspace_owned"],
                loop_manager.WORKSPACE_OWNED,
            )
            resumed_agent.assert_called_once()
            self.assertEqual(resumed_agent.call_args.args[3], "planner")

    def test_execplan_workspace_reservation_survives_post_create_interruption(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                _plan,
                _plan_sha,
                _branch,
                worktree,
            ) = self._ready_execplan(directory)
            real_make_worktree = loop_manager.make_worktree

            def interrupt_after_create(*args, **kwargs):
                real_make_worktree(*args, **kwargs)
                raise KeyboardInterrupt("interrupt after ExecPlan worktree creation")

            with mock.patch(
                "loop_manager.validate_repository",
                return_value=repo,
            ), mock.patch(
                "loop_manager.remote_base",
                return_value="main",
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.make_worktree",
                side_effect=interrupt_after_create,
            ), mock.patch(
                "loop_manager.run_codex"
            ) as interrupted_agent:
                with self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "ExecPlan worktree creation",
                ):
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )

            interrupted = connection.execute(
                "SELECT phase FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            interrupted_plan = connection.execute(
                "SELECT status, workspace_owned FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-012'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(interrupted["phase"], "WORKING")
            self.assertEqual(
                tuple(interrupted_plan),
                ("IMPLEMENTING", loop_manager.WORKSPACE_RESERVED),
            )
            self.assertTrue(worktree.is_dir())
            interrupted_agent.assert_not_called()

            with mock.patch(
                "loop_manager.validate_repository",
                return_value=repo,
            ), mock.patch(
                "loop_manager.remote_base",
                return_value="main",
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch",
                return_value=None,
            ), mock.patch(
                "loop_manager.run_codex",
                return_value=loop_manager.CodexResult(
                    1,
                    "stop after ExecPlan reservation recovery",
                    "worker-session",
                ),
            ) as resumed_agent:
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            resumed = connection.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            resumed_plan = connection.execute(
                "SELECT status, workspace_owned FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-012'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(result, 1)
            self.assertEqual(resumed["phase"], "WORKING")
            self.assertEqual(
                resumed["last_error"],
                "stop after ExecPlan reservation recovery",
            )
            self.assertEqual(
                tuple(resumed_plan),
                ("IMPLEMENTING", loop_manager.WORKSPACE_OWNED),
            )
            resumed_agent.assert_called_once()
            self.assertEqual(resumed_agent.call_args.args[3], "worker")

    def test_existing_catalog_does_not_infer_workspace_ownership(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            CREATE TABLE exec_plans (
                repo TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                status TEXT NOT NULL,
                branch TEXT,
                worktree TEXT,
                blocked_fingerprint TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO exec_plans "
            "(repo, plan_id, status, branch, worktree, blocked_fingerprint) "
            "VALUES ('repo', 'G-012', 'BLOCKED', 'loop-manager/G-012', "
            "'managed', 'fingerprint')"
        )

        loop_manager._ensure_execplans_schema(connection)

        row = connection.execute(
            "SELECT workspace_owned, blocked_worktree_fingerprint "
            "FROM exec_plans WHERE repo = 'repo'"
        ).fetchone()
        self.assertEqual(tuple(row), (0, None))

    def test_missing_worktree_is_recovered_from_owned_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _state_dir, branch, worktree = self._workspace(directory)
            original_head = git_command(worktree, "rev-parse", "HEAD")
            git_command(repo, "worktree", "remove", str(worktree))

            outcome = loop_manager.make_worktree(
                repo,
                worktree,
                branch,
                "main",
                allow_create_branch=False,
            )

            self.assertEqual(outcome, "recovered")
            self.assertEqual(git_command(worktree, "rev-parse", "HEAD"), original_head)
            loop_manager.validate_managed_worktree(repo, worktree, branch)

    def test_interrupted_worker_recovers_missing_owned_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                plan,
                plan_sha,
                branch,
                worktree,
            ) = self._ready_execplan(directory)
            loop_manager.make_worktree(repo, worktree, branch, "main")
            before = loop_manager.capture_git_snapshot(worktree)
            with connection:
                connection.execute(
                    """
                    UPDATE exec_plans
                    SET status = 'IMPLEMENTING', active_sha = ?, branch = ?,
                        worktree = ?, workspace_owned = 1,
                        worker_session_id = 'worker-session',
                        agent_role = 'worker', agent_snapshot = ?
                    WHERE repo = ? AND plan_id = 'G-012'
                    """,
                    (
                        plan_sha,
                        branch,
                        str(worktree),
                        before.serialize(),
                        str(repo),
                    ),
                )
                connection.execute(
                    """
                    UPDATE projects
                    SET active_sha = ?, plan_sha = ?, phase = 'WORKING',
                        branch = ?, worktree = ?, plan_path = ?,
                        worker_session_id = 'worker-session',
                        agent_role = 'worker', agent_snapshot = ?
                    WHERE repo = ?
                    """,
                    (
                        plan_sha,
                        plan_sha,
                        branch,
                        str(worktree),
                        plan.relative_to(repo).as_posix(),
                        before.serialize(),
                        str(repo),
                    ),
                )

            git_command(repo, "worktree", "remove", str(worktree))
            sessions = []

            def resumed_worker(
                _worktree, _state, _config, role_name, _prompt, **kwargs
            ):
                self.assertEqual(role_name, "worker")
                sessions.append(kwargs["session_id"])
                return loop_manager.CodexResult(
                    1, "stop after recovery", "worker-session"
                )

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex", side_effect=resumed_worker
            ):
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT phase, last_error, agent_snapshot FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan_status = connection.execute(
                "SELECT status FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-012'",
                (str(repo),),
            ).fetchone()["status"]

            self.assertEqual(result, 1)
            self.assertEqual(sessions, ["worker-session"])
            self.assertTrue(worktree.is_dir())
            loop_manager.validate_managed_worktree(repo, worktree, branch)
            self.assertEqual(project["phase"], "WORKING")
            self.assertEqual(project["last_error"], "stop after recovery")
            self.assertIsNone(project["agent_snapshot"])
            self.assertEqual(plan_status, "IMPLEMENTING")

    def test_rebase_conflict_resumes_same_worker_session(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                plan,
                plan_sha,
                branch,
                worktree,
            ) = self._ready_execplan(directory)
            loop_manager.make_worktree(repo, worktree, branch, "main")
            with connection:
                connection.execute(
                    """
                    UPDATE exec_plans
                    SET status = 'VERIFYING', active_sha = ?, branch = ?,
                        worktree = ?, workspace_owned = 1,
                        worker_session_id = 'worker-session',
                        worker_result = ?
                    WHERE repo = ? AND plan_id = 'G-012'
                    """,
                    (
                        plan_sha,
                        branch,
                        str(worktree),
                        worker_result(),
                        str(repo),
                    ),
                )
                connection.execute(
                    """
                    UPDATE projects
                    SET active_sha = ?, plan_sha = ?, phase = 'VERIFYING',
                        branch = ?, worktree = ?, plan_path = ?,
                        worker_session_id = 'worker-session',
                        worker_result = ?
                    WHERE repo = ?
                    """,
                    (
                        plan_sha,
                        plan_sha,
                        branch,
                        str(worktree),
                        plan.relative_to(repo).as_posix(),
                        worker_result(),
                        str(repo),
                    ),
                )

            (worktree / "src/app.py").write_text(
                "VALUE = 'worker'\n",
                encoding="utf-8",
            )
            (repo / "src/app.py").write_text(
                "VALUE = 'base'\n",
                encoding="utf-8",
            )
            commit_and_push(repo, "Advance base with conflicting source")

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.verify", return_value=(True, "verify before rebase")
            ), mock.patch(
                "loop_manager.run_codex"
            ) as unexpected_agent:
                first_result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            conflicted = connection.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(first_result, 1)
            self.assertEqual(conflicted["phase"], "WORKING")
            self.assertTrue(
                conflicted["last_error"].startswith(
                    loop_manager.REBASE_CONFLICT_PREFIX
                )
            )
            self.assertTrue(
                loop_manager._rebase_in_progress_for_branch(worktree, branch)
            )
            self.assertEqual(git_command(worktree, "branch", "--show-current"), "")
            unexpected_agent.assert_not_called()

            sessions = []

            def resolve_conflict(
                agent_worktree, _state, _config, role_name, _prompt, **kwargs
            ):
                self.assertEqual(role_name, "worker")
                sessions.append(kwargs["session_id"])
                if len(sessions) == 1:
                    (agent_worktree / "src/app.py").write_text(
                        "VALUE = 'resolved'\n",
                        encoding="utf-8",
                    )
                    return loop_manager.CodexResult(
                        0,
                        worker_result(),
                        "worker-session",
                    )
                return loop_manager.CodexResult(
                    1,
                    "stop after same-run verify recovery",
                    "worker-session",
                )

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.verify",
                return_value=(False, "stop after rebase resolution"),
            ), mock.patch(
                "loop_manager.run_codex", side_effect=resolve_conflict
            ):
                resumed_result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            resumed = connection.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            checkpoint = connection.execute(
                "SELECT operation, snapshot FROM execution_checkpoints "
                "WHERE repo = ? AND plan_id = 'G-012'",
                (str(repo),),
            ).fetchone()
            recovery = connection.execute(
                "SELECT subject, disposition, detail FROM recovery_records "
                "WHERE repo = ? ORDER BY updated_at DESC LIMIT 1",
                (str(repo),),
            ).fetchone()
            self.assertEqual(
                resumed_result, 1,
                (sessions, resumed["phase"], resumed["last_error"],
                 dict(checkpoint) if checkpoint else None,
                 dict(recovery) if recovery else None),
            )
            self.assertEqual(
                sessions, ["worker-session", "worker-session"]
            )
            self.assertEqual(resumed["phase"], "WORKING")
            self.assertEqual(
                resumed["last_error"],
                "stop after same-run verify recovery",
            )
            self.assertFalse(
                loop_manager._rebase_in_progress_for_branch(worktree, branch)
            )
            self.assertEqual(
                git_command(worktree, "branch", "--show-current"),
                branch,
            )
            self.assertEqual(
                (worktree / "src/app.py").read_text(encoding="utf-8"),
                "VALUE = 'resolved'\n",
            )


    def test_missing_worktree_and_branch_blocks_without_recreation(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, branch, worktree = self._workspace(directory)
            git_command(repo, "worktree", "remove", str(worktree))
            git_command(repo, "branch", "-D", branch)

            with self.assertRaisesRegex(
                loop_manager.WorkspaceOwnershipError,
                "모두 복구할 수 없습니다",
            ):
                loop_manager.make_worktree(
                    repo,
                    worktree,
                    branch,
                    "main",
                    allow_create_branch=False,
                )

            self.assertFalse(worktree.exists())
            self.assertNotEqual(
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repo),
                        "show-ref",
                        "--verify",
                        "--quiet",
                        f"refs/heads/{branch}",
                    ]
                ).returncode,
                0,
            )

            (repo / "docs/plans").mkdir()
            plan = write_execplan(repo, "G-012", "Recovery Safety")
            commit_and_push(repo, "Add recovery safety plan")
            plan_sha = git_command(repo, "rev-parse", "origin/main")
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                """
                INSERT INTO projects (
                    repo, base_branch, active_sha, plan_sha, phase, branch,
                    worktree, plan_path
                ) VALUES (?, 'main', ?, ?, 'WORKING', ?, ?, ?)
                """,
                (
                    str(repo),
                    plan_sha,
                    plan_sha,
                    branch,
                    str(worktree),
                    plan.relative_to(repo).as_posix(),
                ),
            )
            connection.commit()

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex"
            ) as run_agent:
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            blocked = connection.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()

            plan_status = connection.execute(
                "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-012'",
                (str(repo),),
            ).fetchone()["status"]

            self.assertEqual(result, 1)
            self.assertEqual(blocked["phase"], "PROJECT_BLOCKED")
            self.assertIn("모두 복구할 수 없습니다", blocked["last_error"])
            self.assertEqual(plan_status, "IMPLEMENTING")
            run_agent.assert_not_called()


    def test_new_workspace_rejects_preexisting_path(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _state_dir, _connection, _plan, _plan_sha, branch, worktree = (
                self._ready_execplan(directory)
            )
            worktree.mkdir(parents=True)
            marker = worktree / "unrelated.txt"
            marker.write_text("preserve me\n", encoding="utf-8")

            with self.assertRaisesRegex(
                loop_manager.WorkspaceOwnershipError,
                "worktree가 이미 존재",
            ):
                loop_manager.make_worktree(
                    repo,
                    worktree,
                    branch,
                    "main",
                    allow_create_branch=True,
                    allow_recovery=False,
                    allow_existing=False,
                )

            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve me\n")

    def test_branch_collision_blocks_then_resumes_after_conflict_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                _plan,
                _plan_sha,
                branch,
                worktree,
            ) = self._ready_execplan(directory)
            git_command(repo, "branch", branch, "origin/main")
            branch_head = git_command(repo, "rev-parse", branch)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.run_codex"
            ) as run_agent:
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan_status = connection.execute(
                "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-012'",
                (str(repo),),
            ).fetchone()["status"]

            self.assertEqual(result, 1)
            self.assertEqual(project["phase"], "PROJECT_BLOCKED")
            self.assertIn("이미 존재해 소유권을 증명할 수 없습니다", project["last_error"])
            self.assertEqual(plan_status, "IMPLEMENTING")
            self.assertEqual(git_command(repo, "rev-parse", branch), branch_head)
            self.assertFalse(worktree.exists())
            run_agent.assert_not_called()

            ownership = connection.execute(
                "SELECT workspace_owned FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-012'",
                (str(repo),),
            ).fetchone()["workspace_owned"]
            self.assertEqual(ownership, 0)

            (repo / "docs/PRODUCT.md").write_text(
                "# Resolve blocked input\n",
                encoding="utf-8",
            )
            commit_and_push(repo, "Change blocked plan input")
            with connection:
                connection.execute(
                    "UPDATE projects SET specification_fingerprint = ? "
                    "WHERE repo = ?",
                    (
                        loop_manager.specification_input_fingerprint(repo, "main"),
                        str(repo),
                    ),
                )
            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex"
            ) as resumed_agent:
                resumed_result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            resumed_project = connection.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            resumed_plan = connection.execute(
                "SELECT status, workspace_owned FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-012'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(resumed_result, 1)
            self.assertEqual(resumed_project["phase"], "PROJECT_BLOCKED")
            self.assertIn(
                "이미 존재해 소유권을 증명할 수 없습니다",
                resumed_project["last_error"],
            )
            self.assertEqual(tuple(resumed_plan), ("IMPLEMENTING", 0))
            self.assertEqual(git_command(repo, "rev-parse", branch), branch_head)
            self.assertFalse(worktree.exists())
            resumed_agent.assert_not_called()

            git_command(repo, "branch", "-D", branch)
            planner_result = loop_manager.CodexResult(0, "", "planner-session")
            worker_stop = loop_manager.CodexResult(
                1, "stop after ownership recovery", "worker-session"
            )
            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex", side_effect=[planner_result, worker_stop]
            ) as recovered_agent:
                recovered_result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            recovered_project = connection.execute(
                "SELECT phase, branch, worktree, last_error "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            recovered_plan = connection.execute(
                "SELECT status, workspace_owned FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-012'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(recovered_result, 1)
            self.assertEqual(recovered_project["phase"], "WORKING")
            self.assertEqual(recovered_project["branch"], branch)
            self.assertEqual(
                recovered_project["last_error"],
                "stop after ownership recovery",
            )
            self.assertEqual(tuple(recovered_plan), ("IMPLEMENTING", 1))
            self.assertTrue(worktree.exists())
            self.assertEqual(
                loop_manager.git(
                    repo,
                    "show-ref",
                    "--verify",
                    "--quiet",
                    f"refs/heads/{branch}",
                    check=False,
                ).returncode,
                0,
            )
            self.assertEqual(
                [call.args[3] for call in recovered_agent.call_args_list],
                ["planner", "worker"],
            )

    def test_new_planning_rejects_preexisting_detached_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_remote_repository(directory)
            state_dir = root / "state"
            fingerprint = loop_manager.planning_input_fingerprint(repo, "main")
            project_id = hashlib.sha256(str(repo).encode()).hexdigest()[:16]
            planning_worktree = (
                state_dir
                / "worktrees"
                / project_id
                / "planning"
                / fingerprint
            )
            planning_worktree.parent.mkdir(parents=True)
            git_command(
                repo,
                "worktree",
                "add",
                "--detach",
                str(planning_worktree),
                "origin/main",
            )
            marker = planning_worktree / "keep.txt"
            marker.write_text("preserve me\n", encoding="utf-8")
            original_head = git_command(planning_worktree, "rev-parse", "HEAD")
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
                (str(repo),),
            )
            connection.commit()

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.run_codex"
            ) as run_agent:
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT phase, planning_error, planning_workspace_owned "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(result, 1)
            self.assertEqual(project["phase"], "PLANNING")
            self.assertIn(
                "새 planning worktree 경로가 이미 존재",
                project["planning_error"],
            )
            self.assertEqual(project["planning_workspace_owned"], 0)
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve me\n")
            self.assertEqual(
                git_command(planning_worktree, "rev-parse", "HEAD"),
                original_head,
            )
            run_agent.assert_not_called()

            (repo / "docs/PRODUCT.md").write_text(
                "# Changed planning input\n",
                encoding="utf-8",
            )
            commit_and_push(repo, "Change planning input")
            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.run_codex"
            ) as resumed_agent:
                resumed_result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            resumed = connection.execute(
                "SELECT planning_error, planning_workspace_owned "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(resumed_result, 1)
            self.assertIn(
                "planning worktree의 Loop Manager 소유권이 확인되지 않았습니다",
                resumed["planning_error"],
            )
            self.assertEqual(resumed["planning_workspace_owned"], 0)
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve me\n")
            self.assertEqual(
                git_command(planning_worktree, "rev-parse", "HEAD"),
                original_head,
            )
            resumed_agent.assert_not_called()

    def test_started_planning_worktree_is_not_recreated_from_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_remote_repository(directory)
            missing = root / "state/worktrees/project/planning/fingerprint"

            with self.assertRaisesRegex(
                loop_manager.WorkspaceOwnershipError,
                "복구할 branch가 없습니다",
            ):
                loop_manager.make_planning_worktree(
                    repo,
                    missing,
                    "main",
                    allow_create=False,
                )

            self.assertFalse(missing.exists())


    def test_changed_planning_input_preserves_unowned_saved_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_remote_repository(directory)
            state_dir = root / "state"
            victim = root / "user-worktree"
            victim_branch = "user/planning"
            git_command(
                repo,
                "worktree",
                "add",
                "-b",
                victim_branch,
                str(victim),
                "origin/main",
            )
            marker = victim / "keep.txt"
            marker.write_text("keep\n", encoding="utf-8")
            victim_head = git_command(victim, "rev-parse", "HEAD")
            plan_sha = git_command(repo, "rev-parse", "origin/main")
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                """
                INSERT INTO projects (
                    repo, base_branch, active_sha, phase, worktree,
                    planning_attempt_fingerprint, planning_validation_error
                ) VALUES (?, 'main', ?, 'PLANNING', ?, 'stale-input', 'invalid')
                """,
                (str(repo), plan_sha, str(victim)),
            )
            connection.commit()

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.run_codex"
            ) as run_agent:
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT phase, planning_error, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(result, 1)
            self.assertEqual(project["phase"], "PLANNING")
            self.assertIn(
                "planning workspace cleanup 소유권 검증 실패",
                project["planning_error"],
            )
            self.assertEqual(project["last_error"], project["planning_error"])
            self.assertTrue(marker.is_file())
            self.assertEqual(git_command(victim, "rev-parse", "HEAD"), victim_head)
            loop_manager.validate_managed_worktree(repo, victim, victim_branch)
            run_agent.assert_not_called()

    def test_wrong_branch_and_wrong_deterministic_path_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_remote_repository(directory)
            state_dir = root / "state"
            branch, worktree = loop_manager.expected_execplan_workspace(
                state_dir, str(repo), "G-012"
            )
            git_command(
                repo,
                "worktree",
                "add",
                "-b",
                "loop-manager/other",
                str(worktree),
                "origin/main",
            )

            with self.assertRaisesRegex(
                loop_manager.WorkspaceOwnershipError,
                "branch 불일치",
            ):
                loop_manager.make_worktree(repo, worktree, branch, "main")
            with self.assertRaisesRegex(
                loop_manager.WorkspaceOwnershipError,
                "결정적 소유 경계",
            ):
                loop_manager.validate_execplan_workspace_identity(
                    state_dir,
                    str(repo),
                    "G-012",
                    branch,
                    root / "elsewhere",
                )


    def test_merge_cleanup_preserves_unowned_deterministic_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                plan,
                plan_sha,
                branch,
                worktree,
            ) = self._ready_execplan(directory)
            planning_baseline = connection.execute(
                "SELECT planning_fingerprint, processed_sha FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            loop_manager.make_worktree(repo, worktree, branch, "main")
            (worktree / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            git_command(worktree, "add", "src/app.py")
            git_command(worktree, "commit", "-m", "Unowned implementation")
            pr_head = git_command(worktree, "rev-parse", "HEAD")
            pr_url = "https://example.test/pr/12"
            connection.execute(
                """
                UPDATE exec_plans
                SET status = 'PR_OPEN', active_sha = ?, branch = ?, worktree = ?,
                    pr_url = ?
                WHERE repo = ? AND plan_id = 'G-012'
                """,
                (plan_sha, branch, str(worktree), pr_url, str(repo)),
            )
            connection.execute(
                """
                UPDATE projects
                SET active_sha = ?, plan_sha = ?, phase = 'PR_OPEN',
                    branch = ?, worktree = ?, plan_path = ?, pr_url = ?
                WHERE repo = ?
                """,
                (
                    plan_sha,
                    plan_sha,
                    branch,
                    str(worktree),
                    plan.relative_to(repo).as_posix(),
                    pr_url,
                    str(repo),
                ),
            )
            connection.commit()
            merged_pr = {
                "url": pr_url,
                "state": "MERGED",
                "mergedAt": "2026-01-01T00:00:00Z",
                "headRefOid": pr_head,
            }

            def planner(_worktree, _state, _config, role_name, _prompt, **_kwargs):
                self.assertEqual(role_name, "planner")
                return loop_manager.CodexResult(0, "", "planner-session")

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=merged_pr
            ), mock.patch(
                "loop_manager.run_codex", side_effect=planner
            ) as run_agent:
                merge_result = loop_manager.run_project_result(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )
                after_merge = connection.execute(
                    "SELECT phase, planning_fingerprint, processed_sha "
                    "FROM projects WHERE repo = ?",
                    (str(repo),),
                ).fetchone()
                run_agent.assert_not_called()
                completion_result = loop_manager.run_project_result(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan_status = connection.execute(
                "SELECT status, workspace_owned FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-012'",
                (str(repo),),
            ).fetchone()
            pending = connection.execute(
                "SELECT workspace_owned, reason FROM cleanup_pending WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(merge_result.kind, loop_manager.ProjectRunKind.BLOCKED)
            self.assertEqual(merge_result.exit_code, 1)
            self.assertEqual(
                completion_result.kind,
                loop_manager.ProjectRunKind.BLOCKED,
            )
            self.assertEqual(completion_result.exit_code, 1)
            self.assertEqual(
                tuple(after_merge),
                ("PROJECT_BLOCKED", *tuple(planning_baseline)),
            )
            self.assertEqual(project["phase"], "PROJECT_BLOCKED")
            self.assertIn(
                "소유권이 확인되지 않았습니다",
                project["last_error"],
            )
            self.assertEqual(tuple(plan_status), ("MERGED", 0))
            self.assertEqual(pending["workspace_owned"], 0)
            self.assertIn(
                "Merge cleanup worktree의 Loop Manager 소유권이 확인되지 않았습니다",
                pending["reason"],
            )
            self.assertTrue(worktree.is_dir())
            self.assertEqual(git_command(worktree, "rev-parse", "HEAD"), pr_head)
            run_agent.assert_not_called()

    def test_merge_cleanup_preserves_unowned_saved_path_as_nonblocking_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                repo,
                state_dir,
                connection,
                plan,
                plan_sha,
                branch,
                _worktree,
            ) = self._ready_execplan(directory)
            victim = root / "user-merge-worktree"
            victim_branch = "user/merge"
            git_command(
                repo,
                "worktree",
                "add",
                "-b",
                victim_branch,
                str(victim),
                "origin/main",
            )
            marker = victim / "keep.txt"
            marker.write_text("keep\n", encoding="utf-8")
            git_command(victim, "add", "keep.txt")
            git_command(victim, "commit", "-m", "Keep user worktree")
            victim_head = git_command(victim, "rev-parse", "HEAD")
            pr_url = "https://example.test/pr/12"
            connection.execute(
                """
                UPDATE exec_plans
                SET status = 'PR_OPEN', active_sha = ?, branch = ?, worktree = ?,
                    pr_url = ?
                WHERE repo = ? AND plan_id = 'G-012'
                """,
                (plan_sha, branch, str(victim), pr_url, str(repo)),
            )
            connection.execute(
                """
                UPDATE projects
                SET active_sha = ?, plan_sha = ?, phase = 'PR_OPEN',
                    branch = ?, worktree = ?, plan_path = ?, pr_url = ?
                WHERE repo = ?
                """,
                (
                    plan_sha,
                    plan_sha,
                    branch,
                    str(victim),
                    plan.relative_to(repo).as_posix(),
                    pr_url,
                    str(repo),
                ),
            )
            connection.commit()
            merged_pr = {
                "url": pr_url,
                "state": "MERGED",
                "mergedAt": "2026-01-01T00:00:00Z",
                "headRefOid": victim_head,
            }

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=merged_pr
            ), mock.patch(
                "loop_manager.run_codex"
            ) as run_agent:
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan_status = connection.execute(
                "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-012'",
                (str(repo),),
            ).fetchone()["status"]
            pending = connection.execute(
                "SELECT workspace_owned, reason FROM cleanup_pending WHERE repo = ?",
                (str(repo),),
            ).fetchone()

            self.assertEqual(result, 1)
            self.assertEqual(project["phase"], "PROJECT_BLOCKED")
            self.assertIn(
                "소유권이 확인되지 않았습니다",
                project["last_error"],
            )
            self.assertEqual(plan_status, "MERGED")
            self.assertEqual(pending["workspace_owned"], 0)
            self.assertIn(
                "Merge cleanup worktree의 Loop Manager 소유권이 확인되지 않았습니다",
                pending["reason"],
            )
            self.assertTrue(marker.is_file())
            self.assertEqual(git_command(victim, "rev-parse", "HEAD"), victim_head)
            loop_manager.validate_managed_worktree(repo, victim, victim_branch)
            run_agent.assert_not_called()


class MergeIndependentWorktreeCleanupTest(unittest.TestCase):
    def _managed_worktree(self, root: Path, name: str = "managed"):
        repo = make_remote_repository(str(root))
        worktree = root / name
        branch = f"loop-manager/{name}"
        git_command(repo, "worktree", "add", "-b", branch, str(worktree), "origin/main")
        (worktree / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
        git_command(worktree, "add", "src/app.py")
        git_command(worktree, "commit", "-m", "Implement cleanup fixture")
        return repo, worktree, branch, git_command(worktree, "rev-parse", "HEAD")

    def _memory_connection(self, repo: Path):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        connection.execute(loop_manager.CLEANUP_PENDING_SCHEMA)
        connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
            (str(repo),),
        )
        connection.commit()
        return connection

    def test_ignored_only_worktree_is_removed_after_safe_assessment(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, worktree, _branch, head = self._managed_worktree(Path(directory))
            exclude = Path(git_command(worktree, "rev-parse", "--git-path", "info/exclude"))
            if not exclude.is_absolute():
                exclude = worktree / exclude
            exclude.write_text(
                exclude.read_text(encoding="utf-8") + "\n*.cache\n",
                encoding="utf-8",
            )
            (worktree / "verify.cache").write_text("artifact", encoding="utf-8")
            assessment = loop_manager.assess_cleanup(worktree, head)
            self.assertEqual(assessment.status, "safe")
            self.assertEqual(
                loop_manager.remove_cleanup_worktree(repo, worktree, assessment).status,
                "safe",
            )
            self.assertFalse(worktree.exists())

    def test_planning_cleanup_accepts_ignored_artifact_but_preserves_dirty_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_remote_repository(directory)
            planning = root / "planning"
            git_command(repo, "worktree", "add", "--detach", str(planning), "origin/main")
            exclude = Path(git_command(planning, "rev-parse", "--git-path", "info/exclude"))
            if not exclude.is_absolute():
                exclude = planning / exclude
            exclude.write_text(
                exclude.read_text(encoding="utf-8") + "\n*.cache\n",
                encoding="utf-8",
            )
            (planning / "build.cache").write_text("artifact", encoding="utf-8")
            port = loop_manager._PlanningGitPorts(sqlite3.connect(":memory:"), repo, root)
            (planning / "unexpected.txt").write_text("publishable", encoding="utf-8")
            with self.assertRaises(loop_manager.WorkspaceOwnershipError):
                port.run(repo, "worktree", "remove", str(planning))
            self.assertTrue(planning.exists())
            (planning / "unexpected.txt").unlink()
            port.run(repo, "worktree", "remove", str(planning))
            self.assertFalse(planning.exists())

    def test_rebase_keeps_ignored_file_that_overlaps_target(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            git_command(repo, "checkout", "-b", "target")
            (repo / "collide.txt").write_text("tracked", encoding="utf-8")
            git_command(repo, "add", "collide.txt")
            git_command(repo, "commit", "-m", "Add target file")
            git_command(repo, "checkout", "main")
            exclude = Path(git_command(repo, "rev-parse", "--git-path", "info/exclude"))
            if not exclude.is_absolute():
                exclude = repo / exclude
            exclude.write_text(
                exclude.read_text(encoding="utf-8") + "\ncollide.txt\n",
                encoding="utf-8",
            )
            (repo / "collide.txt").write_text("local ignored", encoding="utf-8")
            head = git_command(repo, "rev-parse", "HEAD")
            result = loop_manager.git(repo, "rebase", "target", check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("collide.txt", result.stdout)
            self.assertEqual((repo / "collide.txt").read_text(encoding="utf-8"), "local ignored")
            self.assertEqual(git_command(repo, "rev-parse", "HEAD"), head)

    def test_rebase_resume_commands_preserve_ignored_next_commit_path(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            onto = git_command(repo, "rev-parse", "HEAD")
            git_command(repo, "checkout", "-b", "feature")
            (repo / "src/app.py").write_text("VALUE = 3\n", encoding="utf-8")
            git_command(repo, "add", "src/app.py")
            git_command(repo, "commit", "-m", "First feature commit")
            (repo / "later.txt").write_text("tracked", encoding="utf-8")
            git_command(repo, "add", "later.txt")
            git_command(repo, "commit", "-m", "Next feature commit")
            original_head = git_command(repo, "rev-parse", "HEAD")
            git_command(repo, "checkout", "main")
            exclude = Path(git_command(repo, "rev-parse", "--git-path", "info/exclude"))
            if not exclude.is_absolute():
                exclude = repo / exclude
            exclude.write_text(
                exclude.read_text(encoding="utf-8") + "\nlater.txt\n",
                encoding="utf-8",
            )
            ignored = repo / "later.txt"
            ignored.write_text("local ignored", encoding="utf-8")
            rebase_dir = Path(
                git_command(repo, "rev-parse", "--git-path", "rebase-merge")
            )
            if not rebase_dir.is_absolute():
                rebase_dir = repo / rebase_dir
            rebase_dir.mkdir()
            (rebase_dir / "orig-head").write_text(original_head, encoding="ascii")
            (rebase_dir / "onto").write_text(onto, encoding="ascii")
            (rebase_dir / "git-rebase-todo").write_text(
                f"pick {original_head} Next feature commit\n", encoding="utf-8"
            )
            for arguments in (
                ("-c", "core.editor=true", "rebase", "--continue"),
                ("rebase", "--skip"),
                ("rebase", "--abort"),
            ):
                with self.subTest(arguments=arguments), mock.patch(
                    "loop_manager.command",
                    side_effect=AssertionError("Git write started"),
                ):
                    result = loop_manager.git(repo, *arguments, check=False)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("later.txt", result.stdout)
                    self.assertEqual(ignored.read_text(encoding="utf-8"), "local ignored")
            ignored.unlink()
            exclude.write_text(
                exclude.read_text(encoding="utf-8") + "\n*.cache\n",
                encoding="utf-8",
            )
            (repo / "unrelated.cache").write_text("keep", encoding="utf-8")
            with mock.patch(
                "loop_manager.command",
                return_value=subprocess.CompletedProcess([], 0, "continued"),
            ) as command_call:
                allowed = loop_manager.git(
                    repo, "-c", "core.editor=true", "rebase", "--continue",
                    check=False,
                )
            self.assertEqual(allowed.returncode, 0)
            command_call.assert_called_once()

    def test_rebase_resume_ignores_paths_from_already_replayed_commits(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            onto = git_command(repo, "rev-parse", "HEAD")
            git_command(repo, "checkout", "-b", "feature")
            (repo / "old.txt").write_text("tracked", encoding="utf-8")
            git_command(repo, "add", "old.txt")
            git_command(repo, "commit", "-m", "Add old path")
            first_commit = git_command(repo, "rev-parse", "HEAD")
            (repo / "old.txt").unlink()
            git_command(repo, "add", "-u")
            git_command(repo, "commit", "-m", "Remove old path")
            second_commit = git_command(repo, "rev-parse", "HEAD")
            (repo / "src/app.py").write_text("VALUE = 3\n", encoding="utf-8")
            git_command(repo, "add", "src/app.py")
            git_command(repo, "commit", "-m", "Change app")
            original_head = git_command(repo, "rev-parse", "HEAD")
            git_command(repo, "checkout", "main")
            exclude = Path(git_command(repo, "rev-parse", "--git-path", "info/exclude"))
            if not exclude.is_absolute():
                exclude = repo / exclude
            exclude.write_text(
                exclude.read_text(encoding="utf-8") + "\nold.txt\n",
                encoding="utf-8",
            )
            ignored = repo / "old.txt"
            ignored.write_text("local ignored", encoding="utf-8")
            rebase_dir = Path(git_command(repo, "rev-parse", "--git-path", "rebase-merge"))
            if not rebase_dir.is_absolute():
                rebase_dir = repo / rebase_dir
            rebase_dir.mkdir()
            (rebase_dir / "orig-head").write_text(original_head, encoding="ascii")
            (rebase_dir / "onto").write_text(onto, encoding="ascii")
            (rebase_dir / "done").write_text(
                f"pick {first_commit} Add old path\npick {second_commit} Remove old path\n",
                encoding="utf-8",
            )
            (rebase_dir / "git-rebase-todo").write_text(
                f"pick {original_head} Change app\n", encoding="utf-8"
            )
            for arguments in (("rebase", "--continue"), ("rebase", "--skip")):
                with self.subTest(arguments=arguments), mock.patch(
                    "loop_manager.command",
                    return_value=subprocess.CompletedProcess([], 0, "resumed"),
                ) as command_call:
                    result = loop_manager.git(repo, *arguments, check=False)
                    self.assertEqual(result.returncode, 0)
                    command_call.assert_called_once()
                    self.assertEqual(ignored.read_text(encoding="utf-8"), "local ignored")

    def test_apply_rebase_resume_checks_only_remaining_patch_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            onto = git_command(repo, "rev-parse", "HEAD")
            git_command(repo, "checkout", "-b", "feature")
            for filename in ("old.txt", "later.txt"):
                (repo / filename).write_text("tracked", encoding="utf-8")
                git_command(repo, "add", filename)
                git_command(repo, "commit", "-m", f"Add {filename}")
            original_head = git_command(repo, "rev-parse", "HEAD")
            git_command(repo, "checkout", "main")
            exclude = Path(git_command(repo, "rev-parse", "--git-path", "info/exclude"))
            if not exclude.is_absolute():
                exclude = repo / exclude
            exclude.write_text(
                exclude.read_text(encoding="utf-8") + "\nold.txt\nlater.txt\n",
                encoding="utf-8",
            )
            (repo / "old.txt").write_text("old ignored", encoding="utf-8")
            (repo / "later.txt").write_text("later ignored", encoding="utf-8")
            rebase_dir = Path(git_command(repo, "rev-parse", "--git-path", "rebase-apply"))
            if not rebase_dir.is_absolute():
                rebase_dir = repo / rebase_dir
            rebase_dir.mkdir()
            for name, value in (
                ("orig-head", original_head), ("onto", onto),
                ("next", "2"), ("last", "2"),
            ):
                (rebase_dir / name).write_text(value, encoding="ascii")
            with mock.patch(
                "loop_manager.command",
                side_effect=AssertionError("Git write started"),
            ):
                blocked = loop_manager.git(repo, "rebase", "--continue", check=False)
            self.assertIn("later.txt", blocked.stdout)
            with mock.patch(
                "loop_manager.command",
                return_value=subprocess.CompletedProcess([], 0, "skipped"),
            ) as command_call:
                skipped = loop_manager.git(repo, "rebase", "--skip", check=False)
            self.assertEqual(skipped.returncode, 0)
            command_call.assert_called_once()
            (repo / "later.txt").unlink()
            with mock.patch(
                "loop_manager.command",
                return_value=subprocess.CompletedProcess([], 0, "resumed"),
            ) as command_call:
                allowed = loop_manager.git(repo, "rebase", "--continue", check=False)
            self.assertEqual(allowed.returncode, 0)
            command_call.assert_called_once()
            self.assertEqual((repo / "old.txt").read_text(encoding="utf-8"), "old ignored")

    def test_merge_modes_do_not_use_base_ancestry_for_cleanup(self):
        for mode in ("merge", "squash", "rebase"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repo, worktree, branch, pr_head = self._managed_worktree(root)

                if mode == "merge":
                    git_command(repo, "merge", "--no-ff", "-m", "Merge PR", branch)
                elif mode == "squash":
                    git_command(repo, "merge", "--squash", branch)
                    git_command(repo, "commit", "-m", "Squash PR")
                else:
                    (repo / "README.md").write_text("# Base changed\n", encoding="utf-8")
                    git_command(repo, "add", "README.md")
                    git_command(repo, "commit", "-m", "Advance base")
                    git_command(repo, "cherry-pick", pr_head)

                base_contains_head = subprocess.run(
                    ["git", "-C", str(repo), "merge-base", "--is-ancestor", pr_head, "main"]
                ).returncode == 0
                self.assertEqual(base_contains_head, mode == "merge")
                assessment = loop_manager.assess_cleanup(worktree, pr_head)
                self.assertEqual(assessment.status, "safe")
                self.assertEqual(
                    loop_manager.remove_cleanup_worktree(repo, worktree, assessment).status,
                    "safe",
                )
                self.assertFalse(worktree.exists())

    def test_deleted_remote_branch_still_uses_saved_pr_head(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, worktree, branch, pr_head = self._managed_worktree(root)
            git_command(worktree, "push", "-u", "origin", branch)
            git_command(repo, "push", "origin", "--delete", branch)

            self.assertEqual(loop_manager.assess_cleanup(worktree, pr_head).status, "safe")

    def test_dirty_reasons_cover_tracked_staged_and_untracked_changes(self):
        changes = {
            "tracked 파일 수정": lambda worktree: (
                worktree / "src/app.py"
            ).write_text("VALUE = 3\n", encoding="utf-8"),
            "staged 변경": lambda worktree: (
                (worktree / "src/app.py").write_text("VALUE = 3\n", encoding="utf-8"),
                git_command(worktree, "add", "src/app.py"),
            ),
            "untracked 파일": lambda worktree: (
                worktree / "untracked.txt"
            ).write_text("keep\n", encoding="utf-8"),
        }
        for expected_reason, change in changes.items():
            with self.subTest(reason=expected_reason), tempfile.TemporaryDirectory() as directory:
                repo, worktree, _branch, pr_head = self._managed_worktree(Path(directory))
                change(worktree)

                assessment = loop_manager.assess_cleanup(worktree, pr_head)

                self.assertEqual(assessment.status, "dirty")
                self.assertIn(expected_reason, assessment.reason)
                self.assertTrue(worktree.exists())

    def test_local_only_and_unverifiable_commits_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _repo, worktree, _branch, pr_head = self._managed_worktree(root)
            (worktree / "README.md").write_text("# Local only\n", encoding="utf-8")
            git_command(worktree, "add", "README.md")
            git_command(worktree, "commit", "-m", "Local only")

            local_only = loop_manager.assess_cleanup(worktree, pr_head)
            missing_head = loop_manager.assess_cleanup(worktree, None)
            missing_object = loop_manager.assess_cleanup(worktree, "f" * 40)

            self.assertEqual(local_only.status, "local-only commits")
            self.assertIn("PR head에 포함되지 않은", local_only.reason)
            self.assertEqual(missing_head.status, "unverifiable")
            self.assertIn("PR head commit 정보", missing_head.reason)
            self.assertEqual(missing_object.status, "unverifiable")
            self.assertIn("commit object", missing_object.reason)

    def test_existing_database_adds_cleanup_pending_without_losing_project(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            database = sqlite3.connect(state_dir / "loop-manager.db")
            database.execute(
                "CREATE TABLE projects (repo TEXT PRIMARY KEY, base_branch TEXT NOT NULL, phase TEXT)"
            )
            database.execute(
                "INSERT INTO projects (repo, base_branch, phase) VALUES ('repo', 'main', 'PR_OPEN')"
            )
            database.commit()
            database.close()

            with loop_manager.connect_database(state_dir) as connection:
                table = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'cleanup_pending'"
                ).fetchone()
                row = connection.execute(
                    "SELECT repo, base_branch, phase FROM projects WHERE repo = 'repo'"
                ).fetchone()

            self.assertIsNotNone(table)
            self.assertEqual(tuple(row), ("repo", "main", "PR_OPEN"))

    def test_existing_cleanup_records_default_to_owned_during_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            database = sqlite3.connect(state_dir / "loop-manager.db")
            database.execute(
                "CREATE TABLE projects (repo TEXT PRIMARY KEY, base_branch TEXT NOT NULL, phase TEXT)"
            )
            database.execute(
                "INSERT INTO projects (repo, base_branch, phase) VALUES ('repo', 'main', 'PR_OPEN')"
            )
            database.execute(
                """
                CREATE TABLE cleanup_pending (
                    repo TEXT NOT NULL,
                    worktree TEXT NOT NULL,
                    pr_url TEXT NOT NULL,
                    pr_head_oid TEXT,
                    reason TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (repo, worktree)
                )
                """
            )
            database.execute(
                """
                INSERT INTO cleanup_pending (
                    repo, worktree, pr_url, pr_head_oid, reason
                ) VALUES ('repo', '/tmp/managed', 'https://example.test/pr/1', NULL, 'dirty')
                """
            )
            database.commit()
            database.close()

            with loop_manager.connect_database(state_dir) as connection:
                pending = connection.execute(
                    "SELECT reason, workspace_owned FROM cleanup_pending WHERE repo = 'repo'"
                ).fetchone()

            self.assertEqual(tuple(pending), ("dirty", loop_manager.WORKSPACE_OWNED))

    def test_pending_recheck_removes_safe_worktree_and_missing_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_remote_repository(directory)
            state_dir = root / "state"
            branch, worktree = loop_manager.expected_execplan_workspace(
                state_dir, str(repo), "G-001"
            )
            loop_manager.make_worktree(repo, worktree, branch, "main")
            (worktree / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            git_command(worktree, "add", "src/app.py")
            git_command(worktree, "commit", "-m", "Implement cleanup fixture")
            pr_head = git_command(worktree, "rev-parse", "HEAD")
            connection = self._memory_connection(repo)
            missing = root / "already-gone"
            for path, oid in ((worktree, pr_head), (missing, None)):
                loop_manager._store_cleanup_pending(
                    connection,
                    str(repo),
                    path,
                    "https://example.test/pr/1",
                    oid,
                    "preserved",
                )
            connection.commit()

            remaining = loop_manager.recheck_cleanup_pending(
                connection, repo, str(repo), state_dir
            )

            self.assertEqual(remaining, [])
            self.assertFalse(worktree.exists())

    def test_unowned_pending_is_not_inspected_and_clears_only_when_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_remote_repository(directory)
            state_dir = root / "state"
            victim = root / "user-worktree"
            victim.mkdir()
            marker = victim / "keep.txt"
            marker.write_text("keep\n", encoding="utf-8")
            connection = self._memory_connection(repo)
            loop_manager._store_cleanup_pending(
                connection,
                str(repo),
                victim,
                "https://example.test/pr/1",
                None,
                "ownership unverified",
                workspace_owned=0,
            )
            connection.commit()

            with mock.patch(
                "loop_manager.validate_pending_cleanup_worktree"
            ) as validate, mock.patch(
                "loop_manager.remove_cleanup_worktree"
            ) as remove:
                remaining = loop_manager.recheck_cleanup_pending(
                    connection, repo, str(repo), state_dir
                )
                marker.unlink()
                victim.rmdir()
                cleared = loop_manager.recheck_cleanup_pending(
                    connection, repo, str(repo), state_dir
                )

            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["workspace_owned"], 0)
            self.assertEqual(cleared, [])
            validate.assert_not_called()
            remove.assert_not_called()

    def test_merged_dirty_worktree_becomes_nonblocking_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_remote_repository(directory)
            (repo / "docs/plans").mkdir()
            write_execplan(repo, "G-001", "Cleanup")
            commit_and_push(repo, "Add cleanup plan")
            plan_sha = git_command(repo, "rev-parse", "origin/main")
            state_dir = root / "state"
            branch, worktree = loop_manager.expected_execplan_workspace(
                state_dir, str(repo), "G-001"
            )
            loop_manager.make_worktree(repo, worktree, branch, "main")
            (worktree / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            git_command(worktree, "add", "src/app.py")
            git_command(worktree, "commit", "-m", "Implement cleanup fixture")
            pr_head = git_command(worktree, "rev-parse", "HEAD")
            (worktree / "untracked.txt").write_text("keep\n", encoding="utf-8")
            connection = self._memory_connection(repo)
            connection.execute(
                """
                UPDATE projects
                SET active_sha = ?, plan_sha = ?, phase = 'PR_OPEN', branch = ?, worktree = ?,
                    plan_path = 'docs/plans/[G-001] Cleanup.md',
                    pr_url = 'https://example.test/pr/1'
                WHERE repo = ?
                """,
                (pr_head, plan_sha, branch, str(worktree), str(repo)),
            )
            connection.commit()
            merged_pr = {
                "url": "https://example.test/pr/1",
                "state": "MERGED",
                "mergedAt": "2026-01-01T00:00:00Z",
                "headRefOid": pr_head,
            }

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=merged_pr
            ), mock.patch(
                "loop_manager.run_codex",
                return_value=loop_manager.CodexResult(0, "", "planner-session"),
            ) as planner:
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT phase, branch, worktree, pr_url, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            pending = connection.execute(
                "SELECT worktree, pr_head_oid, reason FROM cleanup_pending WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(result, 0)
            self.assertEqual(tuple(project), (None, None, None, None, None))
            self.assertEqual(pending["worktree"], str(worktree))
            self.assertEqual(pending["pr_head_oid"], pr_head)
            self.assertIn("untracked 파일", pending["reason"])
            self.assertTrue(worktree.exists())
            planner.assert_called_once()
            self.assertEqual(planner.call_args.args[3], "planner")

    def test_pr_queries_request_immutable_head_oid(self):
        response = subprocess.CompletedProcess(
            ["gh"], 0, stdout='{"url":"u","state":"MERGED","mergedAt":"t","headRefOid":"abc"}'
        )
        with mock.patch("loop_manager.command", return_value=response) as command:
            row = loop_manager.pr_for_branch(
                Path("/repo"), "loop-manager/G-007", "https://example.test/pr/7"
            )

        self.assertEqual(row["headRefOid"], "abc")
        self.assertIn("headRefOid", command.call_args.args[0][-1])
        self.assertIn("mergeable", command.call_args.args[0][-1])

    def test_main_rejects_unknown_options_before_state_creation(self):
        state_dir = Path("state")
        with mock.patch(
            "sys.argv",
            [
                "loop_manager.py",
                "init",
                "--future-option=value",
            ],
        ), mock.patch(
            "loop_manager.state_directory", return_value=state_dir
        ) as state_directory, mock.patch(
            "loop_manager.process_lock"
        ) as process_lock, mock.patch(
            "loop_manager.connect_database"
        ) as connect_database, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                loop_manager.main()

        self.assertEqual(raised.exception.code, 2)
        state_directory.assert_not_called()
        process_lock.assert_not_called()
        connect_database.assert_not_called()


class LatestBasePublicationTest(unittest.TestCase):
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

    def test_publication_revalidates_after_verify_and_review_base_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                _plan,
                _branch,
                _worktree,
            ) = self._publication_state(directory)
            (repo / "docs/notes.md").write_text(
                "# Base two\n", encoding="utf-8"
            )
            commit_and_push(repo, "Advance base before publication")
            reviews = []
            created = []
            review_base_advanced = False

            def reviewer(_worktree, _state, _config, role_name, _prompt, **_kwargs):
                self.assertEqual(role_name, "reviewer")
                reviews.append(role_name)
                return loop_manager.CodexResult(
                    0, "VERDICT: PASS", "reviewer-session"
                )

            def pr_lookup(*_args, **_kwargs):
                nonlocal review_base_advanced
                phase = connection.execute(
                    "SELECT phase FROM projects WHERE repo = ?",
                    (str(repo),),
                ).fetchone()["phase"]
                if (
                    reviews == ["reviewer"]
                    and phase == "PUBLISHING"
                    and not review_base_advanced
                ):
                    review_base_advanced = True
                    (repo / "README.md").write_text(
                        "# Base three\n", encoding="utf-8"
                    )
                    commit_and_push(repo, "Advance base during review")
                return None
            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", side_effect=pr_lookup
            ), mock.patch(
                "loop_manager.run_codex", side_effect=reviewer
            ), mock.patch(
                "loop_manager.verify", return_value=(True, "verify: PASS")
            ) as verify, mock.patch(
                "loop_manager.build_pr_body", return_value="body"
            ), mock.patch(
                "loop_manager.command",
                side_effect=self._command_stub(created),
            ):
                outcome = loop_manager.run_project_result(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT phase, active_sha, validated_base_sha, pr_head_oid "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan = connection.execute(
                "SELECT status, active_sha, validated_base_sha, pr_head_oid "
                "FROM exec_plans WHERE repo = ? AND plan_id = 'G-014'",
                (str(repo),),
            ).fetchone()
            latest_base = git_command(repo, "rev-parse", "origin/main")

        self.assertEqual(outcome.kind, loop_manager.ProjectRunKind.WAITING)
        self.assertIsNone(project["phase"])
        self.assertEqual(plan["status"], "PR_OPEN")
        self.assertEqual(plan["active_sha"], latest_base)
        self.assertEqual(plan["validated_base_sha"], latest_base)
        self.assertIsNotNone(plan["pr_head_oid"])
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(reviews, ["reviewer", "reviewer"])
        self.assertEqual(len(created), 1)

    def test_publication_rebase_conflict_blocks_when_worker_cannot_resolve(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                _plan,
                branch,
                worktree,
            ) = self._publication_state(directory)
            (repo / "src/app.py").write_text(
                "VALUE = 'base'\n", encoding="utf-8"
            )
            commit_and_push(repo, "Create publication conflict")

            common_patches = (
                mock.patch("loop_manager.validate_repository", return_value=repo),
                mock.patch("loop_manager.remote_base", return_value="main"),
                mock.patch("loop_manager.ensure_safe_base_worktree"),
                mock.patch("loop_manager.pr_for_branch", return_value=None),
            )
            with common_patches[0], common_patches[1], common_patches[2], common_patches[3], mock.patch(
                "loop_manager.run_codex"
            ) as agent, mock.patch("loop_manager.verify") as verify:
                first = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )
            conflicted = connection.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(first, 1)
            self.assertEqual(conflicted["phase"], "WORKING")
            self.assertTrue(
                conflicted["last_error"].startswith(
                    loop_manager.REBASE_CONFLICT_PREFIX
                )
            )
            self.assertTrue(
                loop_manager._rebase_in_progress_for_branch(worktree, branch)
            )
            agent.assert_not_called()
            verify.assert_not_called()

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex",
                return_value=loop_manager.CodexResult(
                    1, "cannot resolve conflict", "worker-session"
                ),
            ):
                second = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            blocked = connection.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan = connection.execute(
                "SELECT status, last_error FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-014'",
                (str(repo),),
            ).fetchone()

        self.assertEqual(second, 0)
        self.assertIsNone(blocked["phase"])
        self.assertIn("cannot resolve conflict", plan["last_error"])
        self.assertEqual(plan["status"], "PLAN_BLOCKED")

    def test_real_autostash_conflict_resumes_to_verified_pr_without_reapplying_stash(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo, state_dir, connection, _plan, branch, worktree,
            ) = self._publication_state(directory)
            original_base = git_command(repo, "rev-parse", "origin/main")
            git_command(worktree, "reset", "--hard", original_base)
            (worktree / "src/feature.py").write_text(
                "FEATURE = True\n", encoding="utf-8"
            )
            git_command(worktree, "add", "src/feature.py")
            git_command(worktree, "commit", "-m", "Add independent feature")
            (worktree / "src/app.py").write_text(
                "VALUE = 'worker'\n", encoding="utf-8"
            )
            (repo / "src/app.py").write_text(
                "VALUE = 'base'\n", encoding="utf-8"
            )
            commit_and_push(repo, "Change app on latest base")
            original_git = loop_manager.git
            rebase_results = []

            def record_initial_git(path, *arguments, **kwargs):
                result = original_git(path, *arguments, **kwargs)
                if path == worktree and arguments[:2] == ("rebase", "--autostash"):
                    rebase_results.append(result.returncode)
                return result

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch("loop_manager.run_codex") as first_agent, mock.patch(
                "loop_manager.git", side_effect=record_initial_git
            ):
                first = loop_manager.run_project(
                    connection, state_dir, str(repo), Path("unused.ini"),
                    "main", False,
                )

            self.assertEqual(first, 1)
            self.assertEqual(rebase_results, [0])
            first_agent.assert_not_called()
            self.assertFalse(
                loop_manager._rebase_in_progress_for_branch(worktree, branch)
            )
            self.assertTrue(git_command(worktree, "ls-files", "-u"))
            saved = connection.execute(
                "SELECT operation, snapshot FROM execution_checkpoints "
                "WHERE repo = ? AND plan_id = 'G-014'", (str(repo),),
            ).fetchone()
            self.assertEqual(saved["operation"], "rebase")
            self.assertTrue(json.loads(saved["snapshot"])["stash_oid"])

            roles = []
            git_steps = []
            created = []

            def record_git(path, *arguments, **kwargs):
                if path == worktree and arguments and arguments[0] in {"rebase", "stash"}:
                    git_steps.append(arguments)
                return original_git(path, *arguments, **kwargs)

            def agent(agent_worktree, _state, _config, role, _prompt, **_kwargs):
                roles.append(role)
                if role == "worker":
                    self.assertTrue(git_command(worktree, "ls-files", "-u"))
                    self.assertFalse(
                        loop_manager._rebase_in_progress_for_branch(worktree, branch)
                    )
                    (agent_worktree / "src/app.py").write_text(
                        "VALUE = 'resolved'\n", encoding="utf-8"
                    )
                    return loop_manager.CodexResult(
                        0,
                        worker_result(
                            "Resolve autostash conflict",
                            "Keeps the feature and resolves the saved app change.",
                            "LatestBasePublicationTest: PASS",
                        ),
                        "worker-session",
                    )
                self.assertEqual(role, "reviewer")
                return loop_manager.CodexResult(
                    0, "VERDICT: PASS", "reviewer-session"
                )

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex", side_effect=agent
            ), mock.patch(
                "loop_manager.verify", return_value=(True, "verify: PASS")
            ) as verify, mock.patch(
                "loop_manager.build_pr_body", return_value="body"
            ), mock.patch(
                "loop_manager.command", side_effect=self._command_stub(created)
            ), mock.patch("loop_manager.git", side_effect=record_git):
                resumed = loop_manager.run_project_result(
                    connection, state_dir, str(repo), Path("unused.ini"),
                    "main", False,
                )

            plan = connection.execute(
                "SELECT status, active_sha, validated_base_sha "
                "FROM exec_plans WHERE repo = ? AND plan_id = 'G-014'",
                (str(repo),),
            ).fetchone()
            latest_base = git_command(repo, "rev-parse", "origin/main")
            self.assertEqual(resumed.kind, loop_manager.ProjectRunKind.WAITING)
            self.assertEqual(plan["status"], "PR_OPEN")
            self.assertEqual(plan["active_sha"], latest_base)
            self.assertEqual(plan["validated_base_sha"], latest_base)
            self.assertFalse(git_command(worktree, "ls-files", "-u"))
            self.assertFalse(
                loop_manager._rebase_in_progress_for_branch(worktree, branch)
            )
            self.assertEqual(
                (worktree / "src/feature.py").read_text(encoding="utf-8"),
                "FEATURE = True\n",
            )
            self.assertEqual(
                (worktree / "src/app.py").read_text(encoding="utf-8"),
                "VALUE = 'resolved'\n",
            )
            self.assertEqual(roles, ["worker", "reviewer"])
            verify.assert_called_once()
            self.assertEqual(git_steps, [])
            self.assertEqual(len(created), 1)

    def test_rebase_recovery_marker_survives_agent_pause(self):
        cases = (
            (
                loop_manager.AgentResultKind.TRANSIENT_FAILURE,
                loop_manager.ProjectRunKind.TRANSIENT_PAUSE,
                loop_manager.TRANSIENT_AGENT_ERROR_PREFIX,
                loop_manager.MAX_TRANSIENT_AGENT_RETRIES + 1,
            ),
            (
                loop_manager.AgentResultKind.USAGE_LIMIT,
                loop_manager.ProjectRunKind.USAGE_LIMIT_PAUSE,
                loop_manager.USAGE_LIMIT_ERROR_PREFIX,
                1,
            ),
        )
        for result_kind, pause_kind, error_prefix, expected_calls in cases:
            with (
                self.subTest(result_kind=result_kind),
                tempfile.TemporaryDirectory() as directory,
            ):
                (
                    repo,
                    state_dir,
                    connection,
                    _plan,
                    branch,
                    worktree,
                ) = self._publication_state(directory)
                (repo / "src/app.py").write_text(
                    "VALUE = 'base'\n", encoding="utf-8"
                )
                commit_and_push(repo, "Create publication conflict")

                with mock.patch(
                    "loop_manager.validate_repository", return_value=repo
                ), mock.patch(
                    "loop_manager.remote_base", return_value="main"
                ), mock.patch(
                    "loop_manager.ensure_safe_base_worktree"
                ), mock.patch(
                    "loop_manager.pr_for_branch", return_value=None
                ), mock.patch(
                    "loop_manager.run_codex"
                ) as agent:
                    first = loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )

                self.assertEqual(first, 1)
                self.assertTrue(
                    loop_manager._rebase_in_progress_for_branch(worktree, branch)
                )
                agent.assert_not_called()

                paused_result = loop_manager.CodexResult(
                    1,
                    "external agent unavailable",
                    "worker-session",
                    result_kind,
                )
                with mock.patch(
                    "loop_manager.validate_repository", return_value=repo
                ), mock.patch(
                    "loop_manager.remote_base", return_value="main"
                ), mock.patch(
                    "loop_manager.ensure_safe_base_worktree"
                ), mock.patch(
                    "loop_manager.pr_for_branch", return_value=None
                ), mock.patch(
                    "loop_manager.run_codex", return_value=paused_result
                ) as paused_agent:
                    paused = loop_manager.run_project_result(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )

                stored = connection.execute(
                    "SELECT phase, last_error, worker_session_id "
                    "FROM projects WHERE repo = ?",
                    (str(repo),),
                ).fetchone()
                self.assertEqual(paused.kind, pause_kind)
                self.assertEqual(stored["phase"], "WORKING")
                self.assertTrue(
                    stored["last_error"].startswith(
                        loop_manager.REBASE_CONFLICT_PREFIX
                    )
                )
                self.assertIn(error_prefix, stored["last_error"])
                self.assertEqual(stored["worker_session_id"], "worker-session")
                self.assertEqual(paused_agent.call_count, expected_calls)
                self.assertTrue(
                    loop_manager._rebase_in_progress_for_branch(worktree, branch)
                )

                def resumed_worker(
                    _worktree,
                    _state,
                    _config,
                    role_name,
                    _prompt,
                    **kwargs,
                ):
                    self.assertEqual(role_name, "worker")
                    self.assertEqual(kwargs["session_id"], "worker-session")
                    self.assertTrue(
                        loop_manager._rebase_in_progress_for_branch(
                            worktree, branch
                        )
                    )
                    return loop_manager.CodexResult(
                        1,
                        "stop after recovery assertion",
                        "worker-session",
                    )

                with mock.patch(
                    "loop_manager.validate_repository", return_value=repo
                ), mock.patch(
                    "loop_manager.remote_base", return_value="main"
                ), mock.patch(
                    "loop_manager.ensure_safe_base_worktree"
                ), mock.patch(
                    "loop_manager.pr_for_branch", return_value=None
                ), mock.patch(
                    "loop_manager.run_codex", side_effect=resumed_worker
                ) as resumed_agent:
                    resumed = loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )

                blocked = connection.execute(
                    "SELECT phase FROM projects WHERE repo = ?",
                    (str(repo),),
                ).fetchone()
                plan_status = connection.execute(
                    "SELECT status FROM exec_plans "
                    "WHERE repo = ? AND plan_id = 'G-014'",
                    (str(repo),),
                ).fetchone()["status"]
                self.assertEqual(resumed, 0)
                self.assertIsNone(blocked["phase"])
                self.assertEqual(plan_status, "PLAN_BLOCKED")
                self.assertEqual(resumed_agent.call_count, 4)

    def test_rebase_recovery_marker_survives_worker_result_validation_failure(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                _plan,
                branch,
                worktree,
            ) = self._publication_state(directory)
            (repo / "src/app.py").write_text(
                "VALUE = 'base'\n", encoding="utf-8"
            )
            commit_and_push(repo, "Create publication conflict")

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex"
            ) as agent:
                first = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            self.assertEqual(first, 1)
            self.assertTrue(
                loop_manager._rebase_in_progress_for_branch(worktree, branch)
            )
            agent.assert_not_called()

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex",
                return_value=loop_manager.CodexResult(
                    0,
                    "invalid worker result",
                    "worker-session",
                ),
            ):
                invalid = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            stored = connection.execute(
                "SELECT phase, last_error, worker_session_id "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(invalid, 1)
            self.assertEqual(stored["phase"], "WORKING")
            self.assertTrue(
                stored["last_error"].startswith(
                    loop_manager.REBASE_CONFLICT_PREFIX
                )
            )
            self.assertIn("Worker 결과 검증 실패", stored["last_error"])
            self.assertEqual(stored["worker_session_id"], "worker-session")
            self.assertTrue(
                loop_manager._rebase_in_progress_for_branch(worktree, branch)
            )

            resumed_prompts = []

            def resumed_worker(
                _worktree,
                _state,
                _config,
                role_name,
                prompt,
                **kwargs,
            ):
                self.assertEqual(role_name, "worker")
                resumed_prompts.append(prompt)
                if len(resumed_prompts) == 1:
                    self.assertIn("Worker 결과 검증 실패", prompt)
                else:
                    self.assertIn("rebase 충돌 해결 필요", prompt)
                    self.assertIn("stop after recovery assertion", prompt)
                self.assertEqual(kwargs["session_id"], "worker-session")
                self.assertTrue(
                    loop_manager._rebase_in_progress_for_branch(
                        worktree, branch
                    )
                )
                return loop_manager.CodexResult(
                    1,
                    "stop after recovery assertion",
                    "worker-session",
                )

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex", side_effect=resumed_worker
            ) as resumed_agent:
                resumed = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            blocked = connection.execute(
                "SELECT phase FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan_status = connection.execute(
                "SELECT status FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-014'",
                (str(repo),),
            ).fetchone()["status"]

        self.assertEqual(resumed, 0)
        self.assertIsNone(blocked["phase"])
        self.assertEqual(plan_status, "PLAN_BLOCKED")
        # The invalid result was already the first correction occurrence;
        # three further abnormal results reach the no-progress safety gate.
        self.assertEqual(resumed_agent.call_count, 3)
        self.assertEqual(len(resumed_prompts), 3)

    def test_empty_conflict_resolution_blocks_instead_of_reinvoking_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                _plan,
                branch,
                worktree,
            ) = self._publication_state(directory)
            (repo / "src/app.py").write_text(
                "VALUE = 'base'\n", encoding="utf-8"
            )
            commit_and_push(repo, "Create publication conflict")

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex"
            ) as agent:
                first = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            self.assertEqual(first, 1)
            self.assertTrue(
                loop_manager._rebase_in_progress_for_branch(worktree, branch)
            )
            agent.assert_not_called()

            def choose_latest_base(
                agent_worktree,
                _state,
                _config,
                role_name,
                _prompt,
                **_kwargs,
            ):
                self.assertEqual(role_name, "worker")
                (agent_worktree / "src/app.py").write_text(
                    "VALUE = 'base'\n", encoding="utf-8"
                )
                return loop_manager.CodexResult(
                    0,
                    worker_result(
                        "Resolve publication conflict",
                        "Keeps the latest base version while resolving "
                        "the conflict.",
                        "LatestBasePublicationTest: PASS",
                    ),
                    "worker-session",
                )

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex", side_effect=choose_latest_base
            ) as resolving_agent, mock.patch(
                "loop_manager.verify"
            ) as verify:
                second = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            blocked = connection.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan = connection.execute(
                "SELECT status, last_error FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-014'",
                (str(repo),),
            ).fetchone()
            rebase_in_progress = loop_manager._rebase_in_progress_for_branch(
                worktree, branch
            )

        self.assertEqual(second, 0)
        self.assertIsNone(blocked["phase"])
        self.assertIn(
            "rebase 충돌 해결 후 Worker 변경이 남지 않아",
            plan["last_error"],
        )
        self.assertEqual(plan["status"], "PLAN_BLOCKED")
        self.assertFalse(
            loop_manager._rebase_in_progress_for_branch(worktree, branch)
        )
        resolving_agent.assert_called_once()
        verify.assert_not_called()

    def test_publication_rebase_that_removes_change_blocks_before_verify(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                _plan,
                _branch,
                _worktree,
            ) = self._publication_state(directory)
            (repo / "src/app.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )
            commit_and_push(repo, "Apply implementation upstream")

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex"
            ) as agent, mock.patch("loop_manager.verify") as verify:
                outcome = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            blocked = connection.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan = connection.execute(
                "SELECT status, last_error FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-014'",
                (str(repo),),
            ).fetchone()

        self.assertEqual(outcome, 0)
        self.assertIsNone(blocked["phase"])
        self.assertEqual(plan["status"], "PLAN_BLOCKED")
        self.assertIn("rebase 후 Worker 변경이 남지 않아", plan["last_error"])
        agent.assert_not_called()
        verify.assert_not_called()

    def test_remote_branch_push_interruption_recovers_without_another_push(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                _plan,
                branch,
                worktree,
            ) = self._publication_state(directory)
            git_command(
                worktree,
                "push",
                "origin",
                f"HEAD:refs/heads/{branch}",
            )
            seed_validated_candidate(connection, repo, worktree, "G-014")
            created = []
            git_calls = []
            original_git = loop_manager.git

            def recording_git(path, *arguments, **kwargs):
                if arguments and arguments[0] == "push":
                    git_calls.append(arguments)
                return original_git(path, *arguments, **kwargs)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.build_pr_body", return_value="body"
            ), mock.patch(
                "loop_manager.command",
                side_effect=self._command_stub(created),
            ), mock.patch(
                "loop_manager.git", side_effect=recording_git
            ):
                outcome = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT phase, publication_remote_oid, pr_head_oid "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan = connection.execute(
                "SELECT status, publication_remote_oid, pr_head_oid "
                "FROM exec_plans WHERE repo = ? AND plan_id = 'G-014'",
                (str(repo),),
            ).fetchone()
            local_head = git_command(worktree, "rev-parse", "HEAD")

        self.assertEqual(outcome, 0)
        self.assertIsNone(project["phase"])
        self.assertEqual(plan["status"], "PR_OPEN")
        self.assertEqual(plan["publication_remote_oid"], local_head)
        self.assertEqual(plan["pr_head_oid"], local_head)
        self.assertEqual(git_calls, [])
        self.assertEqual(len(created), 1)

    def test_owned_remote_branch_rebase_uses_explicit_force_with_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                _plan,
                branch,
                worktree,
            ) = self._publication_state(directory)
            old_head = git_command(worktree, "rev-parse", "HEAD")
            git_command(
                worktree,
                "push",
                "origin",
                f"HEAD:refs/heads/{branch}",
            )
            (repo / "docs/notes.md").write_text(
                "# New base\n", encoding="utf-8"
            )
            commit_and_push(repo, "Advance base after interrupted push")
            pushes = []
            created = []
            original_git = loop_manager.git

            def recording_git(path, *arguments, **kwargs):
                if arguments and arguments[0] == "push":
                    pushes.append(arguments)
                return original_git(path, *arguments, **kwargs)

            def reviewer(_worktree, _state, _config, role_name, _prompt, **_kwargs):
                self.assertEqual(role_name, "reviewer")
                return loop_manager.CodexResult(
                    0, "VERDICT: PASS", "reviewer-session"
                )

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.verify", return_value=(True, "verify: PASS")
            ), mock.patch(
                "loop_manager.run_codex", side_effect=reviewer
            ), mock.patch(
                "loop_manager.build_pr_body", return_value="body"
            ), mock.patch(
                "loop_manager.command",
                side_effect=self._command_stub(created),
            ), mock.patch(
                "loop_manager.git", side_effect=recording_git
            ):
                outcome = loop_manager.run_project_result(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

        lease = f"--force-with-lease=refs/heads/{branch}:{old_head}"
        self.assertEqual(outcome.kind, loop_manager.ProjectRunKind.WAITING)
        self.assertEqual(len(pushes), 1)
        self.assertIn(lease, pushes[0])
        self.assertNotIn("--force", pushes[0])
        self.assertEqual(len(created), 1)

    def test_owned_branch_push_rechecks_live_ref_before_lease(self):
        branch = 'loop-manager/G-081'
        plan = {'publication_remote_oid': 'a' * 40, 'pr_url': None}
        calls = []

        def git(_path, *arguments, **_kwargs):
            calls.append(arguments)
            if arguments[:2] == ('rev-parse', 'HEAD'):
                return subprocess.CompletedProcess(arguments, 0, 'b' * 40 + '\n')
            if arguments[0] == 'ls-remote':
                return subprocess.CompletedProcess(
                    arguments, 0, 'c' * 40 + f'\trefs/heads/{branch}\n',
                )
            raise AssertionError(f'unexpected Git command: {arguments}')

        with (
            mock.patch('loop_manager._owned_publication_plan', return_value=plan),
            mock.patch('loop_manager.remote_branch_oid', return_value='a' * 40) as remote,
            mock.patch('loop_manager.record_remote_publication_branch'),
            mock.patch('loop_manager.git', side_effect=git),
        ):
            with self.assertRaises(loop_manager.PublicationSafetyError):
                loop_manager.push_managed_publication_branch(
                    object(), Path('/repo'), Path('/state'), 'repo',
                    branch, Path('/managed'),
                )
            remote.return_value = None
            with self.assertRaises(loop_manager.PublicationSafetyError):
                loop_manager.push_managed_publication_branch(
                    object(), Path('/repo'), Path('/state'), 'repo',
                    branch, Path('/managed'),
                )
        self.assertFalse(any(args[0] == 'push' for args in calls))

    def test_owned_branch_push_uses_exact_lease_and_saves_confirmed_head(self):
        branch = 'loop-manager/G-081'
        old, new = 'a' * 40, 'b' * 40
        pushes = []
        remote_observations = iter((old, new))

        def git(_path, *arguments, **_kwargs):
            if arguments[:2] == ('rev-parse', 'HEAD'):
                return subprocess.CompletedProcess(arguments, 0, new + '\n')
            if arguments[0] == 'ls-remote':
                return subprocess.CompletedProcess(
                    arguments, 0,
                    next(remote_observations) + f'\trefs/heads/{branch}\n',
                )
            if arguments[0] == 'push':
                pushes.append(arguments)
            return subprocess.CompletedProcess(arguments, 0, '')

        with (
            mock.patch('loop_manager._owned_publication_plan', return_value={
                'publication_remote_oid': old, 'pr_url': None,
            }),
            mock.patch('loop_manager.remote_branch_oid', side_effect=[old, new]),
            mock.patch('loop_manager.record_remote_publication_branch'),
            mock.patch('loop_manager.pr_for_branch', return_value=None),
            mock.patch('loop_manager.update_project') as update,
            mock.patch('loop_manager.git', side_effect=git),
            mock.patch('loop_manager._recorded_base_fetch', return_value=new) as fetched,
        ):
            result = loop_manager.push_managed_publication_branch(
                object(), Path('/repo'), Path('/state'), 'repo',
                branch, Path('/managed'),
            )

        self.assertEqual(result, new)
        fetched.assert_called_once_with(
            Path('/repo'), Path('/state'), branch, owner='project',
        )
        self.assertEqual(len(pushes), 1)
        self.assertIn(f'--force-with-lease=refs/heads/{branch}:{old}', pushes[0])
        update.assert_called_once_with(mock.ANY, 'repo', publication_remote_oid=new)

    def test_unowned_remote_branch_is_blocked_without_force_push(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                _plan,
                branch,
                _worktree,
            ) = self._publication_state(directory)
            git_command(
                repo,
                "push",
                "origin",
                f"HEAD:refs/heads/{branch}",
            )
            pushes = []
            original_git = loop_manager.git

            def recording_git(path, *arguments, **kwargs):
                if arguments and arguments[0] == "push":
                    pushes.append(arguments)
                return original_git(path, *arguments, **kwargs)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.git", side_effect=recording_git
            ):
                outcome = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            blocked = connection.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()

        self.assertEqual(outcome, 1)
        self.assertEqual(blocked["phase"], "PROJECT_BLOCKED")
        self.assertIn("갱신을 거부", blocked["last_error"])
        self.assertEqual(pushes, [])

    def test_existing_open_closed_reopened_and_merged_prs_are_reused(self):
        for initial_state in ("OPEN", "CLOSED", "MERGED"):
            with self.subTest(state=initial_state), tempfile.TemporaryDirectory() as directory:
                (
                    repo,
                    state_dir,
                    connection,
                    _plan,
                    branch,
                    worktree,
                ) = self._publication_state(directory)
                head = git_command(worktree, "rev-parse", "HEAD")
                pull_request = {
                    "url": "https://example.test/pr/14",
                    "state": initial_state,
                    "mergedAt": (
                        "2026-09-19T00:00:00Z"
                        if initial_state == "MERGED"
                        else None
                    ),
                    "headRefOid": head,
                    "headRefName": branch,
                }
                with mock.patch(
                    "loop_manager.validate_repository", return_value=repo
                ), mock.patch(
                    "loop_manager.pr_for_branch", return_value=pull_request
                ), mock.patch("loop_manager.run_codex") as agent:
                    outcome = loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )
                agent.assert_not_called()

                plan = connection.execute(
                    "SELECT status, pr_url, pr_head_oid FROM exec_plans "
                    "WHERE repo = ? AND plan_id = 'G-014'",
                    (str(repo),),
                ).fetchone()
                if initial_state == "OPEN":
                    self.assertEqual(outcome, 0)
                    self.assertEqual(plan["status"], "PR_OPEN")
                    self.assertEqual(plan["pr_head_oid"], head)
                elif initial_state == "CLOSED":
                    self.assertEqual(outcome, 0)
                    self.assertEqual(plan["status"], "PLAN_BLOCKED")
                    reopened = {**pull_request, "state": "OPEN"}
                    with mock.patch(
                        "loop_manager.validate_repository", return_value=repo
                    ), mock.patch(
                        "loop_manager.pr_for_branch", return_value=reopened
                    ):
                        self.assertEqual(
                            loop_manager.run_project(
                                connection,
                                state_dir,
                                str(repo),
                                Path("unused.ini"),
                                "main",
                                False,
                            ),
                            0,
                        )
                    resumed = connection.execute(
                        "SELECT status, pr_url FROM exec_plans "
                        "WHERE repo = ? AND plan_id = 'G-014'",
                        (str(repo),),
                    ).fetchone()
                    self.assertEqual(resumed["status"], "PR_OPEN")
                    self.assertEqual(resumed["pr_url"], pull_request["url"])
                else:
                    self.assertEqual(outcome, 0)
                    self.assertEqual(plan["status"], "MERGED")
                    self.assertFalse(worktree.exists())


class AutomaticOwnedPrConflictRecoveryTest(unittest.TestCase):
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
        return (
            repo,
            state_dir,
            connection,
            branch,
            worktree,
            pr_url,
            head_oid,
        )

    def _advance_base(
        self,
        directory: str,
        repo: Path,
        relative_path: str,
        contents: str,
        message: str,
    ) -> str:
        upstream = Path(directory) / f"upstream-{message.replace(' ', '-')}"
        git_command(Path(directory), "clone", str(repo.parent / "remote.git"), str(upstream))
        git_command(upstream, "config", "user.email", "upstream@example.com")
        git_command(upstream, "config", "user.name", "Upstream Test")
        target = upstream / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")
        git_command(upstream, "add", "-A")
        git_command(upstream, "commit", "-m", message)
        git_command(upstream, "push", "origin", "HEAD:main")
        return git_command(upstream, "rev-parse", "HEAD")

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
            "state": state,
            "mergedAt": None,
            "headRefOid": head_oid,
            "headRefName": branch,
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

    def _add_ignored_cache(self, worktree: Path) -> Path:
        exclude = Path(
            git_command(worktree, "rev-parse", "--git-path", "info/exclude")
        )
        if not exclude.is_absolute():
            exclude = worktree / exclude
        exclude.write_text(
            exclude.read_text(encoding="utf-8") + "\n__pycache__/\n",
            encoding="utf-8",
        )
        cache = worktree / "__pycache__/verify.cpython-312.pyc"
        cache.parent.mkdir()
        cache.write_bytes(b"ignored verify artifact")
        self.assertEqual(git_command(worktree, "status", "--porcelain"), "")
        self.assertNotIn(
            cache.relative_to(worktree).as_posix(),
            loop_manager.capture_git_snapshot(worktree).dirty,
        )
        return cache

    def test_unknown_and_mergeable_prs_do_not_rebase_run_agents_or_push(self):
        for mergeable, advance_base in (("UNKNOWN", False), ("MERGEABLE", True)):
            with self.subTest(mergeable=mergeable), tempfile.TemporaryDirectory() as directory:
                (
                    repo,
                    state_dir,
                    connection,
                    branch,
                    worktree,
                    pr_url,
                    head_oid,
                ) = self._pr_open_state(directory)
                cache = self._add_ignored_cache(worktree)
                if advance_base:
                    self._advance_base(
                        directory,
                        repo,
                        "docs/notes.md",
                        "# Behind only\n",
                        "Advance nonconflicting base",
                    )
                git_calls = []
                original_git = loop_manager.git

                def recording_git(path, *arguments, **kwargs):
                    if arguments and arguments[0] in {"rebase", "push"}:
                        git_calls.append(arguments)
                    return original_git(path, *arguments, **kwargs)

                pull_request = self._pull_request(
                    branch, pr_url, head_oid, mergeable
                )
                with mock.patch(
                    "loop_manager.validate_repository", return_value=repo
                ), mock.patch(
                    "loop_manager.ensure_safe_base_worktree"
                ) as synchronize, mock.patch(
                    "loop_manager.pr_for_branch", return_value=pull_request
                ), mock.patch(
                    "loop_manager.run_codex"
                ) as agent, mock.patch(
                    "loop_manager.verify"
                ) as verify, mock.patch(
                    "loop_manager.git", side_effect=recording_git
                ), mock.patch(
                    "loop_manager._fetch_pr_conflict_recovery_refs"
                ) as conflict_fetch, mock.patch(
                    "loop_manager._validate_pr_conflict_recovery_ownership"
                ) as conflict_ownership:
                    result = loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )

                stored = connection.execute(
                    "SELECT status, pr_conflict_recovery FROM exec_plans "
                    "WHERE repo = ? AND plan_id = 'G-026'",
                    (str(repo),),
                ).fetchone()
                self.assertEqual(result, 0)
                self.assertEqual(tuple(stored), ("PR_OPEN", 0))
                self.assertEqual(git_calls, [])
                self.assertTrue(cache.is_file())
                conflict_fetch.assert_not_called()
                conflict_ownership.assert_not_called()
                synchronize.assert_not_called()
                agent.assert_not_called()
                verify.assert_not_called()

    def test_merged_pr_fast_forwards_clean_source_after_durable_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection, branch, worktree, url, head = (
                self._pr_open_state(directory)
            )
            git_command(worktree, "push", "origin", "HEAD:main")
            original_sync = loop_manager.ensure_safe_base_worktree
            original_catalog = loop_manager.reconcile_execplan_catalog
            observed = []
            catalog_shas = []

            def synchronize(path, base, *, remote_sha=None, state_dir=None):
                plan = connection.execute(
                    "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'",
                    (str(repo),),
                ).fetchone()
                completion = connection.execute(
                    "SELECT last_completed_plan_id FROM project_completion_cycles "
                    "WHERE repo = ?",
                    (str(repo),),
                ).fetchone()
                observed.append(
                    (plan["status"], remote_sha, worktree.exists(),
                     completion["last_completed_plan_id"], connection.in_transaction)
                )
                return original_sync(
                    path, base, remote_sha=remote_sha, state_dir=state_dir,
                )

            def reconcile_catalog(*args, **kwargs):
                catalog_shas.append(kwargs.get("remote_sha"))
                return original_catalog(*args, **kwargs)

            merged = self._pull_request(branch, url, head, "UNKNOWN", state="MERGED")
            merged["mergedAt"] = "2026-09-24T00:00:00Z"
            with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                "loop_manager.pr_for_branch", return_value=merged
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree", side_effect=synchronize
            ), mock.patch(
                "loop_manager.reconcile_execplan_catalog", side_effect=reconcile_catalog
            ), mock.patch("loop_manager.dispatch_execplan_archive_housekeeping"), mock.patch(
                "loop_manager.run_codex",
                return_value=loop_manager.CodexResult(0, "", "planner-session"),
            ) as planner:
                result = loop_manager.run_project(
                    connection, state_dir, str(repo), Path("unused.ini"), "main", False
                )

            self.assertEqual(result, 0)
            self.assertEqual(observed, [("MERGED", head, False, "G-026", False)])
            self.assertEqual(catalog_shas, [head])
            self.assertEqual(git_command(repo, "rev-parse", "HEAD"), head)
            planner.assert_called_once()

    def test_archive_publication_syncs_source_and_catalog_to_final_remote_sha(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection, branch, worktree, url, head = (
                self._pr_open_state(directory)
            )
            git_command(worktree, "push", "origin", "HEAD:main")
            merged = self._pull_request(branch, url, head, "UNKNOWN", state="MERGED")
            merged["mergedAt"] = "2026-09-24T00:00:00Z"
            original_sync = loop_manager.ensure_safe_base_worktree
            original_catalog = loop_manager.reconcile_execplan_catalog
            source_shas = []
            catalog_shas = []

            def synchronize(path, base, *, remote_sha=None, state_dir=None):
                source_shas.append(remote_sha)
                return original_sync(
                    path, base, remote_sha=remote_sha, state_dir=state_dir,
                )

            def reconcile_catalog(*args, **kwargs):
                if kwargs.get("remote_sha") is not None:
                    catalog_shas.append(kwargs["remote_sha"])
                return original_catalog(*args, **kwargs)

            with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                "loop_manager.pr_for_branch", return_value=merged
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree", side_effect=synchronize
            ), mock.patch(
                "loop_manager.reconcile_execplan_catalog", side_effect=reconcile_catalog
            ), mock.patch(
                "loop_manager.run_codex",
                return_value=loop_manager.CodexResult(0, "", "planner-session"),
            ) as planner:
                result = loop_manager.run_project(
                    connection, state_dir, str(repo), Path("unused.ini"), "main", False
                )

            remote_sha = git_command(repo, "rev-parse", "origin/main")
            self.assertEqual(result, 0)
            self.assertNotEqual(remote_sha, head)
            self.assertEqual(source_shas, [remote_sha])
            self.assertEqual(catalog_shas, [remote_sha])
            self.assertEqual(git_command(repo, "rev-parse", "HEAD"), remote_sha)
            planner.assert_called_once()

    def test_closed_pr_is_blocked_before_source_fast_forward(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection, branch, _worktree, url, head = (
                self._pr_open_state(directory)
            )
            remote_sha = self._advance_base(
                directory, repo, "docs/notes.md", "# After close\n", "Advance after close"
            )
            original_sync = loop_manager.ensure_safe_base_worktree
            observed = []

            def synchronize(path, base, *, remote_sha=None, state_dir=None):
                status = connection.execute(
                    "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'",
                    (str(repo),),
                ).fetchone()["status"]
                observed.append((status, remote_sha))
                return original_sync(
                    path, base, remote_sha=remote_sha, state_dir=state_dir,
                )

            closed = self._pull_request(branch, url, head, "UNKNOWN", state="CLOSED")
            with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                "loop_manager.pr_for_branch", return_value=closed
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree", side_effect=synchronize
            ), mock.patch("loop_manager.dispatch_execplan_archive_housekeeping"):
                result = loop_manager.run_project(
                    connection, state_dir, str(repo), Path("unused.ini"), "main", False
                )

            self.assertEqual(result, 0)
            self.assertEqual(observed, [("PLAN_BLOCKED", remote_sha)])
            self.assertEqual(git_command(repo, "rev-parse", "HEAD"), remote_sha)

    def test_one_merged_pr_syncs_source_while_other_pr_stays_open(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection, branch, worktree, url, head = (
                self._pr_open_state(directory)
            )
            second_plan = write_execplan(repo, "G-027", "Other Open PR")
            commit_and_push(repo, "Add second open plan")
            second_base = git_command(repo, "rev-parse", "origin/main")
            loop_manager._register_execplan_rows(
                connection,
                str(repo),
                repo,
                [(second_plan.relative_to(repo).as_posix(), loop_manager._parse_execplan(repo, second_plan))],
                second_base,
            )
            second_branch, second_worktree = loop_manager.expected_execplan_workspace(
                state_dir, str(repo), "G-027"
            )
            loop_manager.make_worktree(repo, second_worktree, second_branch, "main")
            (second_worktree / "docs/other.md").write_text("Other PR\n", encoding="utf-8")
            git_command(second_worktree, "add", "docs/other.md")
            git_command(second_worktree, "commit", "-m", "Other PR")
            second_head = git_command(second_worktree, "rev-parse", "HEAD")
            second_url = "https://example.test/pr/27"
            with connection:
                connection.execute(
                    "UPDATE exec_plans SET status = 'PR_OPEN', active_sha = ?, "
                    "branch = ?, worktree = ?, workspace_owned = 1, pr_url = ?, "
                    "pr_head_oid = ? WHERE repo = ? AND plan_id = 'G-027'",
                    (second_base, second_branch, str(second_worktree), second_url,
                     second_head, str(repo)),
                )
                connection.execute(
                    "UPDATE projects SET phase = 'PR_OPEN', active_sha = ?, "
                    "branch = ?, worktree = ?, plan_path = ?, pr_url = ?, "
                    "pr_head_oid = ? WHERE repo = ?",
                    (second_base, second_branch, str(second_worktree),
                     second_plan.relative_to(repo).as_posix(), second_url,
                     second_head, str(repo)),
                )
            upstream = Path(directory) / "merge-writer"
            git_command(Path(directory), "clone", str(repo.parent / "remote.git"), str(upstream))
            git_command(upstream, "config", "user.email", "upstream@example.com")
            git_command(upstream, "config", "user.name", "Upstream Test")
            git_command(upstream, "merge", "--no-ff", f"origin/{branch}", "-m", "Merge first PR")
            git_command(upstream, "push", "origin", "HEAD:main")
            merged_head = git_command(upstream, "rev-parse", "HEAD")
            first_pr = self._pull_request(branch, url, head, "UNKNOWN", state="MERGED")
            first_pr["mergedAt"] = "2026-09-24T00:00:00Z"
            second_pr = self._pull_request(second_branch, second_url, second_head, "MERGEABLE")
            responses = {branch: first_pr, second_branch: second_pr}
            original_sync = loop_manager.ensure_safe_base_worktree
            observed = []

            def synchronize(path, base, *, remote_sha=None, state_dir=None):
                statuses = connection.execute(
                    "SELECT plan_id, status FROM exec_plans WHERE repo = ? ORDER BY plan_id",
                    (str(repo),),
                ).fetchall()
                project = connection.execute(
                    "SELECT phase FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                observed.append((tuple(tuple(item) for item in statuses),
                                 project["phase"], remote_sha, second_worktree.exists()))
                return original_sync(
                    path, base, remote_sha=remote_sha, state_dir=state_dir,
                )

            with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                "loop_manager.pr_for_branch", side_effect=lambda _repo, name, _url: responses[name]
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree", side_effect=synchronize
            ), mock.patch("loop_manager.dispatch_execplan_archive_housekeeping"), mock.patch(
                "loop_manager.run_codex",
                return_value=loop_manager.CodexResult(0, "", "planner-session"),
            ) as planner:
                result = loop_manager.run_project(
                    connection, state_dir, str(repo), Path("unused.ini"), "main", False
                )

            self.assertEqual(result, 0)
            self.assertEqual(
                observed,
                [((('G-026', 'MERGED'), ('G-027', 'PR_OPEN')),
                  None, merged_head, True)],
            )
            self.assertEqual(git_command(repo, "rev-parse", "HEAD"), merged_head)
            self.assertTrue(second_worktree.exists())
            planner.assert_called_once()

    def test_fetch_failure_after_merge_keeps_transition_for_source_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection, branch, worktree, url, head = (
                self._pr_open_state(directory)
            )
            git_command(worktree, "push", "origin", "HEAD:main")
            merged = self._pull_request(branch, url, head, "UNKNOWN", state="MERGED")
            merged["mergedAt"] = "2026-09-24T00:00:00Z"
            original_fetch = loop_manager._recorded_base_fetch
            failed = False

            def fail_after_transition(path, *arguments, **kwargs):
                nonlocal failed
                if not failed:
                    status = connection.execute(
                        "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'",
                        (str(repo),),
                    ).fetchone()["status"]
                    if status == "MERGED":
                        failed = True
                        raise RuntimeError("fetch unavailable")
                return original_fetch(path, *arguments, **kwargs)

            with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                "loop_manager.pr_for_branch", return_value=merged
            ), mock.patch("loop_manager._recorded_base_fetch", side_effect=fail_after_transition), mock.patch(
                "loop_manager.dispatch_execplan_archive_housekeeping"
            ):
                with self.assertRaisesRegex(RuntimeError, "fetch unavailable"):
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    )
            self.assertTrue(failed)
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'",
                    (str(repo),),
                ).fetchone()["status"], "MERGED"
            )
            self.assertNotEqual(git_command(repo, "rev-parse", "HEAD"), head)

            with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                "loop_manager.dispatch_execplan_archive_housekeeping"
            ), mock.patch(
                "loop_manager.run_codex",
                return_value=loop_manager.CodexResult(0, "", "planner-session"),
            ):
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ), 0,
                )
            self.assertEqual(git_command(repo, "rev-parse", "HEAD"), head)

    def test_github_failure_defers_source_sync_until_pr_state_is_known(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection, branch, worktree, url, head = (
                self._pr_open_state(directory)
            )
            git_command(worktree, "push", "origin", "HEAD:main")
            with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                "loop_manager.pr_for_branch", side_effect=RuntimeError("GitHub unavailable")
            ), mock.patch("loop_manager.ensure_safe_base_worktree") as synchronize:
                with self.assertRaisesRegex(RuntimeError, "GitHub unavailable"):
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    )
            synchronize.assert_not_called()
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'",
                    (str(repo),),
                ).fetchone()["status"], "PR_OPEN"
            )
            self.assertNotEqual(git_command(repo, "rev-parse", "HEAD"), head)

            merged = self._pull_request(branch, url, head, "UNKNOWN", state="MERGED")
            merged["mergedAt"] = "2026-09-24T00:00:00Z"
            with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                "loop_manager.pr_for_branch", return_value=merged
            ), mock.patch("loop_manager.dispatch_execplan_archive_housekeeping"), mock.patch(
                "loop_manager.run_codex",
                return_value=loop_manager.CodexResult(0, "", "planner-session"),
            ) as planner:
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ), 0,
                )
            self.assertEqual(git_command(repo, "rev-parse", "HEAD"), head)
            planner.assert_called_once()

    def test_unverifiable_merge_cleanup_blocks_source_sync_without_losing_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection, branch, worktree, url, head = (
                self._pr_open_state(directory)
            )
            git_command(worktree, "push", "origin", "HEAD:main")
            with connection:
                connection.execute(
                    "UPDATE exec_plans SET workspace_owned = 0 "
                    "WHERE repo = ? AND plan_id = 'G-026'",
                    (str(repo),),
                )
            merged = self._pull_request(branch, url, head, "UNKNOWN", state="MERGED")
            merged["mergedAt"] = "2026-09-24T00:00:00Z"
            with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                "loop_manager.pr_for_branch", return_value=merged
            ), mock.patch("loop_manager.ensure_safe_base_worktree") as synchronize:
                result = loop_manager.run_project(
                    connection, state_dir, str(repo), Path("unused.ini"), "main", False
                )

            self.assertEqual(result, 1)
            synchronize.assert_not_called()
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'",
                    (str(repo),),
                ).fetchone()["status"], "MERGED"
            )
            self.assertIsNotNone(loop_manager.scoped_blocking.project_block(connection, str(repo)))
            self.assertTrue(worktree.exists())
            self.assertNotEqual(git_command(repo, "rev-parse", "HEAD"), head)

    def test_catalog_rejects_remote_ref_change_after_source_sync(self):
        connection = sqlite3.connect(":memory:")
        try:
            newer = subprocess.CompletedProcess(
                ["git"], 0, stdout="newer-sha\n"
            )
            with mock.patch("loop_manager.git", return_value=newer), mock.patch(
                "loop_manager._GitCatalogDefinitionPort"
            ) as catalog:
                result = loop_manager.reconcile_execplan_catalog(
                    connection, Path("/repo"), "/repo", "main", remote_sha="pinned-sha"
                )

            self.assertEqual(
                result.kind, loop_manager.execplan_catalog_state.ReconciliationKind.STALE
            )
            self.assertEqual(result.revision_sha, "pinned-sha")
            catalog.assert_not_called()
        finally:
            connection.close()

    def test_legacy_ignored_only_preflight_block_recovers_to_pr_open(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                branch,
                worktree,
                pr_url,
                head_oid,
            ) = self._pr_open_state(directory)
            cache = self._add_ignored_cache(worktree)
            error = (
                f"{loop_manager.PR_CONFLICT_PREFLIGHT_ERROR_PREFIX} "
                "PR 충돌 복구 시작 전 관리 worktree가 관측한 원격 head의 "
                "clean 상태와 일치하지 않습니다."
            )
            loop_manager.block_active_execplan_from_revision(
                connection,
                repo,
                str(repo),
                "refs/remotes/origin/main",
                error,
                "verify: PASS",
            )
            blocked = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'",
                (str(repo),),
            ).fetchone()
            loop_manager.hydrate_execplan_project(
                connection, str(repo), blocked
            )
            project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            with mock.patch(
                "loop_manager._fetch_pr_conflict_recovery_refs"
            ) as conflict_fetch, mock.patch(
                "loop_manager._validate_pr_conflict_recovery_ownership"
            ) as conflict_ownership:
                result = loop_manager.reconcile_existing_pull_request(
                    connection,
                    repo,
                    state_dir,
                    str(repo),
                    project,
                    self._pull_request(branch, pr_url, head_oid, "MERGEABLE"),
                )
            conflict_fetch.assert_not_called()
            conflict_ownership.assert_not_called()
            stored = connection.execute(
                """
                SELECT status, blocked_reason, blocked_fingerprint,
                       blocked_worktree_fingerprint, last_error,
                       worker_session_id, reviewer_session_id,
                       verify_result, review_result
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'
                """,
                (str(repo),),
            ).fetchone()
            cache_preserved = cache.is_file()

        self.assertEqual(result, 0)
        self.assertEqual(stored["status"], "PR_OPEN")
        self.assertIsNone(stored["blocked_reason"])
        self.assertIsNone(stored["blocked_fingerprint"])
        self.assertIsNone(stored["blocked_worktree_fingerprint"])
        self.assertIsNone(stored["last_error"])
        self.assertEqual(stored["worker_session_id"], "worker-existing")
        self.assertEqual(stored["reviewer_session_id"], "reviewer-existing")
        self.assertEqual(stored["verify_result"], "verify: PASS")
        self.assertEqual(stored["review_result"], "VERDICT: PASS")
        self.assertTrue(cache_preserved)

    def test_unknown_preflight_block_with_ignored_cache_recovers_without_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection, branch, worktree, url, head = (
                self._pr_open_state(directory)
            )
            cache = self._add_ignored_cache(worktree)
            loop_manager.block_active_execplan_from_revision(
                connection, repo, str(repo), "refs/remotes/origin/main",
                loop_manager.PR_CONFLICT_PREFLIGHT_ERROR_PREFIX + " ignored cache",
                "verify: PASS",
            )
            blocked = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'",
                (str(repo),),
            ).fetchone()
            loop_manager.hydrate_execplan_project(connection, str(repo), blocked)
            project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            with mock.patch("loop_manager._fetch_pr_conflict_recovery_refs") as fetch:
                result = loop_manager.reconcile_existing_pull_request(
                    connection, repo, state_dir, str(repo), project,
                    self._pull_request(branch, url, head, "UNKNOWN"),
                )
            stored = connection.execute(
                "SELECT status, pr_conflict_recovery, verify_result FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-026'", (str(repo),),
            ).fetchone()
            self.assertEqual(result, 0)
            self.assertEqual(tuple(stored), ("PR_OPEN", 0, "verify: PASS"))
            self.assertTrue(cache.is_file())
            fetch.assert_not_called()

    def test_preflight_block_with_ignored_cache_and_tracked_change_stays_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection, branch, worktree, url, head = (
                self._pr_open_state(directory)
            )
            cache = self._add_ignored_cache(worktree)
            loop_manager.block_active_execplan_from_revision(
                connection, repo, str(repo), "refs/remotes/origin/main",
                loop_manager.PR_CONFLICT_PREFLIGHT_ERROR_PREFIX + " ignored cache",
                "verify: PASS",
            )
            (worktree / "src/app.py").write_text("VALUE = 99\n", encoding="utf-8")
            blocked = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'",
                (str(repo),),
            ).fetchone()
            loop_manager.hydrate_execplan_project(connection, str(repo), blocked)
            project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            with mock.patch("loop_manager._fetch_pr_conflict_recovery_refs") as fetch:
                result = loop_manager.reconcile_existing_pull_request(
                    connection, repo, state_dir, str(repo), project,
                    self._pull_request(branch, url, head, "MERGEABLE"),
                )
            stored = connection.execute(
                "SELECT status, last_error FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-026'", (str(repo),),
            ).fetchone()
            self.assertEqual(result, 1)
            self.assertEqual(stored["status"], "PLAN_BLOCKED")
            self.assertIn("src/app.py", stored["last_error"])
            self.assertTrue(cache.is_file())
            fetch.assert_not_called()

    def test_preflight_block_with_conflicting_pr_starts_owned_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection, branch, worktree, url, head = (
                self._pr_open_state(directory)
            )
            cache = self._add_ignored_cache(worktree)
            loop_manager.block_active_execplan_from_revision(
                connection, repo, str(repo), "refs/remotes/origin/main",
                loop_manager.PR_CONFLICT_PREFLIGHT_ERROR_PREFIX + " ignored cache",
                "verify: PASS",
            )
            blocked = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'",
                (str(repo),),
            ).fetchone()
            loop_manager.hydrate_execplan_project(connection, str(repo), blocked)
            project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            with mock.patch(
                "loop_manager.notifications.enqueue_current_project_events",
                side_effect=RuntimeError("interrupt before commit"),
            ), self.assertRaisesRegex(RuntimeError, "interrupt before commit"):
                loop_manager.reconcile_existing_pull_request(
                    connection, repo, state_dir, str(repo), project,
                    self._pull_request(branch, url, head, "CONFLICTING"),
                )
            interrupted = connection.execute(
                "SELECT status, pr_conflict_recovery, pr_conflict_head_oid, "
                "pr_conflict_base_oid FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-026'", (str(repo),),
            ).fetchone()
            self.assertEqual(tuple(interrupted), ("PLAN_BLOCKED", 0, None, None))
            interrupted_project = connection.execute(
                "SELECT phase, pr_conflict_recovery FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(tuple(interrupted_project), ("PLAN_BLOCKED", 0))
            result = loop_manager.reconcile_existing_pull_request(
                connection, repo, state_dir, str(repo), project,
                self._pull_request(branch, url, head, "CONFLICTING"),
            )
            stored = connection.execute(
                "SELECT status, pr_conflict_recovery, pr_conflict_head_oid, "
                "worker_session_id, reviewer_session_id FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-026'", (str(repo),),
            ).fetchone()
            self.assertIsNone(result)
            self.assertEqual(
                tuple(stored),
                ("IMPLEMENTING", 1, head, "worker-existing", "reviewer-existing"),
            )
            resumed_project = connection.execute(
                "SELECT phase, pr_conflict_recovery, pr_conflict_head_oid "
                "FROM projects WHERE repo = ?", (str(repo),),
            ).fetchone()
            self.assertEqual(tuple(resumed_project), ("WORKING", 1, head))
            self.assertTrue(cache.is_file())

    def test_conflicting_pr_blocks_ignored_path_overlapping_base_update(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection, branch, worktree, url, head = (
                self._pr_open_state(directory)
            )
            exclude = Path(git_command(worktree, "rev-parse", "--git-path", "info/exclude"))
            if not exclude.is_absolute():
                exclude = worktree / exclude
            exclude.write_text(
                exclude.read_text(encoding="utf-8") + "\noverlap.txt\n",
                encoding="utf-8",
            )
            artifact = worktree / "overlap.txt"
            artifact.write_text("preserve ignored artifact\n", encoding="utf-8")
            self._advance_base(
                directory, repo, "overlap.txt", "tracked base value\n",
                "Add overlapping base file",
            )
            project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            with mock.patch("loop_manager.git", wraps=loop_manager.git) as git_spy:
                result = loop_manager.reconcile_existing_pull_request(
                    connection, repo, state_dir, str(repo), project,
                    self._pull_request(branch, url, head, "CONFLICTING"),
                )
            stored = connection.execute(
                "SELECT status, last_error FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-026'", (str(repo),),
            ).fetchone()
            self.assertEqual(result, 1)
            self.assertEqual(stored["status"], "PLAN_BLOCKED")
            self.assertIn("overlap.txt", stored["last_error"])
            self.assertEqual(artifact.read_text(encoding="utf-8"), "preserve ignored artifact\n")
            self.assertFalse(any(call.args[1:2] == ("rebase",) for call in git_spy.call_args_list))

    def test_conflicting_pr_rejects_nondefault_index_flags_with_ignored_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection, branch, worktree, url, head = (
                self._pr_open_state(directory)
            )
            cache = self._add_ignored_cache(worktree)
            git_command(worktree, "update-index", "--skip-worktree", "src/app.py")
            project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            result = loop_manager.reconcile_existing_pull_request(
                connection, repo, state_dir, str(repo), project,
                self._pull_request(branch, url, head, "CONFLICTING"),
            )
            stored = connection.execute(
                "SELECT status, last_error FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-026'", (str(repo),),
            ).fetchone()
            self.assertEqual(result, 1)
            self.assertEqual(stored["status"], "PLAN_BLOCKED")
            self.assertIn("index flags src/app.py", stored["last_error"])
            self.assertTrue(cache.is_file())

    def test_conflicting_pr_still_blocks_nonignored_untracked_change(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                branch,
                worktree,
                pr_url,
                head_oid,
            ) = self._pr_open_state(directory)
            (worktree / "unexpected.txt").write_text(
                "publishable local change\n",
                encoding="utf-8",
            )
            project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            result = loop_manager.reconcile_existing_pull_request(
                connection,
                repo,
                state_dir,
                str(repo),
                project,
                self._pull_request(branch, pr_url, head_oid, "CONFLICTING"),
            )
            stored = connection.execute(
                """
                SELECT status, blocked_reason, last_error
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'
                """,
                (str(repo),),
            ).fetchone()

        self.assertEqual(result, 1)
        self.assertEqual(stored["status"], "PLAN_BLOCKED")
        self.assertEqual(
            stored["blocked_reason"],
            loop_manager.BlockReasonCode.MANAGED_WORKTREE_CONFLICT.value,
        )
        self.assertIn("unexpected.txt", stored["last_error"])

    def test_conflicting_pr_reuses_sessions_revalidates_new_base_and_leases_push(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                branch,
                worktree,
                pr_url,
                head_oid,
            ) = self._pr_open_state(directory)
            cache = self._add_ignored_cache(worktree)
            self._advance_base(
                directory,
                repo,
                "src/app.py",
                "VALUE = 3\n",
                "Create PR conflict",
            )
            source_before = (
                git_command(repo, "rev-parse", "HEAD"),
                git_command(repo, "symbolic-ref", "--short", "HEAD"),
                git_command(repo, "rev-parse", "refs/heads/main"),
                git_command(repo, "status", "--porcelain"),
            )
            pull_request = self._pull_request(
                branch, pr_url, head_oid, "CONFLICTING"
            )
            project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            started = loop_manager.reconcile_existing_pull_request(
                connection,
                repo,
                state_dir,
                str(repo),
                project,
                pull_request,
            )
            durable = connection.execute(
                """
                SELECT status, pr_conflict_recovery, pr_conflict_head_oid,
                       pr_conflict_base_oid, worker_session_id,
                       reviewer_session_id
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'
                """,
                (str(repo),),
            ).fetchone()
            self.assertIsNone(started)
            self.assertEqual(durable["status"], "IMPLEMENTING")
            self.assertEqual(durable["pr_conflict_recovery"], 1)
            self.assertEqual(durable["pr_conflict_head_oid"], head_oid)
            self.assertEqual(
                durable["pr_conflict_base_oid"],
                git_command(repo, "rev-parse", "origin/main"),
            )
            self.assertEqual(durable["worker_session_id"], "worker-existing")
            self.assertEqual(
                durable["reviewer_session_id"], "reviewer-existing"
            )
            self.assertFalse(
                loop_manager._rebase_in_progress_for_branch(worktree, branch)
            )
            self.assertTrue(cache.is_file())

            roles = []
            session_ids = []
            review_count = 0
            pushes = []
            created = []
            original_git = loop_manager.git
            original_command = loop_manager.command

            def agent_runner(
                agent_worktree,
                _state,
                _config,
                role_name,
                _prompt,
                **kwargs,
            ):
                nonlocal review_count
                roles.append(role_name)
                session_ids.append(kwargs["session_id"])
                if role_name == "worker":
                    (agent_worktree / "src/app.py").write_text(
                        "VALUE = 4\n", encoding="utf-8"
                    )
                    return loop_manager.CodexResult(
                        0,
                        worker_result(
                            "Resolve owned PR conflict",
                            "Rebases and resolves the existing owned PR.",
                            "AutomaticOwnedPrConflictRecoveryTest: PASS",
                        ),
                        "worker-existing",
                    )
                review_count += 1
                if review_count == 1:
                    self._advance_base(
                        directory,
                        repo,
                        "docs/notes.md",
                        "# Base moved during review\n",
                        "Advance base during recovery review",
                    )
                return loop_manager.CodexResult(
                    0, "VERDICT: PASS", "reviewer-existing"
                )

            def recording_git(path, *arguments, **kwargs):
                if arguments and arguments[0] == "push":
                    pushes.append(arguments)
                return original_git(path, *arguments, **kwargs)

            def recording_command(arguments, **kwargs):
                if arguments[:3] == ["gh", "pr", "create"]:
                    created.append(arguments)
                    raise AssertionError("conflict recovery must not create a PR")
                return original_command(arguments, **kwargs)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ) as synchronize, mock.patch(
                "loop_manager.pr_for_branch", return_value=pull_request
            ), mock.patch(
                "loop_manager.verify", return_value=(True, "verify: PASS")
            ) as verify, mock.patch(
                "loop_manager.run_codex", side_effect=agent_runner
            ), mock.patch(
                "loop_manager.git", side_effect=recording_git
            ), mock.patch(
                "loop_manager.command", side_effect=recording_command
            ):
                outcome = loop_manager.run_project_result(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            stored = connection.execute(
                """
                SELECT status, validated_base_sha, publication_remote_oid,
                       pr_url, pr_head_oid, pr_conflict_recovery,
                       pr_conflict_head_oid, pr_conflict_base_oid,
                       worker_session_id, reviewer_session_id
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'
                """,
                (str(repo),),
            ).fetchone()
            final_head = git_command(worktree, "rev-parse", "HEAD")
            source_after = (
                git_command(repo, "rev-parse", "HEAD"),
                git_command(repo, "symbolic-ref", "--short", "HEAD"),
                git_command(repo, "rev-parse", "refs/heads/main"),
                git_command(repo, "status", "--porcelain"),
            )
            lease = f"--force-with-lease=refs/heads/{branch}:{head_oid}"

            self.assertEqual(outcome.kind, loop_manager.ProjectRunKind.WAITING)
            self.assertEqual(stored["status"], "PR_OPEN")
            self.assertEqual(stored["publication_remote_oid"], final_head)
            self.assertEqual(stored["pr_head_oid"], final_head)
            self.assertEqual(stored["pr_url"], pr_url)
            self.assertEqual(stored["pr_conflict_recovery"], 0)
            self.assertIsNone(stored["pr_conflict_head_oid"])
            self.assertIsNone(stored["pr_conflict_base_oid"])
            self.assertEqual(stored["worker_session_id"], "worker-existing")
            self.assertEqual(stored["reviewer_session_id"], "reviewer-existing")
            self.assertEqual(roles, ["worker", "reviewer", "reviewer"])
            self.assertEqual(
                session_ids,
                ["worker-existing", "reviewer-existing", "reviewer-existing"],
            )
            self.assertEqual(verify.call_count, 2)
            self.assertEqual(len(pushes), 1)
            self.assertIn(lease, pushes[0])
            self.assertNotIn("--force", pushes[0])
            self.assertEqual(created, [])
            self.assertEqual(source_after, source_before)
            synchronize.assert_not_called()

    def test_success_preserves_dirty_diverged_source_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                branch,
                _worktree,
                pr_url,
                head_oid,
            ) = self._pr_open_state(directory)
            self._make_source_checkout_dirty_and_ahead(repo)
            self._advance_base(
                directory,
                repo,
                "docs/notes.md",
                "# Remote base advance\n",
                "Diverge source main",
            )
            git_command(repo, "fetch", "origin")
            local_main = git_command(repo, "rev-parse", "refs/heads/main")
            remote_main = git_command(repo, "rev-parse", "origin/main")
            self.assertNotEqual(local_main, remote_main)
            for ancestor, descendant in (
                (local_main, remote_main),
                (remote_main, local_main),
            ):
                relation = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repo),
                        "merge-base",
                        "--is-ancestor",
                        ancestor,
                        descendant,
                    ],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
                self.assertEqual(relation.returncode, 1)
            source_before = self._source_checkout_snapshot(repo)
            pull_request = self._pull_request(
                branch, pr_url, head_oid, "CONFLICTING"
            )

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ) as synchronize, mock.patch(
                "loop_manager.pr_for_branch", return_value=pull_request
            ), mock.patch(
                "loop_manager.verify", return_value=(True, "verify: PASS")
            ), mock.patch(
                "loop_manager.run_codex",
                return_value=loop_manager.CodexResult(
                    0, "VERDICT: PASS", "reviewer-existing"
                ),
            ):
                outcome = loop_manager.run_project_result(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            stored = connection.execute(
                "SELECT status, pr_conflict_recovery FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-026'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(outcome.kind, loop_manager.ProjectRunKind.WAITING)
            self.assertEqual(tuple(stored), ("PR_OPEN", 0))
            self.assertEqual(self._source_checkout_snapshot(repo), source_before)
            synchronize.assert_not_called()

    def test_external_head_block_persists_and_preserves_dirty_ahead_source(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                branch,
                _worktree,
                pr_url,
                observed_head,
            ) = self._pr_open_state(directory)
            self._make_source_checkout_dirty_and_ahead(repo)
            source_before = self._source_checkout_snapshot(repo)
            original_pull_request = self._pull_request(
                branch, pr_url, observed_head, "CONFLICTING"
            )
            project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertIsNone(
                loop_manager.reconcile_existing_pull_request(
                    connection,
                    repo,
                    state_dir,
                    str(repo),
                    project,
                    original_pull_request,
                )
            )

            upstream = Path(directory) / "external-recovery-head"
            git_command(
                Path(directory),
                "clone",
                str(repo.parent / "remote.git"),
                str(upstream),
            )
            external_head = git_command(upstream, "rev-parse", "HEAD")
            git_command(
                upstream,
                "push",
                "--force",
                "origin",
                f"HEAD:refs/heads/{branch}",
            )
            pull_request = self._pull_request(
                branch, pr_url, external_head, "CONFLICTING"
            )
            pushes = []
            original_git = loop_manager.git

            def recording_git(path, *arguments, **kwargs):
                if arguments and arguments[0] == "push":
                    pushes.append(arguments)
                return original_git(path, *arguments, **kwargs)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=pull_request
            ), mock.patch(
                "loop_manager.run_codex"
            ) as agent, mock.patch(
                "loop_manager.git", side_effect=recording_git
            ):
                first = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )
                second = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            stored = connection.execute(
                """
                SELECT status, pr_conflict_recovery, pr_conflict_head_oid,
                       worker_session_id, reviewer_session_id
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'
                """,
                (str(repo),),
            ).fetchone()
            self.assertNotEqual(external_head, observed_head)
            self.assertEqual((first, second), (1, 1))
            self.assertEqual(stored["status"], "IMPLEMENTING")
            self.assertEqual(stored["pr_conflict_recovery"], 1)
            self.assertEqual(stored["pr_conflict_head_oid"], observed_head)
            project = connection.execute(
                "SELECT phase FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(project["phase"], "PROJECT_BLOCKED")
            self.assertEqual(stored["worker_session_id"], "worker-existing")
            self.assertEqual(stored["reviewer_session_id"], "reviewer-existing")
            self.assertEqual(self._source_checkout_snapshot(repo), source_before)
            self.assertEqual(pushes, [])
            agent.assert_not_called()

    def test_external_remote_head_change_blocks_without_push(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                branch,
                _worktree,
                pr_url,
                head_oid,
            ) = self._pr_open_state(directory)
            upstream = Path(directory) / "external-head"
            git_command(Path(directory), "clone", str(repo.parent / "remote.git"), str(upstream))
            external_head = git_command(upstream, "rev-parse", "HEAD")
            git_command(
                upstream,
                "push",
                "--force",
                "origin",
                f"HEAD:refs/heads/{branch}",
            )
            pushes = []
            original_git = loop_manager.git

            def recording_git(path, *arguments, **kwargs):
                if arguments and arguments[0] == "push":
                    pushes.append(arguments)
                return original_git(path, *arguments, **kwargs)

            pull_request = self._pull_request(
                branch, pr_url, external_head, "CONFLICTING"
            )
            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=pull_request
            ), mock.patch(
                "loop_manager.run_codex"
            ) as agent, mock.patch(
                "loop_manager.git", side_effect=recording_git
            ):
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            stored = connection.execute(
                "SELECT phase, pr_conflict_recovery, last_error "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertNotEqual(external_head, head_oid)
            self.assertEqual(result, 1)
            self.assertEqual(stored["phase"], "PROJECT_BLOCKED")
            self.assertEqual(stored["pr_conflict_recovery"], 0)
            self.assertIn("head OID", stored["last_error"])
            self.assertEqual(pushes, [])
            agent.assert_not_called()

    def test_worker_failure_blocks_after_three_corrections_and_stays_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                branch,
                worktree,
                pr_url,
                head_oid,
            ) = self._pr_open_state(directory)
            self._advance_base(
                directory,
                repo,
                "src/app.py",
                "VALUE = 3\n",
                "Conflict for failed Worker",
            )
            pull_request = self._pull_request(
                branch, pr_url, head_oid, "CONFLICTING"
            )
            failed_worker = loop_manager.CodexResult(
                1,
                "Worker cannot resolve this conflict",
                "worker-existing",
                loop_manager.AgentResultKind.PERMANENT_CONFIGURATION,
            )
            roles = []

            def failed_agent(
                _worktree,
                _state,
                _config,
                role_name,
                _prompt,
                **_kwargs,
            ):
                roles.append(role_name)
                return failed_worker

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=pull_request
            ), mock.patch(
                "loop_manager.run_codex", side_effect=failed_agent
            ) as agent:
                first = loop_manager.run_project_result(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )
                second = loop_manager.run_project_result(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            stored = connection.execute(
                """
                SELECT status, pr_conflict_recovery, pr_conflict_head_oid,
                       worker_session_id, reviewer_session_id
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'
                """,
                (str(repo),),
            ).fetchone()
            self.assertEqual(first.kind, loop_manager.ProjectRunKind.WAITING)
            self.assertEqual(second.kind, loop_manager.ProjectRunKind.WAITING)
            self.assertEqual(stored["status"], "PLAN_BLOCKED")
            self.assertEqual(stored["pr_conflict_recovery"], 1)
            self.assertEqual(stored["pr_conflict_head_oid"], head_oid)
            self.assertEqual(stored["worker_session_id"], "worker-existing")
            self.assertEqual(stored["reviewer_session_id"], "reviewer-existing")
            self.assertTrue(
                loop_manager._rebase_in_progress_for_branch(worktree, branch)
            )
            self.assertEqual(roles, ["worker", "worker", "worker", "worker", "planner"])
            self.assertEqual(agent.call_count, 5)

    def test_repeated_verify_fingerprint_blocks_recovery_without_auto_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                branch,
                worktree,
                pr_url,
                head_oid,
            ) = self._pr_open_state(directory)
            pull_request = self._pull_request(
                branch, pr_url, head_oid, "CONFLICTING"
            )
            project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertIsNone(
                loop_manager.reconcile_existing_pull_request(
                    connection, repo, state_dir, str(repo), project, pull_request
                )
            )
            output = (
                "FAILED tests/test_conflict.py::test_owned_pr_recovery\n"
                "AssertionError: same recovery failure"
            )
            decisions = []
            for _attempt in range(
                loop_manager.MAX_VALIDATION_FINGERPRINT_REPEATS + 1
            ):
                loop_manager.update_project(
                    connection, str(repo), phase="VERIFYING"
                )
                decisions.append(
                    loop_manager.transition_validation_failure(
                        connection,
                        str(repo),
                        worktree,
                        "verify",
                        output,
                    )
                )

            blocked_plan = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'",
                (str(repo),),
            ).fetchone()
            loop_manager.hydrate_execplan_project(
                connection, str(repo), blocked_plan
            )
            blocked_project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(
                loop_manager.reconcile_existing_pull_request(
                    connection,
                    repo,
                    state_dir,
                    str(repo),
                    blocked_project,
                    pull_request,
                ),
                1,
            )
            stored = connection.execute(
                """
                SELECT status, pr_conflict_recovery, pr_conflict_head_oid,
                       verify_failure_history
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'
                """,
                (str(repo),),
            ).fetchone()
            history = json.loads(stored["verify_failure_history"])
            self.assertFalse(any(item.blocked for item in decisions[:-1]))
            self.assertTrue(decisions[-1].blocked)
            self.assertEqual(stored["status"], "PLAN_BLOCKED")
            self.assertEqual(stored["pr_conflict_recovery"], 1)
            self.assertEqual(stored["pr_conflict_head_oid"], head_oid)
            self.assertEqual(
                next(iter(history.values()))["occurrences"],
                loop_manager.MAX_VALIDATION_FINGERPRINT_REPEATS + 1,
            )

    def test_transient_and_usage_limit_pauses_resume_same_recovery(self):
        pauses = (
            (
                loop_manager.AgentResultKind.TRANSIENT_FAILURE,
                loop_manager.ProjectRunKind.TRANSIENT_PAUSE,
                "temporary provider failure",
            ),
            (
                loop_manager.AgentResultKind.USAGE_LIMIT,
                loop_manager.ProjectRunKind.USAGE_LIMIT_PAUSE,
                "usage limit reached",
            ),
        )
        for result_kind, expected_kind, message in pauses:
            with self.subTest(result_kind=result_kind), tempfile.TemporaryDirectory() as directory:
                (
                    repo,
                    state_dir,
                    connection,
                    branch,
                    _worktree,
                    pr_url,
                    head_oid,
                ) = self._pr_open_state(directory)
                self._make_source_checkout_dirty_and_ahead(repo)
                self._advance_base(
                    directory,
                    repo,
                    "src/app.py",
                    "VALUE = 3\n",
                    f"Conflict before {result_kind.value}",
                )
                git_command(repo, "fetch", "origin")
                source_before = self._source_checkout_snapshot(repo)
                pull_request = self._pull_request(
                    branch, pr_url, head_oid, "CONFLICTING"
                )
                paused_worker = loop_manager.CodexResult(
                    1,
                    message,
                    "worker-existing",
                    result_kind,
                )

                with mock.patch(
                    "loop_manager.validate_repository", return_value=repo
                ), mock.patch(
                    "loop_manager.pr_for_branch", return_value=pull_request
                ), mock.patch(
                    "loop_manager.run_codex", return_value=paused_worker
                ):
                    paused = loop_manager.run_project_result(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )

                preserved = connection.execute(
                    """
                    SELECT status, pr_conflict_recovery, pr_conflict_head_oid,
                           worker_session_id, reviewer_session_id
                    FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'
                    """,
                    (str(repo),),
                ).fetchone()
                self.assertEqual(paused.kind, expected_kind)
                self.assertEqual(preserved["status"], "IMPLEMENTING")
                self.assertEqual(preserved["pr_conflict_recovery"], 1)
                self.assertEqual(preserved["pr_conflict_head_oid"], head_oid)
                self.assertEqual(preserved["worker_session_id"], "worker-existing")
                self.assertEqual(preserved["reviewer_session_id"], "reviewer-existing")
                self.assertEqual(
                    self._source_checkout_snapshot(repo), source_before
                )

                roles = []

                def successful_agent(
                    agent_worktree,
                    _state,
                    _config,
                    role_name,
                    _prompt,
                    **_kwargs,
                ):
                    roles.append(role_name)
                    if role_name == "worker":
                        (agent_worktree / "src/app.py").write_text(
                            "VALUE = 4\n", encoding="utf-8"
                        )
                        return loop_manager.CodexResult(
                            0,
                            worker_result(
                                "Resume paused PR recovery",
                                "Resolves the preserved conflict.",
                                "AutomaticOwnedPrConflictRecoveryTest: PASS",
                            ),
                            "worker-existing",
                        )
                    return loop_manager.CodexResult(
                        0, "VERDICT: PASS", "reviewer-existing"
                    )

                with mock.patch(
                    "loop_manager.validate_repository", return_value=repo
                ), mock.patch(
                    "loop_manager.pr_for_branch", return_value=pull_request
                ), mock.patch(
                    "loop_manager.verify", return_value=(True, "verify: PASS")
                ), mock.patch(
                    "loop_manager.run_codex", side_effect=successful_agent
                ):
                    resumed = loop_manager.run_project_result(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )

                final = connection.execute(
                    "SELECT status, pr_conflict_recovery FROM exec_plans "
                    "WHERE repo = ? AND plan_id = 'G-026'",
                    (str(repo),),
                ).fetchone()
                self.assertEqual(resumed.kind, loop_manager.ProjectRunKind.WAITING)
                self.assertEqual(tuple(final), ("PR_OPEN", 0))
                self.assertEqual(roles, ["worker", "reviewer"])
                self.assertEqual(
                    self._source_checkout_snapshot(repo), source_before
                )

    def test_lease_competition_blocks_without_overwriting_remote_head(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                branch,
                worktree,
                pr_url,
                observed_head,
            ) = self._pr_open_state(directory)
            (worktree / "docs/notes.md").write_text(
                "# Locally recovered head\n", encoding="utf-8"
            )
            git_command(worktree, "add", "docs/notes.md")
            git_command(worktree, "commit", "-m", "Prepare lease race head")
            local_head = git_command(worktree, "rev-parse", "HEAD")
            base_oid = git_command(repo, "rev-parse", "origin/main")
            loop_manager.update_project(
                connection,
                str(repo),
                phase="PUBLISHING",
                active_sha=base_oid,
                validated_base_sha=base_oid,
                pr_conflict_recovery=1,
                pr_conflict_head_oid=observed_head,
                pr_conflict_base_oid=base_oid,
            )
            seed_validated_candidate(connection, repo, worktree, "G-026")

            competitor = Path(directory) / "lease-competitor"
            git_command(
                Path(directory),
                "clone",
                str(repo.parent / "remote.git"),
                str(competitor),
            )
            git_command(competitor, "config", "user.email", "race@example.com")
            git_command(competitor, "config", "user.name", "Lease Race")
            (competitor / "lease-race.txt").write_text(
                "external update\n", encoding="utf-8"
            )
            git_command(competitor, "add", "lease-race.txt")
            git_command(competitor, "commit", "-m", "Win lease race")
            external_head = git_command(competitor, "rev-parse", "HEAD")
            pull_request = self._pull_request(
                branch, pr_url, observed_head, "CONFLICTING"
            )
            original_git = loop_manager.git
            pushes = []
            raced = False

            def racing_git(path, *arguments, **kwargs):
                nonlocal raced
                if arguments and arguments[0] == "push":
                    pushes.append(arguments)
                    if not raced:
                        raced = True
                        git_command(
                            competitor,
                            "push",
                            "--force",
                            "origin",
                            f"HEAD:refs/heads/{branch}",
                        )
                return original_git(path, *arguments, **kwargs)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=pull_request
            ), mock.patch(
                "loop_manager.git", side_effect=racing_git
            ):
                outcome = loop_manager.run_project_result(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            git_command(repo, "fetch", "origin", branch)
            stored = connection.execute(
                """
                SELECT status, pr_conflict_recovery, pr_conflict_head_oid
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'
                """,
                (str(repo),),
            ).fetchone()
            self.assertEqual(
                outcome.kind, loop_manager.ProjectRunKind.BLOCKED,
                (outcome.detail, stored["status"],
                 stored["pr_conflict_recovery"], pushes),
            )
            self.assertEqual(stored["status"], "PUBLISHING")
            self.assertEqual(stored["pr_conflict_recovery"], 1)
            self.assertEqual(stored["pr_conflict_head_oid"], observed_head)
            self.assertEqual(
                git_command(repo, "rev-parse", f"origin/{branch}"),
                external_head,
            )
            self.assertEqual(git_command(worktree, "rev-parse", "HEAD"), local_head)
            self.assertEqual(len(pushes), 1)
            self.assertIn(
                f"--force-with-lease=refs/heads/{branch}:{observed_head}",
                pushes[0],
            )

    def test_post_push_interruption_confirms_local_head_without_repush(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                branch,
                worktree,
                pr_url,
                observed_head,
            ) = self._pr_open_state(directory)
            (worktree / "docs/notes.md").write_text(
                "# Recovered push\n", encoding="utf-8"
            )
            git_command(worktree, "add", "docs/notes.md")
            git_command(worktree, "commit", "-m", "Prepare recovered PR head")
            local_head = git_command(worktree, "rev-parse", "HEAD")
            git_command(
                worktree,
                "push",
                "--force-with-lease",
                "origin",
                f"HEAD:refs/heads/{branch}",
            )
            base_oid = git_command(repo, "rev-parse", "origin/main")
            loop_manager.update_project(
                connection,
                str(repo),
                phase="PUBLISHING",
                active_sha=base_oid,
                validated_base_sha=base_oid,
                pr_conflict_recovery=1,
                pr_conflict_head_oid=observed_head,
                pr_conflict_base_oid=base_oid,
            )
            project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            pull_request = self._pull_request(
                branch, pr_url, local_head, "MERGEABLE"
            )
            pushes = []
            original_git = loop_manager.git

            def recording_git(path, *arguments, **kwargs):
                if arguments and arguments[0] == "push":
                    pushes.append(arguments)
                return original_git(path, *arguments, **kwargs)

            with mock.patch("loop_manager.git", side_effect=recording_git):
                result = loop_manager.reconcile_existing_pull_request(
                    connection,
                    repo,
                    state_dir,
                    str(repo),
                    project,
                    pull_request,
                )

            stored = connection.execute(
                """
                SELECT status, publication_remote_oid, pr_head_oid,
                       pr_conflict_recovery, pr_conflict_head_oid,
                       pr_conflict_base_oid
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'
                """,
                (str(repo),),
            ).fetchone()
            self.assertEqual(result, 0)
            self.assertEqual(stored["status"], "PR_OPEN")
            self.assertEqual(stored["publication_remote_oid"], local_head)
            self.assertEqual(stored["pr_head_oid"], local_head)
            self.assertEqual(stored["pr_conflict_recovery"], 0)
            self.assertIsNone(stored["pr_conflict_head_oid"])
            self.assertIsNone(stored["pr_conflict_base_oid"])
            self.assertEqual(pushes, [])

    def test_post_push_ownership_failure_blocks_and_preserves_recovery(self):
        for damage in ("missing-worktree", "wrong-branch"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as directory:
                repo, state_dir, connection, branch, worktree, url, observed_head = (
                    self._pr_open_state(directory)
                )
                (worktree / "docs/notes.md").write_text("# Recovered head\n", encoding="utf-8")
                git_command(worktree, "add", "docs/notes.md")
                git_command(worktree, "commit", "-m", "Prepare interrupted push")
                local_head = git_command(worktree, "rev-parse", "HEAD")
                git_command(
                    worktree, "push",
                    f"--force-with-lease=refs/heads/{branch}:{observed_head}",
                    "origin", f"HEAD:refs/heads/{branch}",
                )
                base = git_command(repo, "rev-parse", "origin/main")
                loop_manager.update_project(
                    connection, str(repo), phase="PUBLISHING",
                    active_sha=base, validated_base_sha=base,
                    pr_conflict_recovery=1, pr_conflict_head_oid=observed_head,
                    pr_conflict_base_oid=base,
                )
                self._make_source_checkout_dirty_and_ahead(repo)
                source_before = self._source_checkout_snapshot(repo)
                if damage == "missing-worktree":
                    preserved = worktree.with_name("preserved-worktree")
                    worktree.rename(preserved)
                else:
                    preserved = worktree
                    git_command(worktree, "checkout", "-b", "user-owned-branch")
                evidence = preserved / "keep-untracked.txt"
                evidence.write_text("preserve this evidence\n", encoding="utf-8")
                original_git = loop_manager.git
                writes = []

                def recording_git(path, *arguments, **kwargs):
                    if arguments and arguments[0] in {"push", "rebase", "reset", "clean"}:
                        writes.append(arguments)
                    return original_git(path, *arguments, **kwargs)

                pull_request = self._pull_request(branch, url, local_head, "MERGEABLE")
                with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                    "loop_manager.pr_for_branch", return_value=pull_request
                ), mock.patch("loop_manager.git", side_effect=recording_git), mock.patch(
                    "loop_manager.run_codex"
                ) as agent:
                    for _attempt in range(2):
                        result = loop_manager.run_project_result(
                            connection, state_dir, str(repo), Path("unused.ini"), "main", False
                        )
                        self.assertEqual(result.kind, loop_manager.ProjectRunKind.BLOCKED)
                        plan = connection.execute(
                            "SELECT * FROM exec_plans "
                            "WHERE repo = ? AND plan_id = 'G-026'",
                            (str(repo),),
                        ).fetchone()
                        self.assertEqual(plan["status"], "PUBLISHING")
                        self.assertIn("안전 검증 실패", plan["last_error"])
                        self.assertEqual(plan["pr_conflict_recovery"], 1)
                        self.assertEqual(plan["pr_conflict_head_oid"], observed_head)
                        self.assertEqual(plan["pr_conflict_base_oid"], base)
                        self.assertEqual(plan["pr_head_oid"], observed_head)
                        self.assertEqual(plan["worker_session_id"], "worker-existing")
                        self.assertEqual(plan["reviewer_session_id"], "reviewer-existing")
                        project = connection.execute(
                            "SELECT phase FROM projects WHERE repo = ?", (str(repo),)
                        ).fetchone()
                        self.assertEqual(project["phase"], "PROJECT_BLOCKED")
                    agent.assert_not_called()
                self.assertEqual(writes, [])
                self.assertEqual(self._source_checkout_snapshot(repo), source_before)
                self.assertEqual(evidence.read_text(encoding="utf-8"), "preserve this evidence\n")
                self.assertEqual((preserved / "docs/notes.md").read_text(
                    encoding="utf-8"), "# Recovered head\n")
                self.assertEqual(git_command(repo, "rev-parse", f"refs/heads/{branch}"), local_head)
                self.assertEqual(git_command(
                    repo.parent / "remote.git", "rev-parse", f"refs/heads/{branch}"
                ), local_head)

    def test_only_closed_pr_block_resumes_when_pr_reopens(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                branch,
                _worktree,
                pr_url,
                head_oid,
            ) = self._pr_open_state(directory)
            base_oid = git_command(repo, "rev-parse", "origin/main")
            loop_manager.update_project(
                connection,
                str(repo),
                phase="PUBLISHING",
                active_sha=base_oid,
                validated_base_sha=base_oid,
                pr_conflict_recovery=1,
                pr_conflict_head_oid=head_oid,
                pr_conflict_base_oid=base_oid,
            )
            project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            closed = self._pull_request(
                branch, pr_url, head_oid, "CONFLICTING", state="CLOSED"
            )
            self.assertEqual(
                loop_manager.reconcile_existing_pull_request(
                    connection, repo, state_dir, str(repo), project, closed
                ),
                1,
            )
            blocked = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(blocked["status"], "PLAN_BLOCKED")
            self.assertEqual(
                blocked["blocked_reason"],
                loop_manager.PR_CLOSED_BLOCK_REASON,
            )

            loop_manager.hydrate_execplan_project(
                connection, str(repo), blocked
            )
            blocked_project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            reopened = self._pull_request(
                branch, pr_url, head_oid, "CONFLICTING"
            )
            self.assertIsNone(
                loop_manager.reconcile_existing_pull_request(
                    connection,
                    repo,
                    state_dir,
                    str(repo),
                    blocked_project,
                    reopened,
                )
            )
            resumed = connection.execute(
                """
                SELECT status, blocked_reason, pr_conflict_recovery,
                       pr_conflict_head_oid
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'
                """,
                (str(repo),),
            ).fetchone()
            self.assertEqual(resumed["status"], "IMPLEMENTING")
            self.assertIsNone(resumed["blocked_reason"])
            self.assertEqual(resumed["pr_conflict_recovery"], 1)
            self.assertEqual(resumed["pr_conflict_head_oid"], head_oid)

    def test_recovery_merge_and_close_races_never_push(self):
        for state in ("CLOSED", "MERGED"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                (
                    repo,
                    state_dir,
                    connection,
                    branch,
                    worktree,
                    pr_url,
                    head_oid,
                ) = self._pr_open_state(directory)
                base_oid = git_command(repo, "rev-parse", "origin/main")
                loop_manager.update_project(
                    connection,
                    str(repo),
                    phase="PUBLISHING",
                    active_sha=base_oid,
                    validated_base_sha=base_oid,
                    pr_conflict_recovery=1,
                    pr_conflict_head_oid=head_oid,
                    pr_conflict_base_oid=base_oid,
                )
                project = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                pull_request = self._pull_request(
                    branch, pr_url, head_oid, "CONFLICTING", state=state
                )
                if state == "MERGED":
                    pull_request["mergedAt"] = "2026-09-21T00:00:00Z"
                pushes = []
                original_git = loop_manager.git

                def recording_git(path, *arguments, **kwargs):
                    if arguments and arguments[0] == "push":
                        pushes.append(arguments)
                    return original_git(path, *arguments, **kwargs)

                with mock.patch(
                    "loop_manager.git", side_effect=recording_git
                ):
                    result = loop_manager.reconcile_existing_pull_request(
                        connection,
                        repo,
                        state_dir,
                        str(repo),
                        project,
                        pull_request,
                    )

                stored = connection.execute(
                    "SELECT status, pr_conflict_recovery FROM exec_plans "
                    "WHERE repo = ? AND plan_id = 'G-026'",
                    (str(repo),),
                ).fetchone()
                self.assertEqual(pushes, [])
                if state == "CLOSED":
                    self.assertEqual(result, 1)
                    self.assertEqual(tuple(stored), ("PLAN_BLOCKED", 1))
                    self.assertTrue(worktree.is_dir())
                else:
                    self.assertEqual(
                        result, loop_manager.PROJECT_STEP_PLANNING_PENDING
                    )
                    self.assertEqual(tuple(stored), ("MERGED", 0))
                    self.assertFalse(worktree.exists())

    def test_reopened_head_safety_failure_cannot_resume_when_head_is_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection, branch, worktree, url, head = (
                self._pr_open_state(directory)
            )
            base = git_command(repo, "rev-parse", "origin/main")
            loop_manager.update_project(
                connection, str(repo), phase="PUBLISHING",
                pr_conflict_recovery=1, pr_conflict_head_oid=head,
                pr_conflict_base_oid=base,
            )
            closed = self._pull_request(branch, url, head, "CONFLICTING", state="CLOSED")
            project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(loop_manager.reconcile_existing_pull_request(
                connection, repo, state_dir, str(repo), project, closed
            ), 1)

            with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                "loop_manager.run_codex"
            ) as agent, mock.patch("loop_manager.push_owned_pr_conflict_recovery") as push:
                scenarios = (
                    (base, "OPEN", "PLAN_BLOCKED"),
                    (head, "CLOSED", "PLAN_BLOCKED"),
                    (head, "OPEN", "PR_OPEN"),
                    (head, "OPEN", "PR_OPEN"),
                )
                for observed, state, expected_status in scenarios:
                    with mock.patch("loop_manager.pr_for_branch", return_value=
                                    self._pull_request(
                                        branch, url, observed, "CONFLICTING", state=state
                                    )):
                        result = loop_manager.run_project_result(
                            connection, state_dir, str(repo), Path("unused.ini"), "main", False
                        )
                    self.assertEqual(result.kind, loop_manager.ProjectRunKind.BLOCKED)
                    stored = connection.execute(
                        "SELECT * FROM exec_plans "
                        "WHERE repo = ? AND plan_id = 'G-026'",
                        (str(repo),),
                    ).fetchone()
                    self.assertEqual(stored["status"], expected_status)
                    self.assertEqual(
                        stored["blocked_reason"],
                        loop_manager.PR_CLOSED_BLOCK_REASON
                        if expected_status == "PLAN_BLOCKED"
                        else None,
                    )
                    self.assertEqual(stored["pr_conflict_head_oid"], head)
                    self.assertEqual(stored["pr_conflict_recovery"], 1)
                    blocked_project = connection.execute(
                        "SELECT phase FROM projects WHERE repo = ?",
                        (str(repo),),
                    ).fetchone()
                    self.assertEqual(blocked_project["phase"], "PROJECT_BLOCKED")
                    self.assertEqual(git_command(worktree, "rev-parse", "HEAD"), head)
                agent.assert_not_called()
                push.assert_not_called()

    def test_external_command_interruptions_preserve_recovery_and_resume(self):
        for stage in ("github", "fetch", "rebase", "push"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                repo, state_dir, connection, branch, worktree, url, head = (
                    self._pr_open_state(directory)
                )
                self._make_source_checkout_dirty_and_ahead(repo)
                base = self._advance_base(
                    directory, repo, "docs/notes.md", "# New remote base\n",
                    "Advance before interruption",
                )
                pull_request = self._pull_request(branch, url, head, "CONFLICTING")
                project = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                self.assertIsNone(loop_manager.reconcile_existing_pull_request(
                    connection, repo, state_dir, str(repo), project, pull_request
                ))
                source_before = self._source_checkout_snapshot(repo)
                original_command = loop_manager.command
                original_fetch = loop_manager._recorded_base_fetch
                injected = False
                rebases = []
                pushes = []

                def interrupted_command(arguments, **kwargs):
                    nonlocal injected
                    operation = arguments[3] if arguments[:2] == ["git", "-C"] else None
                    is_query = arguments[:3] == ["gh", "pr", "view"]
                    matches = {
                        "github": is_query,
                        "rebase": operation == "rebase",
                        "push": operation == "push",
                    }
                    if not injected and matches.get(stage, False):
                        injected = True
                        if stage == "github":
                            raise RuntimeError("GitHub HTTP 503 Service Unavailable")
                        output = (
                            "fatal: Unable to create index.lock: File exists"
                            if stage == "rebase"
                            else "fatal: unable to access origin: Connection timed out"
                        )
                        return subprocess.CompletedProcess(arguments, 128, stdout=output)
                    if is_query:
                        return subprocess.CompletedProcess(
                            arguments, 0, stdout=json.dumps(pull_request)
                        )
                    result = original_command(arguments, **kwargs)
                    if operation == "rebase" and result.returncode == 0:
                        rebases.append(arguments)
                    if operation == "push":
                        pushes.append(arguments)
                    return result

                def interrupted_fetch(*args, **kwargs):
                    nonlocal injected
                    if stage == "fetch" and not injected:
                        injected = True
                        raise RuntimeError("fatal: unable to access origin: Connection timed out")
                    return original_fetch(*args, **kwargs)

                with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                    "loop_manager.command", side_effect=interrupted_command
                ), mock.patch(
                    "loop_manager._recorded_base_fetch", side_effect=interrupted_fetch
                ), mock.patch("loop_manager.verify", return_value=(True, "verify: PASS")), mock.patch(
                    "loop_manager.run_codex",
                    return_value=loop_manager.CodexResult(
                        0, "VERDICT: PASS", "reviewer-existing"
                    ),
                ):
                    if stage in ("github", "fetch"):
                        with self.assertRaises(RuntimeError):
                            loop_manager.run_project_result(
                                connection, state_dir, str(repo), Path("unused.ini"), "main", False
                            )
                    else:
                        paused = loop_manager.run_project_result(
                            connection, state_dir, str(repo), Path("unused.ini"), "main", False
                        )
                        self.assertEqual(
                            paused.kind,
                            loop_manager.ProjectRunKind.WAITING if stage == "push"
                            else loop_manager.ProjectRunKind.TRANSIENT_PAUSE,
                        )
                    if stage == "push":
                        self.assertTrue(injected)
                        final = connection.execute(
                            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'",
                            (str(repo),),
                        ).fetchone()
                        self.assertEqual(final["status"], "PR_OPEN")
                        self.assertEqual(final["pr_conflict_recovery"], 0)
                        self.assertEqual(final["pr_url"], url)
                        self.assertEqual(len(rebases), 1)
                        self.assertEqual(len(pushes), 1)
                        self.assertEqual(self._source_checkout_snapshot(repo), source_before)
                        continue
                    self.assertTrue(injected)
                    stored = loop_manager.active_execplan(connection, str(repo))
                    self.assertEqual(
                        stored["status"], "PUBLISHING" if stage == "push" else "IMPLEMENTING"
                    )
                    self.assertEqual(stored["pr_conflict_recovery"], 1)
                    self.assertEqual(stored["pr_conflict_head_oid"], head)
                    self.assertEqual(stored["pr_conflict_base_oid"], base)
                    self.assertEqual(stored["worker_session_id"], "worker-existing")
                    self.assertEqual(stored["reviewer_session_id"], "reviewer-existing")
                    self.assertEqual(self._source_checkout_snapshot(repo), source_before)
                    self.assertTrue(worktree.is_dir())
                    self.assertEqual(pushes, [])

                    resumed = loop_manager.run_project_result(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    )
                self.assertEqual(resumed.kind, loop_manager.ProjectRunKind.WAITING)
                final = connection.execute(
                    "SELECT * FROM exec_plans "
                    "WHERE repo = ? AND plan_id = 'G-026'",
                    (str(repo),),
                ).fetchone()
                self.assertEqual(final["status"], "PR_OPEN")
                self.assertEqual(final["pr_conflict_recovery"], 0)
                self.assertEqual(final["pr_url"], url)
                self.assertEqual(len(rebases), 1)
                self.assertEqual(len(pushes), 1)
                self.assertIn(f"--force-with-lease=refs/heads/{branch}:{head}", pushes[0])
                self.assertEqual(self._source_checkout_snapshot(repo), source_before)

class ConcurrentRegisteredProjectSchedulerTest(unittest.TestCase):
    def _scheduler_state(
        self,
        directory: str,
        project_names: tuple[str, ...],
        *,
        max_workers: int,
    ) -> tuple[Path, sqlite3.Connection, loop_manager.ManagedConfig, tuple[str, ...]]:
        state_dir = Path(directory) / "state"
        state_dir.mkdir()
        connection = loop_manager.connect_database(state_dir, create=True)
        repos = tuple(
            str((Path(directory) / project_name).resolve())
            for project_name in project_names
        )
        connection.executemany(
            "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
            ((repo,) for repo in repos),
        )
        connection.commit()
        config = loop_manager.ManagedConfig(
            path=Path(directory) / "config.ini",
            max_workers=max_workers,
            telegram_token="",
            telegram_chat_id="",
        )
        return state_dir, connection, config, repos

    def test_worker_pool_bounds_parallel_projects_and_isolates_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir, connection, config, repos = self._scheduler_state(
                directory,
                ("repo-0", "repo-1", "repo-2", "repo-3"),
                max_workers=2,
            )
            guard = threading.Lock()
            both_workers_started = threading.Event()
            connections: list[sqlite3.Connection] = []
            thread_ids: set[int] = set()
            planner_mutex_ids: set[int] = set()
            external_run_ids: set[str] = set()
            active_workers = 0
            maximum_workers = 0
            active_planners = 0
            maximum_planners = 0

            def run_one(
                worker_connection,
                _state_dir,
                repo,
                _config_path,
                _requested_base,
                _allow_agents_mismatch,
                *,
                registered_only,
                planner_mutex,
                refactoring_review_interval,
                verify_timeout_seconds,
                on_step_completed,
                external_run_id,
            ):
                nonlocal active_workers, maximum_workers
                nonlocal active_planners, maximum_planners
                self.assertTrue(registered_only)
                self.assertTrue(callable(on_step_completed))
                self.assertEqual(
                    refactoring_review_interval,
                    config.refactoring_review_interval,
                )
                self.assertEqual(
                    verify_timeout_seconds,
                    config.verify_timeout_seconds,
                )
                self.assertTrue(external_run_id)
                with guard:
                    connections.append(worker_connection)
                    thread_ids.add(threading.get_ident())
                    planner_mutex_ids.add(id(planner_mutex))
                    external_run_ids.add(external_run_id)
                    active_workers += 1
                    maximum_workers = max(maximum_workers, active_workers)
                    if active_workers == config.max_workers:
                        both_workers_started.set()
                self.assertTrue(both_workers_started.wait(timeout=1))
                with planner_mutex:
                    with guard:
                        active_planners += 1
                        maximum_planners = max(
                            maximum_planners, active_planners
                        )
                    time.sleep(0.02)
                    with guard:
                        active_planners -= 1
                worker_connection.execute(
                    "UPDATE projects SET last_error = ? WHERE repo = ?",
                    (Path(repo).name, repo),
                )
                worker_connection.commit()
                with guard:
                    active_workers -= 1
                return loop_manager.ProjectRunResult(
                    loop_manager.ProjectRunKind.WAITING,
                    0,
                )

            try:
                with mock.patch(
                    "loop_manager.run_project_result",
                    side_effect=run_one,
                ):
                    outcomes = loop_manager.schedule_registered_projects(
                        connection,
                        state_dir,
                        config,
                    )
                stored = dict(
                    connection.execute(
                        "SELECT repo, last_error FROM projects ORDER BY repo"
                    ).fetchall()
                )
            finally:
                connection.close()

        self.assertEqual(tuple(item.repo for item in outcomes), repos)
        self.assertEqual(maximum_workers, config.max_workers)
        self.assertEqual(len(external_run_ids), 1)
        self.assertGreaterEqual(len(thread_ids), 2)
        self.assertEqual(maximum_planners, 1)
        self.assertEqual(len(planner_mutex_ids), 1)
        self.assertEqual(len({id(item) for item in connections}), len(repos))
        self.assertNotIn(id(connection), {id(item) for item in connections})
        self.assertEqual(
            stored,
            {repo: Path(repo).name for repo in repos},
        )

    def test_scheduler_forwards_runtime_mutation_when_present(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir, connection, config, repos = self._scheduler_state(
                directory, ("manager",), max_workers=1,
            )
            mutation = loop_manager.RuntimeMutation(
                Path(repos[0]), state_dir / "runtime", "a" * 40, "b" * 32,
            )
            try:
                with mock.patch(
                    "loop_manager.run_project_result",
                    return_value=loop_manager.ProjectRunResult(
                        loop_manager.ProjectRunKind.WAITING, 0,
                    ),
                ) as run_project:
                    outcomes = loop_manager.schedule_registered_projects(
                        connection, state_dir, config, runtime_mutation=mutation,
                    )
                self.assertEqual(tuple(item.repo for item in outcomes), repos)
                self.assertIs(run_project.call_args.kwargs["runtime_mutation"], mutation)
            finally:
                connection.close()

    def test_project_failures_are_classified_without_cancelling_peers(self):
        names = (
            "blocked",
            "quota",
            "transient",
            "configuration",
            "dirty",
            "healthy",
        )
        with tempfile.TemporaryDirectory() as directory:
            state_dir, connection, config, repos = self._scheduler_state(
                directory,
                names,
                max_workers=3,
            )
            calls: list[str] = []
            guard = threading.Lock()

            def run_one(
                _connection,
                _state_dir,
                repo,
                _config_path,
                _requested_base,
                _allow_agents_mismatch,
                **_kwargs,
            ):
                name = Path(repo).name
                with guard:
                    calls.append(name)
                if name == "dirty":
                    raise RuntimeError("unsafe base worktree")
                if name == "configuration":
                    raise loop_manager.ManagedConfigError("invalid managed config")
                results = {
                    "blocked": loop_manager.ProjectRunResult(
                        loop_manager.ProjectRunKind.BLOCKED, 1, "blocked"
                    ),
                    "quota": loop_manager.ProjectRunResult(
                        loop_manager.ProjectRunKind.USAGE_LIMIT_PAUSE, 0, "quota"
                    ),
                    "transient": loop_manager.ProjectRunResult(
                        loop_manager.ProjectRunKind.TRANSIENT_PAUSE, 0, "retry"
                    ),
                    "healthy": loop_manager.ProjectRunResult(
                        loop_manager.ProjectRunKind.COMPLETED, 0
                    ),
                }
                return results[name]

            try:
                with mock.patch(
                    "loop_manager.run_project_result",
                    side_effect=run_one,
                ), mock.patch("loop_manager.operation_log"):
                    outcomes = loop_manager.schedule_registered_projects(
                        connection,
                        state_dir,
                        config,
                    )
                by_name = {
                    Path(item.repo).name: item.result.kind for item in outcomes
                }
                with mock.patch(
                    "loop_manager.schedule_registered_projects",
                    return_value=outcomes,
                ):
                    exit_code = loop_manager.run_registered_projects(
                        connection,
                        state_dir,
                        config,
                    )
            finally:
                connection.close()

        self.assertEqual(set(calls), set(names))
        self.assertEqual(
            by_name,
            {
                "blocked": loop_manager.ProjectRunKind.BLOCKED,
                "quota": loop_manager.ProjectRunKind.USAGE_LIMIT_PAUSE,
                "transient": loop_manager.ProjectRunKind.TRANSIENT_PAUSE,
                "configuration": loop_manager.ProjectRunKind.CONFIGURATION_ERROR,
                "dirty": loop_manager.ProjectRunKind.OPERATIONAL_ERROR,
                "healthy": loop_manager.ProjectRunKind.COMPLETED,
            },
        )
        self.assertEqual(exit_code, 1)

    def test_zero_one_and_nonfatal_project_sets_exit_successfully(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir, connection, config, _repos = self._scheduler_state(
                directory,
                (),
                max_workers=3,
            )
            try:
                self.assertEqual(
                    loop_manager.schedule_registered_projects(
                        connection, state_dir, config
                    ),
                    (),
                )
                self.assertEqual(
                    loop_manager.run_registered_projects(
                        connection, state_dir, config
                    ),
                    0,
                )
                single_repo = str((Path(directory) / "single").resolve())
                connection.execute(
                    "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
                    (single_repo,),
                )
                connection.commit()
                with mock.patch(
                    "loop_manager.run_project_result",
                    return_value=loop_manager.ProjectRunResult(
                        loop_manager.ProjectRunKind.WAITING, 0
                    ),
                ):
                    single = loop_manager.schedule_registered_projects(
                        connection, state_dir, config
                    )
                self.assertEqual(tuple(item.repo for item in single), (single_repo,))

                pauses = (
                    loop_manager.ScheduledProjectResult(
                        "quota",
                        loop_manager.ProjectRunResult(
                            loop_manager.ProjectRunKind.USAGE_LIMIT_PAUSE, 0
                        ),
                    ),
                    loop_manager.ScheduledProjectResult(
                        "waiting",
                        loop_manager.ProjectRunResult(
                            loop_manager.ProjectRunKind.WAITING, 0
                        ),
                    ),
                )
                with mock.patch(
                    "loop_manager.schedule_registered_projects",
                    return_value=pauses,
                ):
                    self.assertEqual(
                        loop_manager.run_registered_projects(
                            connection, state_dir, config
                        ),
                        0,
                    )
            finally:
                connection.close()


    def test_one_project_selects_only_the_ready_dependency_priority_winner(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES ('repo', 'main')"
        )
        plans = (
            ("G-001", "normal", ()),
            ("G-002", "critical", ("G-099",)),
            ("G-003", "high", ()),
            ("G-004", "high", ()),
            ("G-099", "low", ()),
        )
        connection.executemany(
            """
            INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on, priority,
                content_hash, status
            ) VALUES ('repo', ?, ?, ?, '["docs/PRODUCT.md"]', ?, ?, 'hash', 'READY')
            """,
            (
                (
                    plan_id,
                    f"docs/plans/[{plan_id}] Plan.md",
                    plan_id,
                    json.dumps(depends_on),
                    priority,
                )
                for plan_id, priority, depends_on in plans
            ),
        )
        connection.commit()

        selected = loop_manager.select_ready_execplan(connection, "repo")
        self.assertEqual(selected["plan_id"], "G-003")
        connection.execute(
            "UPDATE exec_plans SET status = 'IMPLEMENTING' "
            "WHERE repo = 'repo' AND plan_id = 'G-003'"
        )
        connection.commit()
        self.assertIsNone(loop_manager.select_ready_execplan(connection, "repo"))
        with self.assertRaises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    "UPDATE exec_plans SET status = 'IMPLEMENTING' "
                    "WHERE repo = 'repo' AND plan_id = 'G-004'"
                )
        connection.close()


class TimestampedExecutionProgressLogsTest(unittest.TestCase):
    def test_operation_log_uses_fixed_local_timestamp_for_both_streams(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch("loop_manager.local_now", return_value=datetime.datetime(2026, 9, 19, 8, 7, 6)), mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            loop_manager.operation_log("started")
            loop_manager.operation_log("failed", error=True)
        self.assertEqual(stdout.getvalue(), "2026-09-19 08:07:06 started\n")
        self.assertEqual(stderr.getvalue(), "2026-09-19 08:07:06 failed\n")

    def test_skip_if_running_only_silences_lock_contention(self):
        state_dir = Path("state")
        with mock.patch(
            "sys.argv", ["loop_manager.py", "run", "--skip-if-running"]
        ), mock.patch(
            "loop_manager.state_directory", return_value=state_dir
        ), mock.patch(
            "loop_manager.process_lock",
            side_effect=loop_manager.ProcessLockBusyError("busy"),
        ), mock.patch("builtins.print") as output:
            self.assertEqual(loop_manager.main(), 0)
        output.assert_not_called()
        with mock.patch(
            "sys.argv", ["loop_manager.py", "run"]
        ), mock.patch(
            "loop_manager.state_directory", return_value=state_dir
        ), mock.patch(
            "loop_manager.process_lock",
            side_effect=loop_manager.ProcessLockBusyError("busy"),
        ), mock.patch("builtins.print") as output:
            self.assertEqual(loop_manager.main(), 1)
        self.assertTrue(output.called)
        with mock.patch(
            "sys.argv", ["loop_manager.py", "run", "--skip-if-running"]
        ), mock.patch(
            "loop_manager.state_directory", return_value=state_dir
        ), mock.patch(
            "loop_manager.process_lock", side_effect=RuntimeError("broken")
        ), mock.patch("builtins.print") as output:
            self.assertEqual(loop_manager.main(), 1)
        self.assertTrue(output.called)

    def test_process_lock_only_classifies_contention_errnos_as_busy(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch("fcntl.flock", side_effect=OSError(loop_manager.errno.EAGAIN, "busy")):
                with self.assertRaises(loop_manager.ProcessLockBusyError):
                    with loop_manager.process_lock(Path(directory)):
                        pass
            with mock.patch("fcntl.flock", side_effect=OSError(loop_manager.errno.ENOLCK, "lock system failure")):
                with self.assertRaises(OSError) as raised:
                    with loop_manager.process_lock(Path(directory)):
                        pass

        self.assertEqual(raised.exception.errno, loop_manager.errno.ENOLCK)

    def test_bash_launcher_skips_git_and_preserves_run_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "run.sh"
            launcher.write_text((loop_manager.ROOT / "run.sh").read_text(encoding="utf-8"), encoding="utf-8")
            python = root / ".venv/bin/python"
            python.parent.mkdir(parents=True)
            python.write_text("#!/usr/bin/env bash\nprintf 'python:%s\\n' \"$*\"\n", encoding="utf-8")
            python.chmod(0o755)
            git = root / "bin/git"
            git.parent.mkdir()
            git.write_text("#!/usr/bin/env bash\nprintf 'git-called:%s\\n' \"$*\"\nexit 99\n", encoding="utf-8")
            git.chmod(0o755)
            env = {**os.environ, "PATH": f"{git.parent}:{os.environ['PATH']}"}
            completed = subprocess.run(["bash", str(launcher), "target-repo"], cwd=root, env=env, text=True, capture_output=True)
            self.assertEqual(completed.returncode, 0)
            self.assertNotIn("git-called:", completed.stdout)
            self.assertIn("python:-u", completed.stdout)
            self.assertIn("run target-repo", completed.stdout)


    def test_launchers_have_no_timestamp_banner_and_scheduler_option_is_documented(self):
        powershell = (loop_manager.ROOT / "run.ps1").read_text(encoding="utf-8")
        bash = (loop_manager.ROOT / "run.sh").read_text(encoding="utf-8")
        self.assertNotIn("Get-Date -Format", powershell)
        self.assertNotIn("date ", bash)
        self.assertNotIn("git ", powershell.casefold())
        self.assertNotIn("git ", bash.casefold())
        self.assertIn("--skip-if-running", (loop_manager.ROOT / "README.md").read_text(encoding="utf-8"))


class ProviderNeutralAgentRuntimeTest(unittest.TestCase):
    def test_legacy_project_migration_defaults_provider_to_codex(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            database = state_dir / "loop-manager.db"
            legacy = sqlite3.connect(database)
            legacy.execute(
                "CREATE TABLE projects (repo TEXT PRIMARY KEY, base_branch TEXT NOT NULL)"
            )
            legacy.execute(
                "INSERT INTO projects (repo, base_branch) VALUES ('repo', 'main')"
            )
            legacy.commit()
            legacy.close()

            with loop_manager.connect_database(state_dir) as migrated:
                columns = {
                    row[1] for row in migrated.execute("PRAGMA table_info(projects)")
                }
                row = migrated.execute(
                    "SELECT provider FROM projects WHERE repo = 'repo'"
                ).fetchone()

        self.assertIn("provider", columns)
        self.assertEqual(row["provider"], "codex")

    def test_codex_and_claude_profiles_validate_every_role_and_level(self):
        config = loop_manager.ROOT / "config/config.ini.example"
        all_efforts = frozenset(
            {"low", "medium", "high", "xhigh", "max", "ultra"}
        )
        codex_models = {
            "gpt-6-astra": all_efforts,
            "gpt-5.6-sol": all_efforts,
            "gpt-5.6-terra": all_efforts,
            "gpt-5.6-luna": all_efforts,
        }

        loop_manager.validate_provider_profiles(config, "codex", codex_models)
        supported = subprocess.CompletedProcess(
            ["claude", "--init-only"], 0, stdout=""
        )
        with mock.patch(
            "loop_manager.command", return_value=supported
        ) as capability_command:
            claude_models = loop_manager.claude_model_efforts(config)
        loop_manager.validate_provider_profiles(config, "claude", claude_models)
        self.assertTrue(
            any(
                "--init-only" in call.args[0]
                for call in capability_command.call_args_list
            )
        )

        self.assertEqual(
            loop_manager.provider_worker_profile(
                config, "codex", 7, codex_models
            ),
            ("gpt-5.6-sol", "xhigh"),
        )
        self.assertEqual(
            loop_manager.provider_worker_profile(
                config, "claude", 1, claude_models
            ),
            ("claude-haiku-4-5-20251001", "medium"),
        )
        self.assertEqual(
            loop_manager.provider_profile(
                config,
                "claude",
                "claude.roles",
                "reviewer",
                "Claude reviewer",
                claude_models,
            ),
            ("claude-opus-5", "high"),
        )

    def test_invalid_claude_profile_stops_before_cli_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.ini"
            config.write_text(
                "[claude.models]\nfast = claude-haiku-4-5-20251001\n"
                "[claude.worker]\nlevel1 = fast,minimal\n",
                encoding="utf-8",
            )
            with mock.patch("loop_manager.subprocess.run") as run:
                with self.assertRaisesRegex(RuntimeError, "Model/Effort 조합"):
                    loop_manager.run_claude(
                        root / "worktree",
                        root / "state",
                        config,
                        "worker",
                        "prompt",
                        read_only=False,
                        level=1,
                    )

        run.assert_not_called()

    def test_claude_catalog_rejects_cli_unsupported_model_and_effort(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.ini"
            config.write_text(
                "[claude.models]\nstrong = claude-opus-999\n"
                "[claude.roles]\nplanner = strong,high\n",
                encoding="utf-8",
            )

            def reject_model(arguments, **_kwargs):
                return subprocess.CompletedProcess(
                    arguments,
                    1 if "claude-opus-999" in arguments else 0,
                    stdout="Unknown model",
                )

            with mock.patch(
                "loop_manager.command", side_effect=reject_model
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "Model.*claude-opus-999"
                ):
                    loop_manager.claude_model_efforts(config, root)

            config.write_text(
                "[claude.models]\nstrong = claude-opus-5\n"
                "[claude.roles]\nplanner = strong,max\n",
                encoding="utf-8",
            )

            def reject_effort(arguments, **_kwargs):
                return subprocess.CompletedProcess(
                    arguments,
                    1 if "--effort" in arguments else 0,
                    stdout="Effort max is unavailable for this model",
                )

            with mock.patch(
                "loop_manager.command", side_effect=reject_effort
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "Model/Effort.*claude-opus-5,max"
                ):
                    loop_manager.claude_model_efforts(config, root)

    def test_runtime_validation_rejects_missing_and_unauthenticated_cli(self):
        config = loop_manager.ROOT / "config/config.ini.example"
        with mock.patch("loop_manager.shutil.which", return_value=None), mock.patch(
            "loop_manager.command"
        ) as command:
            with self.assertRaisesRegex(RuntimeError, "설치되어 있지 않습니다"):
                loop_manager.validate_provider_runtime(config, "claude")
        command.assert_not_called()

        unauthenticated = subprocess.CompletedProcess(
            ["claude", "auth", "status"], 1, stdout="not logged in"
        )
        with mock.patch(
            "loop_manager.shutil.which", return_value="/bin/claude"
        ), mock.patch(
            "loop_manager.command", return_value=unauthenticated
        ), mock.patch(
            "loop_manager.claude_model_efforts"
        ) as model_efforts:
            with self.assertRaisesRegex(RuntimeError, "인증되어 있지 않습니다"):
                loop_manager.validate_provider_runtime(config, "claude")
        model_efforts.assert_not_called()
        authenticated = subprocess.CompletedProcess(
            ["claude", "auth", "status"], 0, stdout='{"loggedIn":true}'
        )
        with mock.patch(
            "loop_manager.shutil.which", return_value="/bin/claude"
        ), mock.patch(
            "loop_manager.command", return_value=authenticated
        ) as auth_command:
            loop_manager.validate_provider_runtime(config, "claude")
        self.assertEqual(
            auth_command.call_args_list[0].args[0],
            ["claude", "auth", "status"],
        )
        self.assertTrue(
            any(
                "--init-only" in call.args[0]
                for call in auth_command.call_args_list[1:]
            )
        )

    def test_claude_starts_resumes_and_applies_role_permissions(self):
        config = loop_manager.ROOT / "config/config.ini.example"
        session_id = "claude-session"
        calls: list[tuple[list[str], dict[str, object]]] = []
        stream = (
            json.dumps({"type": "system", "session_id": session_id})
            + "\n"
            + json.dumps(
                {
                    "type": "result",
                    "session_id": session_id,
                    "result": worker_result("Implement Claude adapter"),
                    "is_error": False,
                }
            )
            + "\n"
        )

        class StartedOutput:
            def __iter__(self):
                return iter(stream.splitlines(keepends=True))

            def close(self):
                pass

        class StartedProcess:
            def __init__(self, arguments, **kwargs):
                calls.append((arguments, kwargs))
                self.stdin = mock.Mock()
                self.stdout = StartedOutput()

            def wait(self):
                return 0

            def kill(self):
                raise AssertionError("completed Claude process must not be killed")

        resumed_payload = json.dumps(
            {
                "type": "result",
                "session_id": session_id,
                "result": "VERDICT: PASS",
                "is_error": False,
            }
        )

        def resumed(arguments, **kwargs):
            calls.append((arguments, kwargs))
            return subprocess.CompletedProcess(arguments, 0, stdout=resumed_payload)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "worktree"
            worktree.mkdir()
            state_dir = root / "state"
            started_sessions: list[str] = []
            with mock.patch(
                "loop_manager.claude_worker_profile",
                return_value=("claude-sonnet-5", "high"),
            ), mock.patch(
                "loop_manager.claude_role",
                return_value=("claude-opus-5", "high"),
            ), mock.patch(
                "loop_manager.subprocess.Popen", side_effect=StartedProcess
            ), mock.patch(
                "loop_manager.subprocess.run", side_effect=resumed
            ):
                started = loop_manager.run_claude(
                    worktree,
                    state_dir,
                    config,
                    "worker",
                    "worker prompt",
                    read_only=False,
                    level=5,
                    persist_session=True,
                    on_session_started=started_sessions.append,
                )
                resumed_result = loop_manager.run_claude(
                    worktree,
                    state_dir,
                    config,
                    "reviewer",
                    "review prompt",
                    read_only=True,
                    persist_session=True,
                    session_id=session_id,
                )

        self.assertEqual(started.session_id, session_id)
        self.assertIn("COMMIT_TITLE:", started.message)
        self.assertEqual(started_sessions, [session_id])
        self.assertEqual(resumed_result.message, "VERDICT: PASS")
        start_arguments, start_options = calls[0]
        resume_arguments, resume_options = calls[1]
        self.assertIn("stream-json", start_arguments)
        self.assertEqual(
            start_arguments[
                start_arguments.index("--permission-mode") + 1
            ],
            "acceptEdits",
        )
        self.assertNotIn("worker prompt", start_arguments)
        self.assertEqual(start_options["cwd"], worktree)
        self.assertIn("--resume", resume_arguments)
        self.assertIn(session_id, resume_arguments)
        self.assertEqual(
            resume_arguments[
                resume_arguments.index("--permission-mode") + 1
            ],
            "plan",
        )
        self.assertIn("--disallowedTools", resume_arguments)
        self.assertEqual(resume_options["cwd"], worktree)

    def test_claude_planner_processes_do_not_force_bytecode_isolation(self):
        config = loop_manager.ROOT / "config/config.ini.example"
        session_id = "claude-planner-session"
        stream = (
            json.dumps({"type": "system", "session_id": session_id}) + "\n"
            + json.dumps({"type": "result", "session_id": session_id, "result": "planned"})
            + "\n"
        )

        def started_process(*_arguments, **_kwargs):
            process = mock.Mock()
            process.stdin = mock.Mock()
            process.stdout = io.StringIO(stream)
            process.wait.return_value = 0
            return process

        def resumed_process(arguments, **_kwargs):
            return subprocess.CompletedProcess(
                arguments,
                0,
                stdout=json.dumps({"type": "result", "session_id": session_id, "result": "repaired"}),
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "worktree"
            worktree.mkdir()
            with mock.patch.dict(
                os.environ, {"PYTHONDONTWRITEBYTECODE": "0"}, clear=False
            ), mock.patch(
                "loop_manager.claude_role", return_value=("claude-opus-5", "high")
            ), mock.patch(
                "loop_manager.subprocess.Popen", side_effect=started_process
            ) as popen, mock.patch(
                "loop_manager.subprocess.run", side_effect=resumed_process
            ) as run:
                started = loop_manager.run_claude(
                    worktree, root / "state", config, "planner", "plan",
                    read_only=False, persist_session=True,
                )
                resumed = loop_manager.run_claude(
                    worktree, root / "state", config, "planner", "repair",
                    read_only=False, persist_session=True, session_id=started.session_id,
                )
                self.assertEqual(os.environ["PYTHONDONTWRITEBYTECODE"], "0")

        self.assertEqual(started.session_id, session_id)
        self.assertEqual(resumed.session_id, session_id)
        self.assertNotIn("PYTHONDONTWRITEBYTECODE", popen.call_args.kwargs["env"])
        self.assertNotIn("PYTHONDONTWRITEBYTECODE", run.call_args.kwargs["env"])

    def test_session_fallback_and_provider_errors_use_common_contract(self):
        sessions: list[tuple[str | None, bool]] = []

        def invoke(
            session_id: str | None, fallback: bool
        ) -> loop_manager.AgentResult:
            sessions.append((session_id, fallback))
            if session_id:
                return loop_manager.AgentResult(
                    1, "session was not found", session_id
                )
            return loop_manager.AgentResult(0, "fresh", "fresh-session")

        result = loop_manager.run_agent_with_retries(
            invoke, session_id="expired-session"
        )
        self.assertEqual(
            sessions,
            [("expired-session", False), (None, True)],
        )
        self.assertEqual(result.session_id, "fresh-session")
        self.assertEqual(
            loop_manager.classify_agent_result(1, "You've hit your limit"),
            loop_manager.AgentResultKind.USAGE_LIMIT,
        )
        self.assertEqual(
            loop_manager.classify_agent_result(1, "service unavailable"),
            loop_manager.AgentResultKind.TRANSIENT_FAILURE,
        )

        attempts = 0

        def timeout(
            _session_id: str | None, _fallback: bool
        ) -> loop_manager.AgentResult:
            nonlocal attempts
            attempts += 1
            raise subprocess.TimeoutExpired(["claude"], 30)

        timed_out = loop_manager.run_agent_with_retries(timeout, session_id=None)
        self.assertEqual(attempts, loop_manager.MAX_TRANSIENT_AGENT_RETRIES + 1)
        self.assertEqual(
            timed_out.result_kind,
            loop_manager.AgentResultKind.TRANSIENT_FAILURE,
        )

    def test_claude_stream_errors_preserve_output_for_classification_and_retry(self):
        config = loop_manager.ROOT / "config/config.ini.example"

        class StreamOutput:
            def __init__(self, raw_output):
                self.raw_output = raw_output

            def __iter__(self):
                return iter(self.raw_output.splitlines(keepends=True))

            def close(self):
                pass

        class StreamProcess:
            def __init__(self, raw_output, returncode):
                self.stdin = mock.Mock()
                self.stdout = StreamOutput(raw_output)
                self.returncode = returncode

            def wait(self):
                return self.returncode

            def kill(self):
                raise AssertionError("completed Claude process must not be killed")

        success_stream = (
            json.dumps({"type": "system", "session_id": "fresh-session"})
            + "\n"
            + json.dumps(
                {
                    "type": "result",
                    "session_id": "fresh-session",
                    "result": worker_result("Recovered after transient error"),
                    "is_error": False,
                }
            )
            + "\n"
        )
        process_results = iter(
            [
                ("service unavailable\n", 1),
                (success_stream, 0),
            ]
        )

        def start_process(*_args, **_kwargs):
            raw_output, returncode = next(process_results)
            return StreamProcess(raw_output, returncode)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "worktree"
            worktree.mkdir()
            state_dir = root / "state"
            with mock.patch(
                "loop_manager.claude_worker_profile",
                return_value=("claude-sonnet-5", "high"),
            ), mock.patch(
                "loop_manager.subprocess.Popen", side_effect=start_process
            ) as popen:

                def invoke(_session_id, _fallback):
                    return loop_manager.run_claude(
                        worktree,
                        state_dir,
                        config,
                        "worker",
                        "prompt",
                        read_only=False,
                        level=5,
                        persist_session=True,
                    )

                recovered = loop_manager.run_agent_with_retries(
                    invoke, session_id=None
                )

                malformed = '{"type": BROKEN "usage limit reached"\n'
                with mock.patch(
                    "loop_manager.subprocess.Popen",
                    return_value=StreamProcess(malformed, 1),
                ):
                    limited = loop_manager.run_claude(
                        worktree,
                        state_dir,
                        config,
                        "worker",
                        "prompt",
                        read_only=False,
                        level=5,
                        persist_session=True,
                    )

        self.assertEqual(popen.call_count, 2)
        self.assertEqual(recovered.session_id, "fresh-session")
        self.assertEqual(
            limited.message, '{"type": BROKEN "usage limit reached"'
        )
        self.assertEqual(
            limited.result_kind,
            loop_manager.AgentResultKind.USAGE_LIMIT,
        )

    def test_claude_zero_exit_stream_error_remains_failure(self):
        config = loop_manager.ROOT / "config/config.ini.example"
        cases = (
            ({"type": "error", "error": {"message": "usage limit reached"}},
             "usage limit reached", loop_manager.AgentResultKind.USAGE_LIMIT, 1),
            ({"type": "error", "message": "service unavailable"},
             "service unavailable", loop_manager.AgentResultKind.TRANSIENT_FAILURE,
             loop_manager.MAX_TRANSIENT_AGENT_RETRIES + 1),
            ({"type": "error"}, '"type": "error"',
             loop_manager.AgentResultKind.PERMANENT_CONFIGURATION, 1),
            ({"type": "result", "is_error": True, "result": "usage limit reached"},
             "usage limit reached", loop_manager.AgentResultKind.USAGE_LIMIT, 1),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for session_id in (None, "stream-session"):
                for trailing_result in (False, True):
                    for error, expected_message, kind, attempts in cases:
                        with self.subTest(
                            session_id=session_id, trailing_result=trailing_result,
                            error=error,
                        ):
                            events = []
                            if session_id:
                                events.append({"type": "system", "session_id": session_id})
                            events.append(error)
                            if trailing_result:
                                events.append({"type": "result", "result": "done"})
                            stream = "\n".join(json.dumps(event) for event in events)

                            def start(*_args, **_kwargs):
                                process = mock.Mock()
                                process.stdout = io.StringIO(stream)
                                process.wait.return_value = 0
                                return process

                            with mock.patch(
                                "loop_manager.claude_worker_profile",
                                return_value=("claude-sonnet-5", "high"),
                            ), mock.patch(
                                "loop_manager.subprocess.Popen", side_effect=start
                            ):
                                result = loop_manager.run_claude(
                                    root, root / "state", config, "worker", "prompt",
                                    read_only=False, level=5, persist_session=True,
                                )

                            self.assertEqual(result.code, 1)
                            self.assertEqual(result.session_id, session_id)
                            self.assertIn(expected_message, result.message)
                            self.assertNotIn("세션 ID를 확인", result.message)
                            self.assertEqual(result.result_kind, kind)
                            invoke = mock.Mock(return_value=result)
                            retried = loop_manager.run_agent_with_retries(
                                invoke, session_id=None
                            )
                            self.assertEqual(invoke.call_count, attempts)
                            self.assertEqual(retried.result_kind, kind)
                            if session_id and attempts > 1:
                                self.assertEqual(
                                    invoke.call_args_list[1],
                                    mock.call(session_id, False),
                                )

    def test_provider_dispatch_and_environment_strip_manager_secrets(self):
        result = loop_manager.AgentResult(0, "ok", "session")
        with mock.patch(
            "loop_manager.run_codex", return_value=result
        ) as codex, mock.patch(
            "loop_manager.run_claude", return_value=result
        ) as claude:
            loop_manager.run_agent(
                "codex", Path("worktree"), Path("state"), Path("config"), "planner", "p",
                read_only=False,
            )
            loop_manager.run_agent(
                "claude", Path("worktree"), Path("state"), Path("config"), "reviewer", "p",
                read_only=True,
            )
        codex.assert_called_once()
        claude.assert_called_once()

        environment = {
            "HOME": "/home/test",
            "PATH": "/bin",
            "ANTHROPIC_API_KEY": "provider-secret",
            "TELEGRAM_TOKEN": "manager-secret",
            "LOOP_MANAGER_SECRET": "manager-secret",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            claude_environment = loop_manager.agent_environment("claude")
            codex_environment = loop_manager.agent_environment("codex")

        self.assertEqual(
            claude_environment["ANTHROPIC_API_KEY"], "provider-secret"
        )
        self.assertNotIn("TELEGRAM_TOKEN", claude_environment)
        self.assertNotIn("LOOP_MANAGER_SECRET", claude_environment)
        self.assertNotIn("ANTHROPIC_API_KEY", codex_environment)

    def test_cli_accepts_provider_only_as_registration_choice(self):
        args = loop_manager.build_parser().parse_args(
            ["config", "add", "repo", "--provider", "claude"]
        )
        self.assertEqual(args.provider, "claude")
        run = loop_manager.build_parser().parse_args(["run"])
        self.assertFalse(hasattr(run, "provider"))
        with redirect_stderr(io.StringIO()), self.assertRaises(
            SystemExit
        ):
            loop_manager.build_parser().parse_args(
                ["run", "--provider", "claude"]
            )


class ExecPlanArchiveHousekeepingAdapterTest(unittest.TestCase):
    def test_archive_housekeeping_waits_for_preserved_planning_attempt(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        connection.execute(
            "INSERT INTO projects (repo, base_branch, "
            "planning_attempt_fingerprint) VALUES ('/repo', 'main', 'attempt')"
        )
        self.assertFalse(loop_manager._execplan_archive_housekeeping_safe(
            connection, '/repo', None,
            maintenance_assessment_enabled=False,
            refactoring_review_interval=5,
        ))

    def test_archive_commit_ignores_ambient_identity_signing_and_hooks(self):
        workspace = Path("/archive-worktree")
        ambient = {
            "GIT_AUTHOR_NAME": "Ambient Author",
            "GIT_AUTHOR_EMAIL": "ambient-author@example.test",
            "GIT_COMMITTER_NAME": "Ambient Committer",
            "GIT_COMMITTER_EMAIL": "ambient-committer@example.test",
        }
        completed = subprocess.CompletedProcess(["git"], 0, "")

        with mock.patch.dict(os.environ, ambient), mock.patch(
            "loop_manager.command", return_value=completed
        ) as command_mock:
            loop_manager._ExecPlanArchiveGitPort._commit_archive(
                workspace, "Archive merged ExecPlans: G-000"
            )

        arguments = command_mock.call_args.args[0]
        environment = command_mock.call_args.kwargs["env"]
        hooks_config = arguments[2]
        self.assertEqual(arguments[0:2], ["git", "-c"])
        self.assertTrue(hooks_config.startswith("core.hooksPath="))
        self.assertEqual(
            arguments[3:],
            [
                "-c",
                "commit.gpgSign=false",
                "-C",
                str(workspace),
                "commit",
                "-m",
                "Archive merged ExecPlans: G-000",
            ],
        )
        self.assertFalse(Path(hooks_config.partition("=")[2]).exists())
        self.assertFalse(command_mock.call_args.kwargs["check"])
        self.assertEqual(environment["GIT_AUTHOR_NAME"], "Loop Manager")
        self.assertEqual(
            environment["GIT_AUTHOR_EMAIL"], "loop-manager@localhost"
        )
        self.assertEqual(environment["GIT_COMMITTER_NAME"], "Loop Manager")
        self.assertEqual(
            environment["GIT_COMMITTER_EMAIL"], "loop-manager@localhost"
        )

    def test_archive_housekeeping_waits_for_file_due_or_active_legacy_cycle(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES ('/repo', 'main')"
        )
        connection.commit()

        self.assertTrue(
            loop_manager._execplan_archive_housekeeping_safe(
                connection,
                "/repo",
                None,
                maintenance_assessment_enabled=False,
                refactoring_review_interval=5,
            )
        )
        self.assertTrue(
            loop_manager._execplan_archive_housekeeping_safe(
                connection,
                "/repo",
                None,
                maintenance_assessment_enabled=True,
                refactoring_review_interval=5,
            )
        )
        repository = loop_manager.maintenance_state.MaintenanceRepository(
            connection
        )
        cycle, started = repository.begin_assessment_if_due(
            "/repo", 5, "a" * 40, self_repository=True,
        )

        self.assertTrue(started)
        self.assertEqual(
            cycle.phase,
            loop_manager.maintenance_state.MaintenancePhase.ASSESSMENT,
        )
        self.assertFalse(
            loop_manager._execplan_archive_housekeeping_safe(
                connection,
                "/repo",
                None,
                maintenance_assessment_enabled=True,
                refactoring_review_interval=5,
            )
        )
        idle = repository.advance_success_baseline(
            "/repo",
            expected_phase=cycle.phase,
            expected_revision=cycle.revision,
            remote_commit="b" * 40,
        )
        self.assertEqual(
            idle.phase,
            loop_manager.maintenance_state.MaintenancePhase.IDLE,
        )
        self.assertTrue(
            loop_manager._execplan_archive_housekeeping_safe(
                connection,
                "/repo",
                None,
                maintenance_assessment_enabled=True,
                refactoring_review_interval=5,
            )
        )
        connection.execute(
            "INSERT INTO execplan_catalog_revisions "
            "(repo, revision_sha, catalog_fingerprint) "
            "VALUES ('/repo', ?, 'catalog')",
            ("b" * 40,),
        )
        repository.initialize_file_assessment_start(
            "/repo", source_sha="b" * 40, expected_merged_count=0,
        )
        connection.execute(
            "INSERT INTO exec_plans (repo, plan_id, path, title, sources, "
            "depends_on, priority, content_hash, status) VALUES "
            "('/repo', 'G-001', 'docs/plans/[G-001] Done.md', 'Done', "
            "'[]', '[]', 'normal', 'hash', 'MERGED')"
        )
        self.assertFalse(
            loop_manager._execplan_archive_housekeeping_safe(
                connection, "/repo", None,
                maintenance_assessment_enabled=True,
                refactoring_review_interval=1,
            )
        )
        connection.close()

    def test_archive_staging_does_not_add_the_deleted_source_pathspec(self):
        workspace = Path("/archive-worktree")
        move = loop_manager.execplan_archive.ArchiveMove(
            "G-000",
            "docs/plans/[G-000] Merged Foundation.md",
            "docs/plans/archive/[G-000] Merged Foundation.md",
            "content-hash",
        )

        with mock.patch("loop_manager.git") as git_mock:
            loop_manager._ExecPlanArchiveGitPort._stage_archive_moves(
                workspace, (move,)
            )

        self.assertEqual(
            git_mock.call_args_list,
            [
                mock.call(
                    workspace,
                    "update-index",
                    "--force-remove",
                    "--",
                    move.source,
                ),
                mock.call(
                    workspace,
                    "add",
                    "--",
                    f":(literal){move.destination}",
                ),
            ],
        )

    def test_planning_archive_classifier_delegates_exact_requirement_move(self):
        source = b"docs/requirements/[R-001] Change.md"
        destination = b"docs/requirements/archive/[R-001] Change.md"
        value = (b"blob", b"object")
        before = {source: value, b"src/app.py": value}
        after = {destination: value, b"src/app.py": value}

        with mock.patch(
            "loop_manager._git_tree_entries", side_effect=(before, after)
        ):
            self.assertTrue(
                loop_manager._is_requirement_archive_only_change(
                    Path("/repo"), "old", "new"
                )
            )

        changed = dict(after)
        changed[b"src/app.py"] = (b"blob", b"changed")
        with mock.patch(
            "loop_manager._git_tree_entries", side_effect=(before, changed)
        ):
            self.assertFalse(
                loop_manager._is_requirement_archive_only_change(
                    Path("/repo"), "old", "new"
                )
            )

    def test_pull_request_reconciliation_runs_housekeeping_before_selection(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES ('/repo', 'main')"
        )
        connection.commit()
        calls = []

        result = loop_manager.synchronize_waiting_pull_requests(
            connection,
            Path("/repo"),
            Path("/state"),
            "/repo",
            after_reconciliation=lambda: calls.append("housekeeping"),
        )

        self.assertEqual(calls, ["housekeeping"])
        self.assertIsNone(result.step_result)
        self.assertFalse(result.conflict_started)
        connection.close()


class PersistentExecutionFailureBoundaryTest(unittest.TestCase):
    def test_successful_run_resolves_its_observed_retry(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        repo = str(Path("resolved-failure-repo").resolve())
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')", (repo,)
        )
        loop_manager.notifications.configure_session(connection, enabled=True)
        connection.commit()

        def recovered_run(*_args, **_kwargs):
            from recovery_runtime import RecoveryObservation, RecoveryReason
            from recovery_state import RecoveryRepository

            RecoveryRepository(connection).observe(RecoveryObservation(
                RecoveryReason.FETCH_FAILED, repo, None, "WORKING", None,
                "base-sha", "fetch-failure", "head-a", "fetch failed",
            ))
            return loop_manager.ProjectRunResult(loop_manager.ProjectRunKind.WAITING, 0)

        with mock.patch("loop_manager._run_project_result", side_effect=recovered_run):
            for _ in range(3):
                loop_manager.run_project_result(
                    connection, Path("/unused-state"), repo, Path("/unused.ini"),
                    "main", False,
                )
        diagnostic = connection.execute(
            "SELECT active, consecutive_runs, notification_eligible FROM "
            "persistent_execution_failures WHERE repo = ? AND subject = '__project__'",
            (repo,),
        ).fetchone()
        self.assertEqual(tuple(diagnostic), (0, 0, 0))
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM notification_outbox WHERE repo = ?", (repo,)
            ).fetchone()[0],
            0,
        )

    def test_scheduler_invocations_enqueue_only_the_third_unchanged_failure(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        repo = str(Path("persistent-failure-repo").resolve())
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        connection.execute(
            "INSERT INTO projects (repo, base_branch, phase) VALUES (?, 'main', 'WORKING')",
            (repo,),
        )
        loop_manager.notifications.configure_session(connection, enabled=True)
        connection.commit()

        def failed_step(*_args, **_kwargs):
            from recovery_runtime import RecoveryObservation, RecoveryReason
            from recovery_state import RecoveryRepository

            RecoveryRepository(connection).observe(RecoveryObservation(
                RecoveryReason.FETCH_FAILED, repo, None, "WORKING", None,
                "base-sha", "fetch-failure", "head-a", "fetch failed",
            ))
            return loop_manager.PROJECT_STEP_TRANSIENT_PAUSE

        with mock.patch("loop_manager._run_project_step", side_effect=failed_step):
            for expected_events in (0, 0, 1):
                outcome = loop_manager.run_project_result(
                    connection, Path("/unused-state"), repo, Path("/unused.ini"),
                    "main", False,
                )
                self.assertEqual(outcome.kind, loop_manager.ProjectRunKind.TRANSIENT_PAUSE)
                events = connection.execute(
                    "SELECT event_type FROM notification_outbox WHERE repo = ?",
                    (repo,),
                ).fetchall()
                self.assertEqual(len(events), expected_events)


class StableWorkReasonBoundaryTest(unittest.TestCase):
    def _connection(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        loop_manager.notifications.ensure_schema(connection)
        loop_manager.notifications.configure_session(connection, enabled=True)
        repo = "/projects/alpha"
        connection.execute(
            "INSERT INTO projects (repo, base_branch, planning_fingerprint, "
            "specification_fingerprint) VALUES (?, 'main', 'cycle-one', 'spec-one')",
            (repo,),
        )
        connection.execute(
            "INSERT INTO exec_plans (repo, plan_id, path, title, sources, "
            "depends_on, priority, content_hash, status) VALUES "
            "(?, 'G-018', 'docs/plans/[G-018] Work.md', 'Work', '[]', "
            "'[]', 'normal', 'hash', 'PR_OPEN')", (repo,),
        )
        loop_manager.maintenance_state.ensure_schema(connection)
        connection.execute(
            "INSERT INTO maintenance_cycles "
            "(repo, last_success_merged_count, file_assessment_start_merged_count) "
            "VALUES (?, 0, 0)", (repo,),
        )
        connection.commit()
        return connection, repo

    def test_remote_requirement_and_planner_confirmation_gate_completion(self):
        connection, repo = self._connection()
        requirement = b"docs/requirements/[R-031] Waiting.md"
        with mock.patch("loop_manager.git") as git_command, mock.patch(
            "loop_manager._planning_input_fingerprint", return_value="cycle-one"
        ), mock.patch(
            "loop_manager._specification_input_fingerprint", return_value="spec-one"
        ), mock.patch(
            "loop_manager._git_tree_entries", return_value={requirement: (b"blob", b"oid")}
        ):
            git_command.return_value.stdout = "base-sha\n"
            snapshot = loop_manager._stable_work_reason(
                connection, repo, refactoring_review_interval=5,
            )
            self.assertIsNotNone(snapshot)
            self.assertIn("[R-031]", snapshot.describe())
            connection.execute(
                "UPDATE exec_plans SET status = 'MERGED' WHERE repo = ?", (repo,),
            )
            no_plan = loop_manager._stable_work_reason(
                connection, repo, refactoring_review_interval=5,
            )
            self.assertFalse(no_plan.completed)
            connection.execute(
                "UPDATE projects SET planning_fingerprint = 'older' WHERE repo = ?", (repo,),
            )
            self.assertIsNone(loop_manager._stable_work_reason(
                connection, repo, refactoring_review_interval=5,
            ))
        connection.close()

    def test_runnable_and_due_maintenance_do_not_emit_exhaustion(self):
        connection, repo = self._connection()
        connection.execute(
            "UPDATE exec_plans SET status = 'READY' WHERE repo = ?", (repo,),
        )
        self.assertIsNone(loop_manager._stable_work_reason(
            connection, repo, refactoring_review_interval=5,
        ))
        connection.execute(
            "UPDATE exec_plans SET status = 'PR_OPEN' WHERE repo = ?", (repo,),
        )
        connection.execute(
            "UPDATE maintenance_cycles SET last_success_merged_count = NULL "
            "WHERE repo = ?", (repo,),
        )
        connection.execute(
            "INSERT INTO exec_plans (repo, plan_id, path, title, sources, "
            "depends_on, priority, content_hash, status) VALUES "
            "(?, 'G-019', 'docs/plans/[G-019] Done.md', 'Done', "
            "'[]', '[]', 'normal', 'hash-19', 'MERGED')",
            (repo,),
        )
        with mock.patch("loop_manager.is_loop_manager_repository", return_value=False), \
                mock.patch("loop_manager.git") as git_command:
            self.assertIsNone(loop_manager._stable_work_reason(
                connection, repo, refactoring_review_interval=1,
            ))
            git_command.assert_not_called()
        connection.close()

    def test_manual_run_with_due_maintenance_does_not_complete(self):
        connection, repo = self._connection()
        connection.execute(
            "UPDATE exec_plans SET status = 'MERGED' WHERE repo = ?", (repo,),
        )
        connection.execute(
            "UPDATE maintenance_cycles SET last_success_merged_count = NULL "
            "WHERE repo = ?", (repo,),
        )
        with mock.patch("loop_manager._checkpoint_dispatch_ready", return_value=False), \
                mock.patch("loop_manager._run_project_step", return_value=0), \
                mock.patch.object(loop_manager.notifications, "enqueue_completion_if_ready") as completed:
            result = loop_manager._run_project_result(
                connection, Path("/unused"), repo, Path("/unused.ini"),
                None, False, registered_only=False,
                refactoring_review_interval=1,
            )
        self.assertEqual(result.kind, loop_manager.ProjectRunKind.WAITING)
        completed.assert_not_called()
        connection.close()

    def test_planner_no_change_with_active_requirement_waits_without_completion(self):
        connection, repo = self._connection()
        import work_exhaustion
        snapshot = work_exhaustion.WorkReasonSnapshot(
            "cycle-one", (),
            ({"id": "[R-031]", "path": "docs/requirements/[R-031] Waiting.md",
              "reason": "Planner는 추가 계획이 없다고 확인했지만 활성 Requirement가 남아 있습니다"},),
            None, None,
        )
        with mock.patch("loop_manager._checkpoint_dispatch_ready", return_value=False), \
                mock.patch("loop_manager._run_project_step", return_value=0), \
                mock.patch("loop_manager._stable_work_reason", return_value=snapshot), \
                mock.patch.object(loop_manager.notifications, "enqueue_completion_if_ready") as completed:
            result = loop_manager._run_project_result(
                connection, Path("/unused"), repo, Path("/unused.ini"),
                None, False,
            )
        self.assertEqual(result.kind, loop_manager.ProjectRunKind.WAITING)
        self.assertIn("[R-031]", result.detail)
        completed.assert_not_called()
        connection.close()

    def test_stable_result_reports_reason_and_pause_skips_notification(self):
        connection, repo = self._connection()
        import work_exhaustion
        snapshot = work_exhaustion.WorkReasonSnapshot(
            "cycle-one", ({"id": "G-018", "title": "Work", "status": "PR_OPEN",
                            "reason": "PR Merge 대기"},), (), None, None,
        )
        with mock.patch("loop_manager._checkpoint_dispatch_ready", return_value=False), \
                mock.patch("loop_manager._run_project_step", return_value=0), \
                mock.patch("loop_manager._stable_work_reason", return_value=snapshot):
            result = loop_manager._run_project_result(
                connection, Path("/unused"), repo, Path("/unused.ini"),
                None, False,
            )
        self.assertEqual(result.kind, loop_manager.ProjectRunKind.WAITING)
        self.assertIn("PR Merge 대기", result.detail)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE event_type = 'work_waiting'"
        ).fetchone()[0], 1)
        with mock.patch("loop_manager._checkpoint_dispatch_ready", return_value=False), \
                mock.patch("loop_manager._run_project_step", return_value=loop_manager.PROJECT_STEP_TRANSIENT_PAUSE), \
                mock.patch("loop_manager._stable_work_reason") as reason:
            paused = loop_manager._run_project_result(
                connection, Path("/unused"), repo, Path("/unused.ini"),
                None, False,
            )
        self.assertEqual(paused.kind, loop_manager.ProjectRunKind.TRANSIENT_PAUSE)
        reason.assert_not_called()
        connection.close()

    def test_project_block_and_planning_error_report_actual_causes(self):
        connection, repo = self._connection()
        connection.execute(
            "INSERT INTO project_blocks (repo, scope, reason_code, evidence) "
            "VALUES (?, 'project', 'repository_state_unsafe', 'Unsafe worktree')",
            (repo,),
        )
        connection.execute(
            "UPDATE projects SET phase = 'PROJECT_BLOCKED' WHERE repo = ?", (repo,),
        )
        with mock.patch("loop_manager._checkpoint_dispatch_ready", return_value=False), \
                mock.patch("loop_manager._run_project_step", return_value=1):
            blocked = loop_manager._run_project_result(
                connection, Path("/unused"), repo, Path("/unused.ini"),
                None, False,
            )
        self.assertEqual(blocked.kind, loop_manager.ProjectRunKind.BLOCKED)
        self.assertIn("Unsafe worktree", blocked.detail)
        connection.execute("DELETE FROM project_blocks WHERE repo = ?", (repo,))
        connection.execute(
            "UPDATE projects SET phase = 'PLANNING', planning_error = 'Invalid plans' "
            "WHERE repo = ?", (repo,),
        )
        with mock.patch("loop_manager._checkpoint_dispatch_ready", return_value=False), \
                mock.patch("loop_manager._run_project_step", return_value=1):
            planning = loop_manager._run_project_result(
                connection, Path("/unused"), repo, Path("/unused.ini"),
                None, False,
            )
        self.assertEqual(planning.kind, loop_manager.ProjectRunKind.OPERATIONAL_ERROR)
        self.assertIn("planning_error: Invalid plans", planning.detail)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE event_type = 'work_waiting'"
        ).fetchone()[0], 0)
        connection.close()


if __name__ == "__main__":
    unittest.main()
