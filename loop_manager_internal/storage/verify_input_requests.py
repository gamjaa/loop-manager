"""Durable Manager-owned missing-list requests; no candidate-triggered calls."""
import hashlib
import json
from collections.abc import Mapping
from loop_manager_internal.catalog import manager_sources as contract


class InvalidNewVerifyInputPriority(RuntimeError):
    """A newly registered Manager verify-inputs plan did not use critical."""


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


def validate_new_priority(sources, priority):
    if contract.SOURCE in sources and priority != 'critical':
        raise InvalidNewVerifyInputPriority(
            '새 manager:verify-inputs ExecPlan은 priority: critical이어야 합니다.'
        )


def validate(connection, repo, text, sources, *, is_new=False, metadata: Mapping | None = None):
    if not any(str(source).startswith('manager:') for source in sources):
        return
    value = contract.validate_scope(text, sources)
    ensure_schema(connection)
    row = connection.execute('SELECT revision, purpose, version, rule FROM verify_input_requests '
                             'WHERE repo=? AND request_id=?', (repo, value['id'])).fetchone()
    if row is None or tuple(row) != (value['revision'], value['purpose'], value['version'], contract.source_content(contract.SOURCE)):
        raise RuntimeError('unregistered Manager verify-inputs request')
    if is_new:
        validate_new_priority(sources, metadata.get('priority') if metadata is not None else None)
