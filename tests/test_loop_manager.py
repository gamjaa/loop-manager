import json
import io
import os
import datetime
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

import loop_manager


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
                "plan-sha",
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

    def test_legacy_database_migration_reconciles_only_confirmed_merge_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_remote_repository(directory)
            (root / "docs/plans").mkdir()
            write_execplan(root, "G-001", "Completed", priority=None)
            write_execplan(root, "G-002", "Unconfirmed")
            active = write_execplan(root, "G-003", "Active", priority="high")
            commit_and_push(root, "Add legacy plans")
            plan_sha = git_command(root, "rev-parse", "HEAD")
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

            self.assertEqual([plan["id"] for _path, plan in plans or []], ["G-002", "G-003"])

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

        self.assertEqual(loop_manager.select_ready_execplan(connection, "repo")["plan_id"], "G-004")
        connection.execute(
            "UPDATE exec_plans SET status = 'MERGED' WHERE repo = 'repo' AND plan_id = 'G-004'"
        )
        connection.commit()
        self.assertEqual(loop_manager.select_ready_execplan(connection, "repo")["plan_id"], "G-003")

        connection.execute(
            "UPDATE exec_plans SET status = 'IMPLEMENTING' WHERE repo = 'repo' AND plan_id = 'G-005'"
        )
        connection.commit()
        self.assertIsNone(loop_manager.select_ready_execplan(connection, "repo"))


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
            missing_worktree = Path(directory) / "already-removed"
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

    def test_changed_input_plans_before_ready_while_unchanged_input_selects_worker(self):
        for changed, expected_role in ((True, "planner"), (False, "worker")):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                repo = make_remote_repository(directory)
                (repo / "docs/plans").mkdir()
                plan = write_execplan(repo, "G-001", "Queued", priority="high")
                commit_and_push(repo, "Add queued plan")
                state_dir = Path(directory) / "state"
                connection = self._connection(str(repo))
                self._insert_plan(connection, "G-001", "READY", priority="high", repo=str(repo), title="Queued")
                fingerprint = loop_manager.planning_input_fingerprint(repo, "main")
                connection.execute(
                    """
                    UPDATE projects
                    SET planning_fingerprint = ?, processed_sha = ?
                    WHERE repo = ?
                    """,
                    (
                        "changed" if changed else fingerprint,
                        None if changed else git_command(repo, "rev-parse", "origin/main"),
                        str(repo),
                    ),
                )
                connection.commit()
                roles = []

                def agent(_worktree, _state, _config, role_name, _prompt, **_kwargs):
                    roles.append(role_name)
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
                self.assertEqual(roles, [expected_role])
                if changed:
                    self.assertIn(
                        '"id": "G-001"',
                        loop_manager.planner_prompt(
                            None,
                            loop_manager.compact_execplan_catalog(connection, str(repo)),
                        ),
                    )



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
                    git_command(worktree, "add", "-A")
                    git_command(worktree, "commit", "-m", "Invalid Planner output")
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

                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )

            resumed = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(planner_calls, 3)
            self.assertEqual(resumed["planning_retry_count"], 0)
            self.assertEqual(resumed["planning_result_pending_validation"], 0)
            self.assertIsNone(resumed["planning_error"])

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
            if planner_calls == 2:
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
            self.assertEqual(planner_calls, 3)
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
        values = loop_manager.validate_execplan(
            loop_manager.ROOT,
            "docs/plans/[G-001] Minimal Bootstrap Runner.md",
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
            nested = root / "docs/plans/archive/[G-999] Ignored.md"
            nested.parent.mkdir()
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

        for level in (0, 8, True):
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
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        connection.execute(
            """
            INSERT INTO projects (
                repo, base_branch, active_sha, phase, branch, worktree, plan_path
            ) VALUES (?, 'main', ?, 'WORKING', 'loop-manager/G-001', ?, ?)
            """,
            (
                str(repo),
                git_command(repo, "rev-parse", "origin/main"),
                str(repo),
                plan.relative_to(repo).as_posix(),
            ),
        )
        connection.commit()
        return repo, state_dir, connection

    def test_worker_without_diff_blocks_plan_until_pr_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection = self._make_active_worker_project(directory)
            commit_and_push(repo, "Add worker plan")
            plan_sha = git_command(repo, "rev-parse", "HEAD")
            connection.execute(
                """
                UPDATE projects
                SET active_sha = ?, plan_sha = ?
                WHERE repo = ?
                """,
                (plan_sha, plan_sha, str(repo)),
            )
            connection.commit()
            roles = []

            def worker(_worktree, _state, _config, role_name, _prompt, **_kwargs):
                roles.append(role_name)
                return loop_manager.CodexResult(0, worker_result(), "worker-session")

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
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                """
                SELECT phase, worktree, pr_url, last_error, verify_result
                FROM projects WHERE repo = ?
                """,
                (str(repo),),
            ).fetchone()
            plan = connection.execute(
                "SELECT status, pr_url, last_error FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            worktree_preserved = Path(project["worktree"]).exists()

        self.assertEqual(result, 1)
        self.assertEqual(roles, ["worker"])
        verify.assert_called_once()
        self.assertEqual(project["phase"], "BLOCKED")
        self.assertEqual(project["verify_result"], "verify: PASS")
        self.assertIsNone(project["pr_url"])
        self.assertTrue(worktree_preserved)
        self.assertIn("PR을 생성할 수 없습니다", project["last_error"])
        self.assertEqual(plan["status"], "BLOCKED")
        self.assertIsNone(plan["pr_url"])
        self.assertEqual(plan["last_error"], project["last_error"])

    def test_worker_and_reviewer_prompts_define_test_execution_boundary(self):
        worker = loop_manager.worker_prompt("docs/plans/[G-001] Worker Boundary.md", None)
        reviewer = loop_manager.reviewer_prompt("main", "targeted: PASS", "verify: PASS")

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
        self.assertIn("targeted: PASS", reviewer)
        self.assertIn("verify: PASS", reviewer)
        self.assertIn("테스트 command와 scripts/verify를 실행하지 말고", reviewer)
        self.assertIn("COMMIT_TITLE:", worker)
        self.assertIn("PR_SUMMARY:", worker)
        self.assertIn("TARGETED_TESTS:", worker)

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

    def test_verify_failure_resumes_same_worker_session_without_reviewer(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection = self._make_active_worker_project(directory)
            sessions = []
            prompts = []
            logs = []

            def worker(_worktree, _state, _config, role_name, prompt, **kwargs):
                self.assertEqual(role_name, "worker")
                sessions.append(kwargs["session_id"])
                prompts.append(prompt)
                return loop_manager.CodexResult(0, worker_result(), "worker-session")

            with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch("loop_manager.ensure_safe_base_worktree"), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch("loop_manager.run_codex", side_effect=worker), mock.patch(
                "loop_manager.verify", return_value=(False, "verify failed")
            ) as verify, mock.patch("loop_manager.operation_log", side_effect=lambda message, **kwargs: logs.append((message, kwargs))):
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )
                row = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                self.assertEqual(row["phase"], "WORKING")
                self.assertEqual(row["worker_session_id"], "worker-session")
                self.assertEqual(row["worker_result"], worker_result())
                self.assertIsNone(row["verify_result"])
                self.assertIn("전체 검증 실패:\nverify failed", row["last_error"])
                verify.assert_called_once()

                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )

            self.assertEqual(sessions, [None, "worker-session"])
            self.assertIn("전체 검증 실패:\nverify failed", prompts[1])
            self.assertEqual(
                logs,
                [
                    ("Worker 시작: G-001 Worker Boundary", {}),
                    ("전체 verify 시작: G-001 Worker Boundary", {}),
                    ("Worker 반환: G-001 Worker Boundary (전체 verify 실패)", {"error": True}),
                    ("verify failed", {"error": True}),
                    ("Worker 시작: G-001 Worker Boundary", {}),
                    ("전체 verify 시작: G-001 Worker Boundary", {}),
                    ("Worker 반환: G-001 Worker Boundary (전체 verify 실패)", {"error": True}),
                    ("verify failed", {"error": True}),
                ],
            )

    def test_rebase_verify_failure_returns_to_worker_with_identifying_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection = self._make_active_worker_project(directory)
            logs = []
            original_git = loop_manager.git
            origin_reads = 0

            def git_after_base_advance(path, *arguments, **kwargs):
                nonlocal origin_reads
                if arguments == ("rev-parse", "origin/main"):
                    origin_reads += 1
                    if origin_reads > 1:
                        return subprocess.CompletedProcess(["git", *arguments], 0, stdout="f" * 40 + "\n")
                if arguments == ("rebase", "origin/main"):
                    return subprocess.CompletedProcess(["git", *arguments], 0, stdout="")
                return original_git(path, *arguments, **kwargs)

            def worker(worktree, _state, _config, _role, _prompt, **_kwargs):
                (worktree / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
                return loop_manager.CodexResult(0, worker_result("Rebase failure context"), "worker-session")

            with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch("loop_manager.remote_base", return_value="main"), mock.patch("loop_manager.ensure_safe_base_worktree"), mock.patch("loop_manager.pr_for_branch", return_value=None), mock.patch("loop_manager.run_codex", side_effect=worker), mock.patch("loop_manager.verify", side_effect=[(True, "first verify"), (False, "rebased verify failed")]) as verify, mock.patch("loop_manager.git", side_effect=git_after_base_advance), mock.patch("loop_manager.operation_log", side_effect=lambda message, **kwargs: logs.append((message, kwargs))):
                self.assertEqual(loop_manager.run_project(connection, state_dir, str(repo), Path("unused.ini"), "main", False), 1)

            row = connection.execute("SELECT phase, last_error FROM projects WHERE repo = ?", (str(repo),)).fetchone()
            self.assertEqual(row["phase"], "WORKING")
            self.assertIn("rebase 후 전체 검증 실패:\nrebased verify failed", row["last_error"])
            self.assertEqual(verify.call_count, 2)
            self.assertEqual(logs, [("Worker 시작: G-001 Worker Boundary", {}), ("전체 verify 시작: G-001 Worker Boundary", {}), ("전체 verify 시작: G-001 Worker Boundary (rebase 후)", {}), ("Worker 반환: G-001 Worker Boundary (rebase 후 전체 verify 실패)", {"error": True}), ("rebased verify failed", {"error": True})])

    def test_verify_success_passes_worker_and_verify_results_to_reviewer(self):
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
                "loop_manager.pr_for_branch", side_effect=[None, {"url": "https://example.test/pr/1"}]
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
            self.assertEqual(
                git_command(repo, "log", "-1", "--pretty=%s"),
                "Persist worker change",
            )
            self.assertEqual([role for role, _prompt, _kwargs in calls], ["worker", "reviewer"])
            reviewer_prompt = calls[1][1]
            self.assertIn("targeted test: PASS", reviewer_prompt)
            self.assertIn("verify: PASS", reviewer_prompt)
            self.assertIn("테스트 command와 scripts/verify를 실행하지 말고", reviewer_prompt)

            row = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(row["phase"], "PR_OPEN")
            self.assertEqual(row["worker_session_id"], "worker-session")
            self.assertEqual(row["verify_result"], "verify: PASS")



class WorkerPublicationTest(unittest.TestCase):
    plan = {"id": "G-008", "title": "Descriptive Worker Commits"}

    def test_worker_result_contract_preserves_fields_and_rejects_bad_titles(self):
        publication = loop_manager.parse_worker_result(
            worker_result("Store immutable plan links", "Adds immutable PR context.", "test_one: PASS"),
            self.plan,
        )

        self.assertEqual(publication.commit_title, "Store immutable plan links")
        self.assertEqual(publication.summary, "Adds immutable PR context.")
        self.assertEqual(publication.test_evidence, "test_one: PASS")
        invalid_results = (
            worker_result("", "Summary", "test: PASS"),
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
        )
        for result in invalid_results:
            with self.subTest(result=result):
                with self.assertRaises(loop_manager.WorkerResultError):
                    loop_manager.parse_worker_result(result, self.plan)

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

        self.assertEqual(blocked["phase"], "BLOCKED")
        self.assertEqual(blocked["last_error"], loop_manager.PLAN_SHA_BACKFILL_ERROR)
        self.assertEqual(retained["phase"], "BLOCKED")
        self.assertEqual(retained["last_error"], loop_manager.PLAN_SHA_BACKFILL_ERROR)
        run.assert_not_called()

    def test_base_worktree_failure_preserves_file_index_and_head(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(loop_manager.CLEANUP_PENDING_SCHEMA)
            (repo / "src/app.py").write_text("VALUE = 99\n", encoding="utf-8")
            git_command(repo, "add", "src/app.py")
            head_before = git_command(repo, "rev-parse", "HEAD")
            index_before = git_command(repo, "diff", "--cached", "--", "src/app.py")

            with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch("loop_manager.run_codex") as run, mock.patch(
                "loop_manager.make_worktree"
            ) as make_worktree:
                with self.assertRaisesRegex(RuntimeError, "기본 worktree가 dirty"):
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    )

            self.assertEqual((repo / "src/app.py").read_text(encoding="utf-8"), "VALUE = 99\n")
            self.assertEqual(
                git_command(repo, "diff", "--cached", "--", "src/app.py"), index_before
            )
            self.assertEqual(git_command(repo, "rev-parse", "HEAD"), head_before)
            run.assert_not_called()
            make_worktree.assert_not_called()

    def test_verify_and_review_retries_keep_latest_worker_metadata(self):
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
            worktree = state_dir / "worktrees" / "G-001"
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
            review_messages = iter(("VERDICT: FAIL", "VERDICT: PASS"))
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
                "loop_manager.pr_for_branch",
                side_effect=[None, None, None, None],
            ), mock.patch("loop_manager.run_codex", side_effect=agent), mock.patch(
                "loop_manager.verify",
                side_effect=[(False, "verify failed"), (True, "verify: PASS"), (True, "verify: PASS")],
            ), mock.patch("loop_manager.command", side_effect=command_stub), mock.patch("loop_manager.operation_log", side_effect=lambda message, **kwargs: logs.append((message, kwargs))):
                for expected in (1, 1, 0):
                    self.assertEqual(
                        loop_manager.run_project(
                            connection, state_dir, str(repo), Path("unused.ini"), "main", False
                        ),
                        expected,
                    )
                row = connection.execute(
                    "SELECT worker_result, phase FROM projects WHERE repo = ?", (str(repo),)
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
        self.assertIn("Publish reviewed context", reviewer_prompts[-1])
        self.assertIn("Final summary", reviewer_prompts[-1])
        self.assertNotIn("Initial summary", reviewer_prompts[-1])
        self.assertIn("Final summary", latest_body)
        self.assertEqual(row["phase"], "PR_OPEN")
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
                    git_command(repo, "rev-parse", "origin/main"),
                    git_command(repo, "rev-parse", "origin/main"),
                    str(repo),
                    plan.relative_to(repo).as_posix(),
                ),
            )
            connection.commit()
            sessions = []
            prompts = []

            def worker(_worktree, _state, _config, _role, prompt, **kwargs):
                sessions.append(kwargs["session_id"])
                prompts.append(prompt)
                result = (
                    worker_result("Worker Boundary", "Bad title", "test: PASS")
                    if len(sessions) == 1
                    else worker_result("Validate worker metadata", "Good title", "test: PASS")
                )
                return loop_manager.CodexResult(0, result, "worker-session")

            with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch("loop_manager.ensure_safe_base_worktree"), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch("loop_manager.run_codex", side_effect=worker), mock.patch(
                "loop_manager.verify", return_value=(False, "verify failed")
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
                    1,
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

    def test_pending_recheck_removes_safe_worktree_and_missing_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, worktree, _branch, pr_head = self._managed_worktree(root)
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
                connection, repo, str(repo)
            )

            self.assertEqual(remaining, [])
            self.assertFalse(worktree.exists())

    def test_merged_dirty_worktree_becomes_nonblocking_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, worktree, branch, pr_head = self._managed_worktree(root)
            (worktree / "untracked.txt").write_text("keep\n", encoding="utf-8")
            connection = self._memory_connection(repo)
            connection.execute(
                """
                UPDATE projects
                SET active_sha = ?, phase = 'PR_OPEN', branch = ?, worktree = ?,
                    plan_path = 'docs/plans/[G-001] Cleanup.md',
                    pr_url = 'https://example.test/pr/1'
                WHERE repo = ?
                """,
                (pr_head, branch, str(worktree), str(repo)),
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
            ), mock.patch("loop_manager.pr_for_branch", return_value=merged_pr):
                result = loop_manager.run_project(
                    connection,
                    root / "state",
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

    def test_main_ignores_unknown_options(self):
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
        ), mock.patch("loop_manager.process_lock") as process_lock, mock.patch(
            "loop_manager.connect_database"
        ) as connect_database, mock.patch("builtins.print"):
            self.assertEqual(loop_manager.main(), 0)

        process_lock.assert_called_once_with(state_dir)
        connect_database.assert_called_once_with(state_dir, create=True)


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
        with mock.patch("sys.argv", ["loop_manager.py", "run", "repo", "--skip-if-running"]), mock.patch("loop_manager.state_directory", return_value=state_dir), mock.patch("loop_manager.process_lock", side_effect=loop_manager.ProcessLockBusyError("busy")), mock.patch("builtins.print") as output:
            self.assertEqual(loop_manager.main(), 0)
        output.assert_not_called()
        with mock.patch("sys.argv", ["loop_manager.py", "run", "repo"]), mock.patch("loop_manager.state_directory", return_value=state_dir), mock.patch("loop_manager.process_lock", side_effect=loop_manager.ProcessLockBusyError("busy")), mock.patch("builtins.print") as output:
            self.assertEqual(loop_manager.main(), 1)
        self.assertTrue(output.called)
        with mock.patch("sys.argv", ["loop_manager.py", "run", "repo", "--skip-if-running"]), mock.patch("loop_manager.state_directory", return_value=state_dir), mock.patch("loop_manager.process_lock", side_effect=RuntimeError("broken")), mock.patch("builtins.print") as output:
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

    def test_bash_launcher_preserves_git_output_exit_code_and_run_arguments(self):
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
            git.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"${GIT_OUTPUT:-Already up to date.}\"\nexit \"${GIT_EXIT:-0}\"\n", encoding="utf-8")
            git.chmod(0o755)
            env = {**os.environ, "PATH": f"{git.parent}:{os.environ['PATH']}", "GIT_OUTPUT": "Updated"}
            completed = subprocess.run(["bash", str(launcher), "target-repo"], cwd=root, env=env, text=True, capture_output=True)
            self.assertEqual(completed.returncode, 0)
            self.assertIn("Updated", completed.stdout)
            self.assertIn("python:-u", completed.stdout)
            self.assertIn("run target-repo", completed.stdout)
            failed = subprocess.run(["bash", str(launcher), "target-repo"], cwd=root, env={**env, "GIT_OUTPUT": "pull failed", "GIT_EXIT": "2"}, text=True, capture_output=True)
            self.assertEqual(failed.returncode, 1)
            self.assertIn("pull failed", failed.stdout)
            self.assertNotIn("python:", failed.stdout)


    def test_launchers_have_no_timestamp_banner_and_scheduler_option_is_documented(self):
        self.assertNotIn("Get-Date -Format", (loop_manager.ROOT / "run.ps1").read_text(encoding="utf-8"))
        self.assertNotIn("date ", (loop_manager.ROOT / "run.sh").read_text(encoding="utf-8"))
        self.assertIn("--skip-if-running", (loop_manager.ROOT / "README.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
