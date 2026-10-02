"""Adapters from existing runtime policies to current rebase adoption."""
import os
import hashlib
import stat

from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from loop_manager_internal.execution.execution_revision import DefinitionSnapshot
from loop_manager_internal.git.managed_rebase_adoption import ManagedRebaseService
from loop_manager_internal.git.managed_rebase_observation import ObservationAdapters
from loop_manager_internal.git.managed_rebase_validation import RebaseExpectation, Remote, PullRequest, fingerprint
from loop_manager_internal.git.manager_git_journal import MutationEvidenceError

_UNSET = object()


def build_service(ports, connection, repo_path, state_dir, repo, plan_id,
                  worktree, branch, *, target=None, remote_oid=_UNSET):
    """Use current declarations and exact live leases, never execution receipts."""
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
        live = ports.manager_git_journal.live_remote_branch_oid(ports.git, repo_path, branch)
        if live not in (intent['remote_before'], intent['head']):
            raise MutationEvidenceError('pending publication remote lease differs')
        expected_oid = live
        if remote_oid is not _UNSET and remote_oid != live:
            raise MutationEvidenceError('pending publication remote observation changed')
    if remote_oid is not _UNSET:
        expected_oid = remote_oid
    pr = (PullRequest(str(plan['pr_url']), repo, branch,
                      str(plan['pr_head_oid'] or plan['pr_conflict_head_oid'] or ''), 'OPEN')
          if plan['pr_url'] else None)
    remote = Remote(repo, branch, expected_oid, pr)
    if (str(plan['branch'] or '') != branch
            or str(plan['worktree'] or '') != str(worktree)
            or int(plan['workspace_owned']) != ports.WORKSPACE_OWNED):
        raise MutationEvidenceError('current rebase reservation differs')
    project = connection.execute('SELECT base_branch FROM projects WHERE repo = ?', (repo,)).fetchone()

    def current_contract():
        row = connection.execute('SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?',
                                 (repo, plan_id)).fetchone()
        revision = str(row['current_revision_sha'] or row['plan_sha'] or '')
        if not revision:
            raise MutationEvidenceError('current rebase input revision is missing')
        archives = ports.requirement_archive_candidates(connection, repo_path, repo, revision, plan_id)
        guidance = tuple(item[0] for item in connection.execute(
            'SELECT DISTINCT target_path FROM guidance_approvals WHERE repo = ? AND plan_id = ? '
            "AND decision != 'pending' ORDER BY target_path", (repo, plan_id)))
        common = ports._published_plan_allows_common_plans(repo_path, row)
        approvals = tuple(connection.execute(
            'SELECT * FROM guidance_approvals WHERE repo = ? AND plan_id = ? ORDER BY rowid',
            (repo, plan_id)))
        approval_token = [(tuple(item[key] for key in item.keys() if key != 'baseline_content'),
                           hashlib.sha256(bytes(item['baseline_content'])).hexdigest())
                          for item in approvals]
        inputs = fingerprint((ports._revision_execplan_input_fingerprint(repo_path, revision, row),
                              ports._worktree_execplan_input_fingerprint(worktree, row),
                              row['path'], row['sources'], row['depends_on']))
        policy = fingerprint((row['path'], archives, guidance, common, approval_token))
        return row, revision, archives, guidance, common, inputs, policy

    row, revision, archives, guidance, common, inputs, policy = current_contract()
    common_dir = ports.git(repo_path, 'rev-parse', '--git-common-dir').stdout.strip()
    identity = str((repo_path / common_dir).resolve())
    expected = RebaseExpectation(
        repo=repo, repository_identity=identity, plan_id=plan_id,
        plan_path=str(plan['path']), worktree=str(worktree.resolve()), branch=branch,
        target_base_oid=target, generation=checkpoint.generation,
        workspace_owned=int(plan['workspace_owned']) == ports.WORKSPACE_OWNED,
        input_fingerprint=inputs, scope_fingerprint=policy, remote=remote,
        autostash_oid=saved.get('stash_oid') if checkpoint.operation == 'rebase' else None,
        before_head=(checkpoint.expected_head if checkpoint.operation == 'rebase' else
                     proof.get('before_head') or saved.get('before_head')),
    )

    def validate_identity(_expected):
        ports.validate_execplan_workspace_identity(state_dir, repo, plan_id, branch, worktree)
        ports.validate_managed_worktree(repo_path, worktree, branch, allow_rebase=True)
        current = connection.execute('SELECT branch, worktree, workspace_owned FROM exec_plans '
                                     'WHERE repo = ? AND plan_id = ?', (repo, plan_id)).fetchone()
        if tuple(current) != (branch, str(worktree), ports.WORKSPACE_OWNED):
            raise MutationEvidenceError('current rebase reservation changed')

    def read_remote(_expected):
        oid = ports.manager_git_journal.live_remote_branch_oid(ports.git, repo_path, branch)
        live = ports.pr_for_branch(repo_path, branch, str(plan['pr_url'] or '') or None)
        if live is not None and live.get('state') != 'OPEN' and not plan['pr_url']:
            live = None
        pull = (PullRequest(str(live['url']), repo, str(live['headRefName']),
                            str(live['headRefOid']), str(live['state'])) if live else None)
        if live and live.get('baseRefName') != project['base_branch']:
            raise MutationEvidenceError('current PR target differs')
        return Remote(repo, branch, oid, pull)

    def read_file(root, path):
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
                tracked = ports.git(root, 'ls-files', '--stage', '--', path).stdout
                git_mode = tracked.split()[0] if tracked else '100644'
        else:
            raise MutationEvidenceError('current rebase file type is unsupported')
        return git_mode, signature

    # Evaluate content contracts before observing. The scope predicate is pure;
    # the input and policy fingerprints bind these reads to both observations
    # and the transactional recheck.
    pending_metadata = ports._owned_rebase_metadata(worktree)
    pending_index = ports.git(worktree, 'ls-files', '-u', '-z').stdout
    if pending_metadata is None and not pending_index:
        ports.validate_active_execplan_metadata(
            repo_path, worktree, str(row['path']), row,
            rebase_definition=DefinitionSnapshot.deserialize(row['definition_snapshot']))
    archive_ok = True
    archive_blobs = {}
    archive_signatures = {}
    if archives:
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
    guidance_ok = not guidance
    guidance_signature = None
    guidance_content = None
    if guidance:
        rows = tuple(connection.execute('SELECT * FROM guidance_approvals WHERE repo = ? '
                                        'AND plan_id = ? ORDER BY rowid', (repo, plan_id)))
        document = ports._guidance_document(worktree, str(row['path']))
        if document is not None and rows:
            proposals, new = ports._guidance_document_matches_rows(document, rows, require_decisions=True)
            expected_files, _ = ports._guidance_final_expected(rows, proposals)
            last = rows[-1]
            actual = ports._guidance_target_state(worktree, 'AGENTS.md')
            guidance_ok = not new and actual == expected_files[(last['batch_id'], 'AGENTS.md')]
            guidance_signature = ports._path_signature(worktree, 'AGENTS.md')
            guidance_content = actual[0]

    def contracts(observed):
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
        if 'AGENTS.md' in protected:
            if not guidance_ok or guidance_content is None:
                return False
            blob = b'blob ' + str(len(guidance_content)).encode() + b'\0' + guidance_content
            for change in (*observed.committed, *observed.staged):
                if change.path == 'AGENTS.md':
                    hash_fn = hashlib.sha256 if len(change.new_oid) == 64 else hashlib.sha1
                    if change.new_oid != hash_fn(blob).hexdigest():
                        return False
            return all(file.signature == guidance_signature
                                      for file in observed.files if file.path == 'AGENTS.md')
        return True

    return ManagedRebaseService(expected, ObservationAdapters(
        git=ports.git, capture_snapshot=ports.capture_git_snapshot,
        validate_identity=validate_identity, read_rebase_metadata=ports._owned_rebase_metadata,
        read_remote=read_remote, read_inputs=lambda _: current_contract()[-2:],
        read_file=read_file,
        path_allowed=lambda path: ports._worker_path_allowed(
            path, expected.plan_path, archives, guidance, common),
        contracts_allowed=contracts,
    ))
