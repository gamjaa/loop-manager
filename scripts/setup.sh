#!/usr/bin/env bash
set -eu

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
venv="$root/.venv"

if [[ ! -x "$venv/bin/python" ]]; then
    python3 -m venv "$venv"
fi

if ! "$venv/bin/python" -m pip --version >/dev/null 2>&1; then
    "$venv/bin/python" -m ensurepip --upgrade
fi

if grep -Eq '^[[:space:]]*[^#[:space:]]' "$root/requirements.txt"; then
    "$venv/bin/python" -m pip install --disable-pip-version-check -r "$root/requirements.txt"
fi

cd "$root"
"$venv/bin/python" - <<'PY'
from pathlib import Path
from loop_manager_internal.config.execution_path import ManagedConfigError, save_execution_path
try:
    save_execution_path(Path.cwd())
except (OSError, UnicodeError, ManagedConfigError) as error:
    raise SystemExit(f"PATH 저장 실패: {error}")
PY
echo "준비 완료: $venv/bin/python"
