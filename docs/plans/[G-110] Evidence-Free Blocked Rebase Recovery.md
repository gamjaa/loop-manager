---
id: G-110
title: Evidence-Free Blocked Rebase Recovery
sources:
  - docs/requirements/[R-048] Evidence-Free Managed Rebase Recovery.md
depends_on:
  - G-109
complexity: high
risk: critical
priority: critical
---

## Purpose

G-088처럼 `managed_worktree_conflict`로 차단된 뒤 계획 입력이 바뀌고 사용자가 관리 rebase를 직접 해결한 작업을 복구한다. 현재 G-101의 좁은 legacy 진단 selector와 일반 worktree branch 검사가 수동 해결·detached 진행 상태를 가로막는다. G-109의 현재 상태 서비스로 이 rebase 원인만 재평가하고 기존 작업을 잇는다.

## Acceptance Criteria

- `PLAN_BLOCKED` 및 계획과 연결된 rebase 완료/소유권 오탐 차단은 실제 저장 원인·대상 계획과 G-109의 현재 상태로 재평가한다. 특정 R-045 오류 문구, 과거 교정 turn·stage 승인·receipt·replay·journal의 완전성 또는 과거 session 일치가 없다는 이유로 복구를 거부하지 않는다. 저장된 원인과 현재 작업을 연결할 수 없는 차단은 임의로 해제하지 않는다.
- 같은 plan·repository·reservation·등록 worktree·소유 branch·target base·원격 기대값과 현재 rebase metadata가 일치하면 일반 branch 검사보다 먼저 소유 rebase를 인식한다. detached HEAD·미해결 충돌을 같은 worktree에서 보존해 해결·재개하고, 사용자가 stage한 해결은 현재 범위를 검사해 Manager continue로 연결한다. 이미 완료됐으면 continue/rebase를 반복하지 않고 완료를 채택한다.
- G-088 재현에서 차단 후 ExecPlan/근거 입력 변경과 안전한 수동 해결·완료를 확인하면 `managed_worktree_conflict` 원인만 해제한다. 별도 Review 실패, guidance/사용자 결정, PR reopen 조건, 실제 Agent 범위 위반, 다른 계획·planning·project 안전 원인과 실패·알림 감사는 보존한다. 충돌 해소 자체를 사용자 승인·Review 성공 또는 구현 완료로 해석하지 않는다.
- 복구는 프로젝트의 직렬 실행 자리와 dependency gate를 지킨다. 다른 활성 실행이나 planning이 자리를 소유하면 대기하고 투영·session·진단을 교체하지 않는다. 프로젝트 차단은 해당 rebase 원인에 대한 현재 안전 재검증으로 해제 가능한 경우에만 해제한다. 차단이 남은 프로젝트에서 Git 쓰기·게시를 허용하는 R-046 예외는 G-107의 범위다.
- 후보나 입력이 바뀐 복구는 G-109의 Worker 현재 입력·최종 diff 확인 및 targeted test, Manager 전체 verify와 Reviewer 경로를 거친다. 같은 branch·worktree·session을 보존하고 필요한 CLI fallback만 기존 계약으로 처리한다. PR 유무에 따른 기존 게시 ownership·lease·PR identity 조건을 유지한다.
- Git 완료 후 SQLite 반영 전, 원인 해제 transaction 중 또는 Worker 재확인 전 중단돼도 다시 현재 상태를 검사해 한 번만 완료·해제를 적용한다. 같은 해결 결과에 교정 turn·stage·rebase·알림을 중복하지 않는다. 원격 삭제·재작성·경쟁, 소유권 불명확, 범위 밖 파일/index·flags 또는 다른 operation은 차단과 작업물을 보존하며 자동 게시하지 않는다.

## Implementation

- `rebase_block_reconciliation.py`의 legacy 진단 selector와 실제 현재 상태 검사·원인 해제를 분리한다. `loop_manager.py::_reconcile_rebase_evidence_block`, `resume_plan_blocks`, `resume_project_block_if_safe`, `resume_blocked_execplan_if_inputs_changed`, `_resume_interrupted_blocked_rebase`가 동일한 대상 계획의 현재 rebase 복구 서비스를 사용하게 한다.
- 일반 `make_worktree`/branch·Agent scope gate에서 detached HEAD를 거부하기 전에 현재 소유 rebase를 확인한다. `allow_rebase=True` 또는 metadata/path 존재만으로 통과하지 않고 G-108의 identity·원격·범위 판정을 적용한다. 수동 작업 이후 예전 correction progress marker 불일치를 자동 Worker 증거 실패로 취급하지 않으며 실제 현재 scope 검사는 유지한다.
- `scoped_blocking.py`, `recovery_state.py`, `runtime_state.py`의 transaction 경계에서 대상 원인만 해제하고 필요 phase·후보 무효화를 원자적으로 반영한다. G-092의 Agent snapshot 기준선/flags 오탐 수정과 분리하며, 그 계획은 수용된 rebase 상태만 소비한다. 기존 실패·알림 이력은 유지하고 기존 재개 사건 계약을 사용한다.
- 수동 관리 rebase 복구와 원인별 해제의 장기 제품 규칙을 같은 구현 PR의 `docs/PRODUCT.md`에 반영한다. G-058·G-088의 계획 본문·guidance 결정·기존 작업물과 다른 계획의 근거 문서는 수정하지 않는다.

