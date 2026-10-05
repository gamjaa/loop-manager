#!/usr/bin/env bash
set -eu

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
python="$root/.venv/bin/python"
if [[ ! -x "$python" ]]; then python=python3; fi
export PYTHONIOENCODING=utf-8
exec "$python" -u -m loop_manager_internal.verification.runner
