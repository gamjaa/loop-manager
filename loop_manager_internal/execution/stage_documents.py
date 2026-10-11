"""진행 중 작업의 문서 영향 판단과 PR 전 계획 개정 게시."""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
import hashlib
import json
from pathlib import Path

from loop_manager_internal.agents.agent_scope_decision import (
    _same_local_snapshot, manager_comparison_baseline, reconcile_manager_metadata,
)
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.execution.validation_feedback import redact_secrets
from loop_manager_internal.planning.planning_catalog import (
    MutableReadySnapshot, mutable_snapshot_token, snapshot_to_dict, snapshot_from_dict,
)
from loop_manager_internal.planning.planner_validation import PlannerValidationError
from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
from loop_manager_internal.storage import runtime_state, verify_receipts
from loop_manager_internal.execution.execution_revision import DefinitionSnapshot
from loop_manager_internal.execution.execution_runtime import ExecutionOutcome, ExecutionOutcomeKind
from loop_manager_internal.agents.agent_invocation import TrackedAgentCall, AttemptSequence, AgentResultKind
from loop_manager_internal.catalog import requirement_sources
from loop_manager_internal.git.command_adapter import revision_requirement_documents


def related_paths(plan_path, sources, changed, *, requirement_documents=None):
    targets = {plan_path, "docs/PRODUCT.md", "docs/ARCHITECTURE.md"}
    for source in sources:
        if (requirement_documents is None
            or not source.startswith("docs/requirements/") or source.count("/") != 2):
            targets.add(source)
            continue
        resolution = requirement_sources.resolve_requirement_source(
            source, requirement_documents, allow_archive=True,
        )
        if resolution.status is requirement_sources.ResolutionStatus.AMBIGUOUS:
            raise RuntimeError(
                f"ExecPlan source가 모호합니다: {source} ({', '.join(resolution.candidates)})"
            )
        targets.add(resolution.path or source)
    return tuple(sorted(path for path in changed if path in targets))


def _requirement_document_sets(git_bytes, repository, revisions, sources):
    if git_bytes is None or not any(
        source.startswith("docs/requirements/") and source.count("/") == 2
        for source in sources
    ):
        return ()
    return tuple(
        revision_requirement_documents(git_bytes, repository, revision)
        for revision in dict.fromkeys(revisions) if revision
    )


def _related_paths_across_revisions(plan_path, sources, changed, document_sets):
    if not document_sets:
        return related_paths(plan_path, sources, changed)
    return tuple(sorted({
        path for documents in document_sets
        for path in related_paths(
            plan_path, sources, changed, requirement_documents=documents,
        )
    }))


def documentary(paths):
    return bool(paths) and all(path.startswith("docs/") and path.endswith(".md") for path in paths)


def remember_work_branch(git, state, checkpoints, identity, context, remote, local, base, pull_request,
                         *, git_bytes=None):
    """기존 채택 권한과 별도로 변경 관측 당시의 문서 단계만 보존한다."""
    row = state.row()
    current = checkpoints.get(identity.repo, context.plan.plan_id)
    existing = pending(checkpoints, identity.repo, context.plan.plan_id)
    if (pull_request is not None or row['pr_url'] or not remote or not current.observed_remote_oid
            or remote == current.observed_remote_oid or current.expected_head != local
            or (existing and existing['stage'] != 'done')
            or git(context.worktree, 'status', '--porcelain').stdout.strip()):
        return
    previous = DefinitionSnapshot.deserialize(row['definition_snapshot'])
    if previous is None:
        return
    changed = git(identity.repo_path, 'diff', '--name-only', '-z', local, remote, check=False)
    if changed.returncode:
        raise RecoveryFailure(RecoveryReason.STATUS_FAILED, changed.stdout)
    paths = tuple(path for path in changed.stdout.split('\0') if path)
    document_sets = _requirement_document_sets(
        git_bytes, identity.repo_path, (base, remote), previous.sources,
    )
    relevant = _related_paths_across_revisions(
        previous.path, previous.sources, paths, document_sets,
    )
    if not relevant:
        return
    save(checkpoints, identity.repo, context.plan.plan_id, {
        'version': 1, 'stage': 'rebase', 'base': base, 'phase': context.phase.value,
        'paths': list(relevant), 'document_only': documentary(paths), 'previous': previous.serialize(),
        'worker_result': row['worker_result'], 'verify_result': row['verify_result'],
        'verify_base': row['validated_base_sha'], 'assessment': None,
        'work_branch': True, 'before_head': local, 'remote': remote,
    })


