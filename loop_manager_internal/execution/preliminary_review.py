"""Candidate-bound early Review decisions, separate from document Review."""
import json
from pathlib import Path
import hashlib
import sqlite3
from loop_manager_internal.storage import pre_review
from loop_manager_internal.storage import verify_receipts
from loop_manager_internal.verification.impact import digest


def runtime_identity():
    root = Path(__file__).resolve().parents[1]
    paths = [root.parent / 'loop_manager.py', *sorted(root.rglob('*.py'))]
    return digest([(str(path.relative_to(root.parent)), hashlib.sha256(path.read_bytes()).hexdigest()) for path in paths])


def verify_settings(ports):
    from loop_manager_internal.verification.coordination import settings
    from loop_manager_internal.config.managed_config import load_managed_config
    context = settings.get()
    path = ports.identity.config_path
    managed = load_managed_config(path) if path.is_file() else None
    return {'timeout': managed.verify_timeout_seconds if managed else None,
            'parallelism': managed.verify_parallelism if managed else None,
            'partial': managed.verify_partial if managed else None,
            'coordinated_cap': context.cap if context else None}


def candidate(ports, context):
    definition = ports.snapshot()._definition_snapshot
    if definition is None or not context.active_sha:
        return None
    head = ports.git(context.worktree, 'rev-parse', 'HEAD').stdout.strip()
    if ports.git(context.worktree, 'status', '--porcelain').stdout.strip():
        return None
    # HEAD/base already pin both trees; explicit entire diff identity also covers
    # mode, rename and path changes. No document sections are excluded.
    diff = ports.git(context.worktree, 'diff', '--binary', context.active_sha, head, '--').stdout
    return digest({'head': head, 'base': context.active_sha, 'diff': diff,
        'definition': definition.serialize(), 'worker': str(ports._row()['worker_result'] or ''),
        'manager_runtime': runtime_identity(),
        'verification_settings': verify_settings(ports),
        'worktree': str(context.worktree.absolute()), 'branch': context.branch,
        'repo': ports.identity.repo, 'plan': context.plan.plan_id,
        'provider': ports.identity.provider})


def active(ports, context, statuses):
    row = pre_review.current(ports.state.connection, ports.identity.repo, context.plan.plan_id)
    return row if row and row['status'] in statuses and row['candidate'] == candidate(ports, context) else None


def review_identity(ports, context, *, invocation_id=None):
    observer = getattr(ports, 'pre_review_identity', None)
    if observer is None:
        return None
    try:
        # A requested invocation must be attested from its actual prepared and
        # executed request, not reconstructed from today's provider settings.
        value = observer(context, invocation_id=invocation_id) if invocation_id else observer(context)
        required = ('instruction', 'provider', 'model', 'effort', 'environment', 'policy')
        if not isinstance(value, dict) or any(not isinstance(value.get(key), str) or not value[key] for key in required):
            return None
        if value['provider'] != ports.identity.provider:
            return None
        return {key: value[key] for key in required}
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error):
        return None


def reusable(ports, context, row):
    identity = review_identity(ports, context)
    if not row or not identity or row['candidate'] != candidate(ports, context):
        return False
    try:
        original = json.loads(row['identity'] or 'null')
        if (not isinstance(original, dict) or not isinstance(original.get('invocation_id'), str)
                or not original['invocation_id']):
            return False
        actual = review_identity(ports, context, invocation_id=original['invocation_id'])
        return (row['status'] in {'review_pass', 'finalized_reuse'}
                and {key: value for key, value in original.items() if key != 'invocation_id'} == identity == actual)
    except (ValueError, TypeError):
        return False


def full_evidence(ports, context, preliminary):
    """Require the actual full run performed after this preliminary approval."""
    observer = getattr(ports, 'observe_verify_inputs', None)
    try:
        full = json.loads(preliminary['partial'])['full_verify']
        if full.get('passed') is not True or full.get('executed') is not True or observer is None:
            return False
        row = ports._row()
        head = ports.git(context.worktree, 'rev-parse', 'HEAD').stdout.strip()
        evidence = verify_receipts.candidate_evidence(ports.state.connection, ports.identity.repo,
            context.plan.plan_id, head, str(row['verify_result']), observer(context.worktree))
        return bool(evidence and evidence['kind'] == 'run' and evidence['receipt']['id'] == full['receipt_id'])
    except (KeyError, ValueError, TypeError, OSError, RuntimeError):
        return False
