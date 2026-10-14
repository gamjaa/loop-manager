import tempfile
import unittest
from pathlib import Path
from unittest import mock

import loop_manager
from loop_manager_internal.application import commands, scheduler
from loop_manager_internal.config.managed_config import ManagedConfig
from loop_manager_internal.runtime import project_registry
from tests.support.database import runtime_connection


class RunProjectSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.connection = runtime_connection()
        self.addCleanup(self.connection.close)
        self.repositories = tuple(
            self.root / name for name in ("project with space", "second project")
        )
        for repository in self.repositories:
            repository.mkdir()
            project_registry.add_project(
                self.connection,
                project_registry.RegisteredProject(
                    str(repository.resolve()), "main", "codex", None, False,
                ),
            )
        self.state_dir = self.root / "state"
        self.config_path = self.root / "config.ini"
        self.config = ManagedConfig(self.config_path, 2, "", "")

    def _execute_effects(self, run_registered_projects):
        return commands.ExecuteRuntimeCommandEffects(
            _document_drafts_command=mock.Mock(),
            configure_registry=mock.Mock(),
            connect_database=mock.Mock(return_value=self.connection),
            load_managed_config=mock.Mock(return_value=self.config),
            operation_print=mock.Mock(),
            run_registered_projects=run_registered_projects,
            run_project_result=mock.Mock(),
        )

    def test_registry_selection_normalizes_and_deduplicates_paths(self):
        first, second = self.repositories
        selected = project_registry.select_projects(
            self.connection,
            [str(first / "missing" / ".."), str(first), str(second), str(first)],
        )

        self.assertEqual(
            {project.repo for project in selected},
            {str(first.resolve()), str(second.resolve())},
        )
        with self.assertRaisesRegex(project_registry.ProjectRegistryError, "등록되지 않은"):
            project_registry.select_projects(
                self.connection, [str(first), str(self.root / "not registered")],
            )

    def test_bootstrap_child_arguments_preserve_selected_paths_with_spaces(self):
        first, second = self.repositories
        arguments = loop_manager.runtime_child_arguments(
            ["run", "--skip-if-running", str(first), str(second)],
            source=self.root, state_dir=self.state_dir, config_path=self.config_path,
            runtime=loop_manager.RuntimeWorktree(self.root / "runtime", "a" * 40),
        )

        self.assertEqual(
            arguments[-4:], ["run", "--skip-if-running", str(first), str(second)],
        )

    def test_add_and_remove_reuse_the_existing_registry_handlers(self):
        first = self.repositories[0]
        connection = mock.MagicMock()
        registered = project_registry.RegisteredProject(
            str(first.resolve()), "develop", "claude", "agents-hash", True,
        )
        add_registered_project = mock.Mock(return_value=registered)
        registry_effects = commands.ConfigureRegistryEffects(
            add_registered_project=add_registered_project,
            connect_database=mock.Mock(return_value=connection),
            load_managed_config=mock.Mock(return_value=self.config),
            operation_print=mock.Mock(),
        )

        add_args = loop_manager.build_parser().parse_args(
            ["add", str(first), "--base", "develop", "--provider", "claude"]
        )
        commands.configure_registry(
            registry_effects, add_args, self.state_dir, self.config_path,
        )
        add_registered_project.assert_called_once_with(
            connection, first,
            config_path=self.config.path,
            provider="claude",
            requested_base="develop",
            approve_agents_mismatch=False,
            state_dir=self.state_dir,
        )

        remove_args = loop_manager.build_parser().parse_args(["remove", str(first)])
        with mock.patch.object(commands.project_registry, "remove_project") as remove_project:
            commands.configure_registry(
                registry_effects, remove_args, self.state_dir, self.config_path,
            )
        remove_project.assert_called_once_with(connection, str(first.resolve()))

    def test_run_validates_all_paths_before_reconciliation_and_passes_scope(self):
        first, second = self.repositories
        arguments = loop_manager.build_parser().parse_args(
            ["run", str(first / "missing" / ".."), str(first), str(second), str(second)]
        )
        run_registered_projects = mock.Mock(return_value=7)
        effects = self._execute_effects(run_registered_projects)

        with mock.patch.object(
            commands.agent_invocation_state.InvocationRepository,
            "reconcile_interrupted",
        ) as reconcile:
            result = commands.execute_runtime_command(
                effects, arguments, self.state_dir, self.config_path,
            )

        self.assertEqual(result, 7)
        selected = run_registered_projects.call_args.kwargs["selected_projects"]
        self.assertEqual(
            {project.repo for project in selected},
            {str(first.resolve()), str(second.resolve())},
        )
        reconcile.assert_called_once_with(state_dir=self.state_dir)

    def test_run_rejects_any_unregistered_path_before_state_reconciliation(self):
        first = self.repositories[0]
        arguments = loop_manager.build_parser().parse_args(
            ["run", str(first), str(self.root / "not registered")]
        )
        run_registered_projects = mock.Mock()
        effects = self._execute_effects(run_registered_projects)

        with mock.patch.object(
            commands.agent_invocation_state.InvocationRepository,
            "reconcile_interrupted",
        ) as reconcile:
            with self.assertRaisesRegex(
                project_registry.ProjectRegistryError, "등록되지 않은",
            ):
                commands.execute_runtime_command(
                    effects, arguments, self.state_dir, self.config_path,
                )

        reconcile.assert_not_called()
        run_registered_projects.assert_not_called()

    def test_scheduler_runs_only_the_validated_project_scope(self):
        first = self.repositories[0]
        selected = project_registry.select_projects(self.connection, [str(first)])
        preflight = mock.Mock()
        run_one = mock.Mock(side_effect=lambda project, *_args, **_kwargs: project.repo)
        effects = scheduler.ScheduleRegisteredProjectsEffects(preflight, run_one)
        coordinator = mock.MagicMock()
        coordinator.__enter__.return_value = coordinator

        with mock.patch.object(scheduler, "VerifyCoordinator", return_value=coordinator):
            outcomes = scheduler.schedule_registered_projects(
                effects, self.connection, self.state_dir, self.config,
                selected_projects=selected,
            )

        self.assertEqual(outcomes, (str(first.resolve()),))
        self.assertEqual([call.args[0].repo for call in run_one.call_args_list], [str(first.resolve())])
        preflight.assert_called_once_with(
            self.connection, selected, self.state_dir, None, config_path=self.config.path,
        )


if __name__ == "__main__":
    unittest.main()
