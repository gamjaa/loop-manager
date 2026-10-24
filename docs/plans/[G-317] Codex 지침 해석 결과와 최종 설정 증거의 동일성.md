---
id: G-317
title: Codex 지침 해석 결과와 최종 설정 증거의 동일성
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-064] 모델 호출을 통한 공통 Agent 지침 전달.md
  - docs/requirements/[R-085] Codex·Claude 인증 전용 자동 실행 환경.md
  - docs/requirements/[R-072] 입력 동일성이 증명된 구현 후보 성공 결과 재사용.md
  - docs/requirements/[R-086] 영향 기반 부분 검증과 일반 선행 Review.md
depends_on:
  - G-263
complexity: medium
risk: high
priority: high
---

## Purpose

Codex native 지침 해석에서 확인한 설정 identity를 최종 격리·실행 증거까지 연결한다. 지침 해석 완료 뒤 공통 설정이 교체됐을 때 이전 지침과 새 설정을 한 호출의 증거로 확정하는 잔여 경로를 막는다. 실제 모델 실행과 모델 없는 현재 Reviewer 관측에 같은 조건을 적용한다.

현재 `provider_guidance.codex_arguments()`는 baseline·sentinel·최종 override 조회 사이 설정 동일성을 확인하지만 호출자에는 override와 지침 해시만 전달한다. 이후 `provider_adapters.run_codex()`와 `provider_execution.observe()`가 호출하는 `refresh_configuration_evidence()`는 공통 home 설정을 제외한 안정 입력만 이전 증거와 대조하고 현재 설정 지문을 채택한다. 따라서 마지막 native 조회가 설정 A에서 끝난 뒤 refresh 직전에 안전한 설정 B로 원자 교체되면, 지침은 A이고 정책 지문은 B인 증거를 만들 수 있다. 이후 `assert_current()`와 process handoff가 B만 다시 검사해도 이 혼합은 발견하지 못한다.

## Acceptance Criteria

- R-064/R-085: 최종 override·지침 해시를 증명한 설정 identity와 최종 격리·실행 증거의 설정 identity가 같아야 한다. native 지침 해석 완료 뒤 증거 갱신 전에 공통 `config.toml`이 달라지면, 안전한 설정이어도 그 시도에서는 실제 process를 시작하지 않고 현재 Reviewer 관측도 identity를 반환하지 않는다. 설정 지문만 새 값으로 전진시켜 이전 지침을 승인하지 않는다.
- 조회 중 안전한 최초 생성·원자 교체를 재검사하고 baseline부터 다시 해석하는 현재 동작은 유지한다. 같은 설정에서 해석을 완료하면 기존 추가 지침과 common+역할 지침을 보존한 override, 실제 전달 해시, 최종 설정 지문이 함께 결합된다. 이 작업 때문에 정상 생성 자체를 일괄 거부하지 않는다.
- Manager·project/system 설정 변경, 확장·권한 충돌, 읽기 실패·불안정한 내용, 최종 검사 뒤 변경은 기존 거부를 유지한다. 신규·재개·retry·fallback이 공유하는 관리 Codex 호출 경계와 Reviewer 읽기 전용·내장 쓰기 제한·자동 승인 검토를 보존한다.
- R-072/R-086: 설정과 지침을 같은 입력으로 확인하지 못한 현재 관측은 재사용·일반 선행 Review 승격의 동일성 근거가 될 수 없다. 실제 과거 완료 호출의 지침·설정·성공/실패 기록은 현재 값으로 갱신하지 않으며, 필수 실행 전 증거 저장과 process handoff의 재검사는 유지한다.
- 거부 시 기존 결과 파일·session·작업물·설정 원문·부모 환경을 보존한다. 실행 시작·완료·성공이나 사용량을 합성하지 않는다. 새 설정을 확인하는 다음 시도에서는 현재 내용으로 지침을 다시 해석할 수 있다.

## Implementation

- [ ] 확인한 `loop_manager_internal/agents/provider_guidance.py:codex_arguments()`의 `native_lookup()`·설정 안정성 비교·`observe_instruction`을 재사용해 마지막 성공한 지침 해석의 설정 identity도 호출자가 보존하게 한다. 기존 callback/반환 계약의 최소 확장으로 처리하고 native 조회·설정 파서·지침 합성을 별도로 구현하지 않는다.
- [ ] 확인한 `agents/agent_write_isolation.py:refresh_configuration_evidence()`·`_checked_codex_configuration()`에 지침 해석이 확인한 설정과 현재 설정의 동일성 조건을 연결한다. `agents/provider_adapters.py:run_codex()`와 `agents/provider_execution.py:observe()` 모두 이 근거를 소비하게 하고, 불일치 때 증거 갱신·실행 identity 발행·process 시작 전에 거부한다. 기존 공통 설정의 안전한 생성 수용과 Manager/project/system 입력의 변경 거부를 분리한 구조를 유지한다.
- [ ] 확인한 `storage/agent_invocation_state.py:InvocationRepository.record_isolation()`·`record_execution_identity()`와 `agents/invocation_service.py`의 호출 전 저장·거부 경계를 재사용한다. 완료된 호출을 수정하거나 DB schema·대기/재시도 정책을 바꾸지 않는다. `agents/agent_codex_sandbox.py:CodexLaunch.start()`로 전달하는 process handoff 재검사도 유지한다.
- [ ] 확인한 `tests/test_provider_execution.py`와 `tests/support/provider_execution.py:ProviderExecutionFixture`의 실제 지침 조립·설정 검사·SQLite ledger를 사용해 누락된 경계의 최소 회귀를 추가한다. 필요할 때만 `tests/test_agent_write_isolation.py`의 refresh 직접 검사를 보강한다. PRODUCT의 공급자 설정 규칙과 ARCHITECTURE의 준비된 provider 실행 입력 절에는 실제 구현한 동일성 조건을 통합하고 Requirement·다른 ExecPlan은 변경하지 않는다.

