"""Preserve and retire exact withdrawn task resources under the caller's leases."""
import hashlib
import os
from pathlib import Path
import re

from loop_manager_internal.git import manager_git_journal as journal
from loop_manager_internal.git.git_adapter import github_repository
from loop_manager_internal.git.workspace import expected_execplan_workspace, validate_registered_remote
from loop_manager_internal.recovery.checkpoint_adapter import _frozen_push_target
from loop_manager_internal.storage.task_calls import CallDeferred


KINDS = ('deleted_plan_remote_remove', 'deleted_plan_local_remove')
WORKTREE_KINDS = ('managed_worktree_create', 'managed_worktree_remove')


def recoverable(intent, row):
    return (intent.get('owner') == 'plan:' + row['plan_id'] and (
        intent.get('kind') in KINDS and intent.get('target') == 'refs/heads/' + (row['branch'] or '')
        or intent.get('kind') == 'managed_worktree_remove' and intent.get('target') == row['worktree']))


def journal_artifact(path, repo, plan_id):
    """Only a sealed, exclusively owned deletion slot may enter the file queue."""
    if path.is_symlink() or not path.is_dir():
        return False
    intents = list(path.glob('*.intent.json'))
    if not intents or len(list(path.iterdir())) != 2 * len(intents):
        return False
    for source in intents:
        intent = journal._read(source)
        identity = intent['id']
        state = path.parent.parent
        _branch, workspace = expected_execplan_workspace(state, repo, plan_id)
        if (intent.get('version') != 1 or not re.fullmatch(r'[0-9a-f]{32}', identity)
                or source.name != identity + '.intent.json' or source.is_symlink()
                or intent['repo'] != repo or intent['owner'] != 'plan:' + plan_id
                or intent['kind'] not in (*KINDS, *WORKTREE_KINDS)
                or intent['target'] != (str(workspace) if intent['kind'] in WORKTREE_KINDS
                                        else 'refs/heads/loop-manager/' + plan_id)
                or journal._slot(path.parent.parent, repo, intent['kind'], intent['target']) != path):
            return False
        result_path = path / (identity + '.result.json')
        if result_path.is_symlink():
            return False
        result = journal._read(result_path)
        if result['id'] != identity:
            return False
        if result['status'] == 'complete':
            if journal.verified_transition(intent, result['observed'], True) != result['candidates']:
                return False
        elif result['status'] == 'aborted' and intent['kind'] in WORKTREE_KINDS and result['candidates'] == {}:
            journal.verified_transition(intent, intent['expected'], True)
            if result['observed'] != intent['before']:
                journal.verified_transition(dict(intent, before=result['observed']), intent['expected'], True)
        else:
            return False
    return True


def receipt_artifact(path, repo, plan_id):
    if path.is_symlink() or not re.fullmatch(r'[0-9a-f]{32}\.(create|remove)\.json', path.name):
        return False
    saved = journal._read(path)
    identity, action, _suffix = path.name.split('.')
    _branch, target = expected_execplan_workspace(path.parent.parent, repo, plan_id)
    if (set(saved) != {'id', 'repo', 'target', 'before', 'expected'} or saved['id'] != identity
            or saved['repo'] != repo or saved['target'] != str(target)):
        return False
    intent = dict(saved, owner='plan:' + plan_id, kind='managed_worktree_' + action)
    journal.verified_transition(intent, saved['expected'], True)
    return True


def worktree_artifacts(state_dir, repo, row):
    if not row['worktree']:
        return ()
    owned, receipts = [], []
    for kind in WORKTREE_KINDS:
        slot = journal._slot(state_dir, repo, kind, row['worktree'])
        if not slot.exists():
            continue
        if not journal_artifact(slot, repo, row['plan_id']):
            raise CallDeferred('원래 worktree journal의 전용 귀속·확정을 확인할 수 없습니다.')
        owned.append(slot)
        for source in slot.glob('*.intent.json'):
            intent = journal._read(source)
            receipt = state_dir / 'managed-worktree-intents' / (intent['id'] + '.' + kind.removeprefix('managed_worktree_') + '.json')
            if receipt.exists():
                if not receipt_artifact(receipt, repo, row['plan_id']):
                    raise CallDeferred('원래 worktree receipt의 귀속을 확인할 수 없습니다.')
                receipts.append(receipt)
    return (*owned, *receipts)


