#!/usr/bin/env bash
set -eu

python3 -m unittest discover -s tests
python3 -m py_compile loop_manager.py
