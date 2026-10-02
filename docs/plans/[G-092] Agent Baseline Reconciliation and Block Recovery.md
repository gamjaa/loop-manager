---
id: G-092
title: Agent Baseline Reconciliation and Block Recovery
sources:
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
  - docs/requirements/[R-048] Evidence-Free Managed Rebase Recovery.md
depends_on:
  - G-089
  - G-091
  - G-110
complexity: high
risk: high
priority: critical
---

## Purpose

Agent 시작 snapshot 뒤에 Manager의 검증된 Git 작업이 있어도 현재 기준선은 작업별 전후 증거로 갱신되지 않는다. 낡은 기준선 때문에 생긴 기존 `PLAN_BLOCKED`·`PROJECT_BLOCKED`도 정확한 과거 snapshot 복원을 요구하면 안전하게 해제할 수 없다.

## Acceptance Criteria

- G-090/G-091이 증명한 fetch·worktree 등 Manager 전이와 G-109가 현재 상태로 수용한 rebase 중 실제 Git 상태와 일치하는 부분만 G-089의 사용 자원 기준선에 반영한다. rebase로 관리 HEAD·소유 branch ref·index가 바뀐 뒤 재시작해도 원래 Agent의 허용 파일 변경과 session을 보존한다. rebase의 과거 turn·stage 승인·receipt·replay·journal 누락 자체로 거부하지 않으며 현재 소유권·범위 판정이 불확실하거나 예상 밖 영향권 변화가 섞이면 차단한다. rebase 이외 Manager 작업의 증거 검사는 유지한다.
- 현재 소유 상태가 확인된 관리 rebase에서 허용된 충돌 파일을 해결한 뒤 index stage 1/2/3이 stage 0으로 바뀌는 정상 전이(`git ls-files -v`의 `M → H` 포함)를 `index flags` 위반으로 판정하지 않는다. 자동·수동 해결 모두 충돌 stage와 실제 보안 관련 index flags를 구분하며 `skip-worktree`·`assume-unchanged`의 예상 밖 설정·해제, 범위 밖 파일·index 변경과 현재 소유 범위를 확정할 수 없는 충돌 수정은 계속 차단한다. 이 기준은 신규 Worker 결과와 저장된 scope 위반 재검증에 동일하게 적용한다.
- 저장된 차단의 유일한 원인이 격리가 증명된 실행의 무관한 변화 또는 Manager 작업의 낡은 기준선이면, 현재 선택 worktree identity·소유권·허용 변경, 사용 중인 ref·Git 상태와 checkpoint를 재확인해 해당 차단만 해제한다. 격리를 증명할 수 없는 과거 실행의 무관한 변화는 외부 변경으로 추정해 해제하지 않는다.
- `PLAN_BLOCKED`의 소유 rebase가 진행 중이면 G-110의 현재 plan·repository·branch·worktree·target base·rebase metadata·원격 기대 OID·파일/index 범위 판정을 사용해 detached HEAD를 다룬다. 이 계획은 정상 충돌 stage 전이 또는 낡은 기준선 때문에 생긴 Agent scope 차단만 재평가한다. 기존 Worker session과 staged 해결을 보존하고 PR 없는 checkpoint 및 기존 PR의 재개를 공통 서비스로 연결하며, 별도의 rebase 복구 구현이나 과거 작업 증거 gate를 만들지 않는다.
- 충돌이 남아 있으면 공통 Worker 교정 경로로 넘기고, 해결된 상태면 현재 범위 검사 뒤 Manager가 계속한다. 다른 유효한 Review·승인·Agent 범위 위반 원인, 현재 소유권 불명확, metadata·원격 OID 불일치 또는 영향권 외부 변경이 남으면 해당 차단과 진단을 보존한다. rebase 진행 여부만으로 별도 차단을 해제하거나 실패 이력을 초기화하지 않는다.
- 다른 계획의 차단, 기존 Agent session·작업물·retry·승인 기록은 보존한다. 후보 revision이 달라진 경우 필요한 targeted test, 전체 verify와 Review를 다시 거치고 게시 전에 최신 원격 소유권을 재검증한다.
- source가 원격보다 뒤처졌거나 dirty 때문에 safe fast-forward가 불가능한 조건은 차단 해제 뒤 bootstrap freshness 또는 일반 source sync에서 독립적으로 판단한다. `PROJECT_BLOCKED` 상태의 안전 gate를 우회해 Worker나 Git 쓰기를 먼저 시작하지 않는다.

