#!/usr/bin/env bash
set -euo pipefail

# 이 스크립트 파일이 위치한 디렉터리
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

cd "$SCRIPT_DIR"

exec "$SCRIPT_DIR/.venv/bin/python" -u "$SCRIPT_DIR/loop_manager.py" run "$@"
