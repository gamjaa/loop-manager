from contextlib import closing
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.application.commands import execute_runtime_command


class TieredMaintenanceCommandsTest(unittest.TestCase):
    def test_explicit_full_review_is_queued_without_starting_an_agent(self):
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("CREATE TABLE projects (repo TEXT PRIMARY KEY, registered INTEGER)")
            repository = str(Path("/registered").resolve())
            connection.execute("INSERT INTO projects VALUES (?, 1)", (repository,))
            effects = SimpleNamespace(load_managed_config=lambda path: None,
                connect_database=lambda path: connection, operation_print=mock.Mock(), run_registered_projects=mock.Mock())
            arguments = loop_manager.build_parser().parse_args(["maintenance-review", repository])
            self.assertEqual(execute_runtime_command(effects, arguments, Path("/state"), Path("/config")), 0)
            first = connection.execute("SELECT request_id FROM tiered_maintenance_requests WHERE repo = ?", (repository,)).fetchone()[0]
            self.assertEqual(execute_runtime_command(effects, arguments, Path("/state"), Path("/config")), 0)
            second = connection.execute("SELECT request_id FROM tiered_maintenance_requests WHERE repo = ?", (repository,)).fetchone()[0]
            self.assertNotEqual(first, second)
            effects.run_registered_projects.assert_not_called()

    def test_unregistered_project_cannot_queue_a_review(self):
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("CREATE TABLE projects (repo TEXT PRIMARY KEY, registered INTEGER)")
            effects = SimpleNamespace(load_managed_config=lambda path: None, connect_database=lambda path: connection)
            arguments = loop_manager.build_parser().parse_args(["maintenance-review", "/unknown"])
            with self.assertRaisesRegex(ValueError, "등록된 프로젝트"):
                execute_runtime_command(effects, arguments, Path("/state"), Path("/config"))


if __name__ == "__main__":
    unittest.main()