## Implementation

- `agent_scope_decision.py`와 `loop_manager.py`의 저장 snapshot 재검증에서 rebase는 G-109의 현재 상태 수용 결과를, 그 밖의 Manager 작업은 기존 작업별 checkpoint를 검증한 뒤 현재 작업의 사용 자원에 한해 기준선을 조정한다. 수동 rebase 수용을 Manager 명령 수행 증거로 표시하지 않고 전체 ref 복사나 관측 변화 허용을 소유권 증명으로 사용하지 않는다.
- index 변경 분류에서 unmerged stage·`ls-files` 상태 표식과 실제 `skip-worktree`·`assume-unchanged` flags를 분리한다. 충돌 파일의 전후 stage와 허용 경로를 대조해 정상 stage 해소만 허용하고, 실제 flags 변경 검사는 유지한다. 기존 `managed_worktree_scope_violation`도 저장된 전후 snapshot과 소유 rebase 증거로 같은 기준을 적용해 재평가한다.
- `scoped_blocking.py`와 실행 복구 경계에 `default_worktree_mutation` 및 같은 유형의 저장된 차단을 원인별로 재평가하는 좁은 경로를 둔다. 결과는 기존 phase·workspace 소유권과 원자적으로 저장한다.
- `resume_blocked_execplan_if_inputs_changed`와 계획별 worktree·게시 소유권 gate에서는 G-110이 일반 branch 검사보다 먼저 수행하는 현재 소유 rebase 판정을 재사용한다. `allow_rebase=True` 단독 적용이나 path·metadata 존재만으로 소유권을 추정하지 않는다. PR 없는 checkpoint와 PR 충돌 복구의 저장 상태를 각각 보존하고, Git 변경과 SQLite 저장 사이 중단 뒤에도 같은 작업을 이어간다. 과거 journal 또는 교정 turn이 없다는 이유로 현재 안전한 rebase를 다시 거부하지 않는다.
- `execution_handlers.py`의 재개 흐름에서 후보 revision 변경 시 검증 증거를 무효화하고 기존 session에서 필요한 작업을 다시 수행한다. G-081의 게시 checkpoint OID와 다른 계획의 차단 판단은 보존한다.
- 이 계획으로 확정되는 R-023의 장기 Agent 범위·Manager 증거·차단 복구 규칙은 같은 구현 PR의 `docs/PRODUCT.md`에 반영한다.

## Validation

- Worker는 `tests/test_agent_scope_decision.py`, `tests/test_scoped_blocking.py`, `tests/test_origin_isolation.py`, `tests/test_execution_handlers.py`, `tests/test_loop_manager.py`에서 직접 변경한 재조정·해제·index 분류 targeted unit test만 실행한다.
- Manager rebase·fetch·worktree 전후 기준선 갱신, Git/기록 저장 사이 중단, 증명된 무관 변화·낡은 `default_worktree_mutation` 해제, 격리 부재·영향권 변화 혼합·checkpoint 불일치의 차단 유지를 검증한다.
- G-093 재발 조건을 검증한다: Worker 전후 index가 같은 충돌 파일의 stage 1/2/3(`M`)에서 stage 0(`H`)으로 전환되면 신규·저장 snapshot 검사 모두 scope 위반 없이 통과한다. 같은 허용 파일에서 `skip-worktree`·`assume-unchanged`를 예상 밖으로 설정·해제하거나 충돌 해결과 범위 밖 변경을 섞으면 계속 차단한다.
- PR 없는 checkpoint branch와 기존 PR 각각에서 소유 rebase가 `PLAN_BLOCKED`·detached HEAD로 저장된 상태를 검증한다. 미해결 index가 있으면 같은 Worker의 충돌 교정으로, 모두 해결됐으면 Manager의 rebase 계속으로 이어져야 한다. stage 완료 뒤, rebase 완료 뒤와 SQLite 저장 전 중단을 포함해 재시작해도 session·작업물·다른 계획의 차단을 보존한다. 별도 Review 실패가 남으면 detached HEAD를 허용해도 그 실패를 해제하지 않는다.
- 위 재개 경로에서 현재 plan·repository·branch·worktree·target·원격 기대값 불명확, rebase metadata 불일치와 영향권 외부 변경은 안전 차단한다. 정상 자동·수동 결과의 과거 turn·stage 승인·receipt·replay·journal 누락은 차단하지 않으며 Agent 격리와 rebase 이외 Manager 작업의 증거 부재는 기존 엄격 판정을 유지한다. 전체 verify에서는 실제 충돌 해소에서 재시작·Worker 후보 재확인·검증·Review·기존 게시 경로까지 연결되는 회귀를 확인하며 validator 단독 통과로 완료 판정하지 않는다. 실제 Git integration case는 Worker가 직접 실행하지 않는다.
- 해제 뒤 session·작업물·다른 차단이 보존되고 후보 revision 변경 시 targeted test·전체 verify·Review가 재실행되며, 전체 verify에서 source freshness와 게시 소유권이 유지돼야 한다.

