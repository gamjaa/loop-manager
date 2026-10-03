import json
import sqlite3
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

import loop_manager
from loop_manager_internal.maintenance.maintenance_orchestration import (
    MaintenanceOrchestrationError,
    MaintenanceOutcome,
    maintenance_planning_fingerprint,
    parse_maintenance_planner_decision,
    validate_prerequisite_plan_ids,
)
from loop_manager_internal.maintenance.maintenance_planning import MaintenancePlanMode, MaintenancePlanningContext
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.planning import planning_runtime
from loop_manager_internal.maintenance.maintenance_file_inventory import SourceInventoryError
from tests.support.git import commit_and_push, git_command, make_remote_repository
from tests.support.worker import dummy_worker_context, legacy_planning_ports
from tests.support.catalog import plan_text



class MaintenanceCycleCase(unittest.TestCase):
    @staticmethod
    def _legacy_cycle(
        connection: sqlite3.Connection, repo: str, source_sha: str,
        *, include_general_planning: bool = False,
    ):
        maintenance_state.ensure_schema(connection)
        count = connection.execute(
            "SELECT COUNT(*) FROM exec_plans WHERE repo = ? AND status = 'MERGED'",
            (repo,),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO maintenance_cycles "
            "(repo, phase, cycle_base_sha, cycle_start_merged_count, "
            "include_general_planning, revision) "
            "VALUES (?, 'assessment', ?, ?, ?, 1)",
            (repo, source_sha, count, int(include_general_planning)),
        )
        return maintenance_state.MaintenanceRepository(connection).get(repo), True

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        connection.executemany(
            "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
            (("/one",), ("/two",)),
        )
        connection.commit()
        return connection

    @staticmethod
    def _insert_plan(
        connection: sqlite3.Connection,
        repo: str,
        plan_id: str,
        *,
        status: str = "READY",
        depends_on: tuple[str, ...] = (),
        priority: str = "normal",
    ) -> None:
        connection.execute(
            """
            INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on,
                priority, content_hash, status, plan_sha
            ) VALUES (?, ?, ?, ?, '[]', ?, ?, ?, ?, ?)
            """,
            (
                repo,
                plan_id,
                f"docs/plans/[{plan_id}] {plan_id}.md",
                plan_id,
                json.dumps(depends_on),
                priority,
                f"hash-{plan_id}",
                status,
                "a" * 40,
            ),
        )

    @staticmethod
    def _archive_plan(
        connection: sqlite3.Connection, repo: str, plan_id: str
    ) -> None:
        connection.execute(
            "UPDATE exec_plans SET path = ? WHERE repo = ? AND plan_id = ?",
            (f"docs/plans/archive/[{plan_id}] {plan_id}.md", repo, plan_id),
        )
        connection.execute(
            """
            INSERT INTO execplan_catalog_history (
                repo, plan_id, original_path, observed_path,
                original_content_hash, final_content_hash, plan_sha,
                merge_confirmed, identity_verified, currently_archived,
                proof_kind, proof_revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, 1, ?, ?)
            """,
            (
                repo, plan_id,
                f"docs/plans/[{plan_id}] {plan_id}.md",
                f"docs/plans/archive/[{plan_id}] {plan_id}.md",
                f"hash-{plan_id}", f"hash-{plan_id}", "a" * 40,
                "stored-and-git", "b" * 40,
            ),
        )

    def _integration_project(
        self,
        directory: str,
        plans: tuple[
            tuple[str, str, str, tuple[str, ...], str], ...
        ],
    ) -> tuple[Path, Path, sqlite3.Connection, str]:
        root = Path(directory)
        repo_path = make_remote_repository(str(root / "project"))
        plans_dir = repo_path / "docs/plans"
        plans_dir.mkdir()
        paths: list[Path] = []
        for plan_id, title, _status, dependencies, priority in plans:
            path = plans_dir / f"[{plan_id}] {title}.md"
            path.write_text(
                plan_text(
                    plan_id,
                    title,
                    depends_on=dependencies,
                    priority=priority,
                ),
                encoding="utf-8",
            )
            paths.append(path)
        commit_and_push(repo_path, "Add maintenance integration catalog")
        plan_sha = git_command(repo_path, "rev-parse", "origin/main")
        state_dir = root / "state"
        state_dir.mkdir()
        connection = loop_manager.connect_database(state_dir, create=True)
        repo = str(repo_path)
        connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
            (repo,),
        )
        parsed = [
            (
                path.relative_to(repo_path).as_posix(),
                loop_manager._parse_execplan(repo_path, path),
            )
            for path in paths
        ]
        loop_manager._register_execplan_rows(
            connection, repo, repo_path, parsed, plan_sha
        )
        for plan_id, _title, status, _dependencies, _priority in plans:
            connection.execute(
                "UPDATE exec_plans SET status = ? "
                "WHERE repo = ? AND plan_id = ?",
                (status, repo, plan_id),
            )
        # These cases exercise recovery and ordering of a legacy cycle that
        # was already active when the file-assessment schedule was introduced.
        cycle, started = self._legacy_cycle(
            connection, repo, plan_sha,
            include_general_planning=not any(
                status == "READY" for _id, _title, status, _deps, _priority in plans
            ),
        )
        self.assertTrue(started)
        self.assertEqual(cycle.phase, maintenance_state.MaintenancePhase.ASSESSMENT)
        connection.commit()
        return repo_path, state_dir, connection, repo

    def _run_registered(
        self,
        connection: sqlite3.Connection,
        state_dir: Path,
        repo_path: Path,
        agent,
    ) -> loop_manager.ProjectRunResult:
        with mock.patch(
            "loop_manager.validate_repository", return_value=repo_path
        ), mock.patch(
            "loop_manager.validate_provider_runtime"
        ), mock.patch(
            "loop_manager.ensure_safe_base_worktree"
        ), mock.patch(
            "loop_manager.run_codex", side_effect=agent
        ), legacy_planning_ports(), dummy_worker_context(
            roles=("worker", "planner", "maintenance_planner")
        ):
            return loop_manager.run_project_result(
                connection,
                state_dir,
                str(repo_path),
                Path("unused.ini"),
                "main",
                False,
                registered_only=True,
            )

    def _active_plan_id(
        self, connection: sqlite3.Connection, repo: str
    ) -> str | None:
        row = connection.execute(
            "SELECT plan_path FROM projects WHERE repo = ?", (repo,)
        ).fetchone()
        return loop_manager._plan_id_from_path(str(row["plan_path"] or ""))

    def _mark_active_merged(
        self,
        connection: sqlite3.Connection,
        repo_path: Path,
        repo: str,
        plan_id: str,
    ) -> None:
        project = connection.execute(
            "SELECT worktree FROM projects WHERE repo = ?", (repo,)
        ).fetchone()
        worktree = Path(str(project["worktree"] or ""))
        if worktree.is_dir():
            git_command(
                repo_path, "worktree", "remove", "--force", "--force",
                str(worktree),
            )
        with connection:
            connection.execute(
                """
                UPDATE exec_plans
                SET status = 'MERGED', branch = NULL, worktree = NULL,
                    workspace_owned = 0, worker_session_id = NULL,
                    reviewer_session_id = NULL, pr_url = NULL,
                    pr_head_oid = NULL
                WHERE repo = ? AND plan_id = ?
                """,
                (repo, plan_id),
            )
            loop_manager._clear_active_in_current_transaction(connection, repo)

    def _advance_remote(self, repo_path: Path, marker: str) -> str:
        git_command(repo_path, "fetch", "origin")
        git_command(repo_path, "merge", "--ff-only", "origin/main")
        (repo_path / "src/app.py").write_text(
            f"VALUE = {marker!r}\n", encoding="utf-8"
        )
        commit_and_push(repo_path, f"Merge maintenance worker {marker}")
        return git_command(repo_path, "rev-parse", "origin/main")

    def _write_refactoring_batch(
        self, worktree: Path, *, ready_priority: str = "critical"
    ) -> None:
        plans_dir = worktree / "docs/plans"
        (plans_dir / "[G-002] Extract Boundary.md").write_text(
            plan_text("G-002", "Extract Boundary"),
            encoding="utf-8",
        )
        (plans_dir / "[G-001] Ready Feature.md").write_text(
            plan_text(
                "G-001",
                "Ready Feature",
                depends_on=("G-002",),
                priority=ready_priority,
            ),
            encoding="utf-8",
        )

    def _enter_revalidation(
        self,
        connection: sqlite3.Connection,
        repo: str,
        base_sha: str,
    ) -> maintenance_state.MaintenanceCycle:
        repository = maintenance_state.MaintenanceRepository(connection)
        cycle = repository.get(repo)
        self.assertEqual(cycle.phase, maintenance_state.MaintenancePhase.ASSESSMENT)
        refactoring = repository.transition(
            repo,
            expected_phase=cycle.phase,
            expected_revision=cycle.revision,
            next_phase=maintenance_state.MaintenancePhase.REFACTORING,
            refactoring_plan_ids=("G-000",),
            revalidation_plan_ids=("G-001",),
        )
        return repository.transition(
            repo,
            expected_phase=refactoring.phase,
            expected_revision=refactoring.revision,
            next_phase=(
                maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION
            ),
            cycle_base_sha=base_sha,
        )