## Validation

- Worker는 `tests/test_execution_handlers.py`, `tests/test_scoped_blocking.py`, `tests/test_recovery_state.py`, `tests/test_origin_isolation.py`, `tests/test_loop_manager.py`에서 직접 바뀐 selector·해제 transaction·실행 자리 unit case만 실행한다. 실제 Git lifecycle은 Manager 전체 verify에 맡긴다.
- G-088 형태의 plan block에서 입력 변경 후 미해결 detached rebase, 사용자 stage 완료, 사용자 continue 완료를 각각 다룬다. 현재 검사가 통과한 증거 없는 결과는 진행하고 실제 scope·원격·target 불일치는 거부한다. 별도 Review/guidance/Agent/planning 차단이 남으면 해당 원인과 원문·session을 보존한다.
- 실제 Git/SQLite로 PR 없음/기존 PR, 여러 충돌의 자동·수동 혼합 해결, 계획·Requirement 변경, journal이 없는 정상 결과, Git 완료 뒤 저장 전 중단, 해제 중 rollback·재시작, 원격 경쟁을 검증한다. 이후 현재 후보 Worker 확인·targeted test 결과·전체 verify·Review와 기존 게시 gate까지 이어져야 한다.
- Manager 전체 verify는 다른 계획·planning의 실행 자리 보존, 차단·재개 알림 중복 방지, actual Agent scope 위반의 차단 유지, 기본 worktree 보존 및 정확한 lease를 확인한다.

## Progress

- [x] 저장 rebase 차단 selector와 현재 상태 복구 연결
- [x] detached·수동 stage/완료 재개와 원인별 원자 해제
- [x] G-088 형태의 입력 변경·수동 결과와 다른 차단 보존·중단/rollback 회귀 unit 사례 구현
- 최신 base로 rebase한 뒤 autostash 복원에서 같은 파일 끝에 추가된 테스트 클래스가 충돌했다. 기존 checkpoint 게시 테스트와 G-110 테스트를 모두 보존해 병합했으며, 직접 변경한 복구·scope·준비·원인 해제 경계의 targeted unit test 26개를 다시 실행해 모두 통과했다.
- Manager verify 피드백에서 초기 context의 PR 응답 누락, 수동 continue 전 계획 파일의 unstaged 변경과 최초 교정 소진 차단의 동일 상태 재개를 확인했다. fixture의 PR·stage·실패 기록 기대값을 수정하고, 비교 가능한 차단 기준선의 입력·선택 작업 상태가 같은 경우 재개를 억제했다. 관련 targeted unit test 30개의 최종 실행은 모두 통과했으며 실제 Git lifecycle 실패 사례는 Manager의 후속 verify에서 다시 검증한다.
- Worker는 좁은 unit case만 실행한다. 실제 Git lifecycle과 전체 verify·Review는 Worker 종료 뒤 Manager 검증 경로에 남긴다.

## Decision Log

- 이 계획은 R-048의 수동 복구와 rebase 원인 해제만 담당한다. G-092는 R-023의 격리된 Agent 범위·Manager 기준선·index flags 오탐을 계속 담당하고, G-106·G-107은 차단 중 새 원격 입력 반영·checkpoint/진단 게시를 담당한다.
- G-088·G-092에 의존하지 않는다. 차단된 기존 계획을 먼저 Merge해야 복구를 구현할 수 있는 순환을 만들지 않고 G-108 → G-109 → G-110으로 검증 가능한 변경을 진행한다.
- 저장 원인 selector는 충돌 원인 또는 대상 rebase에 연결된 구조화 진단만 선택하며, Git 관측·해제는 G-109 현재 상태 서비스와 원자적 SQLite 경계에서 처리한다. 과거 세션·진전 marker·occurrence history는 gate로 사용하지 않는다.
- 복구 자체는 Git 쓰기 없이 현재 상태를 두 번 관측한다. 진행 중 operation은 보존해 기존 Manager continue/Worker 경로로 연결하고, 완료 후보는 채택과 대상 원인 해제를 같은 transaction으로 적용한다. 별도 차단 원문·session·실패 이력과 검증 전 결과는 감사로 보존한다.
- 최초 교정 소진 차단의 실제 Agent 결과 snapshot과 입력 기준선은 동일 실패 반복을 억제하는 데만 사용한다. ownership은 계속 현재 상태 서비스가 판정하며, 과거 세션·progress 일치나 기준선·실패 기록의 존재를 복구에 요구하지 않는다.