## Progress

- [x] 검증된 Manager 전이의 Agent 기준선 반영
- [x] 저장된 오탐 차단의 증거 기반 해제
- [x] 정상 충돌 stage 전이와 실제 index flags 변경 구분
- [x] PLAN_BLOCKED의 소유 rebase 및 detached HEAD 복구 경계
- [x] 재개·재검증 및 다른 차단 보존 회귀 사례

2026-10-02 Worker: 관련 unit test 73개 통과. 실제 Git 충돌·재시작에서 Worker 후보 확인·verify·Review·게시까지 이어지는 PR 없는 branch 및 기존 PR 회귀를 기존 fixture에 추가했다. integration test와 전체 verify는 실행하지 않았으며 Loop Manager 검증·Reviewer·사용자 Merge는 대기 중이다. 현재 Linux 환경의 기존 `scripts/verify.sh`가 전체 unittest discovery 및 변경 모듈 검증을 포함하므로 스크립트 변경은 필요하지 않았다.

## Decision Log

- 과거 실행에 쓰기 격리 증거가 없으면 무관한 shared ref·worktree 변화를 자동으로 외부 변경이라 단정하지 않는다. G-088/G-089와 G-090/G-091의 독립 증거가 있는 경우에만 해제한다.
- 2026-09-27: 사용자 요청으로 G-093의 정상 충돌 stage 해소가 `index flags` 위반으로 판정되고 이후 차단 복구의 일반 branch 검사에서 detached HEAD가 거부되는 재발 조건을 보강했다. Manager 소유 rebase와 허용된 파일·index 전이를 독립적으로 증명하는 범위로 한정하며, 무관한 외부 Git 변화를 과거 실행에 소급 허용하지 않는다.
- G-088처럼 실제 Review 실패와 진행 rebase가 함께 남은 경우 rebase 소유권 확인은 별도 Review 원인의 해제 근거가 아니다. 이 보강은 기존 dependency를 변경하거나 선행 계획의 차단을 우회해 G-092를 시작하도록 허용하지 않는다.
- 2026-09-30: mutable READY snapshot의 기준 revision·원본 hash를 확인해 R-048을 반영했다. 앞선 기록의 rebase 작업 수행 증거 요구는 G-109·G-110의 현재 상태 판정으로 대체한다. G-110은 rebase 충돌/완료 원인의 수동 복구를 먼저 구현하고, 이 계획은 그 결과에 기반한 Agent scope 기준선·정상 stage 오탐 복구를 담당한다. 격리 없는 과거 Agent 실행의 무관한 ref/worktree 변화는 계속 소급 허용하지 않는다.
- 2026-10-02: G-109/G-110의 현재 rebase 서비스·기준선 수용·원자적 원인 해제·후보 재검증을 재사용했다. 실제 Agent 결과를 먼저 검사한 뒤 선택 rebase 상태만 비교 기준선에 반영하고, fetch에는 일치하는 전후 intent와 끊김 없는 소유 reflog 전이를 요구한다. 복구 worktree는 해당 create receipt만 검증하므로 무관한 과거 rebase journal 누락이 gate가 되지 않는다. 원본 snapshot과 수동 복구 감사를 Manager 실행·격리 증거로 바꾸지 않는다. 새 scope 해제는 입력 지문 변경을 요구하지 않으며 다른 Review·planning·계획 차단과 retry·실패 이력을 보존한다. Requirement archive 허용 경로도 revision별 eligibility를 재사용한다.
