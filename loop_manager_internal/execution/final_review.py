"""Final approval provenance, independent of revision-specific stage tokens."""
from dataclasses import asdict
import json
import sqlite3

from loop_manager_internal.agents import common_guidance
from loop_manager_internal.agents.scope_observation import RAW_DIFF_OPTIONS
from loop_manager_internal.execution import preliminary_review
from loop_manager_internal.git.managed_rebase_observation import parse_raw_changes
from loop_manager_internal.storage import planning_review_state, pre_review, runtime_state, verify_receipts
from loop_manager_internal.storage.execution_checkpoint_state import CheckpointRace


def current(ports, context):
    if ports.checkpoint is None:
        return None
    value = (ports.checkpoint.get(ports.identity.repo, context.plan.plan_id).snapshot or {}).get('final_review')
    if value is not None and not isinstance(value, dict):
        return {'status': 'invalidated', 'invalid_receipt': value}
    return value


def save(ports, context, value):
    """Use the existing checkpoint transaction/generation boundary."""
    if ports.checkpoint is None:
        return
    item = ports.checkpoint.get(ports.identity.repo, context.plan.plan_id)
    ports.checkpoint.record(ports.identity.repo, context.plan.plan_id,
        expected_generation=item.generation, operation=item.operation,
        expected_head=item.expected_head, target_base_oid=item.target_base_oid,
        observed_remote_oid=item.observed_remote_oid,
        snapshot={**(item.snapshot or {}), 'final_review': value})


def inputs(ports, context):
    """Observe the actual PR change and review contracts; never read root guidance."""
    definition = ports.snapshot()._definition_snapshot
    row = ports._row()
    if definition is None or not row['validated_base_sha'] or ports.observe_verify_inputs is None:
        return None
    head = ports.git(context.worktree, 'rev-parse', 'HEAD').stdout.strip()
    if not head or ports.git(context.worktree, 'status', '--porcelain').stdout.strip():
        return None
    observation = ports.observe_verify_inputs(context.worktree)
    if not getattr(observation, 'proven', False):
        return None
    evidence = verify_receipts.candidate_evidence(ports.state.connection, ports.identity.repo,
        context.plan.plan_id, head, str(row['verify_result']), observation)
    if not evidence or evidence['kind'] not in {'run', 'reuse'}:
        return None
    if row['verify_token'] != definition.verify_token(row['validated_base_sha'], head, str(row['verify_result'])):
        return None
    original = evidence.get('origin', evidence['receipt'])
    changes = parse_raw_changes(ports.git(context.worktree, 'diff', *RAW_DIFF_OPTIONS,
        row['validated_base_sha'], head, '--').stdout)
    contracts = _contracts(ports, context, head, definition)
    if contracts is None:
        return None
    declared = json.loads(definition.serialize())
    declared.pop('revision_sha')
    value = {'head': head, 'base': row['validated_base_sha'],
        'repo': ports.identity.repo, 'plan_id': context.plan.plan_id,
        'branch': context.branch, 'worktree': str(context.worktree),
        'definition': declared, 'changes': [asdict(change) for change in changes],
        'contracts': json.loads(json.dumps(contracts)), 'guidance': common_guidance.runtime_content_hash(),
        'verify_origin': original['id'], 'cycle': original['cycle'],
        'verify_fingerprint': observation.fingerprint, 'worker_result': row['worker_result'],
        'review_identity': preliminary_review.review_identity(ports, context)}
    if (ports.git(context.worktree, 'rev-parse', 'HEAD').stdout.strip() != head
            or ports.git(context.worktree, 'status', '--porcelain').stdout.strip()):
        return None
    return value


