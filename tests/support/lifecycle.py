"""Register fixture cleanup immediately, including nested and failed setup."""
from contextlib import ExitStack


def register_connection_cleanup(owner, connection):
    """Register an owned connection immediately and return an idempotent closer."""
    closed = False

    def close_connection():
        nonlocal closed
        if not closed:
            connection.close()
            closed = True

    try:
        owner.addCleanup(close_connection)
    except BaseException:
        close_connection()
        raise
    return close_connection


def fixture_cleanup(test, fixture):
    cleanup = ExitStack()
    fixture.addCleanup = cleanup.callback
    fixture.doCleanups = cleanup.close
    test.addCleanup(cleanup.close)
    # Fake Git adapters retain their existing assertion contract.
    fixture.assertEqual = test.assertEqual
    fixture.fail = test.fail
    return fixture