def cleanup(connection, state_dir, repo_path, repo, row, revision, *, git, pr_for_branch,
            check_owner, assess_cleanup, remove_cleanup_worktree, recover_cleanup_worktree):
    """Return temporary evidence paths only after all three resources are absent.

    Dirty/local-only work remains pending. A full bundle protects published
    commits during partial removal; the existing final-erasure queue retires it.
    """
    branch, raw = row['branch'], row['worktree']
    if not branch:
        if raw:
            registered = git(repo_path, 'worktree', 'list', '--porcelain')
            if (Path(raw).exists() or Path(raw).is_symlink() or registered.returncode
                    or 'worktree ' + raw in registered.stdout.splitlines()):
                raise CallDeferred('branch 소유권이 없는 작업 공간을 보존합니다.')
        return ()
    if branch != 'loop-manager/' + row['plan_id']:
        raise CallDeferred('기본·사용자 branch는 삭제 자원이 아닙니다.')
    state_dir, repo_path = Path(state_dir), Path(repo_path)
    ref = 'refs/heads/' + branch
    slots = tuple(journal._slot(state_dir, repo, kind, ref) for kind in KINDS)
    registrations = git(repo_path, 'worktree', 'list', '--porcelain')
    if registrations.returncode:
        raise CallDeferred('삭제 자원의 worktree 등록을 관측할 수 없습니다.')
    local = git(repo_path, 'rev-parse', '--verify', ref, check=False)
    remote = journal.live_remote_branch_oid(git, repo_path, branch)
    if local.returncode not in (0, 1, 128):
        raise CallDeferred('삭제 자원의 로컬 ref를 관측할 수 없습니다.')
    # Preserve the resource-free legacy path without inventing a new intent.
    if (remote is None and local.returncode and not any(slot.exists() for slot in slots)
            and not (raw and (Path(raw).exists() or Path(raw).is_symlink()))):
        if any(line == 'branch ' + ref or raw and line == 'worktree ' + raw
               for line in registrations.stdout.splitlines()):
            raise CallDeferred('삭제 자원의 남은 worktree 등록을 보존합니다.')
        return worktree_artifacts(state_dir, repo, row)
    expected = row.get('publication_remote_oid')
    _branch, managed = expected_execplan_workspace(state_dir.resolve(), repo, row['plan_id'])
    project = connection.execute('SELECT origin,base_branch FROM projects WHERE repo=?', (repo,)).fetchone()
    if (branch != 'loop-manager/' + row['plan_id'] or row.get('workspace_owned') != 1
            or not raw or Path(raw) != managed or any(parent.is_symlink() for parent in (managed, *managed.parents))
            or project is None or project['base_branch'] == branch
            or not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', expected)):
        raise CallDeferred('삭제 자원의 원래 소유권·게시 OID를 확인할 수 없습니다.')
    removal_slot = journal._slot(state_dir, repo, 'managed_worktree_remove', raw)
    if managed.exists() and removal_slot.exists():
        for source in removal_slot.glob('*.intent.json'):
            intent = journal._read(source)
            receipt = state_dir / 'managed-worktree-intents' / (intent['id'] + '.remove.json')
            result = removal_slot / (intent['id'] + '.result.json')
            if receipt.exists() or result.exists() and journal._read(result)['status'] == 'complete':
                raise CallDeferred('제거 증거 뒤 재생성된 worktree를 보존합니다.')
    def check(*, workspace_state=True):
        check_owner()
        current = connection.execute('SELECT origin,base_branch FROM projects WHERE repo=?', (repo,)).fetchone()
        if current is None or tuple(current) != tuple(project):
            raise CallDeferred('삭제 자원의 등록 저장소가 변경됐습니다.')
        for table in ('exec_plans', 'projects', 'manual_work_groups'):
            columns = {item[1] for item in connection.execute('PRAGMA table_info(' + table + ')')}
            if not {'repo', 'branch', 'worktree'} <= columns:
                continue
            for other in connection.execute('SELECT * FROM ' + table):
                if table == 'exec_plans' and other['repo'] == repo and other['plan_id'] == row['plan_id']:
                    continue
                if (other['worktree'] and (Path(other['worktree']).resolve().is_relative_to(managed)
                        or managed.is_relative_to(Path(other['worktree']).resolve()))
                        or other['repo'] == repo and (other['branch'] == branch
                            or 'source_branch' in columns and other['source_branch'] == branch)):
                    raise CallDeferred('독립·수동 소유자가 사용하는 삭제 자원을 보존합니다.')
        validate_registered_remote(repo_path, managed if managed.exists() else None,
                                   project['origin'], git=git, github_repository=github_repository)
        alias = git(repo_path, 'symbolic-ref', '--quiet', ref, check=False)
        if alias.returncode != 1:
            raise CallDeferred('심볼릭 또는 미확인 로컬 ref를 보존합니다.')
        pr = pr_for_branch(repo_path, branch, history=True,
                          expected_repository=project['origin'], expected_base=project['base_branch'])
        if (pr is None and row['pr_url'] or pr is not None and (
                pr.get('state') != 'CLOSED' or pr.get('mergedAt') or pr.get('headRefName') != branch
                or pr.get('url') != row['pr_url'] or pr.get('headRefOid') != expected
                or pr.get('baseRefName') != project['base_branch'] or pr.get('isCrossRepository') is not False)):
            raise CallDeferred('삭제 자원의 PR identity·head 보존을 확인할 수 없습니다.')
        if workspace_state:
            registered = git(repo_path, 'worktree', 'list', '--porcelain')
            if registered.returncode:
                raise CallDeferred('삭제 자원의 현재 등록을 확인할 수 없습니다.')
            for block in registered.stdout.split('\n\n'):
                lines = block.splitlines()
                if 'branch ' + ref in lines and 'worktree ' + raw not in lines:
                    raise CallDeferred('다른 worktree에서 사용하는 branch를 보존합니다.')
                if 'worktree ' + raw in lines and (not managed.exists() or 'branch ' + ref not in lines):
                    raise CallDeferred('삭제 worktree의 등록·branch가 변경됐습니다.')
                if 'worktree ' + raw in lines and any(
                        line.startswith('locked') and not line.startswith('locked loop-manager-worktree:') for line in lines):
                    raise CallDeferred('수동으로 잠긴 worktree를 보존합니다.')
            if managed.exists():
                if (not any('worktree ' + raw in block.splitlines() and 'branch ' + ref in block.splitlines()
                            for block in registered.stdout.split('\n\n'))
                        or git(managed, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip()
                        != git(repo_path, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip()
                        or assess_cleanup is None):
                    raise CallDeferred('변경되거나 미게시·미확인인 삭제 worktree를 보존합니다.')
                assessment = assess_cleanup(managed, expected)
                if (assessment.status != 'safe' or assessment.observation is None
                        or assessment.observation[1] not in (branch, ref)):
                    raise CallDeferred('변경되거나 미게시·미확인인 삭제 worktree를 보존합니다.')
        urls = git(repo_path, 'remote', 'get-url', '--push', '--all', 'origin').stdout.splitlines()
        if len(urls) != 1 or github_repository(urls[0]) != project['origin']:
            raise CallDeferred('삭제 직전 고정할 push 대상이 등록 origin과 다릅니다.')
        return urls[0]

    check(workspace_state=False)
    if recover_cleanup_worktree is not None:
        recover_cleanup_worktree(state_dir, repo_path, managed)
    origin = check()
    if remote not in (None, expected):
        raise CallDeferred('원격 삭제의 저장된 기대 OID가 변경됐습니다.')
    if local.returncode == 0 and (not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', local.stdout.strip())
            or git(repo_path, 'merge-base', '--is-ancestor', local.stdout.strip(), expected, check=False).returncode):
        raise CallDeferred('미게시 로컬 commit을 보존합니다.')
    evidence = state_dir / 'logs' / hashlib.sha256((repo + '\0' + row['plan_id']).encode()).hexdigest()[:16]
    bundle = evidence / (expected + '.bundle')
    if any(parent.is_symlink() for parent in (bundle, *bundle.parents)):
        raise CallDeferred('삭제 보존 bundle의 경로를 확인할 수 없습니다.')
    if not bundle.exists():
        source = ref if local.returncode == 0 and local.stdout.strip() == expected else 'refs/remotes/origin/' + branch
        if git(repo_path, 'rev-parse', '--verify', source, check=False).stdout.strip() != expected:
            raise CallDeferred('게시 head의 복구 가능한 로컬 원본이 없습니다.')
        evidence.mkdir(parents=True, exist_ok=True)
        journal._sync_directory(evidence.parent)
        temporary = bundle.with_suffix('.tmp')
        git(repo_path, 'bundle', 'create', str(temporary), source)
        with temporary.open('rb') as stream:
            os.fsync(stream.fileno())
        temporary.replace(bundle)
        journal._sync_directory(evidence)
    if (git(repo_path, 'bundle', 'verify', str(bundle), check=False).returncode
            or not any(line.split()[0] == expected for line in git(repo_path, 'bundle', 'list-heads', str(bundle)).stdout.splitlines())):
        raise CallDeferred('삭제 보존 bundle의 내용 검증에 실패했습니다.')
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()

    def remove(kind, oid, observe, apply):
        slot = slots[KINDS.index(kind)]
        current = observe()
        existing = slot.exists() and any(slot.iterdir())
        # An OID cannot identify a ref's lifetime after an interrupted effect.
        # Existing evidence permits confirming absence, never replaying deletion.
        if current is not None and existing:
            raise CallDeferred('중단·완료된 삭제 기록 뒤 살아 있는 ref를 보존합니다.')
        if slot.exists() and list(slot.glob('*.result.json')) and not (slot / 'active.json').exists():
            if not journal_artifact(slot, repo, row['plan_id']) or current is not None:
                raise CallDeferred('정리 뒤 재생성되거나 미확인인 ref를 보존합니다.')
            return
        if current is None and not (slot / 'active.json').exists():
            return
        before = dict(oid=oid, origin=origin, bundle=str(bundle), digest=digest, revision=revision)
        if (slot / 'active.json').exists():
            saved = journal._read(slot / 'active.json')
            if (saved['owner'] != 'plan:' + row['plan_id']
                    or {key: value for key, value in saved['before'].items() if key != 'revision'}
                        != {key: value for key, value in before.items() if key != 'revision'}
                    or git(repo_path, 'merge-base', '--is-ancestor', saved['before']['revision'], revision, check=False).returncode):
                raise CallDeferred('중단된 삭제 intent의 소유·보존·revision이 변경됐습니다.')
            before = saved['before']
        def marker(_intent):
            return bundle.is_file() and hashlib.sha256(bundle.read_bytes()).hexdigest() == digest
        def effect(_token):
            if existing:
                raise CallDeferred('중단된 삭제의 효과가 불확실해 ref 재삭제를 보류합니다.')
            if check() != origin or observe() != oid or not marker(None):
                raise CallDeferred('삭제 직전 소유권·ref·보존본이 변경됐습니다.')
            apply()
        journal.execute(state_dir, owner='plan:' + row['plan_id'], kind=kind, repo=repo, target=ref,
            before=before, expected=dict(before, oid=None),
            observe=lambda: dict(before, oid=observe()), apply=effect, marker=marker)

    options, operand = _frozen_push_target(origin)
    def remote_oid():
        result = git(repo_path, *options, 'ls-remote', '--heads', operand, ref, check=False)
        if result.returncode:
            raise CallDeferred('삭제 원격 ref의 현재 관측에 실패했습니다.')
        return journal.parse_remote_branch_oid(result.stdout, ref)
    def push():
        result = git(repo_path, '-c', 'core.hooksPath=' + os.devnull, *options, 'push',
                     journal.exact_branch_lease(branch, expected), operand, ':' + ref, check=False)
        if result.returncode:
            raise CallDeferred('삭제 lease·권한·보호 정책을 확인할 수 없어 자원을 보존합니다.')
    remove(KINDS[0], expected, remote_oid, push)
    if managed.exists():
        check()
        assessment = assess_cleanup(managed, expected)
        if assessment.status != 'safe' or assessment.observation is None or assessment.observation[1] not in (branch, ref):
            raise CallDeferred('삭제 직전 worktree branch·작업물이 변경됐습니다.')
        if remove_cleanup_worktree is None or remove_cleanup_worktree(repo_path, managed,
                assessment, state_dir=state_dir, owner='plan:' + row['plan_id']).status != 'safe':
            raise CallDeferred('삭제 worktree의 정리를 재시도합니다.')
    def local_oid():
        result = git(repo_path, 'rev-parse', '--verify', ref, check=False)
        if result.returncode not in (0, 1, 128):
            raise CallDeferred('삭제 로컬 ref의 현재 관측에 실패했습니다.')
        return result.stdout.strip() if result.returncode == 0 else None
    # Pin the original local OID too; never replace it with a new observation.
    local_expected = local.stdout.strip() if local.returncode == 0 else expected
    if (slots[1] / 'active.json').exists():
        local_expected = journal._read(slots[1] / 'active.json')['before']['oid']
    remove(KINDS[1], local_expected, local_oid,
           lambda: git(repo_path, 'update-ref', '--no-deref', '-d', ref, local_expected))
    check()
    if managed.exists() or remote_oid() is not None or local_oid() is not None:
        raise CallDeferred('삭제 자원의 최종 부재가 확인되지 않았습니다.')
    return (evidence, *(slot for slot in slots if slot.exists()), *worktree_artifacts(state_dir, repo, row))
