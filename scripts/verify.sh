#!/usr/bin/env bash
set -eu

python3 -m unittest discover -s tests
python3 -m py_compile blocked_checkpoint.py blocked_checkpoint_state.py execution_handlers.py execution_state.py execution_runtime.py loop_manager.py maintenance_git.py maintenance_orchestration.py maintenance_planning.py maintenance_state.py planning_runtime.py runtime_state.py scoped_blocking.py
