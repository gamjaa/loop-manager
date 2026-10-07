"""Durable Manager-owned missing-list requests; no candidate-triggered calls."""
import hashlib
import json
from loop_manager_internal.catalog import manager_sources as contract


def ensure_schema(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS verify_input_requests (
        repo TEXT NOT NULL, request_id TEXT NOT NULL, revision TEXT NOT NULL,
        purpose TEXT NOT NULL, version INTEGER NOT NULL, rule TEXT NOT NULL,
        PRIMARY KEY (repo, request_id))''')


def register(connection, repo, revision):
    ensure_schema(connection)
    request_id = hashlib.sha256(json.dumps((repo, revision, contract.source_content(contract.SOURCE))).encode()).hexdigest()
    connection.execute('INSERT OR IGNORE INTO verify_input_requests VALUES (?, ?, ?, ?, ?, ?)',
                       (repo, request_id, revision, contract.PURPOSE, contract.VERSION, contract.source_content(contract.SOURCE)))
    return {"id": request_id, "revision": revision, "purpose": contract.PURPOSE, "version": contract.VERSION}


def validate(connection, repo, text, sources):
    if not any(str(source).startswith('manager:') for source in sources):
        return
    value = contract.validate_scope(text, sources)
    ensure_schema(connection)
    row = connection.execute('SELECT revision, purpose, version, rule FROM verify_input_requests '
                             'WHERE repo=? AND request_id=?', (repo, value['id'])).fetchone()
    if row is None or tuple(row) != (value['revision'], value['purpose'], value['version'], contract.source_content(contract.SOURCE)):
        raise RuntimeError('unregistered Manager verify-inputs request')
