#!/usr/bin/env bash
set -euo pipefail

# 이 스크립트 파일이 위치한 디렉터리
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

cd "$SCRIPT_DIR"

if git_output=$(git pull --ff-only 2>&1); then
    if [[ "$git_output" != "Already up to date." ]]; then
        printf '%s\n' "$git_output"
    fi
else
    printf '%s\n' "$git_output"
    exit 1
fi

exec "$SCRIPT_DIR/.venv/bin/python" -u "$SCRIPT_DIR/loop_manager.py" run "$@"