$ErrorActionPreference = 'Stop'

py -3 -m unittest discover -s tests
py -3 -m py_compile loop_manager.py maintenance_git.py maintenance_orchestration.py maintenance_planning.py maintenance_state.py scoped_blocking.py
