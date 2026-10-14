"""planning diagnostics boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import hashlib
import os
import sqlite3


def planning_diagnostics_path(
    state_dir: Path,
    repo: str,
    fingerprint: str,
) -> Path:
    project_id = hashlib.sha256(repo.encode()).hexdigest()[:16]
    return state_dir / "logs" / project_id / "planning" / f"{fingerprint}.txt"


@dataclass(frozen=True)
class PreservePlanningDiagnosticsEffects:
    """Named effects for preserve_planning_diagnostics; no entry-point/module access."""
    _nul_git_paths: Callable[..., object]
    git: Callable[..., object]
    planning_diagnostics_path: Callable[..., object]


def preserve_planning_diagnostics(
    effects: PreservePlanningDiagnosticsEffects, state_dir: Path,
    repo: str,
    row: sqlite3.Row,
) -> Path:
    fingerprint = row["planning_attempt_fingerprint"]
    if not fingerprint:
        fingerprint = row["planning_error_fingerprint"] or "unknown"
    worktree = Path(row["worktree"]) if row["worktree"] else None
    error = (
        row["planning_error"]
        or row["planning_validation_error"]
        or row["last_error"]
        or ""
    )
    diff = ""
    if worktree and worktree.is_dir():
        try:
            base_revision = row["active_sha"] or "HEAD"
            diff = effects.git(worktree, "diff", "--binary", base_revision).stdout
            for relative_path in effects._nul_git_paths(
                worktree, "ls-files", "--others", "-z"
            ):
                untracked = effects.git(
                    worktree,
                    "diff",
                    "--binary",
                    "--no-index",
                    "--",
                    os.devnull,
                    relative_path,
                    check=False,
                )
                if untracked.returncode not in {0, 1}:
                    raise RuntimeError(
                        f"untracked diff 생성 실패 ({relative_path}): "
                        f"git 종료 코드 {untracked.returncode}"
                    )
                diff += untracked.stdout
        except RuntimeError as diff_error:
            diff = f"diff 보존 실패: {diff_error}"
    # Preserve each adopted base separately; repeated corrections must not
    # overwrite the original failed result or its retry evidence.
    identity = hashlib.sha256(
        f"{fingerprint}\0{row['active_sha'] or 'unknown'}".encode("utf-8")
    ).hexdigest()
    diagnostic = effects.planning_diagnostics_path(state_dir, repo, identity)
    diagnostic.parent.mkdir(parents=True, exist_ok=True)
    diagnostic.write_text(
        "\n".join(
            [
                f"input_fingerprint: {fingerprint}",
                f"planning_worktree: {worktree or ''}",
                f"planner_session_id: {row['planner_session_id'] or ''}",
                f"validation_retries: {row['planning_retry_count']}",
                f"active_sha: {row['active_sha'] or ''}",
                f"planning_error: {row['planning_error'] or ''}",
                f"planning_error_incident_id: {row['planning_error_incident_id'] or ''}",
                f"planning_error_origin: {row['planning_error_origin'] or ''}",
                f"planning_validation_error: {row['planning_validation_error'] or ''}",
                f"planning_result_completed: {int(bool(row['planning_result_completed']))}",
                f"planning_result: {row['planning_result'] or ''}",
                "",
                "last_error:",
                str(error),
                "",
                "diff:",
                diff,
            ]
        ),
        encoding="utf-8",
    )
    return diagnostic


@dataclass(frozen=True)
class RemotePlanningDiagnosticsEffects:
    preservation_effects: Callable
    adapter_factory: Callable
    capture_git_snapshot: Callable
    validate_planning_workspace_identity: Callable
    validate_managed_worktree: Callable
    git: Callable


def preserve_remote_planning_diagnostics(effects: RemotePlanningDiagnosticsEffects, connection, state_dir, repo, expected):
    """Keep failed planning material separate from the current execution owner."""
    from dataclasses import replace
    from loop_manager_internal.agents.scope_contracts import GitSnapshot
    from loop_manager_internal.planning import planning_rebase
    from loop_manager_internal.git import rebase_file_evidence
    from loop_manager_internal.recovery import blocked_checkpoint, diagnostic_preservation
    from loop_manager_internal.storage import runtime_state
    from loop_manager_internal.storage.blocked_checkpoint_state import DiagnosticPublicationRepository
    from loop_manager_internal.notifications import notifications

    raw = runtime_state._preserved_planning_values(expected.planning_preserved_state) if expected.planning_preserved_state else {}
    row = replace(expected, **{name: value for name, value in raw.items() if name in expected.__dataclass_fields__},
                  planning_preserved_state=None) if raw else expected
    if not (row.planning_error or row.planning_validation_error):
        return None
    cause_identity = str(expected.planning_error_incident_id or expected.planning_error_fingerprint or row.planning_attempt_fingerprint or '')
    store = DiagnosticPublicationRepository(connection)
    payload = dict(result='local_only', diagnostic=True, complete=False, cause_identity=cause_identity,
                   original_branch='', worktree=row.worktree,
                   omitted={'all': 'planning ownership or collection unavailable'})
    try:
        project = connection.execute('SELECT base_branch FROM projects WHERE repo = ?', (repo,)).fetchone()
        branch = project[0]
        payload['original_branch'] = branch
        if raw and (row.planning_error_incident_id != expected.planning_error_incident_id
                    or row.planning_error != expected.planning_error):
            raise RuntimeError('planning 보존 자료와 현재 오류 incident가 다릅니다.')
        if not row.worktree or row.planning_workspace_owned != 1 or not row.active_sha:
            raise RuntimeError('planning 진단 보존의 작업 공간·base 소유권을 확인할 수 없습니다.')
        worktree = Path(row.worktree)
        def validate():
            if runtime_state.RuntimeStateRepository(connection).planning(repo) != expected:
                raise RuntimeError('planning 진단 원인 또는 저장 문맥이 변경됐습니다.')
            runtime = runtime_state.RuntimeStateRepository(connection)
            if runtime.active_row(repo) is not None:
                raise RuntimeError('다른 계획이 변경 실행 자리를 사용 중입니다.')
            effects.validate_planning_workspace_identity(state_dir, repo, row.planning_attempt_fingerprint, worktree)
            effects.validate_managed_worktree(Path(repo), worktree, None, allow_rebase=True)
            current = effects.capture_git_snapshot(worktree)
            if current.branch is not None or not current.shared_metadata_complete:
                raise RuntimeError('planning 진단 worktree의 detached 소유권이 유효하지 않습니다.')
            unmerged = any(entry.split()[-1] != '0' for entries in current.index.values() for entry in entries)
            if current.operations or unmerged:
                if set(current.operations) - {'rebase-merge', 'rebase-apply', 'REBASE_HEAD', 'MERGE_MSG', 'MERGE_MODE'}:
                    raise RuntimeError('planning의 다른 Git operation이 진행 중입니다.')
                journal = planning_rebase._read(planning_rebase._journal_path(state_dir, repo, worktree), repo, worktree)
                if journal is None or journal['old'] != row.active_sha:
                    raise RuntimeError('planning 충돌의 관리 rebase 소유권을 확인할 수 없습니다.')
                if planning_rebase.rebase_in_progress(effects.git, worktree):
                    for directory in ('rebase-merge', 'rebase-apply'):
                        location = Path(effects.git(worktree, 'rev-parse', '--git-path', directory).stdout.strip())
                        location = location if location.is_absolute() else worktree / location
                        if location.exists() and ((location / 'orig-head').read_text().strip() != journal['head']
                                                  or (location / 'onto').read_text().strip() != journal['target']):
                            raise RuntimeError('planning 충돌의 원본·target이 다릅니다.')
                elif (not unmerged or journal['phase'] not in {'applying', 'conflict'}
                      or current.head != journal.get('restored_head', journal['target'])):
                    raise RuntimeError('planning의 다른 Git operation이 진행 중입니다.')
            return current
        snapshot = validate()
        revisions = dict(base=row.active_sha, head=snapshot.head)
        journal = planning_rebase._read(planning_rebase._journal_path(state_dir, repo, worktree), repo, worktree)
        if journal:
            revisions.update(start=journal['head'], target=journal['target'])
            stash = planning_rebase._stash(effects.git, worktree, 'loop-manager-planning-rebase:' + journal['nonce'])
            if stash:
                revisions.update(rebase_file_evidence.stash_originals(worktree, stash[1], effects.git))
                if revisions['stash-base'] != journal['head']:
                    raise RuntimeError('planning stash의 시작 commit이 관리 journal과 다릅니다.')
            elif journal['dirty']:
                raise RuntimeError('planning 원형의 관리 stash가 없어 완전 보존할 수 없습니다.')
        stashed_untracked = set()
        if revisions.get('untracked-original'):
            stashed_untracked = set(filter(None, effects.git(worktree, 'ls-tree', '-r',
                '--name-only', '-z', revisions['untracked-original']).stdout.split('\0')))
        approved = GitSnapshot.deserialize(row.agent_result_snapshot).dirty if row.agent_result_snapshot else {}
        def allowed(name):
            if not planning_rebase._correction_path_allowed(name):
                return False
            state = snapshot.dirty.get(name)
            if name in stashed_untracked or state and 'untracked' in state[0]:
                return state is not None and approved.get(name) == state
            return True
        source = diagnostic_preservation.PreservationSource(
            repo, 'planning', 'planning', cause_identity, branch, worktree, revisions,
            dict(phase=row.phase, evidence=diagnostic_preservation.redact_detail(str(
                row.planning_error or row.planning_validation_error)),
                input_fingerprint=row.planning_attempt_fingerprint,
                operations=sorted(snapshot.operations),
                selection_revisions=[oid for label, oid in revisions.items()
                                     if label not in {'base', 'target', 'untracked-original', 'stash-base', 'stash', 'staged-original'}
                                     and not (journal and label == 'head')],
                untracked_revision=revisions.get('untracked-original')),
            allowed, validate)
        request = blocked_checkpoint.CheckpointRequest(repo, 'planning', cause_identity,
            blocked_checkpoint.CheckpointPhase.REQUESTED, 'planning_error', '', branch, str(worktree))
        adapter = effects.adapter_factory(connection, Path(repo), state_dir, repo)
        payload = diagnostic_preservation.preserve(effects.preservation_effects(), connection,
            state_dir, source, adapter, request, 'planning 실패 자료는 원격 base에 직접 게시하지 않습니다.')
    except (OSError, RuntimeError, ValueError) as error:
        payload['reason'] = diagnostic_preservation.redact_detail(str(error))
        identity = hashlib.sha256((cause_identity + str(row.worktree) + str(row.active_sha)).encode()).hexdigest()
        store.preserve(repo, 'planning', 'planning', identity, payload, retryable=True)
    notifications.enqueue_current_project_events(connection, repo)
    return payload
