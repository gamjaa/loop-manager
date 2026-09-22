---
id: G-034
title: Safe Remote Checkpoints for Plan-Blocked Work
sources:
  - docs/requirements/[R-015] Remote Checkpoint for Safely Blocked ExecPlans.md
depends_on:
  - G-031
  - G-049
complexity: high
risk: critical
priority: critical
---

## Purpose

PR이 없는 `PLAN_BLOCKED` ExecPlan의 관리 branch와 worktree가 Loop Manager 소유이며 변경 범위가 안전하다고 코드로 증명될 때, 현재 작업을 같은 이름의 원격 branch에 멱등한 checkpoint로 보존한다. checkpoint는 검증 성공이나 PR이 아니며, `PROJECT_BLOCKED`와 소유권·범위가 불명확한 작업은 원격에 게시하지 않는다.

## Acceptance Criteria

- PLAN_BLOCKED 상태와 구조화된 차단 원인을 먼저 SQLite에 commit한 뒤에만 checkpoint 검증, commit과 push를 시작하며 checkpoint 실패가 차단 상태 저장을 되돌리지 않는다.
- PR이 없는 대상만 checkpoint 후보로 삼는다. 동일 head branch에 Open, Draft, Closed 또는 Merged PR이 있으면 새 PR·대체 PR·checkpoint PR을 만들지 않고 기존 PR 복구 정책을 유지한다.
- 저장된 ExecPlan branch, deterministic worktree path, workspace ownership, worktree branch와 HEAD가 모두 일치하고 detached HEAD가 아니어야 한다.
- merge, rebase, cherry-pick 또는 revert가 진행 중이거나 ref 소유권을 확인할 수 없으면 commit과 push를 수행하지 않는다.
- commit되지 않은 변경은 Worker에게 허용된 path와 G-020에서 승인된 정확한 guidance patch 범위 안에만 있어야 한다. symlink, ignored file, 범위 밖 staged entry, index flag와 외부 worktree·공유 ref 변경은 포함하지 않는다.
- 안전한 dirty 변경이 있으면 Loop Manager 코드가 검증된 path만 stage해 checkpoint commit을 만들고 기존 staged/unstaged 경계를 숨기거나 범위 밖 파일을 `git add -A`로 포함하지 않는다.
- checkpoint commit 제목은 ExecPlan ID와 BLOCKED checkpoint임을 식별하고 본문에는 구조화된 scope/reason code, 판단 근거와 정렬된 포함 path를 결정적인 형식으로 기록한다. AI Agent가 commit message, commit 또는 push를 수행하지 않는다.
- 모든 변경이 이미 commit됐거나 변경이 없으면 빈 checkpoint commit을 만들지 않고 안전하게 확인된 현재 HEAD를 게시·기록한다.
- remote branch가 없으면 동일한 Loop Manager 소유 branch를 생성한다. remote가 있으면 저장된 expected OID와 일치하고 fast-forward인 경우에만 push한다.
- PR이 없는 동일 소유 branch의 기존 rebase 복구로 local commit이 바뀐 경우에만 관측한 expected OID를 지정한 `--force-with-lease`를 허용한다. 일반 force, 예상하지 못한 remote commit 덮어쓰기와 다른 branch push는 거부한다.
- local commit 생성 뒤 또는 push 성공 뒤 프로세스가 중단돼도 저장된 checkpoint identity와 local/remote OID를 대조해 같은 commit, push와 알림을 중복 생성하지 않는다.
- transient network/Git push 실패는 기존 bounded retry를 적용하고, 소진 뒤에도 local commit과 worktree를 보존해 다음 실행에서 같은 checkpoint를 재시도한다. branch protection과 권한 거부는 우회하지 않는다.
- checkpoint 성공 여부와 무관하게 ExecPlan은 PLAN_BLOCKED로 남고 독립 READY 선택을 막지 않는다. safety 검증에서 project-scope 원인이 새로 발견되면 G-031에 따라 PROJECT_BLOCKED로 승격한다.
- PROJECT_BLOCKED, planning_error, 등록 실패, branch 생성 전 실패와 소유권을 증명할 수 없는 legacy BLOCKED에는 remote branch를 만들거나 갱신하지 않고 구체적인 미게시 reason을 저장한다.
- G-018 BLOCKED 알림은 checkpoint 결과 확정 뒤 생성한다. 성공 payload에는 immutable branch link와 commit OID를, 미게시 payload에는 local-only 보존 reason을 포함하며 기존 사건 identity로 중복을 막는다.
- plan block 해제 뒤 기존 local worktree와 branch에서 작업을 재개한다. remote checkpoint의 외부 변경을 자동 merge·rebase·덮어쓰지 않고, 최종 verify와 Review 성공 후 같은 branch에서 최초 PR을 생성한다.
- 장기 제품 규칙을 R-015에 맞게 `docs/PRODUCT.md`에 갱신하되 Requirement와 ARCHITECTURE는 수정하지 않는다.

## Implementation

