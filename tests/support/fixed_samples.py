"""Paths to static repository samples used by self-verification tests."""
from pathlib import Path


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PROJECT_FILES = FIXTURES / "project-files"
PROVIDER_PROFILES = FIXTURES / "provider-profiles.ini"
