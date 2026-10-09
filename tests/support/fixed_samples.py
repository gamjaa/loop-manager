"""Paths to static repository samples used by self-verification tests."""
from pathlib import Path
from unittest import mock


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PROJECT_FILES = FIXTURES / "project-files"
PROVIDER_PROFILES = FIXTURES / "provider-profiles.ini"


def use_fixed_guidance(test):
    """Keep preparation, hash comparisons and follow-up calls on static samples."""
    patch = mock.patch('loop_manager_internal.agents.common_guidance.RUNTIME_ROOT', PROJECT_FILES)
    patch.start()
    test.addCleanup(patch.stop)
