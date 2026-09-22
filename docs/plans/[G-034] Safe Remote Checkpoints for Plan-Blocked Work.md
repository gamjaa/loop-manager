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

- ExecPlan별 checkpoint phase, expected/local/remote OID, commit identity, push result와 미게시 reason을 additive SQLite migration으로 저장하고 PLAN_BLOCKED 전환 transaction 이후 dispatcher가 처리하게 한다.
- G-012/G-031의 workspace identity와 persisted Agent snapshot 비교를 read-only safety assessor로 분리해 checkpoint 허용 path, Git operation, index flags, symlink/ignored 상태와 shared metadata를 결정적으로 반환하게 한다.
- isolated index 또는 명시적인 pathspec으로 허용된 변경만 stage하고 tree를 사전 계산해, 저장된 checkpoint identity와 같은 tree·parent·message이면 기존 commit을 재사용한다. 사용자 index 상태를 임의로 넓히지 않는다.
- G-014/G-026의 publication ownership과 remote OID helper를 재사용하되 checkpoint 전용 push policy를 별도 함수로 둔다. PR 없는 branch, fast-forward와 좁은 rebase lease 예외 외에는 기존 publication 권한을 확장하지 않는다.
- 모든 PLAN_BLOCKED row의 pending checkpoint를 현재 실행 slot과 분리해 재시도하고 결과가 확정된 뒤 G-018 outbox event를 생성한다. 다음 독립 READY 선택은 checkpoint 네트워크 성공을 기다리지 않는다.
- unblock/rebase/publication 진입 시 저장된 remote checkpoint OID와 실제 remote를 비교해 외부 mutation을 감지하고, 불일치하면 local 작업을 보존한 채 구조화된 block reason을 갱신한다.

## Validation

- Worker는 새 blocked-checkpoint test module과 remote publication helper의 직접 관련 test case만 targeted unit test로 실행한다.
- 안전한 dirty 변경, 이미 commit된 변경과 변경 없음에서 정확한 tree/commit/OID가 기록되고 빈 commit이나 범위 밖 staging이 없는지 검증한다.
- 신규 remote branch, 기존 fast-forward branch, 같은 OID 재실행, commit 후·push 후 중단과 transient push 재시도의 멱등성을 검증한다.
- 예상하지 못한 remote commit, lease 실패, branch protection과 권한 거부가 force push나 원격 덮어쓰기 없이 local 작업을 보존하는지 검증한다.
- 기존 Open/Draft/Closed/Merged PR이 있는 branch에는 checkpoint와 새 PR을 만들지 않는지 검증한다.
- detached/wrong branch, 미해결 rebase·merge·cherry-pick·revert, symlink, ignored/unowned/staged 범위 위반, index flag와 shared ref mutation을 검증한다.
- PROJECT_BLOCKED, planning_error, 등록·branch 생성 전 실패와 불명확 legacy block이 remote write를 수행하지 않는지 검증한다.
- 성공·미게시 BLOCKED 알림 payload와 중복 방지, checkpoint 실패 중 독립 READY 진행을 검증한다.
- 해제 뒤 같은 branch/worktree/session 재개, remote 외부 변경 거부와 verify·Review 뒤 최초 PR 생성까지 검증한다.
- 전체 verify에서 G-014 PR 없는 publication recovery, G-018 outbox, G-020 승인 patch, G-026 PR conflict recovery와 G-031 scoped scheduling이 약화되지 않아야 한다.

## Progress

- [ ] durable checkpoint state와 PLAN_BLOCKED dispatcher 구현
- [ ] workspace·diff·Git operation safety assessor 구현
- [ ] 결정적 checkpoint commit과 멱등한 remote push 구현
- [ ] unblock/publication 및 알림 통합
- [ ] checkpoint 안전·복구·회귀 테스트 추가

## Decision Log

- 원격 checkpoint는 사용자 검토용 백업이며 품질 gate를 통과한 publication이 아니다. PR 생성과 PR_OPEN 전이는 기존 verify·Review 성공 경계에만 남긴다.
- G-031이 plan/project scope와 다중 PLAN_BLOCKED를 확정한 뒤 활성화해 unsafe project block을 실수로 게시 대상에 포함하지 않는다.
- 원격 branch가 이미 존재하는 경우의 권한은 일반 publication과 공유하지 않고 expected OID에 결합한다. rebase 복구의 기존 좁은 lease 예외만 재사용한다.
