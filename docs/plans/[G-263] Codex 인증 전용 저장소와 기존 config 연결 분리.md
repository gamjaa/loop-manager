---
id: G-263
title: Codex 인증 전용 저장소와 기존 config 연결 분리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-085] Codex·Claude 인증 전용 자동 실행 환경.md
depends_on:
  - G-262
  - G-265
  - G-266
  - G-267
  - G-268
complexity: medium
risk: high
priority: high
---

## Purpose

직접 구현된 Codex 공통 홈을 재사용해, 준비용 CLI 조회가 설정을 생성·교체하는 경우에도 최종 사전 검사와 실제 호출이 같은 현재 설정에 결합되게 한다. 역할별 config 연결·정리 구현은 추가하지 않는다. 남은 범위는 공통 설정 수명의 호출 경계 회귀와 검사 순서이며, 기존 session 이력·fallback·쓰기 제한을 보존한다.

## Acceptance Criteria

- R-085: 인증·profile 조회부터 실제 자식 실행까지 선택 state_dir의 `automation-codex`를 세 OS에서 직접 사용한다. 같은 공급자·상태 루트의 역할·repository·worktree가 달라도 홈은 같고 호출별 임시 경로·결과·지침·권한·사용량은 분리된다. 구현된 외부 home 무시·기본 사용자 auth.json 연결·부모 환경 보존을 유지한다.
- 공통 `config.toml`의 최초 부재·CLI 생성·수정·원자 교체·삭제·재생성을 현재 설정으로 검사한다. 준비용 조회가 파일을 바꿨으면 새 내용의 확장·권한 검사를 마친 뒤에만 최종 지문과 호출 증거를 확정한다. 정상 생성 자체를 낡은 runtime 연결 위반으로 거부하거나 지문만 새 값으로 교체해 검사를 우회하지 않는다.
- 최종 검사 뒤 설정 변경·읽기 실패·소유권 불명·확장/권한 충돌은 기존 안전 거부로 처리한다. 재개·retry·새 session fallback도 현재 공통 홈에서 새 호출 검사를 수행하며 불안정한 입력을 성공으로 수용하지 않는다.
- Manager가 빈 config를 만들거나 개인 설정을 연결·복제하지 않는다. 과거 역할별 홈의 config·auth·session 및 끊긴 연결은 보존하고 새 입력으로 검사·복원·동기화하지 않는다. 현재 공통 홈·프로젝트·관리 설정만 지문에 포함하며, 사용하지 않는 개인 설정 변화는 입력 변화가 아니다.
- 기본 인증 파일 연결·원자 교체·CLI의 링크 교체 확인, 기존 인증 저장소 거부·실제 전용 경로 안내와 비밀값 비노출을 유지한다. 공통 홈과 개인 홈은 Agent 도구 쓰기 범위에 추가하지 않으며 Reviewer 읽기 전용·자동 승인 검토·기존 repository/worktree/role/session 결합을 유지한다.
- R-085 Non-Goals의 기존 session 재개 호환성, 실계정 인증/갱신·credential store와 실행 불가능한 OS의 실제 CLI 검증은 완료 조건이 아니다. 기존 이력 보존과 fallback은 모의 호출로 확인하되 실환경 성공을 합성하거나 미지원 호출을 허용하지 않는다.

## Implementation

- [x] `prepare_isolation`은 CLI version/features/sandbox 조회 전에 현재 설정을 검사하고, 조회 뒤와 sandbox probe 뒤 정책을 다시 검사한다. 안전하게 갱신할 수 있는 설정은 선택된 공통 Codex home의 `config.toml`뿐이며 Manager `config.ini`, project/system Codex 설정, 그 밖의 입력 변경은 기존 증거에 결합하지 않고 거부한다. 실제 소비자 조회 뒤 안정된 최종 지문을 저장하며 `assert_current`와 process handoff 재검사로 이후 변경을 거부한다.
- [x] `provider_adapters.py`의 `run_codex`, `provider_execution.py`의 읽기 전용 `observe`와 G-262의 준비 환경을 대조한다. version 및 각 native 지침 조회가 끝날 때마다 설정 정책을 검사해 충돌 발견 뒤 다음 Codex 조회가 실행되지 않게 한다. 준비 중 공통 home 설정의 안전한 생성은 재검사 후 현재 호출 증거에 결합하고, 다른 입력 변화·충돌에서는 실행/Review 동일성 증거를 만들지 않는다. 신규·재개·retry·fallback의 기존 session·ledger 관계를 바꾸지 않는다.
- [x] 확인한 `provider_home.py`, `agent_profiles.py`, `agent_codex_sandbox.py`의 공통 홈·인증 링크·호출별 attempt 구조를 유지한다. 삭제된 `_read_only_link`나 역할별 config 제거를 복원하지 않는다. `tests/test_provider_home.py`의 설정 수명/과거 홈 보존 fixture와 `tests/test_codex_native_invocation.py`·`test_agent_write_isolation.py`·`test_provider_runtime.py`의 실제 판단/모의 CLI 경계를 재사용한다.
- [x] PRODUCT·ARCHITECTURE·`docs/OPERATIONS.md`의 관련 절을 검사 시점·정상 생성·경쟁 거부에 맞춰 통합한다. 공통 홈의 직접 사용과 과거 자료 보존은 이미 반영됐으므로 재구현 목표로 쓰지 않는다.

