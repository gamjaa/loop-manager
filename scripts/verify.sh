#!/usr/bin/env bash
set -eu

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
python="$root/.venv/bin/python"
if [[ ! -x "$python" ]]; then python=python3; fi
export PYTHONIOENCODING=utf-8
if [[ -n "${LOOP_MANAGER_VERIFY_RUNNER:-}" ]]; then
    : "${LOOP_MANAGER_VERIFY_RUNTIME_PYTHON:?Manager runtime Python is required}"
    : "${LOOP_MANAGER_VERIFY_VERSION:?Manager runner version is required}"
    [[ -f "$LOOP_MANAGER_VERIFY_RUNNER" ]] || { echo "verify runner is missing" >&2; exit 2; }
    exec "$LOOP_MANAGER_VERIFY_RUNTIME_PYTHON" -u "$LOOP_MANAGER_VERIFY_RUNNER" --protocol-version "$LOOP_MANAGER_VERIFY_VERSION" --project-python "$("$python" -c 'import sys; print(sys.executable)')" --timeout "${LOOP_MANAGER_VERIFY_TIMEOUT:-600}"
fi
exec "$python" -u -m loop_manager_internal.verification.runner --protocol-version 1 --project-python "$("$python" -c 'import sys; print(sys.executable)')"
