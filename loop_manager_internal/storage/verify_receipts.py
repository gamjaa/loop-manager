"""Durable verify runs and reuse links, scoped to one execution cycle.

Starting a run revokes the old success before any process is launched. All
selection/confirmation writes use a generation CAS inside the caller's runtime
transaction; a missing or interrupted completion is never a reusable result.
"""
import json
from uuid import uuid4

from loop_manager_internal.storage.execution_checkpoint_state import CheckpointRace


def ensure_schema(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS verify_receipt_cycles (
        repo TEXT NOT NULL, plan_id TEXT NOT NULL, cycle TEXT NOT NULL,
        generation INTEGER NOT NULL DEFAULT 0, receipt_id TEXT,
        PRIMARY KEY (repo, plan_id))''')
    connection.execute('''CREATE TABLE IF NOT EXISTS verify_receipts (
        id TEXT PRIMARY KEY, repo TEXT NOT NULL, plan_id TEXT NOT NULL,
        cycle TEXT NOT NULL, kind TEXT NOT NULL, origin_id TEXT,
        head TEXT NOT NULL, fingerprint TEXT, observation TEXT NOT NULL,
        result TEXT, status TEXT NOT NULL, reason TEXT NOT NULL)''')
    connection.execute('''CREATE INDEX IF NOT EXISTS verify_receipts_cycle
        ON verify_receipts (repo, plan_id, cycle)''')


def start_cycle(connection, repo, plan_id):
    connection.execute('''INSERT INTO verify_receipt_cycles (repo, plan_id, cycle)
        VALUES (?, ?, ?) ON CONFLICT (repo, plan_id) DO UPDATE SET
        cycle=excluded.cycle, generation=generation+1, receipt_id=NULL''',
        (repo, plan_id, uuid4().hex))


def selected(connection, repo, plan_id):
    row = connection.execute('SELECT * FROM verify_receipt_cycles WHERE repo=? AND plan_id=?',
                             (repo, plan_id)).fetchone()
    return dict(row) if row else None


def current_receipt(connection, selection):
    """Return only the receipt selected by the active cycle generation."""
    if selection is None or not selection['receipt_id']:
        return None
    row = connection.execute('SELECT * FROM verify_receipts WHERE id=?',
                             (selection['receipt_id'],)).fetchone()
    if (row is None or any(row[key] != selection[key]
                           for key in ('repo', 'plan_id', 'cycle'))):
        return None
    return dict(row)


def latest_decision(connection, selection):
    if selection is None:
        return None
    row = connection.execute('''SELECT * FROM verify_receipts
        WHERE repo=? AND plan_id=? AND cycle=? ORDER BY rowid DESC LIMIT 1''',
        (selection['repo'], selection['plan_id'], selection['cycle'])).fetchone()
    return dict(row) if row else None


def _observation_matches(receipt, observation):
    if observation is None or not getattr(observation, 'proven', False):
        # A direct run is bound to its candidate HEAD; reuse needs a proven observation.
        return True
    try:
        original = json.loads(receipt['observation'])
    except (KeyError, TypeError, ValueError):
        return False
    return (
        original.get('fingerprint') == receipt['fingerprint']
        and receipt['fingerprint'] == observation.fingerprint
    )


def success(connection, selection, observation):
    if (selection is None or observation is None or not observation.proven
            or not selection['receipt_id']):
        return None
    row = connection.execute('SELECT * FROM verify_receipts WHERE id=?',
                             (selection['receipt_id'],)).fetchone()
    if (row is None or row['status'] != 'success' or row['kind'] != 'run'
            or not isinstance(row['result'], str)
            or any(row[key] != selection[key] for key in ('repo', 'plan_id', 'cycle'))
            or row['fingerprint'] != observation.fingerprint):
        return None
    try:
        original = json.loads(row['observation'])
        if original['reason'] is not None or original['fingerprint'] != row['fingerprint']:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return dict(row)


def candidate_evidence(connection, repo, plan_id, head, output, observation=None):
    """Bind a run, proven reuse, or waiver to the current candidate result."""
    selection = selected(connection, repo, plan_id)
    receipt = current_receipt(connection, selection)
    latest = latest_decision(connection, selection)
    if (receipt and latest and latest['id'] == receipt['id']
            and receipt['head'] == head and receipt['result'] == output):
        if (receipt['kind'] == 'run' and receipt['status'] == 'success'
                and _observation_matches(receipt, observation)):
            return {'kind': 'run', 'receipt': receipt}
        if receipt['kind'] == 'waiver' and receipt['status'] == 'waived':
            return {'kind': 'waiver', 'receipt': receipt}

    origin = (success(connection, selection, observation)
              if getattr(observation, 'proven', False) else None)
    reused = reused_candidate(connection, repo, plan_id, head, output)
    if (origin and origin['result'] == output and reused
            and reused['origin_id'] == origin['id']
            and reused['fingerprint'] == observation.fingerprint):
        return {'kind': 'reuse', 'receipt': reused, 'origin': origin}
    return None


def reused_candidate(connection, repo, plan_id, head, output):
    """Only the latest decision in the active cycle can bind this candidate."""
    row = connection.execute('''SELECT r.* FROM verify_receipts r
        JOIN verify_receipt_cycles c ON r.repo=c.repo AND r.plan_id=c.plan_id AND r.cycle=c.cycle
        WHERE r.repo=? AND r.plan_id=? ORDER BY r.rowid DESC LIMIT 1''',
        (repo, plan_id)).fetchone()
    if (row and row['kind'] == 'reuse' and row['status'] == 'success'
            and row['head'] == head and row['result'] == output):
        return dict(row)
    return None


def record(connection, selection, *, head, observation, kind, reason, origin=None):
    identifier = uuid4().hex
    result = connection.execute('''UPDATE verify_receipt_cycles
        SET generation=generation+1, receipt_id=?
        WHERE repo=? AND plan_id=? AND cycle=? AND generation=?''',
        (identifier if kind == 'run' else selection['receipt_id'], selection['repo'],
         selection['plan_id'], selection['cycle'], selection['generation']))
    if result.rowcount != 1:
        raise CheckpointRace('verify receipt selection changed')
    from dataclasses import asdict
    connection.execute('''INSERT INTO verify_receipts
        (id, repo, plan_id, cycle, kind, origin_id, head, fingerprint,
         observation, result, status, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (identifier, selection['repo'], selection['plan_id'], selection['cycle'], kind,
         origin['id'] if origin else None, head, observation.fingerprint,
         json.dumps(asdict(observation), sort_keys=True), origin['result'] if origin else None,
         'success' if origin else 'pending', reason))
    return identifier, selected(connection, selection['repo'], selection['plan_id'])


def complete(connection, selection, identifier, *, passed, output, executed=True):
    if selected(connection, selection['repo'], selection['plan_id']) != selection:
        raise CheckpointRace('verify receipt completion changed')
    result = connection.execute('''UPDATE verify_receipts SET status=?, result=?, kind=?
        WHERE id=? AND cycle=? AND status='pending' ''',
        (('success' if passed else 'failure') if executed else 'waived', str(output),
         'run' if executed else 'waiver', identifier, selection['cycle']))
    if result.rowcount != 1:
        raise CheckpointRace('verify receipt completion is no longer pending')