## Validation

- Worker는 `python3 -m unittest tests.test_agent_write_isolation.WriteIsolationTests.test_configuration_drift_refuses_launch_after_successful_probe tests.test_agent_write_isolation.WriteIsolationTests.test_native_tool_probe_binds_new_configuration_and_resume`와 새로 추가한 검사 순서/설정 수명 unit case를 실행한다. `tests/test_provider_home.py`의 기존 준비/지문 수명 검사를 전체 호출 검증으로 대신하지 않는다.
- 모의 version/features/probe 또는 profile/지침 조회가 안전한 config를 최초 생성·원자 교체하는 case에서 최종 증거가 현재 bytes에 결합되고 다음 동일 호출이 낡은 연결 때문에 실패하지 않아야 한다. 같은 시점에 Hook/권한 충돌을 생성하면 모델 process 시작 전 거부하고 파일·기존 결과를 보존해야 한다.
- 최종 검사 이후 교체·읽기 실패·중단·불안정 입력에서는 새 성공 증거가 없어야 한다. 재개·retry·session-unavailable fallback이 각각 현재 공통 홈을 검사하며 저장된 session/실패와 이전 홈 파일·부모 환경을 보존하는지 기존 invocation fixture로 확인한다.
- 부분/전체 verify의 고유 회귀는 실제 Reviewer 환경 관측과 호출 지문의 일치, Fast 기본 off·명시 profile/instruction·인증 링크 보호·사용량·호출별 임시 경로·자식 종료·Reviewer 읽기 전용이다. CLI/계정과 다른 OS의 실제 효과를 모의 검증의 성공으로 보고하지 않는다.

## Progress

기존 전용 홈·기본 인증 연결에 대한 정적 확인은 보존한다. 최신 원격 직접 구현에서는 `prepare_launch`가 세 OS 공통 홈을 직접 사용하고 `_read_only_link`를 제거했으며 설정 수명·과거 홈 보존·동시 역할의 임시 경로 회귀가 추가됐다.