- 새 `blocked_checkpoint.py`에 read-only safety assessment, deterministic tree/commit identity와 checkpoint 전용 push policy를 두고, 새 `blocked_checkpoint_state.py` repository가 phase, expected/local/remote OID, commit identity, 결과와 미게시 reason을 additive하게 저장한다.
- PLAN_BLOCKED 전이는 `runtime_state.py`와 `scoped_blocking.py`의 기존 원자적 경계를 사용하고, 같은 transaction 뒤 durable checkpoint request만 기록한다. checkpoint dispatcher는 active `ExecutionPhaseCoordinator`와 별도로 pending request를 처리해 네트워크 성공이 execution slot이나 독립 READY 선택을 점유하지 않게 한다.
- G-012/G-031의 workspace identity 및 persisted Agent snapshot helper는 주입형 safety port adapter가 호출한다. domain assessor는 허용 path, Git operation, index flags, symlink/ignored 상태와 shared metadata를 typed verdict로 반환하고 raw SQLite나 자유 형식 오류를 읽지 않는다.
- isolated index 또는 명시적 pathspec으로 검증된 변경만 stage하고 tree를 사전 계산한다. G-014/G-026 remote ownership helper는 별도 publication port로 감싸며 PR 없는 branch·fast-forward·기존 좁은 rebase lease 이외 권한은 공유하지 않는다.
- checkpoint 결과 확정 뒤 notification outbox가 저장된 structured payload를 읽도록 연결한다. unblock과 G-049 publication preflight에는 remote checkpoint observation port만 추가해 외부 mutation을 검출하고 phase sequencing을 checkpoint 모듈에 복제하지 않는다.
- `loop_manager.py`는 repository/Git/GitHub/outbox adapter wiring과 project-level dispatcher 호출만 소유하고 checkpoint commit 정책이나 상태 전이를 다시 구현하지 않는다.

## Validation

- Worker는 새 `tests/test_blocked_checkpoint.py`, `tests/test_blocked_checkpoint_state.py`와 remote publication adapter의 직접 관련 targeted case만 실행한다.
- 안전한 dirty 변경, 이미 commit된 변경과 변경 없음에서 정확한 tree/commit/OID가 기록되고 빈 commit이나 범위 밖 staging이 없는지 검증한다.
- 신규 remote branch, 기존 fast-forward branch, 같은 OID 재실행, commit 후·push 후 중단과 transient push 재시도의 멱등성을 검증한다.
- 예상하지 못한 remote commit, lease 실패, branch protection과 권한 거부가 force push나 원격 덮어쓰기 없이 local 작업을 보존하는지 검증한다.
- 기존 Open/Draft/Closed/Merged PR이 있는 branch에는 checkpoint와 새 PR을 만들지 않는지 검증한다.
- detached/wrong branch, 미해결 rebase·merge·cherry-pick·revert, symlink, ignored/unowned/staged 범위 위반, index flag와 shared ref mutation을 검증한다.
- PROJECT_BLOCKED, planning_error, 등록·branch 생성 전 실패와 불명확 legacy block이 remote write를 수행하지 않는지 검증한다.
- `tests/test_notifications.py`의 targeted case로 성공·미게시 payload와 중복 방지를, scheduler adapter case로 checkpoint 실패 중 독립 READY 진행과 execution slot 비점유를 검증한다.
- `tests/test_execution_handlers.py`의 targeted case로 해제 뒤 같은 branch/worktree/session 재개, remote 외부 변경 거부와 verify·Review 뒤 최초 PR 생성까지 검증한다.
- 전체 verify에서 G-014 PR 없는 publication recovery, G-018 outbox, G-020 승인 patch, G-026 PR conflict recovery와 G-031 scoped scheduling이 약화되지 않아야 한다.

## Progress

- [ ] checkpoint domain assessor와 durable request/result repository 구현
- [ ] workspace·diff·Git operation safety port 및 결정적 tree 구성 구현
- [ ] checkpoint 전용 remote policy와 멱등 commit/push 구현
- [ ] project dispatcher, unblock/publication observation과 outbox 통합
- [ ] checkpoint state/domain/execution adapter targeted 회귀 테스트 추가

## Decision Log

- 원격 checkpoint는 사용자 검토용 백업이며 품질 gate를 통과한 publication이 아니다. PR 생성과 PR_OPEN 전이는 기존 verify·Review 성공 경계에만 남긴다.
- G-031이 plan/project scope와 다중 PLAN_BLOCKED를 확정한 뒤 활성화해 unsafe project block을 실수로 게시 대상에 포함하지 않는다.
- 원격 branch가 이미 존재하는 경우의 권한은 일반 publication과 공유하지 않고 expected OID에 결합한다. rebase 복구의 기존 좁은 lease 예외만 재사용한다.
- Post-refactor revalidation: checkpoint는 G-049 publication handler의 하위 단계가 아니라 최종 PLAN_BLOCKED 뒤의 별도 durable dispatcher다. active execution context에는 remote observation seam만 추가해 checkpoint 재시도와 phase 제어를 결합하지 않는다.
