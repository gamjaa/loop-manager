from tests.support.catalog import plan_text
import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from loop_manager_internal.documents import document_drafts
from loop_manager_internal.documents import document_publication
from loop_manager_internal.catalog import execplan_catalog
from loop_manager_internal.storage import execplan_catalog_state
import loop_manager
from loop_manager_internal.maintenance import maintenance_planning
from loop_manager_internal.maintenance import maintenance_findings
from loop_manager_internal.maintenance import maintenance_file_inventory
from loop_manager_internal.storage import maintenance_file_ledger
from loop_manager_internal.maintenance import maintenance_file_publication
from loop_manager_internal.maintenance import maintenance_reconciliation
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.storage import planning_input_state
from loop_manager_internal.maintenance.maintenance_file_inventory import SourceInventory, TrackedSource
from loop_manager_internal.planning.planning_catalog import MutableReadySnapshot, mutable_snapshot_token


def git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return result.stdout.strip()


class MaintenancePlanningCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.remote = root / "remote.git"
        self.repo = root / "repo"
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(self.remote)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.repo.mkdir()
        git(self.repo, "init", "--initial-branch=main")
        git(self.repo, "config", "user.email", "loop-manager@example.com")
        git(self.repo, "config", "user.name", "Loop Manager Test")
        git(self.repo, "remote", "add", "origin", str(self.remote))
        (self.repo / "docs/plans").mkdir(parents=True)
        (self.repo / "docs/PRODUCT.md").write_text("# Product\n", encoding="utf-8")
        for name in ("legacy/module.py", "refactored/adapter.py"):
            source = self.repo / name
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("# tracked module\n", encoding="utf-8")
        self._write_plan("G-000", "Merged Foundation")
        self.ready_path = self._write_plan(
            "G-001", "Ready Feature", depends_on=("G-000",)
        )
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "Add initial catalog")
        git(self.repo, "push", "-u", "origin", "main")
        self.base_sha = git(self.repo, "rev-parse", "HEAD")

        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.connection)
        self.connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
            (str(self.repo),),
        )
        self._insert_plan("G-000", "Merged Foundation", "MERGED")
        self._insert_plan("G-001", "Ready Feature", "READY")
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def _path(self, plan_id: str, title: str) -> Path:
        return self.repo / "docs/plans" / f"[{plan_id}] {title}.md"

    def _write_plan(
        self,
        plan_id: str,
        title: str,
        *,
        depends_on: tuple[str, ...] = (),
        priority: str = "normal",
        purpose: str = (
            "A concrete structural responsibility problem crosses the module boundary. "
            "Preserve user behavior, the public contract, and storage format."
        ),
        acceptance_criteria: str = (
            "Preserve user behavior, the public API contract, and storage format "
            "for `legacy/module.py`."
        ),
        implementation: str = "Separate the responsibility into a testable module boundary.",
    ) -> Path:
        path = self._path(plan_id, title)
        path.write_text(
            plan_text(
                plan_id,
                title,
                depends_on=depends_on,
                priority=priority,
                purpose=purpose,
                acceptance_criteria=acceptance_criteria,
                implementation=implementation,
            ),
            encoding="utf-8",
        )
        return path

    def _insert_plan(self, plan_id: str, title: str, status: str) -> None:
        path = self._path(plan_id, title)
        parsed = loop_manager._parse_execplan(self.repo, path)
        relative_path = path.relative_to(self.repo).as_posix()
        content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        self.connection.execute(
            """
            INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on, priority,
                content_hash, current_complexity, current_risk,
                current_revision_sha, publication_path,
                publication_content_hash, publication_sha, status, plan_sha
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(self.repo),
                plan_id,
                relative_path,
                title,
                json.dumps(parsed["sources"], separators=(",", ":")),
                json.dumps(parsed["depends_on"], separators=(",", ":")),
                parsed["priority"],
                content_hash,
                parsed["complexity"],
                parsed["risk"],
                self.base_sha,
                relative_path,
                content_hash,
                self.base_sha,
                status,
                self.base_sha,
            ),
        )

    def _begin_barrier(self) -> loop_manager.MaintenancePlanningContext:
        self._begin_legacy_assessment(self.base_sha)
        return loop_manager.prepare_maintenance_planning_context(
            self.connection, str(self.repo), self.repo, self.base_sha
        )

    def _begin_legacy_assessment(self, source_sha: str):
        """Model a cycle already active before file schedules were installed."""
        maintenance_state.ensure_schema(self.connection)
        self.connection.execute(
            "INSERT INTO maintenance_cycles "
            "(repo, phase, cycle_base_sha, cycle_start_merged_count, revision) "
            "VALUES (?, 'assessment', ?, 1, 1) "
            "ON CONFLICT(repo) DO UPDATE SET phase = 'assessment', "
            "cycle_base_sha = excluded.cycle_base_sha, "
            "cycle_start_merged_count = excluded.cycle_start_merged_count, "
            "revision = maintenance_cycles.revision + 1",
            (str(self.repo), source_sha),
        )
        return maintenance_state.MaintenanceRepository(self.connection).get(
            str(self.repo)
        ), True

    def _write_barrier_batch(self) -> None:
        self._write_plan("G-002", "Extract Boundary")
        self._write_plan(
            "G-003", "Separate Adapter", depends_on=("G-002",)
        )
        self._write_plan(
            "G-001", "Ready Feature", depends_on=("G-000", "G-003")
        )

    def _validate_barrier(
        self, context: loop_manager.MaintenancePlanningContext
    ) -> loop_manager.ValidatedPlanningResult:
        result = loop_manager.validate_maintenance_planner_result(
            self.connection,
            context,
            self.repo,
            loop_manager.changed_files(self.repo),
        )
        self.assertIsNotNone(result)
        assert result is not None
        return result

    def _assert_prepared_batch_restarts(self, revalidation=False):
        repository = maintenance_state.MaintenanceRepository(self.connection)
        context = self._begin_barrier()
        if revalidation:
            cycle = repository.transition(
                str(self.repo),
                expected_phase=maintenance_state.MaintenancePhase.ASSESSMENT,
                expected_revision=context.cycle_revision,
                next_phase=maintenance_state.MaintenancePhase.REFACTORING,
                refactoring_plan_ids=("G-000",),
                revalidation_plan_ids=("G-001",),
            )
            repository.transition(
                str(self.repo),
                expected_phase=cycle.phase,
                expected_revision=cycle.revision,
                next_phase=maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
            )
            context = loop_manager.prepare_maintenance_planning_context(
                self.connection, str(self.repo), self.repo, self.base_sha
            )
            self._write_plan(
                "G-001", "Ready Feature", depends_on=("G-000",),
                implementation="Use the extracted adapter module.",
            )
        else:
            self._write_barrier_batch()
        result = self._validate_barrier(context)
        git(self.repo, "add", "docs/plans")
        git(self.repo, "commit", "-m", "Prepare local maintenance batch")
        commit = git(self.repo, "rev-parse", "HEAD")
        intent = loop_manager.record_maintenance_publication_intent(
            self.connection, str(self.repo), self.repo, "main", result, commit
        )
        before_rows = [tuple(row) for row in self.connection.execute(
            "SELECT * FROM exec_plans ORDER BY plan_id"
        )]
        other = Path(self.temporary.name) / "advanced-base"
        git(self.repo, "worktree", "add", "--detach", str(other), self.base_sha)
        git(other, "commit", "--allow-empty", "-m", "Concurrent base update")
        git(other, "push", "origin", "HEAD:main")
        latest = git(other, "rev-parse", "HEAD")
        rejected = loop_manager.git(
            self.repo, "push", "origin", "HEAD:main", check=False
        )
        self.assertNotEqual(rejected.returncode, 0)
        git(self.repo, "fetch", "origin")
        restarted = loop_manager.recover_maintenance_planning_batch(
            self.connection, str(self.repo), self.repo, "main"
        )
        self.assertEqual(restarted.phase, intent.phase)
        self.assertEqual(restarted.revision, intent.revision + 1)
        self.assertEqual(restarted.cycle_base_sha, latest)
        self.assertIsNone(restarted.publication_stage)
        self.assertIsNone(restarted.publication_commit_sha)
        self.assertEqual(restarted.last_success_commit, intent.last_success_commit)
        self.assertEqual(
            restarted.last_success_merged_count, intent.last_success_merged_count
        )
        self.assertEqual(restarted.revalidation_plan_ids, intent.revalidation_plan_ids)
        self.assertEqual(restarted.refactoring_plan_ids, intent.refactoring_plan_ids)
        self.assertEqual(
            before_rows,
            [tuple(row) for row in self.connection.execute(
                "SELECT * FROM exec_plans ORDER BY plan_id"
            )],
        )
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), commit)
        with self.assertRaises(maintenance_state.MaintenanceStateError):
            repository.restart_prepared_batch(
                str(self.repo), expected=intent, new_base_sha=latest
            )
        with self.assertRaises(loop_manager.PlannerValidationError):
            self._validate_barrier(context)
        fresh = loop_manager.prepare_maintenance_planning_context(
            self.connection, str(self.repo), other, latest
        )
        self.assertEqual(fresh.active_sha, latest)
        # A new publication intent can be recorded in the same phase.
        (other / self.ready_path.relative_to(self.repo)).write_bytes(
            self.ready_path.read_bytes()
        )
        if not revalidation:
            for plan_id, title in (
                ("G-002", "Extract Boundary"), ("G-003", "Separate Adapter")
            ):
                source = self._path(plan_id, title)
                (other / source.relative_to(self.repo)).write_bytes(source.read_bytes())
        fresh_result = loop_manager.validate_maintenance_planner_result(
            self.connection, fresh, other, loop_manager.changed_files(other)
        )
        git(other, "add", "docs/plans")
        git(other, "commit", "-m", "Replan maintenance at latest base")
        new_intent = loop_manager.record_maintenance_publication_intent(
            self.connection, str(self.repo), other, "main",
            fresh_result, git(other, "rev-parse", "HEAD"),
        )
        self.assertEqual(new_intent.publication_base_sha, latest)
        self.assertEqual(new_intent.publication_stage, "prepared")

    def _prepare_pushed_file_assessment_batch(self, *, legacy=False):
        context = self._begin_barrier()
        self.connection.execute(
            "UPDATE maintenance_cycles SET file_assessment_start_merged_count = 1 "
            "WHERE repo = ?", (str(self.repo),),
        )
        self.connection.execute(
            "INSERT INTO execplan_catalog_revisions "
            "(repo, revision_sha, catalog_fingerprint) VALUES (?, ?, 'catalog')",
            (str(self.repo), self.base_sha),
        )
        self._write_barrier_batch()
        result = self._validate_barrier(context)
        inventory = maintenance_file_inventory.tracked_source_inventory(
            self.repo, "main", self.base_sha,
        )
        reviewed = tuple(maintenance_file_ledger.ReviewedSource(
            source.path, source.content_hash, source.mode, source.revision,
            "Reviewed implementation, dependencies, and related tests.",
        ) for source in inventory.sources)
        maps = tuple((source.path, json.dumps({
            "responsibility": "Review the module responsibility",
            "entry_points": [], "public_symbols": [],
            "direct_references": [], "related_tests": [],
        })) for source in inventory.sources)
        from tests.support.maintenance import root_results_for
        evidence = maintenance_file_publication.AssessmentEvidence(
            inventory, reviewed, maps, 1,
            root_results_for(self.repo, inventory),
        )
        git(self.repo, "add", "docs/plans")
        git(self.repo, "commit", "-m", "Publish refactoring plans")
        plan_commit = git(self.repo, "rev-parse", "HEAD")
        with mock.patch.object(
            maintenance_file_publication, "collect", return_value=evidence,
        ):
            intent = loop_manager.record_maintenance_publication_intent(
                self.connection, str(self.repo), self.repo, "main", result,
                plan_commit,
            )
        if legacy:
            row = self.connection.execute("SELECT evidence_json FROM file_assessment_publications WHERE repo = ?",
                                          (str(self.repo),)).fetchone()
            payload = json.loads(row[0])
            del payload["root_reviews"]
            self.connection.execute("UPDATE file_assessment_publications SET evidence_json = ? WHERE repo = ?",
                                    (json.dumps(payload), str(self.repo)))
        git(self.repo, "push", "origin", "HEAD:main")
        return plan_commit, intent, inventory

    def _assert_published_batch_recovery_preserves_catalog_drift(
        self, drift: str
    ) -> None:
        planning_input_state.record_general(
            self.connection, str(self.repo), self.base_sha,
            loop_manager.plan_declarations(self.repo, self.base_sha),
        )
        context = self._begin_barrier()
        self._write_barrier_batch()
        result = self._validate_barrier(context)
        git(self.repo, "add", "docs/plans")
        git(self.repo, "commit", "-m", "Prepare batch")
        plan_commit = git(self.repo, "rev-parse", "HEAD")
        intent = loop_manager.record_maintenance_publication_intent(
            self.connection, str(self.repo), self.repo, "main",
            result, plan_commit,
        )
        git(self.repo, "push", "origin", "HEAD:main")
        other = Path(self.temporary.name) / f"catalog-{drift}"
        git(self.repo, "worktree", "add", "--detach", str(other), plan_commit)
        if drift == "addition":
            changed_plan = (
                other / "docs/plans/[G-004] Out Of Batch Plan.md"
            )
            changed_plan.write_text(
                plan_text("G-004", "Out Of Batch Plan"), encoding="utf-8"
            )
        elif drift == "modification":
            changed_plan = other / self._path(
                "G-000", "Merged Foundation"
            ).relative_to(self.repo)
            changed_plan.write_text(
                changed_plan.read_text(encoding="utf-8") + "\nOut-of-batch edit.\n",
                encoding="utf-8",
            )
        else:
            self.fail(f"unknown catalog drift fixture: {drift}")
        git(other, "add", changed_plan.relative_to(other).as_posix())
        git(other, "commit", "-m", f"Catalog {drift} after publication")
        git(other, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")

        recovered = loop_manager.recover_maintenance_planning_batch(
            self.connection, str(self.repo), self.repo, "main"
        )
        self.assertEqual(recovered.publication_commit_sha, plan_commit)
        self.assertNotEqual(recovered, intent)
        project = self.connection.execute(
            "SELECT processed_sha, planning_fingerprint FROM projects WHERE repo = ?",
            (str(self.repo),),
        ).fetchone()
        self.assertEqual(tuple(project), (None, None))
        self.assertTrue(planning_input_state.matches(
            self.connection, str(self.repo),
            loop_manager.plan_declarations(self.repo, plan_commit),
        ))
        remote = git(self.repo, "rev-parse", "origin/main")
        self.assertFalse(planning_input_state.matches(
            self.connection, str(self.repo),
            loop_manager.plan_declarations(self.repo, remote),
        ))
