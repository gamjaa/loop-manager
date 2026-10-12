"""Independent fresh import shard 2; scheduled by the common adapter."""
from tests.support import fresh_imports


def load_tests(loader, tests, pattern):
    return fresh_imports.suite(loader, __name__, 2)
