from tests.support.database import AutomationCatalogFixture
import json
import tempfile
import unittest
import loop_manager
from tests.support.catalog import make_catalog_root, write_execplan, planner_git_observations


class CatalogBatchesTest(AutomationCatalogFixture, unittest.TestCase):
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
            repo = make_catalog_root(directory)
            existing = write_execplan(repo, "G-001", "Existing", priority=None)
            second = write_execplan(repo, "G-002", "Second", priority="high")
            third = write_execplan(
                repo,
                "G-003",
                "Third",
                priority="critical",
                depends_on=("G-002",),
            )

            with planner_git_observations({existing.relative_to(repo).as_posix(): existing.read_text(encoding='utf-8')}):
                plans = loop_manager.validate_planner_result(
                    repo, 'base',
                    [second.relative_to(repo).as_posix(), third.relative_to(repo).as_posix()],
                )

            self.assertEqual([plan["id"] for _path, plan in plans.plans], ["G-002", "G-003"])

        with tempfile.TemporaryDirectory() as directory:
            repo = make_catalog_root(directory)
            existing = write_execplan(repo, "G-001", "Existing")
            gap = write_execplan(repo, "G-003", "Gap")

            with planner_git_observations({existing.relative_to(repo).as_posix(): existing.read_text(encoding='utf-8')}), \
                 self.assertRaisesRegex(loop_manager.PlannerValidationError, "연속"):
                loop_manager.validate_planner_result(
                    repo,
                    'base',
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
