"""planning planning ports boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.planning.planning_runtime import PlanningContext
from loop_manager_internal.planning.assessment_port import MaintenanceAssessmentPort
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.storage import runtime_state
import sqlite3
import threading


@dataclass(frozen=True)
class PlanningLifecyclePortsEffects:
    """Named effects for _PlanningLifecyclePorts; no entry-point/module access."""
    _PlanningAgentPorts: Callable[..., object]
    _PlanningEventPorts: Callable[..., object]
    _PlanningFileReviewPorts: Callable[..., object]
    _PlanningGitPorts: Callable[..., object]
    _PlanningReconciliationPorts: Callable[..., object]
    _PlanningStatePorts: Callable[..., object]
    _PlanningValidationPorts: Callable[..., object]


class _PlanningLifecyclePorts:
    """Concrete bootstrap bindings for the independent coordinator."""

    def __init__(
        self,
        effects: PlanningLifecyclePortsEffects, connection: sqlite3.Connection,
        runtime_repository: runtime_state.RuntimeStateRepository,
        maintenance_repository: maintenance_state.MaintenanceRepository,
        context: PlanningContext,
        planner_mutex: threading.Lock | None,
    ):
        self._effects = effects
        self.state = self._effects._PlanningStatePorts(
            connection, runtime_repository, maintenance_repository, context.state_dir
        )
        self.git = self._effects._PlanningGitPorts(
            connection, context.repo_path, context.state_dir
        )
        self.agent = self._effects._PlanningAgentPorts(connection, context, planner_mutex)
        self.validation = self._effects._PlanningValidationPorts(connection)
        self.file_review = self._effects._PlanningFileReviewPorts(connection)
        self.reconciliation = self._effects._PlanningReconciliationPorts(connection)
        from loop_manager_internal.maintenance.tiered_ports import TieredMaintenancePort
        self.tiered = TieredMaintenancePort(connection)
        self.assessment = MaintenanceAssessmentPort()
        self.events = self._effects._PlanningEventPorts()
