"""Own documents dependency wiring; no CLI import or resource effects."""
from __future__ import annotations

from loop_manager_internal.application.composition import Composition
from loop_manager_internal.documents import guidance_service
from loop_manager_internal.documents import pr_description


def register(assembly: Composition) -> None:
    assembly.bind('_published_plan_allows_common_plans', guidance_service._published_plan_allows_common_plans,
        lambda: guidance_service.PublishedPlanAllowsCommonPlansEffects(
            git=assembly.callback('git'),
            is_loop_manager_repository=assembly.callback('is_loop_manager_repository'),
        ),
    )

    assembly.bind('_plan_block_selected_for_release', guidance_service._plan_block_selected_for_release,
        lambda: guidance_service.PlanBlockSelectedForReleaseEffects(
            hydrate_execplan_project=assembly.callback('hydrate_execplan_project'),
            project_execplan=assembly.callback('project_execplan'),
        ),
    )

    assembly.bind('resume_retired_guidance_block', guidance_service.resume_retired_guidance_block,
        lambda: guidance_service.ResumeRetiredGuidanceBlockEffects(
            _current_rebase_service=assembly.callback('_current_rebase_service'),
            _plan_block_selected_for_release=assembly.callback('_plan_block_selected_for_release'),
            _rebase_in_progress_for_branch=assembly.callback('_rebase_in_progress_for_branch'),
            _update_blocked_error=assembly.callback('_update_blocked_error'),
            clear_agent_scope=assembly.callback('clear_agent_scope'),
            make_worktree=assembly.callback('make_worktree'),
            update_project=assembly.callback('update_project'),
            validate_blocked_agent_scope=assembly.callback('validate_blocked_agent_scope'),
            validate_execplan_workspace_identity=assembly.callback('validate_execplan_workspace_identity'),
        ),
    )

    assembly.bind('build_pr_body', pr_description.build_pr_body,
        lambda: pr_description.BuildPrBodyEffects(
            execplan_purpose=assembly.callback('execplan_purpose'),
            immutable_github_blob_url=assembly.callback('immutable_github_blob_url'),
        ),
    )
