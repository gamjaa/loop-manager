---
id: G-077
title: Guidance Checkpoint Ownership Reconciliation
sources:
  - docs/PRODUCT.md
depends_on:
  - G-020
  - G-031
  - G-034
  - G-067
complexity: high
risk: critical
priority: critical
---

## Purpose

Worker의 AGENTS.md guidance 제안이 사용자 결정을 기다리는 동안, Loop Manager가 만든 checkpoint와 Git ref 변경을 다음 실행에서 Agent 범위 위반이나 외부 공유 ref 변경으로 오인하지 않게 한다. 이미 게시된 checkpoint의 소유권을 증명할 수 있는 차단 상태도 원래의 guidance 승인 대기로 안전하게 복구한다.

## Acceptance Criteria

- 실제 Worker 결과 처리 경로에서 유효한 guidance 제안을 발견하면, 제안 전에 이미 수정한 허용 코드·테스트와 새 ExecPlan 제안을 함께 보존한다. 검증된 Worker 역할과 실행 전·결과 Git snapshot, 작업 공간과 Worker 세션도 일관되게 보존한다. 저장된 Agent snapshot이 있는데 역할이 비어 있는 상태를 만들지 않으며, 사용자 결정 전 AGENTS.md 수정·전체 verify·Review·PR 게시는 시작하지 않는다.
- 변경이 ExecPlan 제안만 있는 경우와 기존 코드·테스트에 제안이 더해진 경우 모두, 안전한 허용 변경 전체가 한 checkpoint tree에 정확히 포함된다. commit·push 뒤 프로세스를 재시작해도 같은 ExecPlan은 guidance_approval_required 상태로 남고, checkpoint commit·push·차단 알림을 중복 생성하거나 managed_worktree_scope_violation으로 재차단하지 않는다.
- Loop Manager가 게시한 소유 branch의 정확한 checkpoint OID와 그에 따른 원격 추적 ref 변경은 다음 checkpoint 요청이나 범위 재검사에서도 소유된 변경으로 확인한다. 요청이 새 차단 사유로 교체되거나 commit·push·상태 저장 사이에서 중단돼도 게시 증거와 현재 원격 OID를 대조해 같은 결론을 낸다.
- Loop Manager가 직접 수행하고 증거를 저장한 base fetch의 ref 변경도 Agent 작업으로 귀속하지 않는다. 소유권을 증명하지 못한 원격 ref·tag·다른 branch 변경, 예상 밖 원격 OID와 외부 worktree 변경은 기존 PROJECT_BLOCKED 안전 경계를 유지한다. ref namespace 전체를 허용하거나 snapshot을 무조건 현재 값으로 덮어쓰지 않는다.
- 다른 ExecPlan의 PR Merge를 확인한 프로그램이 해당 관리 worktree를 정리한 경우에도, 저장된 Merge 결과·workspace 소유권·정리 전후 Git 상태로 그 변경을 증명하면 guidance 대기 계획의 Agent 범위 위반이나 checkpoint 외부 worktree 변경으로 오인하지 않는다. 증명되지 않은 worktree 생성·삭제·수정은 계속 차단한다.
- 사용자 결정은 기존 User Decision의 Decision과 Revision으로만 반영한다. 유효한 결정 뒤에는 보존된 branch·worktree·Worker 세션으로 재개하고, 승인된 patch만 적용하며 기존 targeted test·전체 verify·Review·PR 순서를 따른다.
- G-058처럼 이미 오탐으로 이중 차단된 기록은 관리 worktree 소유권, checkpoint commit identity와 tree, 저장된 게시 OID와 현재 원격 head, guidance 제안·결정 및 AGENTS.md 불변성을 모두 증명할 수 있을 때에만 프로젝트 차단과 잘못된 계획 차단을 해제한다. 복구 과정에서 User Decision을 생성하거나 변경하지 않는다. pending 또는 사용자가 직접 확정한 결정을 그대로 보존한다. 증거가 없거나 충돌하면 기존 작업과 차단을 보존하고 원인을 기록한다.
- 이 수정으로 다른 PLAN_BLOCKED 원인, 실제 Agent 범위 위반, 공유 ref 보호, 독립 READY 선택과 기존 checkpoint 원격 복구 조건이 약화되지 않는다.

## Implementation

