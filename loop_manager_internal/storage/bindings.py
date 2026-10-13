"""Own storage dependency wiring; no CLI import or resource effects."""
from __future__ import annotations

from loop_manager_internal.application.composition import Composition
from loop_manager_internal.storage import project_state_adapter


def register(assembly: Composition) -> None:
    assembly.bind('update_project', project_state_adapter.update_project,
        lambda: project_state_adapter.UpdateProjectEffects(_ensure_execplans_schema=assembly.callback('_ensure_execplans_schema')),
    )

    assembly.bind('activate_ready_execplan', project_state_adapter.activate_ready_execplan,
        lambda: project_state_adapter.ActivateReadyExecplanEffects(
            assessment_can_yield=assembly.callback('assessment_can_yield'),
            git=assembly.callback('git'),
            is_unowned_ready=assembly.callback('is_unowned_ready'),
            select_ready_execplan=assembly.callback('select_ready_execplan'),
        ),
    )

    assembly.bind('hydrate_active_project', project_state_adapter.hydrate_active_project,
        lambda: project_state_adapter.HydrateActiveProjectEffects(
            active_execplan=assembly.callback('active_execplan'),
            hydrate_execplan_project=assembly.callback('hydrate_execplan_project'),
        ),
    )

    assembly.bind('_mirror_project_update_to_execplan', project_state_adapter._mirror_project_update_to_execplan,
        lambda: project_state_adapter.MirrorProjectUpdateToExecplanEffects(_ensure_execplans_schema=assembly.callback('_ensure_execplans_schema')),
    )

    assembly.bind('clear_planning_validation_state', project_state_adapter.clear_planning_validation_state,
        lambda: project_state_adapter.ClearPlanningValidationStateEffects(update_project=assembly.callback('update_project')),
    )
