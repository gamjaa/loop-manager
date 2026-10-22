"""Stable resource observations for nested manifest/native-result fixtures."""
from pathlib import Path


def nested_runner_source(entrypoint):
    # Manifest/result integration must not wait on the enclosing suite's CPU load.
    return f"""import runpy
import sys
import time
from unittest.mock import patch
sys.path.insert(0, {str(Path(entrypoint).resolve().parents[2])!r})
from loop_manager_internal.verification.resources import ResourceSampler, Snapshot
with patch.object(ResourceSampler, 'sample',
        lambda self: Snapshot(time.monotonic(), 2, 0, 4 * 1024 ** 3, 'manifest fixture')):
    runpy.run_path({str(entrypoint)!r}, run_name='__main__')
"""

