#!/usr/bin/env python3
"""Requirement에서 계획·구현 PR까지 조율하는 Loop Manager CLI."""
from loop_manager_internal.application.entrypoint import main

REWRITE_RUNTIME_VERSION = 1

if __name__ == "__main__":
    raise SystemExit(main())
