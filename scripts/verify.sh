#!/usr/bin/env bash
set -eu

exec python3 -u -m loop_manager_internal.verification.runner