def pending(checkpoints, repo, plan_id):
    value = (checkpoints.get(repo, plan_id).snapshot or {}).get("document_revision")
    if value is None:
        return None
    if (not isinstance(value, dict) or value.get("version") != 1
            or value.get("phase") not in {"IMPLEMENTING", "VERIFYING", "REVIEWING", "PUBLISHING"}
            or value.get("stage") not in {"rebase", "assess", "proposed", "prepared", "done"}
            or not isinstance(value.get("base"), str)
            or type(value.get('document_only')) is not bool
            or (value.get('stage') != 'done' and (
                not isinstance(value.get('paths'), list)
                or any(not isinstance(path, str) for path in value['paths'])
                or DefinitionSnapshot.deserialize(value.get('previous')) is None))
            or (value.get('stage') in {'proposed', 'prepared', 'done'}
                and not isinstance(value.get('assessment'), dict))
            or (value.get('stage') == 'prepared'
                and any(not isinstance(value.get(key), str) for key in ('workspace', 'commit')))):
        raise PlannerValidationError("보존된 문서 반영 상태가 유효하지 않습니다.")
    return value


def save(checkpoints, repo, plan_id, value):
    current = checkpoints.get(repo, plan_id)
    return checkpoints.record(repo, plan_id, expected_generation=current.generation,
        operation=current.operation, expected_head=current.expected_head,
        target_base_oid=current.target_base_oid, observed_remote_oid=current.observed_remote_oid,
        snapshot={**(current.snapshot or {}), "document_revision": value})


def restart_assessment(item, base, changed, *, git_bytes=None, repository=None):
    """원격 경합에서 게시 후보는 보존하고 새 입력의 판단 권한을 분리한다."""
    previous = DefinitionSnapshot.deserialize(item['previous'])
    document_sets = _requirement_document_sets(
        git_bytes, repository, (previous.revision_sha, base), previous.sources,
    )
    paths = _related_paths_across_revisions(
        previous.path, previous.sources, changed, document_sets,
    )
    return {**{key: value for key, value in item.items() if key not in {
                'workspace', 'original', 'commit', 'candidate', 'assessment_complete', 'published_plan_text',
                'planner_workspace', 'planner_generation', 'planner_scope', 'planner_scope_history', 'worker_snapshot'}},
            'stage': 'rebase', 'base': base, 'assessment': None,
            'paths': sorted(set(item['paths']) | set(paths)),
            'document_only': item['document_only'] and documentary(changed),
            'superseded': [*item.get('superseded', []),
                {key: item.get(key) for key in ('base', 'assessment', 'workspace', 'commit', 'candidate',
                    'planner_workspace', 'planner_scope', 'planner_scope_history', 'worker_snapshot')}]}


def review_pending(checkpoints, repo, plan_id, definition, candidate):
    item = pending(checkpoints, repo, plan_id) if checkpoints is not None else None
    return bool(item and item["stage"] == "done" and item.get("verification_pending")
                and item['base'] == definition.revision_sha
                and item.get("definition") == definition.definition_token
                and item.get("review_candidate") == candidate)


def parse_assessment(output):
    lines = [line for line in output.splitlines() if line.startswith("DOCUMENT_RESULT:")]
    if len(lines) != 1:
        raise PlannerValidationError("문서 영향 판단에는 하나의 DOCUMENT_RESULT가 필요합니다.")
    try:
        value = json.loads(lines[0].partition(":")[2])
    except (ValueError, TypeError) as error:
        raise PlannerValidationError("DOCUMENT_RESULT JSON이 유효하지 않습니다.") from error
    if (not isinstance(value, dict) or set(value) != {"relevant", "implementation", "plan_text", "evidence"}
            or type(value["relevant"]) is not bool
            or not isinstance(value['implementation'], str)
            or value["implementation"] not in {"satisfied", "needed", "uncertain"}
            or (value["plan_text"] is not None and not isinstance(value["plan_text"], str))
            or not isinstance(value["evidence"], str) or not value["evidence"].strip()
            or (not value["relevant"] and (value["plan_text"] is not None or value["implementation"] != "satisfied"))):
        raise PlannerValidationError("DOCUMENT_RESULT의 판단·계획·근거가 유효하지 않습니다.")
    return value


