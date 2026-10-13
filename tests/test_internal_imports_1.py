"""Independent fresh import shard 1; scheduled by the common adapter."""
from tests.support import fresh_imports


def load_tests(loader, tests, pattern):
    return fresh_imports.suite(loader, __name__, 1)
