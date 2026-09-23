$ErrorActionPreference = 'Stop'

py -3 -u -m unittest discover -s tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
py -3 -m py_compile blocked_checkpoint.py blocked_checkpoint_state.py execution_checkpoint.py execution_checkpoint_state.py execution_handlers.py execution_state.py execution_runtime.py execplan_archive.py execplan_archive_state.py execplan_catalog.py execplan_catalog_state.py loop_manager.py maintenance_git.py maintenance_orchestration.py maintenance_planning.py maintenance_state.py planning_catalog.py planning_runtime.py publication_resume.py rebase_correction.py rebase_correction_state.py recovery_dispatcher.py recovery_runtime.py recovery_state.py requirement_archive.py requirement_archive_recovery.py requirement_archive_state.py runtime_state.py scoped_blocking.py agent_invocation.py agent_invocation_state.py agent_provider_usage.py worker_session.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
