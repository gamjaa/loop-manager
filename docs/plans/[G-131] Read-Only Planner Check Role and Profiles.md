---
id: G-131
title: Read-Only Planner Check Role and Profiles
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-049] Input-Specific Planner Roles.md
  - docs/requirements/[R-050] Direct Codex Exec with Native Sandbox.md
depends_on: []
complexity: medium
risk: high
priority: high
---

## Purpose

현재 Planner 호출은 일반 계획용 prompt와 쓰기 정책을 사용하고 provider별 `planner` profile 하나만 선택한다. 예정 작업의 유지 가능성만 판단하는 읽기 전용 `planner_check` 역할, 결과 계약과 독립 Model·Effort 설정을 기존 Agent 호출 경계에 추가한다. 스케줄 선택과 결과의 durable 적용은 G-132가 담당한다.

## Acceptance Criteria

- 점검 prompt는 마지막 확인 기준 이후의 관련 코드 변경, 현재 비종료 compact catalog와 수정 가능한 미착수 READY를 제공한다. 계획 본문·근거·코드·테스트는 영향 판단에 필요한 부분만 읽도록 하며 전체 미충족 작업이나 저장소 전체 개선점 탐색을 요구하지 않는다.
- 결과는 수정 불필요, 수정 필요, 판단 불확실을 구분하고 확인한 근거 및 필요한 인계 사유를 전달한다. 잘못된 형식·알 수 없는 결과·입력과 무관한 계획 ID는 유효한 결과로 채택하지 않는다. 수정 필요와 불확실은 실패나 수정 불필요로 바꾸지 않는다.
- Codex와 Claude에 일반 Planner와 점검 Planner의 Model·Effort를 독립 설정할 수 있다. 기존 `[<provider>.roles]`의 `planner_check`를 생략하면 `planner` 값을 사용한다. 명시한 잘못된 값은 설정 오류이며 서로 다른 값이나 특정 모델을 강제하지 않는다.
- 점검은 선택 canonical 관리 worktree를 읽기 전용으로 사용한다. 신규·재개·지원되는 session-unavailable fallback에도 같은 역할·profile·정책을 유지하며 ExecPlan, FUTURE_IDEAS, 코드·문서·Git metadata를 쓸 권한이 없다. 일반 Planner·유지보수 Planner의 기존 쓰기 권한은 확대하지 않는다.
- Codex 직접 `codex exec`, 내장 sandbox·granular 자동 검토와 Claude의 기존 provider 경계를 재사용한다. 역할별 diff·scope 검사도 읽기 전용을 검증한다. 정책 확인 실패·권한 확대·격리 밖 재실행을 점검 성공으로 처리하지 않는다.
- 실제 호출과 사용량 기록에서 `planner_check` 역할, 선택한 Model·Effort, session·retry·fallback을 일반 계획과 구분할 수 있다. 결과와 사유는 비밀값 없이 반환하며 실패 시 기존 session·scope 증거를 보존한다. 점검 세션을 일반 Planner 세션으로 재개하지 않는다.
- 점검 지침은 구현·테스트·verify 실행·commit·push·PR 생성과 파일 변경을 금지한다. 읽기 전용 검색·diff 명령은 사용할 수 있으며 sandbox가 모든 테스트 command를 식별해 차단한다고 주장하지 않는다.

## Implementation