- execution_handlers.py의 일반 Worker 결과와 rebase 교정 결과 경로에서 범위 검증, guidance 제안 판정, Agent scope 정리 순서를 맞춘다. guidance 차단 전이가 검증된 역할과 snapshot을 원자적으로 보존하게 하고, snapshot만 다시 저장하는 경로에는 역할·소유권 불변식을 적용한다.
- blocked_checkpoint_state.py와 loop_manager.py의 checkpoint 게시·재개 경계에서 관리자가 소유한 local/remote OID와 ref 갱신 증거를 지속 상태에 결합한다. 새 요청이나 재시작에서 정확한 소유 ref만 허용하며, 중단 시점별 재조정은 저장된 phase와 원격 관측 결과를 사용한다.
- 다른 계획의 Merge 후 관리 worktree 정리 사실을 SQLite 실행 이력과 Manager Git 작업 증거에 결합하고, 대기 중인 guidance snapshot 검증에서 정확한 해당 worktree 변경만 허용한다.
- 기존 G-058 사고에도 적용할 수 있는 검증 기반 상태 재조정 경로를 마련한다. 특정 ExecPlan ID나 commit을 코드에 고정하지 않고, 검증된 checkpoint와 guidance 승인 기록에서 원래의 계획 차단 상태를 복원한다. 소유권이 불확실하면 운영자가 수동으로 SQLite 값을 고쳐 차단을 우회하도록 요구하지 않는다.

## Validation

- Worker는 수정한 경계에 직접 대응하는 tests/test_execution_handlers.py, tests/test_guidance_approval.py, tests/test_blocked_checkpoint.py와 tests/test_scoped_blocking.py의 새 targeted unit case만 실행한다.
- 실제 Worker 처리 순서에서 문서만 있는 경우와 기존 코드·테스트 변경에 제안을 추가한 경우를 모두 검증한다. 제안 기록, scope 정리, 차단 저장, checkpoint tree의 정확한 파일 집합, 게시와 다음 실행까지 이어지는 시나리오를 전체 verify에 포함하고, 두 번째 checkpoint 요청의 local_oid가 비어 있는 경우와 재시작·중단 시점도 확인한다.
- 소유 checkpoint ref와 증명된 base fetch는 통과하고, 다른 원격 ref·tag·branch 변경, 게시 OID 불일치와 위조된 소유권은 PROJECT_BLOCKED로 남는지 확인한다.
- 다른 계획의 검증된 Merge·worktree 정리는 통과하고, 임의 worktree 삭제·수정은 차단되는지 확인한다.
- 이미 이중 차단된 상태의 증명 가능한 복구와 증명 불가 시 보존, guidance User Decision 불변, 동일 알림·commit·push의 중복 방지를 확인한다.
- 전체 verify에서 기존 guidance 승인, Agent scope, 원격 checkpoint, 계획별 차단 해제와 PR 게시 회귀가 없어야 한다.

## Progress

- [ ] Worker guidance 차단의 역할·snapshot 보존 경계 수정
- [ ] 관리자 소유 checkpoint ref와 base fetch 재조정 구현
- [ ] 기존 이중 차단 기록의 증명 기반 복구 경로 구현
- [ ] 실제 실행 순서 및 안전 거부 회귀 시나리오 추가

## Decision Log

- PRODUCT의 checkpoint OID 보존, guidance 결정별 차단 해제와 허용되지 않은 공유 ref만 프로젝트 차단한다는 기존 규칙을 구현한다. 새로운 사용자 동작이나 AGENTS.md 권한을 추가하지 않는다.
- checkpoint 게시 뒤의 ref 소유권 판정은 ARCHITECTURE.md의 Architecture Change Policy 중 branch·commit·push 경계와 맞닿아 있다. 구현이 그 경계를 바꾸는 것으로 판정되면 사용자 승인을 받기 전에는 해당 변경을 진행하지 않고 ExecPlan을 BLOCKED로 둔다.
- G-058의 guidance 제안과 User Decision은 이 계획의 변경 대상이 아니다. 프로젝트가 PROJECT_BLOCKED인 동안 자동 Worker를 시작하거나 안전 gate를 우회하지 않는다.

- 재개 시 확인: 이전 자동 승인 검토에서 checkpoint 영속 상태 형식과 branch/ref 소유권 판정 변경이 거부되었으며, 현재 문서에는 이를 승인한 사용자 결정이 없다. Architecture Change Policy에 따라 해당 구현을 보류한다. 승인 대상은 저장된 게시 OID를 이용한 후속 checkpoint ref 소유권 판정, 검증된 Merge worktree 정리 증거의 반영, checkpoint·게시·guidance 증거를 이용한 기존 이중 차단 복구다. 기존 공유 ref 보호와 증명 불가 시 차단 보존은 유지해야 한다.
- 미해결 검증 범위: 실제 코드·테스트 파일이 포함된 checkpoint tree와 게시 후 재시작 중복 방지는 아직 검증하지 못했다. 기존 Worker 상태 보존 단위 테스트의 합성 snapshot은 이 Acceptance Criteria를 충족하는 증거로 간주하지 않는다. AGENTS.md 규칙 변경은 필요하지 않으며, 이 기록은 사용자 승인이나 완료 판정을 대신하지 않는다.
