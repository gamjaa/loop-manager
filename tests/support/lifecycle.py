"""Register fixture cleanup immediately, including nested and failed setup."""
from contextlib import ExitStack


def fixture_cleanup(test, fixture):
    cleanup = ExitStack()
    fixture.addCleanup = cleanup.callback
    fixture.doCleanups = cleanup.close
    test.addCleanup(cleanup.close)
    # Fake Git adapters retain their existing assertion contract.
    fixture.assertEqual = test.assertEqual
    fixture.fail = test.fail
    return fixture