def _contracts(ports, context, head, definition):
    # Product/design and the transitive dependency documents are review inputs,
    # even when the selected plan omits them from its direct sources.
    paths = {'docs/PRODUCT.md', 'docs/ARCHITECTURE.md', *definition.sources}
    required = set(definition.sources)
    pending = list(definition.depends_on)
    seen = set()
    while pending:
        plan_id = pending.pop()
        if plan_id in seen:
            continue
        seen.add(plan_id)
        dependency = ports.state.connection.execute(
            'SELECT path, sources, depends_on, status FROM exec_plans WHERE repo=? AND plan_id=?',
            (ports.identity.repo, plan_id)).fetchone()
        if dependency is None or dependency['status'] != 'MERGED':
            return None
        required.add(dependency['path'])
        required.update(json.loads(dependency['sources']))
        pending.extend(json.loads(dependency['depends_on']))
    paths.update(required)
    from loop_manager_internal.catalog import requirement_sources
    from loop_manager_internal.catalog.manager_sources import source_content
    from loop_manager_internal.git.command_adapter import revision_requirement_documents
    candidates = {path for path in paths if not path.startswith('manager:')}
    for path in tuple(candidates):
        if path.startswith(('docs/requirements/', 'docs/plans/')) and path.count('/') == 2:
            directory, name = path.rsplit('/', 1)
            candidates.add(directory + '/archive/' + name)
    tree = ports.git(context.worktree, 'ls-tree', '-r', '-z', head, '--', *sorted(candidates)).stdout
    entries = {}
    for record in tree.split('\0'):
        if record:
            metadata, path = record.split('\t')
            mode, kind, oid = metadata.split()
            if kind != 'blob' or mode not in {'100644', '100755'}:
                return None
            entries[path] = (mode, kind, oid)
    contracts = []
    documents = None
    for path in sorted(paths):
        if path.startswith('manager:'):
            contracts.append((path, source_content(path)))
            continue
        resolved = path
        if path not in entries and path.startswith(('docs/requirements/', 'docs/plans/')) and path.count('/') == 2:
            directory, name = path.rsplit('/', 1)
            resolved = directory + '/archive/' + name
            if resolved not in entries and path.startswith('docs/requirements/'):
                if documents is None:
                    documents = revision_requirement_documents(
                        lambda root, *args: ports.git(root, *args).stdout.encode('utf-8'), context.worktree, head)
                result = requirement_sources.resolve_requirement_source(path, documents)
                if result.status is not requirement_sources.ResolutionStatus.RESOLVED:
                    return None
                resolved = result.path
                # Include the resolved entry's mode/type as well as its contents.
                metadata = ports.git(context.worktree, 'ls-tree', '-z', head, '--', resolved).stdout
                fields, actual_path = metadata.rstrip('\0').split('\t')
                entries[actual_path] = tuple(fields.split())
        if resolved not in entries and path in required:
            return None
        contracts.append((path, resolved, entries.get(resolved)))
    return contracts


def observe(ports, context):
    if ports.checkpoint is None:
        return None
    try:
        return inputs(ports, context)
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error):
        return None


def approval(ports, context, before, invocation_id, review):
    """Seal only a successful final gate and its actual invocation identity."""
    if before is None:
        return {**(current(ports, context) or {}), 'status': 'unproven'}
    after = observe(ports, context)
    identity = after['review_identity'] if after is not None else None
    actual = (preliminary_review.review_identity(ports, context, invocation_id=invocation_id)
              if isinstance(invocation_id, str) and invocation_id else None)
    if before != after:
        return {**(current(ports, context) or {}), 'status': 'invalidated'}
    if identity is None or actual is None:
        return {**(current(ports, context) or {}), 'status': 'unproven'}
    if identity != actual:
        return {**(current(ports, context) or {}), 'status': 'invalidated'}
    return {'version': 1, 'status': 'pass', 'approval': {'inputs': before,
        'identity': actual, 'invocation_id': invocation_id, 'review': review}, 'binding': after}


def invalidate(ports, context, status):
    saved = current(ports, context) or {}
    save(ports, context, {**saved, 'version': 1, 'status': status})


