"""execution result validation boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.application.result_normalization import normalize_markers
from loop_manager_internal.agents import agent_profiles
from loop_manager_internal.application.contracts import ReviewerProtocolError
from loop_manager_internal.application.contracts import VerifyOutput
from loop_manager_internal.application.contracts import WorkerLevelDecision
from loop_manager_internal.application.contracts import WorkerPublication
from loop_manager_internal.application.contracts import WorkerResultError
from loop_manager_internal.application.contracts import _ANSI_ESCAPE
from loop_manager_internal.application.contracts import _DURATION_NOISE
from loop_manager_internal.application.contracts import _INLINE_TEST_COMMAND
from loop_manager_internal.application.contracts import _NO_RUNNABLE_TARGETED_TEST
from loop_manager_internal.application.contracts import _REVIEW_ISSUE_CATEGORY
from loop_manager_internal.application.contracts import _REVIEW_ISSUE_DECLARATION
from loop_manager_internal.application.contracts import _REVIEW_ISSUE_ID
from loop_manager_internal.application.contracts import _REVIEW_ISSUE_SYMBOL
from loop_manager_internal.application.contracts import _REVIEW_VERDICT
from loop_manager_internal.application.contracts import _REVIEW_VERDICT_DECLARATION
from loop_manager_internal.application.contracts import _TARGETED_TEST_COMMAND
from loop_manager_internal.application.contracts import _TARGETED_TEST_EXCEPTION_FAILURE
from loop_manager_internal.application.contracts import _TARGETED_TEST_FAILURE
from loop_manager_internal.application.contracts import _TARGETED_TEST_METADATA
from loop_manager_internal.application.contracts import _TARGETED_TEST_SUCCESS
from loop_manager_internal.application.contracts import _TEMP_DIRECTORY_CONTEXT
from loop_manager_internal.application.contracts import _TEMP_PATH_NOISE
from loop_manager_internal.application.contracts import _TIMESTAMP_NOISE
from loop_manager_internal.documents import reviewer_specification
from loop_manager_internal.execution.validation_feedback import redact_secrets
from loop_manager_internal.git import github_observation
from loop_manager_internal.runtime.handoff_gate import _runtime_handoff_signal
from loop_manager_internal.storage import review_correction_state
from loop_manager_internal.storage import recovery_state
from loop_manager_internal.storage import runtime_state
from pathlib import Path
from urllib.parse import quote
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3


class TargetedTestFailure(WorkerResultError):
    """최종 보고의 미해결 테스트 실패. 실행 횟수의 근거로 사용하지 않는다."""


def _deserialize_review_level_history(
    value: object,
) -> dict[str, dict[str, int]]:
    if value in {None, ""}:
        return {}
    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise RuntimeError("저장된 level별 Review 승격 이력이 유효하지 않습니다.")
    history: dict[str, dict[str, int]] = {}
    for level, records in parsed.items():
        if (
            not isinstance(level, str)
            or not level.isdigit()
            or not 1 <= int(level) <= 8
            or not isinstance(records, dict)
        ):
            raise RuntimeError("저장된 level별 Review 승격 이력이 유효하지 않습니다.")
        normalized_records: dict[str, int] = {}
        for fingerprint, occurrences in records.items():
            if (
                not isinstance(fingerprint, str)
                or not fingerprint
                or type(occurrences) is not int
                or occurrences < 1
            ):
                raise RuntimeError("저장된 level별 Review 승격 이력이 유효하지 않습니다.")
            normalized_records[fingerprint] = occurrences
        history[level] = normalized_records
    return history


def evaluate_review_worker_level(
    effective_level: int,
    review_history: dict[str, dict[str, int]],
    fingerprints: tuple[str, ...],
    correction_state: dict | None = None,
    *,
    review_level: int | None = None,
    candidate_head: str | None = None,
) -> WorkerLevelDecision:
    """Count only completed, verified feedback turns after a per-level baseline."""
    if type(effective_level) is not int or not 1 <= effective_level <= 8:
        raise ValueError(f"effective Worker level이 유효하지 않습니다: {effective_level}")
    if not fingerprints or any(not fingerprint for fingerprint in fingerprints):
        raise ValueError("Review 승격 평가에는 실패 지문이 하나 이상 필요합니다.")
    history = {level: dict(records) for level, records in review_history.items()}
    # Copy nested pending/issues as well: callers may retain the pre-transition state.
    state = json.loads(json.dumps(correction_state or {}))
    if review_level is not None and review_level != effective_level:
        return WorkerLevelDecision(effective_level, effective_level, history, state)
    baseline = 'level' not in state or state.get('closed', False) or state['level'] != effective_level
    pending = state.get('pending')
    completed = bool(pending and pending['level'] == effective_level
                     and pending['completed'] and candidate_head
                     and (pending['verified_head'] == candidate_head
                          or pending.get('partial_head') == candidate_head))
    if baseline:
        state = {'version': 1, 'level': effective_level,
                 'baseline': sorted(set(fingerprints)), 'failures': 0, 'issues': {},
                 'pending': None,
                 **({'candidate': state['candidate']} if 'candidate' in state else {}),
                 'audit': [*state.get('audit', []), {
                     'kind': 'baseline', 'level': effective_level,
                     'issues': sorted(set(fingerprints)),
                 }]}
    elif not completed:
        return WorkerLevelDecision(effective_level, effective_level, history, state)
    else:
        state.setdefault('audit', []).append({
            'kind': 'correction', 'level': effective_level,
            'source_review_id': pending['source'], 'verified_head': pending['verified_head'],
            **({'partial_head': pending['partial_head']} if pending.get('partial_head') == candidate_head else {}),
            'issues': sorted(set(fingerprints)),
        })
        state['pending'] = None  # Consumed atomically with the level update.
        state['failures'] += 1
        for fingerprint in set(fingerprints):
            state['issues'][fingerprint] = state['issues'].get(fingerprint, 0) + 1
    current = history.setdefault(str(effective_level), {})
    for fingerprint in set(fingerprints):
        current[fingerprint] = current.get(fingerprint, 0) + 1
    escalate = (state['failures'] >= 3
                or any(state['issues'].get(issue, 0) >= 2 for issue in state['baseline']))
    next_level = min(8, effective_level + 1) if escalate else effective_level
    state['audit'][-1]['effective_level'] = next_level
    return WorkerLevelDecision(effective_level, next_level, history, state)


@dataclass(frozen=True)
class EnsureWorkerLevelStateEffects:
    """Named effects for ensure_worker_level_state; no entry-point/module access."""
    _deserialize_review_level_history: Callable[..., object]
    active_execplan: Callable[..., object]
    worker_level: Callable[..., object]


def ensure_worker_level_state(
    effects: EnsureWorkerLevelStateEffects, connection: sqlite3.Connection,
    repo: str,
    plan: dict[str, object],
) -> int:
    """Backfill and validate the persisted initial/effective Worker levels."""

    catalog_plan = effects.active_execplan(connection, repo)
    if catalog_plan is None:
        raise RuntimeError("Worker level을 저장할 활성 ExecPlan을 찾을 수 없습니다.")
    calculated_initial = effects.worker_level(plan)
    initial = catalog_plan["initial_worker_level"]
    effective = catalog_plan["effective_worker_level"]
    if initial is None and effective is None:
        with connection:
            runtime_state.RuntimeStateRepository(connection).update_plan(
                repo,
                str(catalog_plan["plan_id"]),
                plan_values={
                    "initial_worker_level": calculated_initial,
                    "effective_worker_level": calculated_initial,
                    "review_level_history": "{}",
                },
                enqueue=False,
            )
        return calculated_initial
    if initial is None or effective is None:
        raise RuntimeError("저장된 Worker level 상태가 완전하지 않습니다.")
    if (
        type(initial) is not int
        or not 1 <= initial <= 7
        or type(effective) is not int
        or not initial <= effective <= 8
    ):
        raise RuntimeError("저장된 Worker level 상태가 ExecPlan과 일치하지 않습니다.")
    review_correction_state.deserialize(catalog_plan["review_correction_state"])
    review_history = effects._deserialize_review_level_history(
        catalog_plan["review_level_history"]
    )
    if any(not 1 <= int(level) <= effective for level in review_history):
        raise RuntimeError("저장된 Review 승격 이력이 Worker level과 일치하지 않습니다.")

    if initial != calculated_initial:
        # Recalibrate only the declaration cache; running profiles and audit
        # records belong to the execution and survive policy changes.
        with connection:
            runtime_state.RuntimeStateRepository(connection).update_plan(
                repo, str(catalog_plan["plan_id"]),
                plan_values={"initial_worker_level": calculated_initial},
                enqueue=False,
            )
    return effective


@dataclass(frozen=True)
class CodexModelEffortsEffects:
    """Named effects for codex_model_efforts; no entry-point/module access."""
    _agent_profiles_effects: Callable[..., object]


def codex_model_efforts(effects: CodexModelEffortsEffects, worktree: Path | None=None) -> dict[str, frozenset[str]]:
    return agent_profiles.codex_model_efforts(worktree, effects=effects._agent_profiles_effects())


@dataclass(frozen=True)
class VerifyEffects:
    """Named effects for verify; no entry-point/module access."""
    _check_runtime_handoff: Callable[..., object]
    _handoff_popen: Callable[..., object]
    run_verify_process: Callable[..., object]


def verify_arguments(worktree: Path):
    if os.name == "nt":
        script = worktree / "scripts/verify.ps1"
        executable = shutil.which("pwsh") or shutil.which("powershell")
        arguments = [executable or "pwsh", "-NoProfile", "-File", str(script)]
    else:
        script = worktree / "scripts/verify.sh"
        from loop_manager_internal.verification.manager_adapter import isolated_compile_arguments
        arguments = isolated_compile_arguments(script, worktree) or ["bash", str(script)]
    return script, arguments


def observe_verify_inputs(worktree: Path):
    """Arbitrary scripts have unmeasured dynamic/Git/external dependencies.

    Never infer their absence from source spelling or a passing invocation.
    The exact isolated compile contract has a bounded, measured adapter.
    Everything else keeps the existing full verify fallback.
    """
    from loop_manager_internal.verification.manager_adapter import (
        observe_inputs, observe_isolated_compile, isolated_compile_arguments)
    script, arguments = verify_arguments(worktree)
    if arguments == isolated_compile_arguments(script, worktree):
        return observe_isolated_compile(worktree, script, arguments, dict(os.environ))
    return observe_inputs(worktree, arguments, environment=dict(os.environ),
                          runtime_identity=None)


from loop_manager_internal.application.task_scheduling import scheduled_call


@scheduled_call("verify")
def verify(effects: VerifyEffects, worktree: Path, timeout_seconds: int = 600, *, partial_base=None) -> tuple[bool, str]:
    from loop_manager_internal.verification.execution_clock import ExecutionClock, current_execution_clock
    with ExecutionClock() as budget:
        token = current_execution_clock.set(budget)
        try:
            passed, output = _verify(effects, worktree, timeout_seconds, partial_base=partial_base)
            if passed and budget.remaining(timeout_seconds) <= 0:
                failed = VerifyOutput(str(output)+f"\n전체 verify 실제 실행 시간 초과: verify_timeout_seconds={timeout_seconds}",
                                      timed_out=True, exit_code=getattr(output, "exit_code", None))
                if hasattr(output, "partial_result"):
                    failed.partial_result = output.partial_result
                return False, failed
            return passed, output
        finally:
            current_execution_clock.reset(token)


def _verify(effects: VerifyEffects, worktree: Path, timeout_seconds: int = 600, *, partial_base=None) -> tuple[bool, str]:
    effects._check_runtime_handoff()
    script, arguments = verify_arguments(worktree)
    if not script.is_file():
        return False, f"전체 검증 스크립트가 없습니다: {script.relative_to(worktree)}"
    gate = _runtime_handoff_signal.get()
    from loop_manager_internal.verification.contract import load, ContractError
    try:
        connected = load(worktree) is not None
    except ContractError as error:
        return False, VerifyOutput(f"verify connection error: {error}", timed_out=False, exit_code=2)
    if connected:
        from loop_manager_internal.verification import manager_adapter
        passed, timed_out, output = manager_adapter.run(
            effects, arguments, worktree, timeout_seconds,
            launch=effects._handoff_popen if gate is not None else None,
            **({"partial_base": partial_base} if partial_base else {}),
        )
    elif partial_base:
        return False, VerifyOutput("partial verify requires unittest connection", exit_code=2)
    else:
        passed, timed_out, output = effects.run_verify_process(
            arguments, worktree, timeout_seconds,
            **({"launch_process": effects._handoff_popen} if gate is not None else {}),
        )
    exit_code = getattr(output, "exit_code", 0 if passed else None)
    partial_report = getattr(output, "partial_result", None)
    if timed_out:
        output += (
            f"\n전체 verify 시간 초과: verify_timeout_seconds={timeout_seconds}. "
            "verify와 자식 프로세스 종료를 확인했습니다."
        )
    result = VerifyOutput(output, timed_out=timed_out, exit_code=exit_code)
    if partial_base:
        result.partial_result = partial_report or {"kind": "partial_verify", "status": "ERROR"}
    return passed, result


def _valid_review_issue_path(path: str) -> bool:
    if (
        not path
        or path != path.strip()
        or path.startswith("/")
        or "\\" in path
        or ":" in path
        or not path.isprintable()
    ):
        return False
    parts = path.split("/")
    return all(
        part and part == part.strip() and part not in {".", ".."}
        for part in parts
    )


@dataclass(frozen=True)
class ValidReviewIssueIdentifierEffects:
    """Named effects for _valid_review_issue_identifier; no entry-point/module access."""
    _valid_review_issue_path: Callable[..., object]


def _valid_review_issue_identifier(effects: ValidReviewIssueIdentifierEffects, identifier: str) -> bool:
    components = identifier.split("::")
    if len(components) != 3:
        return False
    path, symbol, category = components
    return (
        effects._valid_review_issue_path(path)
        and _REVIEW_ISSUE_SYMBOL.fullmatch(symbol) is not None
        and _REVIEW_ISSUE_CATEGORY.fullmatch(category) is not None
    )


@dataclass(frozen=True)
class NormalizeReviewIssueIdsEffects:
    """Named effects for _normalize_review_issue_ids; no entry-point/module access."""
    _valid_review_issue_identifier: Callable[..., object]


def _normalize_review_issue_ids(effects: NormalizeReviewIssueIdsEffects, output: str) -> tuple[str, ...]:
    output = normalize_markers(output, ("ISSUE_ID",))
    declarations = [
        line
        for line in output.splitlines()
        if _REVIEW_ISSUE_DECLARATION.match(line)
    ]
    if not declarations:
        raise ReviewerProtocolError(
            "Reviewer FAIL 결과에 유효한 ISSUE_ID가 없습니다."
        )
    matches = list(_REVIEW_ISSUE_ID.finditer(output))
    if len(matches) != len(declarations):
        raise ReviewerProtocolError(
            "Reviewer FAIL 결과의 ISSUE_ID 형식이 유효하지 않습니다."
        )
    raw_identifiers = [
        "::".join(part.strip() for part in match.group("identifier").split("::")).casefold()
        for match in matches
    ]
    if any(
        not effects._valid_review_issue_identifier(identifier)
        for identifier in raw_identifiers
    ):
        raise ReviewerProtocolError(
            "Reviewer FAIL 결과의 ISSUE_ID 형식이 유효하지 않습니다."
        )
    identifiers = set(raw_identifiers)
    return tuple(f"ISSUE_ID:{identifier}" for identifier in sorted(identifiers))


@dataclass(frozen=True)
class ParseReviewerResultEffects:
    """Named effects for parse_reviewer_result; no entry-point/module access."""
    _normalize_review_issue_ids: Callable[..., object]


def parse_reviewer_result(
    effects: ParseReviewerResultEffects, output: str,
) -> tuple[str, tuple[str, ...] | reviewer_specification.SpecificationDeadlock]:
    """Validate the Reviewer response contract before any state transition."""

    output = normalize_markers(output, ("VERDICT", "ISSUE_ID", "SPEC_DEADLOCK"))
    lines = output.splitlines()
    meaningful_lines = [
        (index, line)
        for index, line in enumerate(lines)
        if line.strip()
    ]
    verdict_lines = [
        (index, line)
        for index, line in meaningful_lines
        if _REVIEW_VERDICT_DECLARATION.match(line)
    ]
    if len(verdict_lines) != 1:
        raise ReviewerProtocolError(
            "Reviewer 결과는 정확히 하나의 VERDICT를 포함해야 합니다."
        )
    _verdict_index, verdict_line = verdict_lines[0]
    verdict_match = _REVIEW_VERDICT.fullmatch(verdict_line)
    if verdict_match is None:
        raise ReviewerProtocolError(
            "Reviewer VERDICT는 PASS, FAIL, SPEC_BLOCKED 또는 VERIFY_REQUIRED이어야 합니다."
        )

    verdict = verdict_match.group(1).upper()
    issue_declarations = [
        line
        for _index, line in meaningful_lines
        if _REVIEW_ISSUE_DECLARATION.match(line)
    ]
    specification_declarations = [
        line for _index, line in meaningful_lines
        if line.lstrip().upper().startswith("SPEC_DEADLOCK:")
    ]
    if verdict == "SPEC_BLOCKED":
        if issue_declarations:
            raise ReviewerProtocolError("SPEC_BLOCKED 결과에는 ISSUE_ID가 없어야 합니다.")
        try:
            return verdict, reviewer_specification.parse_evidence(output)
        except ValueError as error:
            raise ReviewerProtocolError(str(error)) from error
    if specification_declarations:
        raise ReviewerProtocolError("PASS·FAIL 결과에는 SPEC_DEADLOCK이 없어야 합니다.")
    if verdict in {"PASS", "VERIFY_REQUIRED"}:
        if issue_declarations:
            raise ReviewerProtocolError(
                "Reviewer PASS·VERIFY_REQUIRED 결과에는 ISSUE_ID가 없어야 합니다."
            )
        return verdict, ()
    return verdict, effects._normalize_review_issue_ids(output)


@dataclass(frozen=True)
class ReviewIssuePathsEffects:
    """Named effects for _review_issue_paths; no entry-point/module access."""
    parse_reviewer_result: Callable[..., object]


def _review_issue_paths(effects: ReviewIssuePathsEffects, output: object) -> list[str]:
    if not isinstance(output, str) or not output.strip():
        return []
    try:
        verdict, details = effects.parse_reviewer_result(output)
    except ReviewerProtocolError:
        return []
    if verdict == "SPEC_BLOCKED":
        assert isinstance(details, reviewer_specification.SpecificationDeadlock)
        return sorted(details.paths)
    if verdict != "FAIL":
        return []
    output = normalize_markers(output, ("ISSUE_ID",))
    return sorted(
        {
            match.group("identifier").strip().split("::", 1)[0].strip()
            for match in _REVIEW_ISSUE_ID.finditer(output)
        }
    )


def _normalize_temp_path(match: re.Match[str]) -> str:
    tail = match.group("tail").strip("\\/")
    components = [part for part in re.split(r"[\\/]", tail) if part]
    leaf = components[-1]
    line_start = match.string.rfind("\n", 0, match.start()) + 1
    prefix = match.string[line_start:match.start()]
    if len(components) == 1 and _TEMP_DIRECTORY_CONTEXT.search(prefix):
        return "<TEMP_PATH>"
    return f"<TEMP_PATH>/{leaf}"


@dataclass(frozen=True)
class NormalizeValidationFailureEffects:
    """Named effects for normalize_validation_failure; no entry-point/module access."""
    _normalize_review_issue_ids: Callable[..., object]
    _normalize_temp_path: Callable[..., object]


def normalize_validation_failure(
    effects: NormalizeValidationFailureEffects, stage: str,
    output: str,
    worktree: Path | None = None,
) -> str:
    """Remove unstable execution noise and make failure line ordering irrelevant."""

    if stage not in {"verify", "review"}:
        raise ValueError(f"검증 실패 단계가 유효하지 않습니다: {stage}")
    fingerprint_source = (
        output.fingerprint_source if isinstance(output, VerifyOutput) else output
    )
    normalized = fingerprint_source.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _ANSI_ESCAPE.sub("", normalized)
    if worktree is not None:
        path_values = {
            str(worktree),
            str(worktree.resolve()),
            str(worktree).replace("\\", "/"),
            str(worktree).replace("/", "\\"),
        }
        for path_value in sorted(path_values, key=len, reverse=True):
            if path_value:
                normalized = re.sub(
                    re.escape(path_value),
                    "<WORKTREE>",
                    normalized,
                    flags=re.IGNORECASE,
                )
    normalized = _TEMP_PATH_NOISE.sub(effects._normalize_temp_path, normalized)
    normalized = _TIMESTAMP_NOISE.sub("<TIMESTAMP>", normalized)
    normalized = _DURATION_NOISE.sub("<DURATION>", normalized)
    if stage == "review":
        return "\n".join(effects._normalize_review_issue_ids(normalized))
    lines = []
    for line in normalized.splitlines():
        line = " ".join(line.split())
        if not line:
            continue
        lines.append(line)
    return "\n".join(sorted(lines))


@dataclass(frozen=True)
class ValidationFailureFingerprintsEffects:
    """Named effects for validation_failure_fingerprints; no entry-point/module access."""
    normalize_validation_failure: Callable[..., object]


def validation_failure_fingerprints(
    effects: ValidationFailureFingerprintsEffects, stage: str,
    output: str,
    worktree: Path | None = None,
) -> tuple[tuple[str, str], ...]:
    normalized = (
        "전체 verify 시간 초과"
        if stage == "verify" and getattr(output, "timed_out", False)
        else effects.normalize_validation_failure(stage, output, worktree)
    )
    failures = normalized.splitlines() if stage == "review" else [normalized]
    fingerprints = []
    for failure in failures:
        digest = hashlib.sha256()
        digest.update(b"loop-manager-validation-failure-v1\0")
        digest.update(stage.encode("utf-8"))
        digest.update(b"\0")
        digest.update(failure.encode("utf-8"))
        fingerprints.append((digest.hexdigest(), failure))
    return tuple(fingerprints)


@dataclass(frozen=True)
class ValidationFailureFingerprintEffects:
    """Named effects for validation_failure_fingerprint; no entry-point/module access."""
    validation_failure_fingerprints: Callable[..., object]


def validation_failure_fingerprint(
    effects: ValidationFailureFingerprintEffects, stage: str,
    output: str,
    worktree: Path | None = None,
) -> tuple[str, str]:
    fingerprints = effects.validation_failure_fingerprints(stage, output, worktree)
    if len(fingerprints) != 1:
        raise ValueError(
            "복수 Review issue는 validation_failure_fingerprints()로 처리해야 합니다."
        )
    return fingerprints[0]


def _deserialize_failure_history(value: object) -> dict[str, dict[str, object]]:
    if value in {None, ""}:
        return {}
    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise RuntimeError("저장된 검증 실패 지문 이력이 유효하지 않습니다.")
    history: dict[str, dict[str, object]] = {}
    for fingerprint, record in parsed.items():
        if not isinstance(fingerprint, str) or not isinstance(record, dict):
            raise RuntimeError("저장된 검증 실패 지문 이력이 유효하지 않습니다.")
        occurrences = record.get("occurrences")
        if type(occurrences) is not int or occurrences < 1:
            raise RuntimeError("저장된 검증 실패 반복 횟수가 유효하지 않습니다.")
        history[fingerprint] = {
            "occurrences": occurrences,
            "sample": str(record.get("sample", "")),
        }
    return history


@dataclass(frozen=True)
class ClearValidationFailureHistoryEffects:
    """Named effects for clear_validation_failure_history; no entry-point/module access."""
    _deserialize_failure_history: Callable[..., object]
    active_execplan: Callable[..., object]


def clear_validation_failure_history(
    effects: ClearValidationFailureHistoryEffects, connection: sqlite3.Connection,
    repo: str,
    stage: str,
) -> None:
    column = {
        "verify": "verify_failure_history",
        "review": "review_failure_history",
    }.get(stage)
    if column is None:
        raise ValueError(f"검증 성공 단계가 유효하지 않습니다: {stage}")
    plan = effects.active_execplan(connection, repo)
    if plan is None:
        raise RuntimeError("지문 이력을 초기화할 활성 ExecPlan을 찾을 수 없습니다.")
    fingerprints = tuple(effects._deserialize_failure_history(plan[column]))
    plan_values = {column: "{}"}
    if stage == "review":
        state = review_correction_state.deserialize(plan["review_correction_state"])
        plan_values["review_correction_state"] = json.dumps(
            review_correction_state.end_episode(state, plan["effective_worker_level"]),
        )
    with runtime_state._transaction(connection):
        runtime_state.RuntimeStateRepository(connection).update_plan(
            repo,
            str(plan["plan_id"]),
            plan_values=plan_values,
            enqueue=False,
        )
        recovery_state.RecoveryRepository(connection).clear_correction_occurrences(
            repo, str(plan["plan_id"]), fingerprints,
        )


@dataclass(frozen=True)
class PrForBranchEffects:
    """Named effects for pr_for_branch; no entry-point/module access."""
    _github_observation_effects_pr_for_branch: Callable[..., object]


def pr_for_branch(effects: PrForBranchEffects, repo: Path, branch: str, url: str | None = None) -> dict[str, object] | None:
    return github_observation.pr_for_branch(effects._github_observation_effects_pr_for_branch(), repo, branch, url)


def _targeted_test_failure_matches(evidence: str) -> list[re.Match[str]]:
    failures = []
    command_spans = [
        (match.start(), match.end())
        for match in _TARGETED_TEST_COMMAND.finditer(evidence)
    ]
    candidates = sorted(
        (
            *_TARGETED_TEST_FAILURE.finditer(evidence),
            *_TARGETED_TEST_EXCEPTION_FAILURE.finditer(evidence),
        ),
        key=lambda match: (match.start(), match.end()),
    )
    for match in candidates:
        if any(start <= match.start() < end for start, end in command_spans):
            continue
        prefix = evidence[max(0, match.start() - 32):match.start()]
        count = re.search(
            r"(?i)(?<![\w])(\d+)(?:\s+(?:tests?|cases?))?\s*$",
            prefix,
        )
        if count is not None and int(count.group(1)) == 0:
            continue
        if re.search(r"(?i)\b(?:no|without)\s+$", prefix):
            continue
        if match.group(0).casefold() in {"실패", "fail", "failed", "failure", "failures", "error", "errors"} and re.match(
            r"(?i)(?:[ \t]*(?:[·/,]|및|와|과)[ \t]*(?:skips?|오류|에러|실패|errors?|failures?))*"
            r"[ \t]+없(?:음|습니다|었습니다|다)(?=[ \t]*(?:[.!;\n]|$))",
            evidence[match.end():],
        ):
            continue
        failures.append(match)
    return failures


@dataclass(frozen=True)
class HasUnresolvedTargetedTestFailureEffects:
    """Named effects for _has_unresolved_targeted_test_failure; no entry-point/module access."""
    _targeted_test_failure_matches: Callable[..., object]


def _has_unresolved_targeted_test_failure(effects: HasUnresolvedTargetedTestFailureEffects, evidence: str) -> bool:
    commands = list(_TARGETED_TEST_COMMAND.finditer(evidence))
    failures = effects._targeted_test_failure_matches(evidence)
    if not failures:
        return bool(commands)
    recovery_rerun = None

    for index, failure in enumerate(failures):
        prior_commands = [
            match
            for match in commands
            if match.end() <= failure.start()
        ]
        if not prior_commands:
            return True
        failed_run = prior_commands[-1]
        if (
            index == 0
            and failed_run.group("label").upper() != "FAILED_COMMAND"
        ):
            return True
        failed_command = " ".join(
            (failed_run.group("command") or "").split()
        )
        if not failed_command:
            return True

        subsequent_commands = [
            match
            for match in commands
            if match.start() >= failure.end()
        ]
        if not subsequent_commands:
            return True
        rerun = subsequent_commands[0]
        rerun_command = " ".join((rerun.group("command") or "").split())
        if (
            rerun.group("label").upper() != "RERUN"
            or rerun_command != failed_command
        ):
            return True
        recovery_rerun = rerun

    if recovery_rerun is None:
        return True
    if commands[-1].start() != recovery_rerun.start():
        return True
    return (
        _TARGETED_TEST_SUCCESS.search(evidence, recovery_rerun.end())
        is None
    )


@dataclass(frozen=True)
class ParseWorkerResultEffects:
    """Named effects for parse_worker_result; no entry-point/module access."""
    _has_unresolved_targeted_test_failure: Callable[..., object]
    reviewer_targeted_test_reason: Callable[..., object]


def parse_worker_result(effects: ParseWorkerResultEffects, message: str, plan: dict[str, object]) -> WorkerPublication:
    """Parse and validate the Worker final-result contract before publishing."""
    markers = ("COMMIT_TITLE", "PR_SUMMARY", "TARGETED_TESTS", "WORKER_OUTCOME")
    message = normalize_markers(message, markers)
    labels = list(re.finditer(r"(?m)^(COMMIT_TITLE|PR_SUMMARY|TARGETED_TESTS|WORKER_OUTCOME): *", message))
    publication_labels = [item.group(1) for item in labels if item.group(1) != "WORKER_OUTCOME"]
    if len(publication_labels) != 3 or set(publication_labels) != set(markers[:3]):
        raise WorkerResultError("Worker publication 선언이 누락되거나 중복됐습니다.")
    fields = {
        item.group(1): message[item.end():labels[index + 1].start() if index + 1 < len(labels) else len(message)].strip()
        for index, item in enumerate(labels) if item.group(1) != "WORKER_OUTCOME"
    }
    title, summary, test_evidence = (fields[key] for key in markers[:3])
    # A wrapped title is still one declared field. Fold its whitespace before
    # passing it to Git so formatting alone does not trigger a Worker retry.
    title = " ".join(title.split())
    if not title:
        raise WorkerResultError("Worker commit 제목이 비어 있습니다.")
    if not summary:
        raise WorkerResultError("Worker PR Summary가 비어 있습니다.")
    if not test_evidence:
        raise WorkerResultError("Worker targeted unit test 증거가 비어 있습니다.")
    if effects._has_unresolved_targeted_test_failure(test_evidence):
        raise TargetedTestFailure(
            "Worker가 해결되지 않은 targeted unit test 실패를 보고했습니다."
        )
    no_test_reason = effects.reviewer_targeted_test_reason(test_evidence)
    if _NO_RUNNABLE_TARGETED_TEST.search(test_evidence) and not no_test_reason:
        raise WorkerResultError(
            "Worker targeted unit test 미실행 사유에서 성공 내역을 제외하고 구체적인 이유를 보고해야 합니다."
        )
    if not (_TARGETED_TEST_SUCCESS.search(test_evidence) or no_test_reason):
        raise WorkerResultError(
            "Worker targeted unit test 통과 결과 또는 실행하지 못한 구체적인 사유가 필요합니다."
        )

    plan_id = str(plan["id"])
    plan_title = str(plan["title"])
    normalized = " ".join(title.split()).casefold().rstrip(" .,:;!?…")
    generic_title_words = {
        "implement", "implementation", "implementations", "change", "changes", "code",
        "update", "updates", "fix", "fixes", "the", "work", "feature", "features",
        "변경", "사항", "구현",
    }
    prohibited = {
        " ".join(plan_title.split()).casefold().rstrip(" .,:;!?…"),
        f"implement {plan_id}".casefold(),
        f"implement {plan_id}: {plan_title}".casefold(),
        f"implement {plan_title}".casefold(),
    }
    if (
        (bool(normalized.split()) and set(normalized.split()) <= generic_title_words)
        or normalized in prohibited
        or re.match(rf"implement\s+(?:execplan\s+)?{re.escape(plan_id.casefold())}\b", normalized)
    ):
        raise WorkerResultError(
            "Worker commit 제목은 ExecPlan 제목이나 일반적인 변경 문구가 아니라 실제 변경을 설명해야 합니다."
        )
    return WorkerPublication(title, summary, test_evidence)


def reviewer_targeted_test_reason(test_evidence: str) -> str | None:
    """Return only an explicit exceptional no-test disposition for review.

    Successful test prose is deliberately not interpreted: the validated Worker
    result and verify checkpoint are the program's evidence for entering Review.
    """
    for line in test_evidence.splitlines():
        for clause in re.split(r"[;|]", line):
            normalized = " ".join(clause.split())
            match = _NO_RUNNABLE_TARGETED_TEST.search(normalized)
            if match is None:
                continue
            if any(command.start() < match.start()
                   for command in _INLINE_TEST_COMMAND.finditer(normalized)):
                continue
            metadata = list(_TARGETED_TEST_METADATA.finditer(normalized))
            start = max(
                (item.end() for item in metadata if item.end() <= match.start()),
                default=0,
            )
            end = min(
                (item.start() for item in metadata if item.start() >= match.end()),
                default=len(normalized),
            )
            reason = normalized[start:end].lstrip(" :;|,.-").rstrip(" :;|,")
            reason_match = _NO_RUNNABLE_TARGETED_TEST.search(reason)
            if reason_match and (
                reason[:reason_match.start()].strip(" :;|,.-")
                or reason[reason_match.end():].strip(" :;|,.-")
            ):
                return reason[:600]
    return None


def _looks_like_reported_test_command(line: str) -> bool:
    """Recognize executable paths and generic commands with test arguments."""
    try:
        words = shlex.split(line.strip())
    except ValueError:
        return False
    if not words:
        return False
    executable = words[0]
    if "/" in executable or "\\" in executable:
        return True
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+", executable):
        return False
    if (executable[0].isupper() and "/" not in executable
            and "\\" not in executable
            and not executable.lower().endswith((".exe", ".cmd", ".bat"))):
        return False
    return any(
        argument.startswith(("-", "./", "../", "/", ".\\", "..\\", "\\"))
        or "/" in argument
        or "\\" in argument
        or re.search(r"\.[A-Za-z0-9]{1,10}$", argument)
        for argument in words[1:]
    )


def reviewer_targeted_test_evidence(test_evidence: str) -> str:
    """Keep Worker-reported commands and outcomes without forwarding raw logs."""
    reason = reviewer_targeted_test_reason(test_evidence)
    if reason:
        return redact_secrets(reason)

    relevant = []
    lines = test_evidence.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        while line.endswith(("\\", "`")) and index + 1 < len(lines):
            line = line[:-1].rstrip() + " " + lines[index + 1].strip()
            index += 1
        if (re.match(r"(?i)^COMMAND:\s*\S", line)
                or _TARGETED_TEST_COMMAND.search(line)
                or _INLINE_TEST_COMMAND.search(line)
                or _looks_like_reported_test_command(line)):
            relevant.append(line)
            index += 1
            continue
        for clause in re.split(r"[;|]", line):
            if (_TARGETED_TEST_SUCCESS.search(clause)
                    or _TARGETED_TEST_FAILURE.search(clause)
                    or _TARGETED_TEST_EXCEPTION_FAILURE.search(clause)):
                relevant.append(clause.strip())
        index += 1
    return redact_secrets("\n".join(relevant) if relevant else
                          'Worker targeted test 결과를 요약할 수 없습니다.')


@dataclass(frozen=True)
class ImmutableGithubBlobUrlEffects:
    """Named effects for immutable_github_blob_url; no entry-point/module access."""
    github_repository: Callable[..., object]


def immutable_github_blob_url(effects: ImmutableGithubBlobUrlEffects, origin_url: str, commit_sha: str, relative_path: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", commit_sha):
        raise RuntimeError("계획 commit SHA가 유효하지 않습니다.")
    repository = effects.github_repository(origin_url)
    encoded_path = quote(relative_path, safe="/")
    return f"https://github.com/{repository}/blob/{commit_sha}/{encoded_path}"


def execplan_purpose(plan_text: str) -> str:
    body = re.sub(r"(?s)\A---.*?---\s*", "", plan_text, count=1)
    match = re.search(r"(?ms)^## Purpose\s*\n+(.*?)(?=^## |\Z)", body)
    if not match or not match.group(1).strip():
        raise RuntimeError("ExecPlan Purpose를 찾을 수 없습니다.")
    return " ".join(match.group(1).split())