@dataclass(frozen=True)
class StageDocumentEffects:
    git: Callable
    capture_scope: Callable
    validate_scope: Callable
    run_agent: Callable
    prepare_snapshot: Callable
    assert_snapshot: Callable
    validate_result: Callable
    validate_worktree: Callable
    pr_for_branch: Callable
    log: Callable
    run_with_retries: Callable
    git_bytes: Callable[..., bytes] | None = None


def assert_assessment_inputs(effects, checkpoints, identity, context, item):
    current = effects.capture_scope(context.worktree)
    saved = item.get('worker_snapshot')
    if (current.head != item['candidate']
            or (saved is not None and not _same_local_snapshot(GitSnapshot.deserialize(saved), current))
            or effects.git(identity.repo_path, 'rev-parse', f'origin/{identity.base}').stdout.strip() != item['base']):
        save(checkpoints, identity.repo, context.plan.plan_id,
             restart_assessment(
                 item, item['base'], item['paths'], git_bytes=effects.git_bytes,
                 repository=identity.repo_path,
             ))
        raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE, '문서 판단의 구현 후보 또는 원격 입력이 변경됐습니다.')


def remember_workspace_metadata(effects, state, checkpoints, identity, context, workspace, *, published=False):
    """Worker 원본 범위는 보존하고 Manager가 만든 등록만 비교 기준에 반영한다."""
    row = state.row()
    if not row['agent_snapshot']:
        return
    original = GitSnapshot.deserialize(row['agent_snapshot'])
    current = checkpoints.get(identity.repo, context.plan.plan_id)
    baseline = manager_comparison_baseline(original, original, current.snapshot or {}, row['agent_role'])
    observed = effects.capture_scope(context.worktree)
    baseline = reconcile_manager_metadata(baseline, observed,
                                          refs=frozenset((f'refs/remotes/origin/{identity.base}',)) if published else frozenset(),
                                          worktrees=frozenset((str(workspace.resolve()),)))
    checkpoints.record(identity.repo, context.plan.plan_id, expected_generation=current.generation,
        operation=current.operation, expected_head=current.expected_head,
        target_base_oid=current.target_base_oid, observed_remote_oid=current.observed_remote_oid,
        snapshot={**(current.snapshot or {}), 'manager_metadata_agent_baseline': {
            'role': row['agent_role'], 'original': hashlib.sha256(original.serialize().encode()).hexdigest(),
            'snapshot': baseline.serialize()}})


