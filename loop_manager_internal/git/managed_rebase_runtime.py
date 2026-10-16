"""Adapters from existing runtime policies to current rebase adoption."""
from __future__ import annotations
from loop_manager_internal.storage.manual_work_state import manual_workspace_owner
from dataclasses import dataclass
from collections.abc import Callable
from loop_manager_internal.runtime.contracts import WORKSPACE_OWNED
import os
import hashlib
import stat

from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from loop_manager_internal.execution.execution_revision import DefinitionSnapshot
from loop_manager_internal.catalog.execplan_catalog import parse_execplan_text
from loop_manager_internal.application.contracts import ExecPlanMetadataError
from loop_manager_internal.git.managed_rebase_adoption import ManagedRebaseService
from loop_manager_internal.git.managed_rebase_observation import ObservationAdapters
from loop_manager_internal.git.managed_rebase_validation import RebaseExpectation, Remote, PullRequest, fingerprint, valid_acceptance
from loop_manager_internal.git.manager_git_journal import MutationEvidenceError
from loop_manager_internal.recovery.work_branch_integration import user_changes, matching_user_paths
from loop_manager_internal.agents.agent_scope_decision import _worker_path_allowed
from dataclasses import replace
from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
from loop_manager_internal.execution.execution_checkpoint import controlling_git_operation


@dataclass(frozen=True)
class RebaseRuntimeEffects:
    """Explicit effects for this service; persisted decisions remain in repositories."""
    _owned_rebase_metadata: Callable[..., object]
    _normalize_legacy_ignored_snapshot: Callable[..., object]
    _path_signature: Callable[..., object]
    _published_plan_allows_common_plans: Callable[..., object]
    _revision_execplan_input_fingerprint: Callable[..., object]
    _worker_path_allowed: Callable[..., object]
    _worktree_execplan_input_fingerprint: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    git: Callable[..., object]
    live_remote_branch_oid: Callable[..., object]
    pr_for_branch: Callable[..., object]
    requirement_archive_candidates: Callable[..., object]
    requirement_archive_move_set: Callable[..., object]
    requirement_archive_path: Callable[..., object]
    validate_active_execplan_metadata: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]
    validate_managed_worktree: Callable[..., object]


_UNSET = object()