- [x] 준비 중 생성·교체된 설정을 probe 뒤와 profile/native 지침 조회 뒤 다시 검사하고, 최종 지문·정책을 현재 invocation에 결합했다. process handoff에서 설정·workspace snapshot 변경을 다시 거부하며 미시작 invocation 증거만 갱신한다.
- [x] 관리 rebase 후 현재 코드 후보 `57cd72e`에서 프로젝트 Python의 targeted runner로 configuration drift·probe 중 안전한 원자 교체·probe 중 확장 충돌·native 지침 조회 중 생성/충돌·read-only 관측의 안전한 생성·최종 검사 뒤 경쟁 변경·호출 identity 결합 등 8개 unit case를 실행해 모두 통과했다. 전체 verify는 실행하지 않았다.
- [x] 2026-10-21 관리 rebase 후 현재 HEAD `739e33f`에서 프로젝트 Python의 targeted runner로 위 8개 설정 수명·검사 순서·호출 증거 unit case를 다시 실행해 모두 통과했다. 전달된 전체 verify 실패는 변경되지 않은 maintenance-cycle assertion과 부분/Perl verify 하위 실행 timeout에서 발생해 이 ExecPlan 범위에 포함되지 않는다.
- [x] 2026-10-21 현재 rebase 후보 HEAD `fa877ad`에서 프로젝트 Python의 targeted runner로 같은 8개 설정 수명·검사 순서·호출 증거 unit case를 새로 실행해 모두 통과했다. 전달된 전체 verify 진단은 이전 revision `739e33f` 결과이며 현재 후보의 전체 verify 결과는 아직 없다.
- [x] 2026-10-21 현재 HEAD `e8dba98`에서 프로젝트 Python의 targeted runner로 설정 drift, probe 중 안전한 생성·충돌, 지침 조회 후 설정 수명, 실제 호출 관측 identity를 확인하는 8개 unit test를 새로 실행해 모두 통과했다. 전달된 현재 verify 실패는 G-263 diff에 없는 Perl 예제의 Manager 전용 통합 테스트가 단독 하위 실행 중 timeout된 건이며, 해당 통합 테스트는 Worker가 실행하지 않는다.
- [x] 2026-10-23 관리 rebase 후 현재 HEAD `8c0f36c`에서 프로젝트 Python targeted runner로 설정 drift, probe 중 생성·충돌, 지침 조회 후 설정 수명, process handoff 경쟁, 호출 관측 identity를 확인하는 8개 unit test를 새로 실행해 모두 통과했다. 전달된 Perl 예제 실패 요약은 이전 revision `e8dba988`에 해당하며 세 번째 standalone/failing 하위 사례가 약 30초 뒤 timeout됐다. 해당 Manager 전용 통합 테스트는 G-263 diff에 없고 `docs/VERIFY.md`에 따라 Worker는 실행하지 않았다.
- [x] 2026-10-23 현재 HEAD `81e1718`에서 프로젝트 Python targeted runner로 위 설정 수명·검사 순서·process handoff 관련 8개 unit test를 다시 실행해 모두 통과했다. 전달된 base `7621566` 대비 최종 diff와 PRODUCT·ARCHITECTURE·OPERATIONS 설명을 재확인했다. 이전 Perl 통합 테스트 진단은 이 HEAD의 verify 결과가 아니므로 전체 verify는 Manager에게 맡긴다.
- [x] 2026-10-23 현재 HEAD `a2e5aee`의 최신 계획 조건을 코드와 대조해, version 조회와 features 조회 사이에 안전한 설정을 재검사하는 절차가 빠진 것을 확인했다. version 조회 뒤와 features 조회 뒤 sandbox probe 전에 정책을 검사하도록 보완했다. 프로젝트 Python targeted runner로 두 중간 충돌 거부 회귀와 기존 안전한 probe 생성 회귀를 실행해 3개 모두 통과했다.
- [x] 2026-10-23 HEAD `4935299` 기반 후보에서 features 조회 중 안전한 원자 교체가 허용되고 현재 지문에 결합된 뒤 다음 호출에서도 재사용되는 회귀를 추가했다. 프로젝트 Python targeted runner로 version/features 조회 중 충돌·생성, probe 후 설정 결합·재개, 최종 검사 뒤 drift를 확인하는 5개 unit case가 통과했다.
- [x] 2026-10-23 handoff 회귀 mock을 새 features-check callback 인자에 맞춰 feature 확인 후 handoff를 발생시키도록 갱신했다. 프로젝트 Python targeted runner에서 해당 worker/reviewer/session subtest가 모두 통과했다.
- [x] 2026-10-23 현재 후보 `0e359bd`에 `refresh_configuration_evidence`의 Manager `config_path` 변경 거부와 첫 native 지침 조회 뒤 후속 Codex 조회 중단 회귀를 추가했다. 프로젝트 Python targeted runner로 직접 증거 갱신 거부, 호출 전 Manager drift 거부, hook 충돌의 조기 중단, 안전한 공통 home 설정 생성·관측 등 5건을 실행해 모두 통과했다.
- [x] 2026-10-23 Manager 복구점 HEAD `624a3a6`에서 위 Manager 설정 drift 거부·native 조회 중단·공통 설정 생성 회귀 5건을 프로젝트 Python targeted runner로 다시 실행해 모두 통과했다. 전체 verify와 Review는 Manager 확인으로 남긴다.
- [x] 2026-10-23 현재 HEAD `61f9288`에서 Planner interruption recovery 테스트의 모의 사전검사가 현재 Manager·project/system 입력으로 안정 설정 지문을 제공하도록 갱신했다. 실제 준비 조회별 재검사는 유지했고 `test_interrupted_planner_persists_started_session_and_resumes` targeted unit test가 통과했다.
- [x] PRODUCT·ARCHITECTURE·OPERATIONS에 정상 생성 수용과 검사·거부 순서를 반영했다.

## Decision Log

- 기존 ID·경로·sources·dependency·priority를 유지하고 직접 구현에 소급 계획/검증 성공을 만들지 않는다. 이전 config 연결 정리 방식은 최신 R-085의 공통 홈 직접 사용으로 대체됐으며 인증 보호를 일반 설정 수명 정책으로 완화하지 않는다.
- 준비 중 설정 변경은 새 내용 전체의 재검사 대상이며 최종 검사 뒤 경쟁은 계속 거부한다. 현재 입력을 검증하지 않고 저장 지문만 전진시키지 않는다. 공통 홈 전환의 session/실계정/OS 호환 검증은 최신 Non-Goals의 후속 메모로 남기고 이 작업의 gate로 쓰지 않는다.
- complexity medium: 남은 설계는 기존 설정 검사·호출 증거·재검사를 여러 소비자의 명확한 경계에 연결하는 작업이다. risk high: 검사 순서 교정이 확장/권한 충돌을 놓치면 안전하지 않은 Agent 실행이나 낡은 Review 근거를 수용할 수 있다. 원본·설정·session을 보존한 안전 거부와 새 호출 재검사로 복구하며 생성/교체·충돌·최종 검사 후 경쟁을 확인한다.
- priority high: 정상 공통 홈 준비의 불필요한 거부와 설정 검사의 우회를 함께 막는 무인 호출 보완이다. Model/Effort는 프로그램의 명시 설정을 사용한다.
