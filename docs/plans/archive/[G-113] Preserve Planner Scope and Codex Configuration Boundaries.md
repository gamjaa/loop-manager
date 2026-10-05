---
id: G-113
title: Preserve Planner Scope and Codex Configuration Boundaries
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
  - docs/requirements/[R-050] Direct Codex Exec with Native Sandbox.md
depends_on:
  - G-127
  - G-129
complexity: medium
risk: high
priority: critical
---

## Purpose

활성 계획의 호환 투영이 진행 중 Planner의 worktree와 snapshot을 덮어써 서로 다른 작업 공간을 비교하는 오탐 차단을 수정한다. 명시적인 별도 CODEX_HOME을 사용해도 repository 밖 사용자 설정을 project config로 다시 읽는 결함을 수정한다.

## Acceptance Criteria

- Planner가 실행 슬롯을 소유하는 동안 자동 active-plan hydration은 Planner 상태를 변경하지 않는다.
- Planner가 없으면 기존 활성 계획의 투영과 재개가 유지된다. READY priority와 dependency 선택 규칙은 바꾸지 않는다.
- Codex 사용자 설정은 선택된 CODEX_HOME에서 읽고 project config는 선택 worktree에서 repository root까지 읽는다. repository 밖 다른 사용자 설정은 project config로 읽지 않는다.
- 현재 호출에 실제 적용되는 확장·exec-policy·관리 설정의 충돌과 내장 sandbox 확인 실패는 계속 거부한다. 비활성화된 개인 plugin 등록 자체는 충돌로 판정하지 않으며 사용자 설정을 삭제하거나 수정하지 않는다.
- Codex 호출은 현재 PRODUCT/R-050의 직접 `codex exec`, 역할별 내장 쓰기 정책 및 granular 자동 검토 계약을 유지한다. 바깥 wrapper, 전체 mount 증명, 중첩 sandbox 성공 및 시스템 AppArmor 설정 변경을 이 계획의 시작·완료 조건으로 요구하지 않는다.
- 현재 소유 workspace, Agent session·snapshot과 사용자 guidance 결정을 보존한다. 과거 G-058 보류 지시를 현재 상태에 재적용하거나 운영 SQLite 상태를 수동 수정하지 않는다.

## Implementation

- `loop_manager_internal/storage/project_state_adapter.py`의 `hydrate_active_project`와 `loop_manager_internal/storage/runtime_state.py`의 `recovery_slot_available`을 기준으로 Planner 또는 중단된 planning attempt가 소유한 슬롯을 active-plan 투영이 덮어쓰지 않는지 확인한다. 현재 구현을 재작성하지 않고 확인된 잔여 결함만 수정한다.
- `loop_manager_internal/agents/agent_write_isolation.py`의 `_codex_config_files`가 선택된 CODEX_HOME과 선택 worktree의 repository 경계만 사용하는지 확인한다. 현재 코드에 이미 있는 경계 제한을 재사용하고 사용자 인증·설정 파일을 변경하거나 복제하지 않는다.
- 직접 호출·정책 확인은 `loop_manager_internal/agents/agent_codex_sandbox.py`와 기존 isolation 경계가 소유한다. 필요한 수정도 이 책임 경계 안에서 수행하며 outer permission profile이나 mount gate를 되살리지 않는다. G-114는 선언된 경로 probe의 오탐·거부 조건 회귀를 담당하므로 해당 검증을 여기서 중복 구현하지 않는다.

## Validation

- Worker는 `tests/test_runtime_state.py`의 `test_active_hydration_preserves_planner_scope_and_interrupted_attempt`와 `test_active_hydration_resumes_worker_when_planner_does_not_own_slot`, `tests/test_agent_write_isolation.py`의 `test_selected_codex_home_does_not_inherit_config_above_git_root` 및 실제 설정 충돌 사례 중 변경한 경계의 최소 targeted unit test만 실행한다.
- 정책 연결을 수정할 때만 `tests/test_codex_native_invocation.py`의 직접 호출·재개·fallback 사례를 포함한다. 코드·테스트 변경이 없으면 현재 구현·테스트의 충족 근거와 targeted test 미실행 사유를 기록한다.
- 전체 verify는 Manager가 수행하고, Reviewer는 전달된 결과를 읽기 전용으로 검토한다. 실제 모델 실행, 운영 DB 수정 또는 개인 설정 변경을 Worker 검증 조건으로 요구하지 않는다.

## Progress

- [x] 과거 작업 기록: 코드 수정 및 targeted unit test 5개 통과 (현재 revision의 검증 결과가 아님)
- [x] 현재 Planner 소유권·설정 경계 구현 재확인; 잔여 구현 결함 없이 기존 회귀 테스트 강화
- [ ] Manager 전체 verify, Review 및 사용자 Merge

