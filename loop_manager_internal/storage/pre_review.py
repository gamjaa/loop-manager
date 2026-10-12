"""Independent partial/preliminary evidence; never a full verify receipt."""
import json
import time
from uuid import uuid4
from loop_manager_internal.storage import verify_receipts
from loop_manager_internal.storage.execution_checkpoint_state import CheckpointRace


def ensure_schema(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS preliminary_reviews (
        id TEXT PRIMARY KEY, repo TEXT NOT NULL, plan_id TEXT NOT NULL,
        cycle TEXT NOT NULL, candidate TEXT NOT NULL, status TEXT NOT NULL,
        partial TEXT, review TEXT, identity TEXT, started REAL NOT NULL,
        updated REAL NOT NULL, generation INTEGER NOT NULL DEFAULT 0)''')
    connection.execute('''CREATE INDEX IF NOT EXISTS preliminary_reviews_cycle
        ON preliminary_reviews(repo, plan_id, cycle)''')


def current(connection, repo, plan_id):
    ensure_schema(connection)
    cycle = verify_receipts.selected(connection, repo, plan_id)
    if not cycle:
        return None
    row = connection.execute('''SELECT * FROM preliminary_reviews
        WHERE repo=? AND plan_id=? AND cycle=? ORDER BY rowid DESC LIMIT 1''',
        (repo, plan_id, cycle['cycle'])).fetchone()
    return dict(row) if row else None


def begin(connection, repo, plan_id, candidate):
    ensure_schema(connection)
    cycle = verify_receipts.selected(connection, repo, plan_id)
    if not cycle:
        verify_receipts.start_cycle(connection, repo, plan_id)
        cycle = verify_receipts.selected(connection, repo, plan_id)
    identifier, now = uuid4().hex, time.time()
    connection.execute('''INSERT INTO preliminary_reviews
        (id,repo,plan_id,cycle,candidate,status,started,updated)
        VALUES (?,?,?,?,?,'partial_running',?,?)''',
        (identifier, repo, plan_id, cycle['cycle'], candidate, now, now))
    return current(connection, repo, plan_id)


def update(connection, row, status, *, partial=None, review=None, identity=None):
    selected = current(connection, row['repo'], row['plan_id'])
    if not selected or selected['id'] != row['id']:
        raise CheckpointRace('preliminary review cycle or selected receipt changed')
    result = connection.execute('''UPDATE preliminary_reviews
        SET status=?, partial=COALESCE(?,partial), review=COALESCE(?,review),
            identity=COALESCE(?,identity), updated=?, generation=generation+1
        WHERE id=? AND generation=?''',
        (status, json.dumps(partial, sort_keys=True) if partial is not None else None,
         review, json.dumps(identity, sort_keys=True) if identity is not None else None,
         time.time(), row['id'], row['generation']))
    if result.rowcount != 1:
        raise CheckpointRace('preliminary review generation changed')
    return current(connection, row['repo'], row['plan_id'])