def build_service(ports: RebaseRuntimeEffects, connection, repo_path, state_dir, repo, plan_id,
                  worktree, branch, *, target=None, remote_oid=_UNSET, normalizing_frontmatter=False):
    """Check checkpoint-bound declarations and exact live leases before revision adoption."""
    plan = connection.execute('SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?',
                              (repo, plan_id)).fetchone()
    if plan is None:
        raise MutationEvidenceError('current rebase plan is missing')
    checkpoint = ExecutionCheckpointRepository(connection).get(repo, plan_id)
    saved = checkpoint.snapshot or {}
    proof = saved.get('current_rebase_acceptance') or saved.get('completed_rebase') or saved.get('blocked_resume') or {}
    target = target or checkpoint.target_base_oid or proof.get('target_base_oid')
    if not target:
        raise MutationEvidenceError('current rebase target is missing')
    expected_oid = (saved.get('rebase_remote_oid', checkpoint.observed_remote_oid)
                    if checkpoint.operation == 'rebase' else
                    plan['publication_remote_oid'] or checkpoint.observed_remote_oid)
    if checkpoint.operation == 'rebase_publish':
        intent = checkpoint.rebase_publication_intent()
        if intent is None:
            raise MutationEvidenceError('pending publication identity differs')
        live = ports.live_remote_branch_oid(ports.git, repo_path, branch)
        if live not in (intent['remote_before'], intent['head']):
            raise MutationEvidenceError('pending publication remote lease differs')
        expected_oid = live
        if remote_oid is not _UNSET and remote_oid != live:
            raise MutationEvidenceError('pending publication remote observation changed')
    if remote_oid is not _UNSET:
        expected_oid = remote_oid
    pr = (PullRequest(str(plan['pr_url']), repo, branch,
                      str(expected_oid or ''), 'OPEN')
          if plan['pr_url'] else None)
    remote = Remote(repo, branch, expected_oid, pr)
    if (str(plan['branch'] or '') != branch
            or str(plan['worktree'] or '') != str(worktree)
            or int(plan['workspace_owned']) != WORKSPACE_OWNED):
        raise MutationEvidenceError('current rebase reservation differs')
    project = connection.execute('SELECT base_branch FROM projects WHERE repo = ?', (repo,)).fetchone()
    common_dir = ports.git(repo_path, 'rev-parse', '--git-common-dir').stdout.strip()
    identity = str((repo_path / common_dir).resolve())
    applied = DefinitionSnapshot.deserialize(plan['definition_snapshot'])
    # A historical declaration is usable only with this exact saved operation
    # or acceptance target. A fresh candidate has no such metadata exception.
    accepted_target = bool(target == checkpoint.target_base_oid
        and valid_acceptance(proof) and all(proof[key] == value for key, value in (
                 ('repo', repo), ('repository_identity', identity), ('plan_id', plan_id),
                 ('plan_path', str(plan['path'])), ('worktree', str(worktree.resolve())),
                 ('branch', branch), ('target_base_oid', target), ('role', 'worker'))))
    owned_definition = bool(target == checkpoint.target_base_oid
                            and (checkpoint.operation == 'rebase' or accepted_target))

    def current_contract():
        row = connection.execute('SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?',
                                 (repo, plan_id)).fetchone()
        revision = target if owned_definition else str(row['current_revision_sha'] or row['plan_sha'] or '')
        if not revision:
            raise MutationEvidenceError('current rebase input revision is missing')
        archives = ports.requirement_archive_candidates(connection, repo_path, repo, revision, plan_id)
        common = ports._published_plan_allows_common_plans(repo_path, row)
        inputs = fingerprint((ports._revision_execplan_input_fingerprint(repo_path, revision, row),
                              ports._worktree_execplan_input_fingerprint(worktree, row),
                              row['path'], row['sources'], row['depends_on'],
                              row['current_revision_sha'], row['definition_snapshot']))
        policy = fingerprint((row['path'], archives, common))
        return row, revision, archives, common, inputs, policy

    row, revision, archives, common, inputs, policy = current_contract()
    expected = RebaseExpectation(
        repo=repo, repository_identity=identity, plan_id=plan_id,
        plan_path=str(plan['path']), worktree=str(worktree.resolve()), branch=branch,
        target_base_oid=target, generation=checkpoint.generation,
        workspace_owned=int(plan['workspace_owned']) == WORKSPACE_OWNED,
        input_fingerprint=inputs, scope_fingerprint=policy, remote=remote,
        autostash_oid=saved.get('stash_oid') if checkpoint.operation == 'rebase' else None,
        before_head=(checkpoint.expected_head if checkpoint.operation == 'rebase' else
                     proof.get('before_head') or saved.get('before_head')),
    )

    def validate_identity(_expected):
        ports.validate_execplan_workspace_identity(state_dir, repo, manual_workspace_owner(connection, repo, plan_id), branch, worktree)
        ports.validate_managed_worktree(repo_path, worktree, branch, allow_rebase=True)
        current = connection.execute('SELECT branch, worktree, workspace_owned FROM exec_plans '
                                     'WHERE repo = ? AND plan_id = ?', (repo, plan_id)).fetchone()
        if tuple(current) != (branch, str(worktree), WORKSPACE_OWNED):
            raise MutationEvidenceError('current rebase reservation changed')

    def read_remote(_expected):
        oid = ports.live_remote_branch_oid(ports.git, repo_path, branch)
        live = ports.pr_for_branch(repo_path, branch, str(plan['pr_url'] or '') or None)
        if live is not None and live.get('state') != 'OPEN' and not plan['pr_url']:
            live = None
        pull = (PullRequest(str(live['url']), repo, str(live['headRefName']),
                            str(live['headRefOid']), str(live['state'])) if live else None)
        if live and live.get('baseRefName') != project['base_branch']:
            raise MutationEvidenceError('current PR target differs')
        return Remote(repo, branch, oid, pull)

    def read_file(root, path, index_mode):
        signature = ports._path_signature(root, path)
        try:
            mode = (root / path).lstat().st_mode
        except FileNotFoundError:
            return '000000', signature
        if stat.S_ISLNK(mode):
            git_mode = '120000'
        elif stat.S_ISDIR(mode):
            git_mode = '160000'
        elif stat.S_ISREG(mode):
            git_mode = '100755' if mode & stat.S_IXUSR else '100644'
            if os.name == 'nt':
                git_mode = index_mode or '100644'
        else:
            raise MutationEvidenceError('current rebase file type is unsupported')
        return git_mode, signature

    # Evaluate content contracts before observing. The scope predicate is pure;
    # the input and policy fingerprints bind these reads to both observations
    # and the transactional recheck.
    pending_metadata = ports._owned_rebase_metadata(worktree)
    pending_index = ports.git(worktree, 'ls-files', '-u', '-z').stdout
    content_pending = bool(pending_metadata is not None or pending_index)
    if expected.autostash_oid and not content_pending:
        staged = ports.git(worktree, 'diff', '--cached', '--quiet', check=False)
        if staged.returncode not in (0, 1):
            raise MutationEvidenceError('autostash index observation failed')
        content_pending = staged.returncode == 1
    from loop_manager_internal.git.rebase_documents import document_path, adopted_originals, selected_plan_output
    metadata_owned = bool(isinstance(pending_metadata, dict) and checkpoint.operation == 'rebase'
        and pending_metadata.get('orig-head') == expected.before_head
        and pending_metadata.get('head-name') == f'refs/heads/{branch}'
        and pending_metadata.get('onto') == expected.target_base_oid)
    document_conflicts = set()
    if metadata_owned:
        from loop_manager_internal.git.rebase_correction import parse_unmerged
        from loop_manager_internal.catalog.frontmatter import requirement_identity
        try:
            conflict_paths = {path for path, _, _ in parse_unmerged(pending_index)}
        except ValueError:
            conflict_paths = set()
        for path in conflict_paths:
            if not document_path(path):
                continue
            for source in (target, expected.before_head):
                text = ports.git(repo_path, 'show', f'{source}:{path}', check=False)
                if text.returncode:
                    continue
                try:
                    if path.startswith('docs/requirements/'):
                        requirement_identity(text.stdout)
                    else:
                        parse_execplan_text(path, text.stdout, validate_sources=False)
                except (RuntimeError, ValueError):
                    continue
                document_conflicts.add(path)
                break
    # Older checkpoints retain completed pins; later Worker edits use normal scope.
    documents_adopted = checkpoint.operation != 'rebase' and accepted_target
    document_intent = {} if documents_adopted else saved.get('rebase_documents') or {}
    document_blobs = {}
    document_heads = {}
    document_files = set()
    start_document_paths = set()
    starting_before_base = False
    if (expected.before_head and checkpoint.operation in {None, 'rebase'}
            and pending_metadata is None and not pending_index):
        start = ports.capture_git_snapshot(worktree)
        if start.head == expected.before_head and not controlling_git_operation(start.operations):
            ancestor = ports.git(worktree, 'merge-base', '--is-ancestor',
                                 target, start.head, check=False)
            if ancestor.returncode == 1:
                starting_before_base = True
                base = ports.git(worktree, 'merge-base', target, start.head, check=False)
                if base.returncode == 0 and base.stdout.strip():
                    def changed_paths(revision):
                        result = ports.git(repo_path, 'diff', '--no-ext-diff', '--no-textconv',
                                           '--name-only', '--no-renames', '-z',
                                           base.stdout.strip(), revision, '--', check=False)
                        output = result.stdout
                        if result.returncode or (output and not output.endswith('\0')):
                            return set()
                        return {path for path in output.split('\0') if path}

                    start_document_paths = {
                        path for path in changed_paths(target) & changed_paths(start.head)
                        if document_path(path)
                    }
    originals = {} if documents_adopted else adopted_originals(checkpoint)
    for path, desired in originals.items():
        document_blobs[path] = ({('000000', '0' * width) for width in (40, 64)}
                               if desired.get('absent') else {(desired['mode'], desired['oid'])})
    if (document_intent.get('state') in {'prepared', 'staged'}
            and document_intent.get('before_head') == expected.before_head
            and document_intent.get('target') == target):
        for path in document_intent['paths']:
            if not document_path(path):
                raise MutationEvidenceError('document resolution contains foreign paths')
            selected = worktree / path
            if any(parent.is_symlink() for parent in [selected, *selected.parents]
                   if parent != worktree and worktree in parent.parents):
                raise MutationEvidenceError('document resolution symlink path')
            desired = document_intent['originals'][path]['base']
            document_blobs[path] = {('000000', '0' * width) for width in (40, 64)} if desired.get('absent') else {(desired['mode'], desired['oid'])}
            preserved = document_intent['originals'][path].get('head')
            if preserved is not None:
                document_heads[path] = (document_intent['workspace']['head'],
                    {('000000', '0' * width) for width in (40, 64)} if preserved.get('absent')
                    else {(preserved['mode'], preserved['oid'])})
            if desired.get('absent'):
                if not (worktree / path).exists() and not (worktree / path).is_symlink():
                    document_files.add(path)
            elif (worktree / path).is_file() and not (worktree / path).is_symlink():
                hashed = ports.git(worktree, 'hash-object', '--no-filters', '--', path)
                mode, _ = read_file(worktree, path, desired['mode'])
                if (mode, hashed.stdout.strip()) in document_blobs[path]:
                    document_files.add(path)
    pending_plan_output = False
    if expected.plan_path in document_blobs:
        approved = selected_plan_output(checkpoint, worktree, expected.plan_path,
                                        ignore_declaration=normalizing_frontmatter)
        if not approved and metadata_owned:
            approved = selected_plan_output(checkpoint, worktree, expected.plan_path, pending=True)
            pending_plan_output = bool(approved)
        if approved:
            blob = ports.git(worktree, 'hash-object', '--no-filters', '--', expected.plan_path).stdout.strip()
            document_blobs[expected.plan_path].add((approved['mode'], blob))
            document_files.add(expected.plan_path)
    declaration_error = None
    if not content_pending and not normalizing_frontmatter:
        options = {}
        try:
            if owned_definition:
                target_text = ports.git(repo_path, 'show', f"{target}:{row['path']}").stdout
                try:
                    options['rebase_target_metadata'] = parse_execplan_text(
                        str(row['path']), target_text, validate_sources=False)
                except RuntimeError as error:
                    raise ExecPlanMetadataError(str(error)) from error
            ports.validate_active_execplan_metadata(
                repo_path, worktree, str(row['path']), row,
                rebase_definition=applied, **options)
        except ExecPlanMetadataError as error:
            # Deterministic declaration failures cannot improve by polling the
            # same checkout. Preserve the diagnostic and use the input gate.
            declaration_error = RecoveryFailure(RecoveryReason.PLAN_INPUT_INVALID, str(error))
    archive_ok = True
    archive_blobs = {}
    archive_signatures = {}
    if archives and not content_pending:
        archive_base = target
        # Before the rebase starts, this checkout cannot contain requirements
        # added by the target. Match the observation adapter's original-branch
        # diff base; once rebase starts or target is included, check target.
        if pending_metadata is None and not pending_index and expected.before_head:
            head = ports.git(worktree, 'rev-parse', 'HEAD').stdout.strip()
            if head == expected.before_head:
                ancestry = ports.git(worktree, 'merge-base', '--is-ancestor',
                                     target, head, check=False)
                if ancestry.returncode not in (0, 1):
                    raise MutationEvidenceError('archive target ancestry unavailable')
                if ancestry.returncode == 1:
                    archive_base = ports.git(worktree, 'merge-base', target, head).stdout.strip()
                    if not archive_base:
                        raise MutationEvidenceError('archive original branch base unavailable')
        moves = ports.requirement_archive_move_set(repo_path, archive_base, worktree)
        archive_ok = not (set(moves) - set(archives))
        for source in archives:
            entry = ports.git(repo_path, 'ls-tree', '-z', target, '--', source).stdout
            fields = entry.split('	', 1)[0].split()
            if len(fields) != 3 or fields[1] != 'blob':
                raise MutationEvidenceError('archive source identity unavailable')
            archived = ports.requirement_archive_path(source)
            archive_blobs[archived] = (fields[0], fields[2])
            for path in (source, archived):
                archive_signatures[path] = ports._path_signature(worktree, path)
    integration = saved.get('work_branch_integration') or {}
    user_sources = [source for item in [*saved.get('work_branch_integration_history', ()), integration]
                    for source in (item.get('remote'), item.get('local_commit'))]
    adopted = saved.get('remote_adoption_scope') or {}
    user_sources.extend((adopted.get('remote', adopted.get('head')), saved.get('remote_user_input')))
    user_blobs = user_changes(ports.git, worktree, target, user_sources)
    decision_preserved = True
    if not content_pending:
        from loop_manager_internal.catalog.execplan_drafts import validate_agent_change
        try:
            declaration_base = expected.before_head if starting_before_base else target
            original = ports.git(repo_path, 'show', f'{declaration_base}:{expected.plan_path}').stdout
            selected = parse_execplan_text(expected.plan_path,
                (worktree / expected.plan_path).read_text(encoding='utf-8'), validate_sources=False)
            validate_agent_change(selected, original)
        except (RuntimeError, OSError, UnicodeError):
            decision_preserved = False

    def contracts(observed):
        if not decision_preserved:
            return False
        approved = matching_user_paths(observed, user_blobs)
        observed = replace(observed,
            committed=tuple(change for change in observed.committed if change.path not in approved),
            staged=tuple(change for change in observed.staged if change.path not in approved))
        protected = {change.path for change in (*observed.committed, *observed.staged)}
        protected.update(file.path for file in observed.files)
        if any(path.startswith('docs/requirements/') for path in protected):
            if not archive_ok:
                return False
            for change in (*observed.committed, *observed.staged):
                if change.path in archives and change.new_mode != '000000':
                    return False
                if change.path in archive_blobs and (change.new_mode, change.new_oid) != archive_blobs[change.path]:
                    return False
            if any(file.signature != archive_signatures.get(file.path)
                   for file in observed.files if file.path.startswith('docs/requirements/')):
                return False
        return True

    # assess_scope's path callback has no blob argument. Check protected paths
    # in contracts using the complete observation instead of granting a blanket
    # path exemption to a later Agent invocation.
    worker_allowed = lambda path: ports._worker_path_allowed(
        path, expected.plan_path, archives, (), common)
    checked_contracts = contracts
    def scoped_contracts(observed):
        if archives and content_pending:
            # A service built during replay must rebuild final archive inputs.
            raise MutationEvidenceError('current rebase phase changed; refresh archive contracts')
        # A pending Manager rebase may start with the previous Worker branch's
        # stale document versions. Permit only exact Requirement/ExecPlan paths
        # changed on both sides of the merge base, and only while HEAD is still
        # the reserved pre-rebase commit. Rebase resolution must replace them
        # with the pinned target bytes before a later observation can complete.
        starting_documents = (start_document_paths
            if (observed.head == expected.before_head and observed.metadata is None
                and not controlling_git_operation(dict(observed.operations)) and not observed.staged
                and not any(entry.stage for entry in observed.index)) else set())
        if starting_documents:
            if any(file.path in starting_documents for file in observed.files):
                return False
            observed = replace(observed, committed=tuple(
                change for change in observed.committed
                if change.path not in starting_documents))
        # Conflict bytes are preservation input, never a final approval. Only
        # recorded user paths in this exact owned operation may bypass Worker
        # document contracts while their nonzero index stages remain. The
        # outer policy still checks identity, lease, modes, stages and ownership.
        metadata = dict(observed.metadata or ())
        if (checkpoint.operation == 'rebase' and expected.before_head
                and metadata.get('head-name') == f'refs/heads/{branch}'
                and metadata.get('orig-head') == expected.before_head
                and metadata.get('onto') == expected.target_base_oid):
            conflicts = {entry.path for entry in observed.index
                         if entry.stage and entry.path in user_blobs}
            observed = replace(observed,
                committed=tuple(change for change in observed.committed if change.path not in conflicts),
                staged=tuple(change for change in observed.staged if change.path not in conflicts),
                files=tuple(file for file in observed.files if file.path not in conflicts),
                index=tuple(entry for entry in observed.index if entry.path not in conflicts))
        # Manager document changes are confined to the saved immutable blobs.
        # Pending conflict bytes are preservation inputs under this exact owned
        # metadata; they never grant a later Agent document-writing authority.
        owned_metadata = bool(checkpoint.operation == 'rebase'
            and metadata.get('head-name') == f'refs/heads/{branch}'
            and metadata.get('orig-head') == expected.before_head
            and metadata.get('onto') == expected.target_base_oid)
        if pending_plan_output and not owned_metadata:
            raise MutationEvidenceError('current rebase phase changed; refresh selected plan contracts')
        conflicts = {entry.path for entry in observed.index
                     if entry.stage and entry.path in document_conflicts} if owned_metadata else set()
        if not metadata:
            # A clean replay can change an adopted document without another
            # conflict. Its final index must still equal every pinned original.
            for path, blobs in document_blobs.items():
                entries = [entry for entry in observed.index if entry.path == path]
                if entries:
                    if len(entries) != 1 or entries[0].stage or (entries[0].mode, entries[0].oid) not in blobs:
                        return False
                elif not any(mode == '000000' for mode, _ in blobs):
                    return False
        approved_documents = matching_user_paths(
            replace(observed,
                committed=tuple(change for change in observed.committed
                    if not (owned_metadata and change.path in document_heads
                            and observed.head == document_heads[change.path][0]
                            and (change.new_mode, change.new_oid) in document_heads[change.path][1])),
                files=tuple(file for file in observed.files
                                          if file.path not in document_files)), document_blobs)
        manager_paths = conflicts | approved_documents
        if any(change.path in document_blobs and change.path not in manager_paths
               for change in (*observed.committed, *observed.staged, *observed.files)):
            return False
        observed = replace(observed,
            committed=tuple(change for change in observed.committed if change.path not in manager_paths),
            staged=tuple(change for change in observed.staged if change.path not in manager_paths),
            files=tuple(file for file in observed.files if file.path not in manager_paths),
            index=tuple(entry for entry in observed.index if entry.path not in manager_paths))
        approved = matching_user_paths(observed, user_blobs)
        paths = {change.path for change in (*observed.committed, *observed.staged)}
        paths.update(file.path for file in observed.files)
        paths.update(entry.path for entry in observed.index if entry.stage)
        return all(_worker_path_allowed(path, expected.plan_path, archives, (), common)
                   or path in approved for path in paths) and checked_contracts(observed)

    service = ManagedRebaseService(expected, ObservationAdapters(
        git=ports.git, capture_snapshot=ports.capture_git_snapshot,
        validate_identity=validate_identity, read_rebase_metadata=ports._owned_rebase_metadata,
        read_remote=read_remote, read_inputs=lambda _: current_contract()[-2:],
        read_file=read_file,
        path_allowed=lambda path: worker_allowed(path) or path in user_blobs
            or path in start_document_paths
            or path in document_blobs or path in document_conflicts,
        contracts_allowed=scoped_contracts,
        normalize_ignored_snapshot=ports._normalize_legacy_ignored_snapshot,
    ), declaration_error=declaration_error)
    if normalizing_frontmatter:
        return service
    from loop_manager_internal.git.rebase_documents import restore_rebase_frontmatter
    return replace(service, normalizer=lambda: restore_rebase_frontmatter(
        connection, repo, plan_id, worktree, branch, git=ports.git,
        capture_snapshot=ports.capture_git_snapshot, metadata=ports._owned_rebase_metadata,
        service=lambda: build_service(ports, connection, repo_path, state_dir, repo,
            plan_id, worktree, branch, target=target, remote_oid=remote_oid,
            normalizing_frontmatter=True)))
