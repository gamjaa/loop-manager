#!/usr/bin/env bash
set -eu
cd "$(dirname "${BASH_SOURCE[0]}")/.."
: "${LOOP_MANAGER_VERIFY_RUNTIME_PYTHON:?Set the installed Manager Python absolute path}"
: "${LOOP_MANAGER_VERIFY_RUNNER:?Set the installed entrypoint.py absolute path}"
exec "$LOOP_MANAGER_VERIFY_RUNTIME_PYTHON" -u "$LOOP_MANAGER_VERIFY_RUNNER" --protocol-version "${LOOP_MANAGER_VERIFY_VERSION:-1}" --project-python "${LOOP_MANAGER_EXAMPLE_PYTHON:-$LOOP_MANAGER_VERIFY_RUNTIME_PYTHON}" --timeout "${LOOP_MANAGER_VERIFY_TIMEOUT:-600}"
