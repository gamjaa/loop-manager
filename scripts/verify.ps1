$ErrorActionPreference = 'Stop'

py -3 -m unittest discover -s tests
py -3 -m py_compile loop_manager.py maintenance_planning.py
