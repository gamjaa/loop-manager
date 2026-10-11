"""Observe, publish and recover deterministic Requirement edits without an Agent."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import tempfile

from loop_manager_internal.catalog import execplan_catalog, requirement_corrections, requirement_sources
from loop_manager_internal.planning.requirement_retirement import _safe_parent, _sources
from loop_manager_internal.runtime.resource_lock import resource_locks
from loop_manager_internal.storage import work_claims
from loop_manager_internal.storage.runtime_state import _transaction
from loop_manager_internal.storage.manual_work_state import live_requirement_paths
from loop_manager_internal.storage.requirement_correction_state import RequirementCorrectionRepository


class CorrectionPending(RuntimeError):
    """Preserve the candidate and stop this step; never fall through to Planner."""


def _requirement(path):
    parts = PurePosixPath(path).parts
    return (len(parts) in (3, 4) and parts[:2] == ('docs', 'requirements')
            and (len(parts) == 3 or parts[2] == 'archive') and path.endswith('.md'))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class RequirementCorrectionPublisher:
    """Use the existing detached-worktree, fetch journal and fast-forward adapter."""

    def __init__(self, connection, git, *, refresh, safe):
        self.connection = connection
        self.git = git
        self.refresh = refresh
        self.safe = safe
        self.base_branch = None
        self.repository = RequirementCorrectionRepository(connection)

    def _read(self, revision, manifest, path):
        mode, kind, oid = manifest[path]
        if mode not in {'100644', '100755'} or kind != 'blob':
            raise CorrectionPending(f'교정 입력이 일반 파일이 아닙니다: {path}')
        return self.git.effects.git_bytes(self.git.repo_path, 'cat-file', 'blob', oid)

    def observe(self, base):
        if not self.safe() or not self.refresh(base) or not self.safe():
            raise CorrectionPending('Requirement 교정의 안전한 처리 경계를 확인할 수 없습니다.')
        from loop_manager_internal.git import manager_git_journal
        if manager_git_journal.has_pending_project_mutation(self.git.state_dir, self.git.repo):
            raise CorrectionPending('미확정 Manager Git 쓰기를 보존하고 Requirement 교정을 보류합니다.')
        if self.connection.execute("SELECT 1 FROM sqlite_master WHERE name='agent_invocations'").fetchone():
            if self.connection.execute(
                "SELECT 1 FROM agent_invocations WHERE repo=? AND (finished_at IS NULL OR "
                "result_code IS NULL AND result_kind IN ('interrupted','exception','timeout')) LIMIT 1",
                (self.git.repo,),
            ).fetchone():
                raise CorrectionPending('종료가 확인되지 않은 Agent writer가 있어 교정을 보류합니다.')
        tree = self.git._manifest(base)
        documents = {path: self._read(base, tree, path) for path in tree if _requirement(path)}
        references = set()
        for path in tree:
            if path.startswith('docs/plans/') and path.endswith('.md'):
                plan = execplan_catalog.parse_execplan_text(
                    path, self._read(base, tree, path).decode('utf-8'), validate_sources=False,
                )
                references.update(plan['sources'])
        # Stored-only relationships, drafts and pending publication are not absence.
        for table in ('exec_plans', 'execplan_cancellations'):
            for row in self.connection.execute(f'SELECT plan_id,sources FROM {table} WHERE repo=?', (self.git.repo,)):
                references.update(_sources(row['sources'], row['plan_id']))
        if self.connection.execute("SELECT 1 FROM sqlite_master WHERE name='execplan_drafts'").fetchone():
            for row in self.connection.execute('SELECT evidence FROM execplan_drafts WHERE repo=? AND pending=1', (self.git.repo,)):
                evidence = json.loads(row[0])
                references.update(path for path in evidence['basis']['inputs'] if _requirement(path))
        references.update(work_claims.publication_intake_sources(self.connection, self.git.repo))
        manual = live_requirement_paths(self.connection, self.git.repo)
        if manual:
            # A manual source can resolve by ID in an older snapshot. Preserve
            # the whole candidate rather than broadening its edit permission.
            raise CorrectionPending('수동 작업이 Requirement source를 소유하고 있어 교정을 보류합니다.')
        _ids, publication_paths = work_claims.publication_claims(self.connection, self.git.repo)
        if publication_paths:
            raise CorrectionPending('기존 planning publication이 문서를 소유하고 있습니다.')
        draft_paths = {str(row[0]) for row in self.connection.execute(
            "SELECT target_path FROM document_drafts WHERE repo=? AND state IN ('pending','approved')",
            (self.git.repo,),
        )}
        if draft_paths & documents.keys():
            raise CorrectionPending('Requirement 문서 draft가 원문을 소유하고 있습니다.')
        history = self.git.effects.git(self.git.repo_path, 'rev-list', '--first-parent', '--reverse', base).stdout.splitlines()
        ranks = {oid: index for index, oid in enumerate(history)}
        order = {}
        for path in documents:
            added = self.git.effects.git(
                self.git.repo_path, 'log', '--first-parent', '--reverse', '--diff-filter=A',
                '--format=%H', base, '--', f':(literal){path}',
            ).stdout.splitlines()
            order[path] = ranks.get(added[0]) if added else None
        # ExecPlans (including draft/PR/archive) retain their original bytes.
        editable = {path: self._read(base, tree, path) for path in
                    ('docs/PRODUCT.md', 'docs/ARCHITECTURE.md', 'docs/FUTURE_IDEAS.md')
                    if path in tree and path not in draft_paths}
        plan = requirement_corrections.plan_requirement_corrections(
            documents, referenced_sources=references, references_complete=True,
            first_seen_order=order, editable_documents=editable,
        )
        after = {path: raw.hex() for path, raw in plan.updated_documents}
        before = {path: self._read(base, tree, path).hex() if path in tree else None for path in after}
        for item in plan.corrections:
            before[item.source_path] = item.before.hex()
            if item.source_path != item.target_path:
                after[item.source_path] = None
        return {
            'repo': self.git.repo, 'base_branch': self.base_branch,
            'base': base, 'before': before, 'after': after,
            'modes': {path: tree[path][0] if path in tree else next(
                tree[item.source_path][0] for item in plan.corrections if item.target_path == path
            ) for path in before},
            'references': sorted(references), 'order': order,
            'assignments': [[item.source_path, item.target_path, item.new_id] for item in plan.corrections],
            'deferred': [[item.path, item.reason] for item in plan.deferred],
        }

    def _workspace(self, operation_id):
        project_id = hashlib.sha256(self.git.repo.encode()).hexdigest()[:16]
        workspace = self.git.state_dir / 'worktrees' / project_id / 'requirement-corrections' / operation_id
        self.git.effects._validate_managed_path_boundary(self.git.state_dir, workspace, workspace, 'Requirement 교정 worktree')
        return workspace

    def _validate_tree(self, manifest, revision):
        before_tree = self.git._manifest(manifest['base'])
        after_tree = self.git._manifest(revision)
        changed = {path for path in before_tree.keys() | after_tree.keys() if before_tree.get(path) != after_tree.get(path)}
        if changed != set(manifest['after']):
            raise CorrectionPending('Requirement 교정 diff가 보존된 허용 경로와 다릅니다.')
        for path, contents in manifest['after'].items():
            if contents is None:
                if path in after_tree:
                    raise CorrectionPending(f'교정 rename 원문이 남아 있습니다: {path}')
            elif (path not in after_tree or after_tree[path][:2] != (manifest['modes'][path], 'blob')
                  or self._read(revision, after_tree, path).hex() != contents):
                raise CorrectionPending(f'교정 결과 bytes 또는 mode가 다릅니다: {path}')
        return self.git.effects.git(self.git.repo_path, 'rev-parse', f'{revision}^{{tree}}').stdout.strip()

    def _validate_commit(self, manifest, commit):
        parents = self.git.effects.git(self.git.repo_path, 'rev-list', '--parents', '-n', '1', commit).stdout.split()
        if parents != [commit, manifest['base']]:
            raise CorrectionPending('Requirement 교정 commit이 원격 base의 단일 자식이 아닙니다.')
        return self._validate_tree(manifest, commit)

    def prepare(self, operation_id, manifest):
        workspace = self._workspace(operation_id)
        if workspace.exists() or workspace.is_symlink():
            self.git.effects.validate_managed_worktree(self.git.repo_path, workspace, None)
        else:
            workspace.parent.mkdir(parents=True, exist_ok=True)
            self.git.effects.git(self.git.repo_path, 'worktree', 'add', '--detach', str(workspace), manifest['base'])
            self.git.effects.validate_managed_worktree(self.git.repo_path, workspace, None)
        head = self.git.effects.git(workspace, 'rev-parse', 'HEAD').stdout.strip()
        if head != manifest['base']:
            self._validate_commit(manifest, head)
            self._validate_workspace(workspace, manifest, committed=True)
            return head
        self._validate_workspace(workspace, manifest)
        # Write each result before removing rename sources: every crash leaves
        # only original or saved final bytes, including both sides of a rename.
        for path, value in manifest['after'].items():
            if value is None:
                continue
            _safe_parent(workspace, path, create=True)
            target = workspace / path
            # Keep staging outside the worktree so an interrupted atomic write
            # cannot become an unknown file in the publication diff.
            with tempfile.NamedTemporaryFile(dir=workspace.parent, delete=False) as handle:
                temporary = Path(handle.name)
                try:
                    handle.write(bytes.fromhex(value))
                    handle.flush()
                    os.fsync(handle.fileno())
                    os.chmod(temporary, 0o755 if manifest['modes'][path] == '100755' else 0o644)
                except BaseException:
                    temporary.unlink(missing_ok=True)
                    raise
            try:
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        for path, value in manifest['after'].items():
            if value is None:
                (workspace / path).unlink(missing_ok=True)
        deleted = [path for path, value in manifest['after'].items() if value is None]
        present = [f':(literal){path}' for path, value in manifest['after'].items() if value is not None]
        if deleted:
            self.git.effects.git(workspace, 'update-index', '--force-remove', '--', *deleted)
        if present:
            self.git.effects.git(workspace, 'add', '--', *present)
        staged = self.git.effects.git(workspace, 'write-tree').stdout.strip()
        self._validate_tree(manifest, staged)
        self.git._commit_archive(workspace, 'Correct Requirement identities and confirmed document links')
        head = self.git.effects.git(workspace, 'rev-parse', 'HEAD').stdout.strip()
        self._validate_commit(manifest, head)
        return head

    def _validate_workspace(self, workspace, manifest, *, committed=False):
        changed = self.git.effects.git(workspace, 'diff', '--name-only', '-z', manifest['base'], '--').stdout.split('\0')
        others = self.git.effects.git(workspace, 'ls-files', '--others', '--exclude-standard', '-z').stdout.split('\0')
        if (set(changed + others) - {''}) - manifest['before'].keys():
            raise CorrectionPending('교정 worktree에 허용되지 않은 변경이 있습니다.')
        for path, original in manifest['before'].items():
            _safe_parent(workspace, path)
            target = workspace / path
            if target.exists() or target.is_symlink():
                if not stat.S_ISREG(target.lstat().st_mode):
                    raise CorrectionPending(f'교정 worktree 파일이 일반 파일이 아닙니다: {path}')
                if bool(target.stat().st_mode & 0o111) != (manifest['modes'][path] == '100755'):
                    raise CorrectionPending(f'교정 worktree 파일 mode가 다릅니다: {path}')
                actual = target.read_bytes().hex()
                allowed = {manifest['after'][path]} if committed else {original, manifest['after'][path]}
                if actual not in allowed:
                    raise CorrectionPending(f'교정 작업물이 변조됐습니다: {path}')
            elif (manifest['after'][path] if committed else original) is not None:
                # A removed source is a legitimate partial application.
                if committed or manifest['after'][path] is not None:
                    raise CorrectionPending(f'교정 원문 또는 결과가 제거됐습니다: {path}')
                destination = next(target for source, target, _id in manifest['assignments'] if source == path)
                if not (workspace / destination).is_file() or (workspace / destination).read_bytes().hex() != manifest['after'][destination]:
                    raise CorrectionPending(f'교정 rename 결과 없이 원문이 제거됐습니다: {path}')
        index_paths = self.git.effects.git(workspace, 'diff', '--cached', '--name-only', '-z', manifest['base'], '--').stdout.split('\0')
        if (set(index_paths) - {''}) - manifest['before'].keys():
            raise CorrectionPending('교정 index에 허용되지 않은 변경이 있습니다.')
        index = self.git._manifest(self.git.effects.git(workspace, 'write-tree').stdout.strip())
        for path in set(index_paths) - {''}:
            if path in index and (index[path][0] != manifest['modes'][path] or
                    self._read('', index, path).hex() not in {manifest['before'][path], manifest['after'][path]}):
                raise CorrectionPending(f'교정 index bytes가 변조됐습니다: {path}')
        if committed and self.git.effects.git(workspace, 'status', '--porcelain=v1', '-z').stdout:
            raise CorrectionPending('게시된 교정 worktree가 변경됐습니다.')

    def dispatch(self, base_branch):
        self.base_branch = base_branch
        if not self.safe():
            return None
        repo = self.git.repo
        saved = self.repository.pending(repo)
        try:
            # An occupied boundary is normal deferral, not a publication
            # failure. Preserve pending candidates while independent manual
            # work continues; observe() still checks ownership during a write.
            if live_requirement_paths(self.connection, repo):
                return None
            base = self.git.fetch_remote_base(base_branch)
            for confirmed in self.repository.confirmed(repo):
                if self.git.effects.git(self.git.repo_path, 'merge-base', '--is-ancestor', confirmed['commit_oid'], base, check=False).returncode:
                    raise CorrectionPending('이미 게시된 Requirement 교정 commit이 원격 이력에서 제거됐습니다.')
                current_tree = self.git._manifest(base)
                confirmed_manifest = json.loads(confirmed['manifest'])
                missing = [path for path, contents in confirmed_manifest['after'].items()
                           if contents is not None and path not in current_tree]
                if missing:
                    documents = {path: self._read(base, current_tree, path)
                                 for path in current_tree if _requirement(path)}
                    for path in missing:
                        if (not _requirement(path) or requirement_sources.resolve_requirement_source(
                                path, documents, allow_archive=True,
                        ).status is not requirement_sources.ResolutionStatus.RESOLVED):
                            raise CorrectionPending('이미 게시된 Requirement 교정 결과가 원격에서 제거됐습니다.')
            if saved is None:
                manifest = self.observe(base)
                if not manifest['after']:
                    return None
                operation_id = _digest(manifest)
            else:
                operation_id = saved['operation_id']
                manifest = json.loads(saved['manifest'])
                if _digest(manifest) != operation_id:
                    raise CorrectionPending('보존된 Requirement 교정 manifest가 변조됐습니다.')
            resources = ['requirement:' + path for path in manifest['before']]
            with resource_locks(self.git.state_dir, repo, resources, owner='requirement-corrections'):
                if not self.safe():
                    raise CorrectionPending('Requirement 교정 게시 소유권 gate가 닫혔습니다.')
                if saved is None:
                    self.repository.record(repo, operation_id, manifest, current=lambda: self.observe(base) == manifest)
                    saved = self.repository.pending(repo)
                workspace = self._workspace(operation_id)
                commit = saved['commit_oid']
                if not commit and workspace.exists():
                    head = self.git.effects.git(workspace, 'rev-parse', 'HEAD').stdout.strip()
                    if head != manifest['base']:
                        commit = head
                if commit:
                    final_hash = self._validate_commit(manifest, commit)
                    self.git.effects.validate_managed_worktree(self.git.repo_path, workspace, None)
                    self._validate_workspace(workspace, manifest, committed=True)
                    self.repository.update(repo, operation_id, commit_oid=commit)
                    ancestor = self.git.effects.git(self.git.repo_path, 'merge-base', '--is-ancestor', commit, base, check=False)
                    if ancestor.returncode == 0:
                        # A later remote commit must not have removed/changed the result.
                        tree = self.git._manifest(base)
                        for path, contents in manifest['after'].items():
                            if ((contents is None and path in tree) or (contents is not None and
                                    (path not in tree or self._read(base, tree, path).hex() != contents))):
                                raise CorrectionPending('이미 게시된 교정 결과가 원격에서 변경·제거됐습니다.')
                        self.repository.update(repo, operation_id, stage='confirmed', commit_oid=commit)
                        return commit
                    if ancestor.returncode != 1:
                        raise CorrectionPending('Requirement 교정의 원격 게시 여부를 조회할 수 없습니다.')
                current = self.observe(base)
                if current != manifest:
                    # Preserve the old intent and worktree, and recompute on the
                    # next step from the fresh remote snapshot and relationships.
                    self.repository.update(repo, operation_id, stage='superseded', reason='원격 base·참조·목적지·소유권 변경')
                    raise CorrectionPending('Requirement 교정 입력이 변경돼 이전 후보를 보존했습니다. 최신 입력으로 다시 계산합니다.')
                if not commit:
                    commit = self.prepare(operation_id, manifest)
                    final_hash = self._validate_commit(manifest, commit)
                    self.repository.update(repo, operation_id, commit_oid=commit)
                # The intent was committed before editing. Hold the SQLite
                # writer through the final relationship check and remote/DB
                # confirmation so manual intake cannot change this snapshot.
                with _transaction(self.connection):
                    self.repository.update(repo, operation_id, commit_oid=commit)
                    latest = self.git.fetch_remote_base(base_branch)
                    if latest != base or self.observe(latest) != manifest or not self.safe():
                        raise CorrectionPending('Requirement 교정 게시 직전 원격 또는 참조가 변경됐습니다.')
                    from loop_manager_internal.catalog.execplan_archive import PreparedArchive
                    result = self.git.push_fast_forward(PreparedArchive(operation_id, base, final_hash, commit), base_branch)
                    if not result.success or result.remote_oid != commit:
                        raise CorrectionPending(result.error or 'Requirement 교정 push 결과를 확인할 수 없습니다.')
                    self.repository.update(repo, operation_id, stage='confirmed', commit_oid=commit)
                return commit
        except (RuntimeError, OSError, ValueError, sqlite3.Error, KeyError) as error:
            try:
                if saved and self.repository.pending(repo):
                    self.repository.update(repo, saved['operation_id'], reason=str(error))
            except (RuntimeError, sqlite3.Error) as persistence_error:
                raise CorrectionPending(f'{error}; 교정 실패 진단 저장 실패: {persistence_error}') from error
            raise CorrectionPending(str(error)) from error