## Decision Log

- 2026-10-01: 사용자가 재차 차단된 자동 실행의 복구와 G-058 대신 critical 계획 처리를 요청했다. 직전 수동 해제의 IMPLEMENTING 상태가 active 재개를 유발했고, Planner snapshot과 G-058 worktree 비교 오류를 확인했다. 제품의 비선점 정책은 변경하지 않고 사용자 요청으로 G-058을 보류한다.
- OpenAI 공식 문서에 따라 Codex 사용자 설정과 repository project 설정의 경계를 구분한다. 사용자 plugin 설정은 삭제하지 않으며 새 자동 실행 home은 비밀값을 출력하거나 복제하지 않는다.

- 2026-10-01: 실제 자동 실행에서 CLI 0.159.3의 outer sandbox는 named permission profile을 요구함을 확인했다. 기존 mount proof와 쓰기 경계를 유지하는 형식으로 갱신한다. Ubuntu AppArmor가 vendor bwrap의 namespace 초기화를 거부하는 운영 환경 조건은 개별 binary profile로 해결하며 시스템 전체 userns 정책은 변경하지 않는다.
- 2026-10-02 사용자 결정: 리팩터링 우선 요청에 따라 미착수·무소유 READY인 이 계획은 G-127 Merge 이후 진행한다. 원래 Planner 소유권·설정 경계·거부 조건과 목표는 유지하고 실행 시 최신 application·Agent 경로와 현재 PRODUCT/R-050의 직접 Codex 실행 계약을 확인한다. 과거 운영·targeted test 기록은 보존하되 새 revision의 검증 성공으로 사용하지 않는다.

- 2026-10-03 사용자 요청에 따른 정책 정합성 수정: 현재 PRODUCT/R-050을 우선해 과거 outer sandbox·mount proof·AppArmor 운영 조정 지시를 현재 Implementation에서 제거했다. 원래 Planner 소유권과 repository 설정 경계 목표는 유지한다. G-058은 운영 SQLite에서 이미 MERGED이므로 과거 보류 지시를 현재 Acceptance Criteria에서 제거했다. 위 2026-10-01 기록은 당시의 진단이며 현재 실행 지시가 아니다. 현재 hydration·config 경계는 구현돼 있으므로 잔여 결함만 다루고, 과거 결과로 완료를 선언하지 않는다. SQLite와 실행 상태는 수정하지 않는다.

- 2026-10-03 Worker 구현 확인: `hydrate_active_project`는 `recovery_slot_available`로 live·보존 planning 소유권을 검사한 뒤에만 활성 계획을 조회·투영한다. `_codex_config_files`는 선택 CODEX_HOME과 `.git` 파일·디렉터리로 식별한 repository 내부 설정 및 기존 시스템 설정만 열거한다. 개인 plugin 등록은 호출의 `features.plugins=false`·`features.apps=false`로 비활성화하며, MCP·exec-policy·관리 확장 강제 활성화의 기존 거부 조건과 내장 sandbox probe는 유지된다. 잔여 구현 결함이 없어 production 코드·호출 정책·verify 스크립트는 변경하지 않았다. 기존 Linux verify는 두 테스트 모듈을 전체 discovery에 포함한다.
- 회귀 보강: hydration 테스트는 11개 독립 planning 소유권 표식 각각에서 project·ExecPlan 전체 row 보존을 확인하고, 슬롯이 비면 Worker session·snapshot·workspace 소유권·guidance hash와 성공 planning 지문을 보존하며 재개하는지 확인한다. 설정 테스트는 일반·linked repository에서 루트 위 MCP·잘못된 TOML이 검사와 지문에 포함되지 않고, 선택 home·repository 루트·하위 설정의 MCP는 거부되며 지문에 반영되는지 확인한다. 모든 설정 조작은 임시 fixture에 한정했다.
- 현재 revision targeted 검증: Validation에 명시된 hydration 2개, 설정 경계 1개와 개인 plugin·관리 확장·exec-policy·상속 MCP 4개를 한 명령으로 실행했다. 최초 실행은 Worker 재개 테스트의 전체 row 불변 기대 때문에 실패했다. 기존 활성 계획 조회의 catalog cache 초기화를 확인하고 비교를 보존해야 할 실행 필드로 한정한 뒤 동일 명령을 재실행해 7개 모두 통과했다. 전체 verify·Review·Merge는 이 결과에 포함되지 않으며 운영 SQLite·사용자 설정·기존 guidance 결정은 변경하지 않았다.