## Validation

- Worker는 `tests.test_provider_execution.ProviderExecutionTests`에서 새 경계의 unit case와 아래 기존 관련 case만 실행한다. CLI/probe·process는 기존 모의 경계를 사용하고 실제 Agent나 전체 verify를 호출하지 않는다.
- 실제 `codex_arguments()`가 설정 A의 추가 지침을 해석한 직후, `refresh_configuration_evidence()` 진입 전에 공통 config를 추가 지침 B로 원자 교체한다. 실행 경로에서는 process 호출 0회와 실행 identity·시작 기록 부재, 현재 관측에서는 `None`을 확인한다. 이전 결과·session·설정 B와 과거 완료 invocation의 증거가 보존돼야 한다. 지침 해석이나 refresh 자체를 성공 mock으로 대체하지 않는다.
- 교체가 없는 호출과 위 거부 뒤 설정 B로 다시 준비한 호출은 실제 전달 override가 B와 공통 지침을 포함하고 현재/실제 호출 identity가 일치해야 한다. 신규/재개의 공통 경계 적용을 확인하되 기존 session fallback 조합을 반복하지 않는다.
- 기존 `test_instruction_change_during_native_lookup_restarts_observation_baseline`·`test_instruction_change_during_native_lookup_restarts_execution_baseline`과 `test_native_instruction_lookup_config_generation_is_bound_to_actual_call`을 유지해 조회 중 안전한 생성/교체 수용과 새 거부 경계를 구별한다. `test_config_changed_after_final_check_is_rejected_at_process_handoff`·`test_manager_config_change_during_native_lookup_is_rejected_before_next_lookup`·과거 실제 identity 불변 회귀도 재사용한다. 같은 조건의 별도 fixture나 실제 Git/OS 실행 조합은 추가하지 않는다.
- 부분/전체 검증에서 현재 관측 실패가 동일성 성공으로 바뀌지 않고 기존 fallback·필수 검증/Review 조건이 유지돼야 한다. R-072의 저장소 코드 입력 기준 verify 재사용과 실제 Reviewer 환경/정책 비교를 혼합하지 않는다.

## Progress

- [x] delta 기준 `e69a2399992a7e598217741cdcd3833d9b0257de`부터 `85c605033cb894c95b1171304831404ce87bf3c2`까지 변경과 활성 R-064/R-085/R-072/R-086, 관련 구현·회귀를 정적으로 대조했다. 조회 사이 재해석과 handoff 검사는 구현된 범위로 제외하고 완료된 지침 해석→증거 갱신 사이 동일성 누락만 식별했다. 실행 재현·테스트·verify는 하지 않았다.
- [ ] 지침 해석의 설정 identity와 최종 증거 결합
- [ ] 실행/현재 관측의 경계 회귀 및 관련 설계 문서 정합화

## Decision Log

- G-263의 Merge된 설정 수명·조회 사이 재검사·baseline 재해석은 재사용한다. 이 계획은 그 이후의 확인된 연결 누락만 보완하며 완료 계획이나 직접 구현에 소급 작업·검증 성공을 만들지 않는다. G-183은 최종 승인 재사용 소비자, G-189는 후속 차단/재개 감사이고 G-264는 Claude source 제외이므로 이 Codex 공통 경계의 구현을 각각에 중복 배정하지 않는다.
- 설정을 전역 잠그거나 공통 홈을 호출별로 복제하지 않는다. 기존 조회 동안 허용한 안전한 갱신과 완료된 해석 뒤의 변경 거부를 구분하면 현재 설정 수명·병렬 호출·인증 보존 계약 안에서 해결할 수 있다. 새로운 사용자 정책·infrastructure·인증 저장 방식이나 실환경 호환성 완료 조건은 필요하지 않다.
- complexity medium(최초 level 3): 기존 해석·설정 검사와 두 호출자의 명확한 경계에 동일성 근거를 연결한다. risk high: 혼합 증거는 현재 추가 지침을 누락한 호출과 잘못된 Review 동일성 판단을 허용할 수 있다. 새 process 전 거부·과거 증거/결과 보존·다음 호출 재준비를 복구 경계로 삼고 실제 조립/ledger 회귀로 확인한다. priority high는 무인 호출과 승인 증거의 필수 입력 결합을 보완하는 기준이다. Model·Effort는 Manager 설정을 따른다.
- `scripts/verify.json`의 version 1·unittest·launcher Python, `docs/VERIFY.md`의 Python 3.11–3.14·모듈별 독립 수집·필수 compile·실제 Agent 차단 계약을 유지한다. 이 delta 계획은 프로젝트 최종 전문 확인이나 구현·검증·Merge 완료 판단이 아니다.