def reusable(ports, context):
    """Return original approval plus the current binding, or choose fresh Review."""
    saved = current(ports, context)
    if not isinstance(saved, dict) or saved.get('status') != 'pass' or saved.get('version') != 1:
        return None
    try:
        approval = saved['approval']
        if (not isinstance(approval, dict) or not isinstance(approval.get('inputs'), dict)
                or not isinstance(approval.get('invocation_id'), str) or not approval['invocation_id']):
            return None
        before = approval['inputs']
        after = observe(ports, context)
        if after is None:
            return None
        if {k: v for k, v in before.items() if k not in {'head', 'base'}} != {
                k: v for k, v in after.items() if k not in {'head', 'base'}}:
            return None
        identity = after['review_identity']
        if (identity is None or identity != approval['identity']
                or identity != preliminary_review.review_identity(ports, context,
                    invocation_id=approval['invocation_id'])):
            return None
        if before['base'] != after['base']:
            checkpoint = ports.checkpoint.get(ports.identity.repo, context.plan.plan_id)
            origins = (checkpoint.snapshot or {}).get('stage_result_origins', {})
            if not isinstance(origins, dict):
                return None
            matched = False
            from loop_manager_internal.execution.execution_revision import DefinitionSnapshot
            for item in origins.values():
                if not isinstance(item, dict) or not isinstance(item.get('evidence'), dict):
                    return None
                origin = item['evidence']
                receipt = origin.get('final_review')
                if not isinstance(receipt, dict):
                    continue
                binding = receipt.get('binding')
                verify = origin.get('verify_receipt')
                selection = origin.get('verify_selection')
                if not all(isinstance(value, dict) for value in (binding, verify, selection)):
                    continue
                previous = DefinitionSnapshot.deserialize(origin.get('definition_snapshot'))
                if (previous is not None and receipt.get('status') == 'pass' and receipt.get('approval') == approval
                        and origin.get('phase') == 'PUBLISHING'
                        and origin.get('observed_head') == binding.get('head')
                        and origin.get('validated_base_sha') == binding.get('base')
                        and origin.get('changes') == binding.get('changes')
                        and verify.get('id') == before['verify_origin']
                        and selection.get('cycle') == before['cycle']
                        and origin.get('review_result') == approval['review']
                        and origin.get('repo') == after['repo'] and origin.get('plan_id') == after['plan_id']
                        and origin.get('review_token') == previous.review_token(
                            origin.get('validated_base_sha'), origin.get('observed_head'))):
                    matched = True
                    break
            if not matched:
                return None
            # General planning covers shared conditions beyond direct sources.
            # Never substitute Worker patch normalization or a PR notification.
            general = planning_review_state.get(ports.state.connection, ports.identity.repo, 'general')
            if (planning_review_state.general_pending(ports.state.connection, ports.identity.repo)
                    or general is None or general['purpose'] not in {'full', 'delta', 'final'}
                    or not general['confirmed_revision'] or not general['result'] or not general['outcome']):
                return None
            for descendant in (after['base'], before['head']):
                if ports.git(context.worktree, 'merge-base', '--is-ancestor', before['base'],
                             descendant, check=False).returncode != 0:
                    return None
            if general['confirmed_revision'] != after['base']:
                from loop_manager_internal.catalog.execplan_catalog import classify_archive_only_tree_change
                if ports.git(context.worktree, 'merge-base', '--is-ancestor', general['confirmed_revision'],
                             after['base'], check=False).returncode != 0:
                    return None
                delta = parse_raw_changes(ports.git(context.worktree, 'diff', *RAW_DIFF_OPTIONS,
                    general['confirmed_revision'], after['base'], '--').stdout)
                old = {change.path: (change.old_mode, change.old_oid) for change in delta if change.old_mode != '000000'}
                new = {change.path: (change.new_mode, change.new_oid) for change in delta if change.new_mode != '000000'}
                if delta and not classify_archive_only_tree_change(old, new).archive_only:
                    return None
        return {'version': 1, 'status': 'pass', 'approval': approval, 'binding': after}
    except (KeyError, TypeError, ValueError, OSError, RuntimeError, sqlite3.Error):
        return None


def publication_valid(ports, context):
    saved = current(ports, context)
    if saved is None or isinstance(saved, dict) and saved.get('status') == 'unproven':
        return True  # Legacy/unobservable actual Review keeps the existing token gate.
    if not isinstance(saved, dict) or saved.get('status') != 'pass':
        return False
    return reusable(ports, context) == saved


def reconnect(ports, context):
    """Rebind without manufacturing a Reviewer invocation or changing its origin."""
    row = dict(ports._row())
    checkpoint = ports.checkpoint.get(ports.identity.repo, context.plan.plan_id) if ports.checkpoint else None
    proof = reusable(ports, context)
    if proof is None:
        return False
    selection = verify_receipts.selected(ports.state.connection, ports.identity.repo, context.plan.plan_id)
    general_row = planning_review_state.get(ports.state.connection, ports.identity.repo, 'general')
    general = dict(general_row) if general_row is not None else None
    pending = planning_review_state.general_pending(ports.state.connection, ports.identity.repo)
    if reusable(ports, context) != proof:
        raise CheckpointRace('final Review inputs changed during reconnection')
    with runtime_state._transaction(ports.state.connection):
        ports.state.check_context(context)
        now = planning_review_state.get(ports.state.connection, ports.identity.repo, 'general')
        if (dict(ports._row()) != row
                or ports.checkpoint.get(ports.identity.repo, context.plan.plan_id) != checkpoint
                or verify_receipts.selected(ports.state.connection, ports.identity.repo, context.plan.plan_id) != selection
                or (dict(now) if now is not None else None) != general
                or planning_review_state.general_pending(ports.state.connection, ports.identity.repo) != pending):
            raise CheckpointRace('final Review approval changed during reconnection')
        save(ports, context, proof)
        preliminary = pre_review.current(ports.state.connection, ports.identity.repo, context.plan.plan_id)
        if preliminary and preliminary['status'] == 'finalized_reuse':
            pre_review.update(ports.state.connection, preliminary, 'finalized_reconnected')
        definition = ports.snapshot()._definition_snapshot
        ports.state.update_project(ports.identity.repo, preserve_transaction=True,
            phase='PUBLISHING', last_error=None, review_result=proof['approval']['review'],
            review_token=definition.review_token(proof['binding']['base'], proof['binding']['head']),
            publication_token=None)
    return True