1. `loop_manager_internal/agents/agent_profiles.py`의 `provider_profile`, role resolver와 provider runtime 검증을 확장한다. optional `planner_check`가 없는 설정의 fallback만 허용하고 설정된 값에는 기존 alias/Model/Effort 검증을 적용한다. `config/config.ini.example`와 README의 설정 안내에 한 가지 일관된 key와 fallback을 기록한다. 새 설정 파일·설정 모드·사용자 진입점은 만들지 않는다.
2. `loop_manager_internal/agents/agent_prompts.py` 및 planning 내부의 작은 순수 parser에서 좁은 점검 문맥과 세 가지 결과를 정의한다. 기존 compact catalog·`MutableReadySnapshot`을 재사용하되 snapshot은 판단 대상의 identity이며 점검의 수정 권한이 아님을 명시한다. parser는 Agent가 주장한 결과를 구조화할 뿐 입력·소유권 확인이나 성공 기준 저장을 대신하지 않는다.
3. `loop_manager_internal/planning/agent_ports.py`의 `run_scoped_planner_turn`, 기존 `run_agent`/`TrackedAgentCall`·session callback을 재사용해 점검 호출을 제공한다. 새 역할은 `read_only=True`와 독립 profile로 실행한다. 일반·유지보수 호출의 기본값과 기존 correction 연결을 보존하며 provider runner를 복제하지 않는다.
4. `scope_validation.py`, `agent_scope_decision.py`와 `agent_invocation_state.py`의 역할·session·scope 결합에 필요한 최소 연결만 추가한다. 점검 파일 변경은 항상 거부하고, 범위 감시 축소는 기존 호출별 격리 증거가 실제 역할·session·workspace와 일치할 때만 허용한다. 알려지지 않은 역할이나 과거 증거에 새 성공 권한을 부여하지 않는다. 새 호출의 정책 확인은 기존 `agent_write_isolation.py`와 provider adapter가 수행한다.
5. 스케줄러·점검 성공 watermark·인계 상태 저장은 변경하지 않는다. 직접 호출 경계와 parser/profile/scope의 targeted 검증만으로 독립 Merge 가능한 역할을 제공한다. 필요한 파일은 기존 agents/planning/tests 경계에 두고 현재 OS verify의 검증 범위를 유지한다.

## Validation

- Worker는 `tests/test_provider_profiles.py`의 role 설정 사례, `tests/test_scope_role_boundaries.py`의 읽기 전용 변경 거부 사례와 `tests/test_agent_invocation.py`의 실제 profile·role/session ledger 사례 중 수정한 메서드 및 새 점검 회귀만 실행한다. 예: `python3 -m unittest tests.test_scope_role_boundaries.ScopeRoleBoundariesTest.test_role_boundaries_reject_protected_and_read_only_changes`.
- 두 provider의 독립 설정·생략 fallback·명시한 빈값/잘못된 alias·지원하지 않는 조합, 점검에서 일반 역할로의 session 혼용 거부, 신규·재개·fallback의 읽기 전용 정책을 확인한다. adapter 정책 연결을 변경한 경우에만 `tests/test_codex_native_invocation.py`와 기존 Claude adapter의 직접 관련 mocked unit test를 추가한다.
- prompt와 parser의 세 결과·근거·유효한 대상 ID, 누락·모호한 응답·알 수 없는 결과, 전체 작업 탐색 지시 부재와 모든 파일 쓰기 거부를 검증한다. 실제 모델 호출이나 운영 설정 변경을 targeted 검증 조건으로 요구하지 않는다.
- Manager 전체 verify는 기존 일반·유지보수 Planner, Worker·Reviewer profile/권한과 session 복구를 보존하면서 새로운 읽기 전용 역할을 지원하는지 확인한다. Reviewer의 기존 테스트 재실행 금지와 Worker/Manager 검증 책임은 유지한다.

## Progress

- [ ] 독립 점검 profile과 기존 설정 fallback 지원
- [ ] 좁은 prompt·세 결과 parser와 읽기 전용 호출 연결
- [ ] 역할·session·scope·usage 및 정책 거부 회귀 검증
- [ ] Manager 전체 verify, 독립 Review 및 사용자 Merge

## Decision Log

- 2026-10-03: `_PlanningAgentPorts.run_planner`는 `planner`와 `read_only=False`를 고정하고 `validate_provider_profiles`는 planner/reviewer만 검증한다. 현재 코드와 설정 예제에 점검 역할이 없음을 확인했다.
- G-130의 입력 분류와 독립적으로 기존 snapshot·Agent 호출 API를 사용하는 역할을 작성한다. G-132가 두 경계를 조합한다. 점검을 위해 별도 CLI transport, 외부 서비스, root 파일이나 provider 구현을 추가하지 않는다.
- R-050의 직접 실행과 기존 정책 확인을 그대로 사용한다. G-113의 설정 경계와 G-114의 보호 경로 probe 작업을 여기서 재구현하지 않는다.
- priority는 `high`다. R-049의 자동 역할 선택에 필요한 읽기 전용 실행과 독립 설정을 제공한다. 더 가벼운 모델의 사용은 사용자 설정에 맡긴다.