def refresh(effects, state, checkpoints, identity, context, previous, definition, changed):
    """안전한 rebase 뒤 같은 작업만 판단하고 기존 게시 경계를 유지한다."""
    repo, plan_id = identity.repo, context.plan.plan_id
    row = state.row()
    item = pending(checkpoints, repo, plan_id)
    sources = tuple(dict.fromkeys((*(previous.sources if previous else ()), *definition.sources)))
    document_sets = _requirement_document_sets(
        effects.git_bytes, identity.repo_path,
        (definition.revision_sha, previous.revision_sha if previous else None), sources,
    )
    relevant = tuple(sorted({
        path
        for plan in (previous, definition) if plan is not None
        for path in _related_paths_across_revisions(
            plan.path, plan.sources, changed, document_sets,
        )
    }))
    if item is None and (not relevant or previous is None or context.resolving_rebase):
        return None
    if row['pr_url'] or effects.pr_for_branch(identity.repo_path, context.branch) is not None:
        if item is not None:
            save(checkpoints, repo, plan_id, None)
        return None
    if item and item.get('work_branch'):
        adoption = (checkpoints.get(repo, plan_id).snapshot or {}).get('remote_adoption_scope')
        if not isinstance(adoption, dict) or adoption.get('remote') != item['remote']:
            return None  # 문서 관측은 원격 branch 채택 권한을 대신하지 않는다.
    if (item is None and previous is not None and relevant == (definition.path,)
            and previous.worker_token == definition.worker_token):
        return None  # 수행 기록만 바뀐 ExecPlan은 기존 R-072 입력 증명으로 처리한다.
    if item is None or (item["stage"] == "done" and (
            item.get("definition") != definition.definition_token
            or (item['base'] != definition.revision_sha and relevant))):
        if not relevant or previous is None or context.resolving_rebase:
            return None
        item = {"version": 1, "stage": "rebase", "base": definition.revision_sha,
                "phase": context.phase.value, "paths": list(relevant),
                "document_only": documentary(changed), "previous": previous.serialize(),
                "worker_result": row["worker_result"], "verify_result": row["verify_result"],
                "verify_base": row["validated_base_sha"], "assessment": None,
                'before_head': effects.git(context.worktree, 'rev-parse', 'HEAD').stdout.strip()}
        save(checkpoints, repo, plan_id, item)
    if item["stage"] == "done":
        if (item['base'] == definition.revision_sha and item.get('worker_snapshot')
                and not item.get('decision_applied') and not item.get('assessment_complete')):
            assert_assessment_inputs(effects, checkpoints, identity, context, item)
        return item if item['base'] == definition.revision_sha else None
    if context.resolving_rebase:
        return None
    if item['base'] != definition.revision_sha and item['stage'] != 'prepared':
        item = restart_assessment(
            item, definition.revision_sha, changed, git_bytes=effects.git_bytes,
            repository=identity.repo_path,
        )
        save(checkpoints, repo, plan_id, item)
    if str(row["active_sha"] or "") != definition.revision_sha and item["stage"] != "prepared":
        return item
    if item["stage"] == "rebase":
        item = {**item, "stage": "proposed" if item.get("assessment_complete") else "assess",
                "base": definition.revision_sha}
        save(checkpoints, repo, plan_id, item)
    if item["stage"] == "assess":
        head = effects.git(context.worktree, "rev-parse", "HEAD").stdout.strip()
        generation = item.get('planner_generation', checkpoints.get(repo, plan_id).generation)
        if type(generation) is not int or generation < 0:
            raise PlannerValidationError('문서 판단 작업 공간의 세대가 유효하지 않습니다.')
        workspace = (identity.state_dir / 'worktrees' / hashlib.sha256(repo.encode()).hexdigest()[:16]
                     / 'document-assessments' / plan_id
                     / hashlib.sha256(f"{item['base']}:{head}:{generation}".encode()).hexdigest())
        if not workspace.resolve().is_relative_to(identity.state_dir.resolve()) or workspace.is_symlink():
            raise PlannerValidationError('문서 판단 작업 공간이 관리 경계를 벗어났습니다.')
        if item.get('planner_workspace') != str(workspace) and workspace.exists():
            raise PlannerValidationError('소유권이 없는 기존 문서 판단 작업 공간을 사용하지 않습니다.')
        item = {**item, 'planner_workspace': str(workspace), 'planner_generation': generation, 'candidate': head}
        save(checkpoints, repo, plan_id, item)
        if not workspace.exists():
            workspace.parent.mkdir(parents=True, exist_ok=True)
            effects.git(identity.repo_path, 'worktree', 'add', '--detach', str(workspace), head)
        effects.validate_worktree(identity.repo_path, workspace, None)
        if (effects.git(workspace, 'rev-parse', 'HEAD').stdout.strip() != head
                or effects.git(workspace, 'status', '--porcelain').stdout.strip()):
            raise PlannerValidationError('문서 판단 작업 공간이 구현 revision과 다릅니다.')
        if item.get('planner_scope'):
            prior = item['planner_scope']
            if prior['role'] != 'planner' or prior['worktree'] != str(workspace):
                raise PlannerValidationError('보존된 Planner 범위가 현재 문서 판단 작업 공간과 다릅니다.')
            before = GitSnapshot.deserialize(prior['before'])
            after = effects.capture_scope(workspace)
            effects.validate_scope('planner', workspace, before, after,
                                   external_worktree=identity.repo_path, base_branch=identity.base)
            if not _same_local_snapshot(before, after):
                raise PlannerValidationError('중단된 읽기 전용 문서 판단의 작업 공간이 변경됐습니다.')
        remember_workspace_metadata(effects, state, checkpoints, identity, context, workspace)
        worker_before = effects.capture_scope(context.worktree)
        item = {**item, 'worker_snapshot': worker_before.serialize()}
        save(checkpoints, repo, plan_id, item)
        document_head = head if item.get('work_branch') else item['base']
        prompt = f"""현재 진행 중인 {plan_id}의 원격 문서 변경을 판단하는 일반 Planner 호출이다.
현재 위치는 Planner 전용 worktree이며 Worker 구현 revision은 {head}다.
읽기 전용으로 구현·테스트와 최신 문서 revision {document_head}의 ExecPlan·연결 Requirement·PRODUCT·ARCHITECTURE를 대조하라.
최신 문서 원문은 git show {document_head}:<문서 경로>로 확인하라.
Worker 작업 공간 {context.worktree}의 미커밋 작업도 읽기 전용 검토 입력이며 파일·Git·범위 기록을 변경하지 마라.
확인한 변경 문서는 {json.dumps(item['paths'], ensure_ascii=False)}다. 이전 선언은 {item['previous']}다.
현재 단계를 {item['phase']}로 이어갈 판단이며 다른 작업을 계획하거나 파일·Git을 변경하거나 테스트를 실행하지 마라.
PR 등록 전인 현재 ExecPlan만 필요한 경우 같은 ID·경로와 기존 수행 기록을 보존한 전체 문서 수정안으로 반환하라.
구현 조건이 바뀌었지만 현재 구현이 충족하면 satisfied, 코드·테스트 수정이 필요하면 needed,
명세 판단이 불확실하면 uncertain으로 표시하고 근거를 기록하라. 문서 변경 자체를 구현 부족이나 검증 성공으로 취급하지 마라.
정확히 한 줄 DOCUMENT_RESULT: JSON을 반환하라. 필드는 relevant(bool), implementation(satisfied|needed|uncertain),
plan_text(전체 ExecPlan 문자열 또는 수정 불필요면 null), evidence(확인한 구현·문서 관계의 근거)다.
"""
        protocol_error = item.get('validation_error', '')
        for attempt in range(3):
            before = effects.capture_scope(workspace)
            item = {**item, 'planner_scope_history': [*item.get('planner_scope_history', []),
                    *([item['planner_scope']] if item.get('planner_scope') else [])],
                    'planner_scope': {'role': 'planner', 'worktree': str(workspace),
                    'input_fingerprint': definition.definition_token, 'before': before.serialize(), 'after': None}}
            save(checkpoints, repo, plan_id, item)
            result = effects.run_with_retries(TrackedAgentCall(
                lambda session, recovery, invocation: effects.run_agent(
                    identity.provider, workspace, identity.state_dir, identity.config_path,
                    "planner", prompt + ('\n이전 결과 검증 오류: ' + protocol_error if protocol_error else ''),
                    read_only=True, persist_session=False, invocation=invocation),
                AttemptSequence(repo, plan_id, definition.revision_sha,
                                purpose="document_change_assessment", target=plan_id)), session_id=None)
            after = effects.capture_scope(workspace)
            item = {**item, 'planner_scope': {**item['planner_scope'], 'after': after.serialize()}}
            save(checkpoints, repo, plan_id, item)
            effects.validate_scope("planner", workspace, before, after,
                                   external_worktree=identity.repo_path, base_branch=identity.base)
            if not _same_local_snapshot(before, after):
                raise PlannerValidationError("읽기 전용 문서 판단 중 작업 공간이 변경됐습니다.")
            effects.git(identity.repo_path, 'fetch', 'origin')
            assert_assessment_inputs(effects, checkpoints, identity, context, item)
            code, output = result
            output = redact_secrets(str(output))
            if code:
                if result.result_kind is AgentResultKind.USAGE_LIMIT:
                    state.update_project(repo, last_error=output)
                    return ExecutionOutcome(ExecutionOutcomeKind.USAGE_PAUSE, detail=output, error=True)
                raise RecoveryFailure(RecoveryReason.SERVICE_UNAVAILABLE, output)
            try:
                assessment = parse_assessment(output)
                break
            except PlannerValidationError as error:
                protocol_error = str(error)
                item = {**item, 'validation_error': protocol_error, 'last_result': output}
                save(checkpoints, repo, plan_id, item)
                if attempt == 2:
                    raise
        item = {**item, "stage": "proposed", "assessment": assessment, "candidate": head}
        save(checkpoints, repo, plan_id, item)
    assessment = item["assessment"]
    assessment = parse_assessment('DOCUMENT_RESULT: ' + json.dumps(assessment))
    if item['stage'] == 'proposed' and not item.get('assessment_complete'):
        assert_assessment_inputs(effects, checkpoints, identity, context, item)
    if assessment["implementation"] == "uncertain":
        raise PlannerValidationError("진행 중 작업의 명세 영향 판단이 불확실합니다: " + assessment["evidence"])
    if item["stage"] == "proposed" and assessment["plan_text"] is not None:
        snapshot = effects.prepare_snapshot(state.connection, repo, identity.repo_path, item["base"])
        original = snapshot.by_id.get(plan_id)
        if original is None:
            raise PlannerValidationError("현재 계획의 PR 전 갱신 권한이 없습니다.")
        if assessment['plan_text'] == original.text:
            item = {**item, 'assessment': {**assessment, 'plan_text': None}}
            save(checkpoints, repo, plan_id, item)
            return refresh(effects, state, checkpoints, identity, context, previous, definition, changed)
        snapshot = MutableReadySnapshot(repo, snapshot.active_sha, (original,),
            mutable_snapshot_token(repo, snapshot.active_sha, (original,)))
        workspace = identity.state_dir / "worktrees" / hashlib.sha256(repo.encode()).hexdigest()[:16] / "document-revisions" / plan_id / item["base"]
        if not workspace.resolve().is_relative_to(identity.state_dir.resolve()):
            raise PlannerValidationError("문서 게시 작업 공간이 관리 경계를 벗어났습니다.")
        if item.get("workspace") != str(workspace) and workspace.exists():
            raise PlannerValidationError("소유권이 없는 기존 문서 게시 작업 공간을 사용하지 않습니다.")
        item = {**item, "workspace": str(workspace), "original": snapshot_to_dict(original)}
        save(checkpoints, repo, plan_id, item)
        if not workspace.exists():
            workspace.parent.mkdir(parents=True, exist_ok=True)
            effects.git(identity.repo_path, "worktree", "add", "--detach", str(workspace), item["base"])
        effects.validate_worktree(identity.repo_path, workspace, None)
        if (workspace.is_symlink() or (workspace / original.path).is_symlink()
                or not (workspace / original.path).resolve().is_relative_to(workspace.resolve())):
            raise PlannerValidationError("문서 게시 작업 공간의 symlink를 허용하지 않습니다.")
        head = effects.git(workspace, 'rev-parse', 'HEAD').stdout.strip()
        if head != item['base']:
            if (effects.git(workspace, 'status', '--porcelain').stdout.strip()
                    or effects.git(workspace, 'rev-parse', 'HEAD^').stdout.strip() != item['base']
                    or effects.git(workspace, 'diff', '--name-only', item['base'], head).stdout.splitlines() != [original.path]
                    or (workspace / original.path).read_text(encoding='utf-8') != assessment['plan_text']):
                raise PlannerValidationError('중단된 문서 게시 commit의 근거를 확인할 수 없습니다.')
            save(checkpoints, repo, plan_id, {**item, 'stage': 'prepared', 'commit': head})
            return refresh(effects, state, checkpoints, identity, context, previous, definition, changed)
        staged = effects.git(workspace, 'diff', '--name-only', '--cached').stdout.splitlines()
        dirty = effects.git(workspace, 'diff', '--name-only').stdout.splitlines()
        if (head != item['base'] or any(path != original.path for path in (*staged, *dirty))
                or effects.git(workspace, 'ls-files', '--others', '--exclude-standard').stdout.strip()
                or (workspace / original.path).read_text(encoding='utf-8') not in {original.text, assessment['plan_text']}):
            raise PlannerValidationError("보존된 문서 게시 작업 공간이 예상 입력과 다릅니다.")
        (workspace / original.path).write_text(assessment["plan_text"], encoding="utf-8")
        result = effects.validate_result(workspace, item["base"], [original.path],
                                         connection=state.connection, repo=repo, mutable_snapshot=snapshot)
        if result is None or result.plans or result.deletions or len(result.revisions) != 1:
            raise PlannerValidationError("현재 작업의 단일 문서 개정만 게시할 수 있습니다.")
        parsed = result.revisions[0].plan
        from loop_manager_internal.catalog.execplan_catalog import parse_execplan_text
        prior_plan = parse_execplan_text(original.path, original.text, validate_sources=False)
        if any(parsed[key] != prior_plan[key] for key in ("id", "title")):
            raise PlannerValidationError("진행 중 문서 개정은 계획 identity·경로를 보존해야 합니다.")
        effects.git(workspace, "add", "--", original.path)
        if effects.git(workspace, "diff", "--cached", "--quiet", check=False).returncode:
            effects.git(workspace, "commit", "-m", f"docs: {plan_id} 최신 원격 명세 반영")
        commit = effects.git(workspace, "rev-parse", "HEAD").stdout.strip()
        item = {**item, "stage": "prepared", "commit": commit}
        save(checkpoints, repo, plan_id, item)
    if item["stage"] == "prepared":
        workspace = Path(item["workspace"])
        expected = identity.state_dir / 'worktrees' / hashlib.sha256(repo.encode()).hexdigest()[:16] / 'document-revisions' / plan_id / item['base']
        if workspace != expected or not workspace.resolve().is_relative_to(identity.state_dir.resolve()):
            raise PlannerValidationError("보존된 문서 게시 작업 공간이 관리 경계를 벗어났습니다.")
        effects.validate_worktree(identity.repo_path, workspace, None)
        original = snapshot_from_dict(item["original"])
        snapshot = MutableReadySnapshot(repo, item["base"], (original,),
            mutable_snapshot_token(repo, item["base"], (original,)))
        effects.git(identity.repo_path, "fetch", "origin")
        remote = effects.git(identity.repo_path, "rev-parse", f"origin/{identity.base}").stdout.strip()
        published = effects.git(identity.repo_path, "merge-base", "--is-ancestor", item["commit"], remote, check=False)
        if published.returncode not in {0, 1}:
            raise RecoveryFailure(RecoveryReason.STATUS_FAILED, published.stdout)
        if published.returncode:
            if remote != item["base"]:
                delta = effects.git(identity.repo_path, 'diff', '--name-only', '-z', item['base'], remote).stdout.split('\0')
                item = restart_assessment(
                    item, remote, [path for path in delta if path],
                    git_bytes=effects.git_bytes, repository=identity.repo_path,
                )
                save(checkpoints, repo, plan_id, item)
                return item
            effects.assert_snapshot(state.connection, snapshot)
            assert_assessment_inputs(effects, checkpoints, identity, context, item)
            if effects.pr_for_branch(identity.repo_path, context.branch) is not None:
                raise PlannerValidationError("문서 게시 전에 연결 PR이 등록됐습니다.")
            if (effects.git(workspace, "rev-parse", "HEAD").stdout.strip() != item["commit"]
                    or effects.git(workspace, "status", "--porcelain").stdout.strip()
                    or effects.git(workspace, "diff", "--name-only", item["base"], item["commit"]).stdout.splitlines() != [original.path]):
                raise PlannerValidationError("보존된 문서 게시 후보가 변경됐습니다.")
            remember_workspace_metadata(effects, state, checkpoints, identity, context, workspace)
            effects.git(workspace, "push", "origin", f"{item['commit']}:refs/heads/{identity.base}")
            effects.git(identity.repo_path, "fetch", "origin")
        remote = effects.git(identity.repo_path, 'rev-parse', f'origin/{identity.base}').stdout.strip()
        if remote != item['commit']:
            delta = effects.git(identity.repo_path, 'diff', '--name-only', '-z', item['commit'], remote).stdout.split('\0')
            item = restart_assessment(
                item, remote, [path for path in delta if path],
                git_bytes=effects.git_bytes, repository=identity.repo_path,
            )
            save(checkpoints, repo, plan_id, item)
            return item
        remember_workspace_metadata(effects, state, checkpoints, identity, context, workspace, published=True)
        item = {**item, "stage": "rebase", "base": remote,
                "assessment_complete": True, "published_plan_text": assessment["plan_text"],
                "assessment": {**assessment, "plan_text": None}}
        save(checkpoints, repo, plan_id, item)
        return item
    if item["stage"] == "proposed":
        item = {**item, "stage": "done", "definition": definition.definition_token,
                "verification_pending": assessment["relevant"] and item["document_only"]}
        with runtime_state._transaction(state.connection):
            if item["verification_pending"]:
                verify_receipts.start_cycle(state.connection, repo, plan_id)
            save(checkpoints, repo, plan_id, item)
        effects.log(f"원격 문서 반영: {plan_id}, 관련={assessment['relevant']}, 구현={assessment['implementation']}")
    return item
