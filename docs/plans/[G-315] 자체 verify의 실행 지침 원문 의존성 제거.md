---
id: G-315
title: 자체 verify의 실행 지침 원문 의존성 제거
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-072] 입력 동일성이 증명된 구현 후보 성공 결과 재사용.md
  - docs/requirements/[R-064] 모델 호출을 통한 공통 Agent 지침 전달.md
depends_on: []
complexity: medium
risk: medium
priority: normal
---

## Purpose

자체 verify에서 실제 `docs/AGENTS.*.md`의 정책 문구를 검사하는 잔여 테스트를 고정 샘플 기반 기능 검증으로 바꾸고, 소비가 제거된 지침 원문을 verify 입력 목록에서 제외한다. 지침을 읽고 합성·전달·해시·거부하는 제품 기능과 현재 runtime 지침을 적용하는 Agent 동작은 유지한다.

revision `ce0b989bda05fc89d6405d2577d7a3bb199e6259`에서 일반 verify의 코드 입력 관측·Worker 소비 검토·문서 제외 재확인과 원래 run 재사용은 구현돼 있다. 그러나 `tests/test_loop_manager.py`와 `tests/test_verify_contract.py`는 실제 Worker 지침을 읽어 특정 안내 문구를 요구하고, `tests/test_agent_invocation.py`는 실제 지침 묶음을 임시 runtime에 복사해 정책 문구를 검사한다. `scripts/verify-inputs.json`도 공통·역할·Ponytail 원문 6개를 포함하며 `tests/test_verify_inputs.py`가 그 포함을 요구한다. 지침 표현 변경이 자체 verify의 테스트 결과·코드 입력을 바꾸는 R-072의 잔여 범위다.

## Acceptance Criteria

- R-072: 자체 테스트는 저장소의 실제 AGENTS·README·제품 문서·Skill prose의 정책·안내 문구를 검사하지 않는다. 확인한 지침 원문 소비자를 고정 샘플 또는 테스트가 소유한 임시 원문으로 전환하고, 동일한 실제 원문 소비가 다른 실행·준비 경로에 남았는지 확인한다. 원본에서 샘플을 자동 생성·동기화하지 않는다.
- R-064: 현재 runtime 원문 선택, common+해당 역할과 Worker 전용 Ponytail의 정확한 전달, 실제 전달 해시, 신규·재개·fallback, 후보 지침 편집과 실행 원본의 분리 및 원본 오류 거부를 기존 실제 코드와 모의 provider로 계속 검증한다. 정책 문구의 존재 검사만 제거하며 instruction 전달·작업 문맥·결과 protocol 검증을 약화하지 않는다.
- 실제 테스트 소비 제거를 확인한 뒤 `scripts/verify-inputs.json`의 공통·역할·Ponytail 원문 6개만 제외한다. 테스트의 고정 샘플, 실행 코드·Skill 스크립트·의존성·verify/copy/launcher 및 소비되는 setup·Windows driver/선택 목록은 계속 입력이다. 단순 확장자·폴더 분류나 과거 PASS만으로 제외하지 않는다.
- 목록 변경은 과거 Worker 제외 선언·completeness와 verify 입력 지문을 무효화한다. 새 코드·목록 기준의 유효한 실제 전체 run과 소비 검토 근거를 확보한 뒤에만 문서 내용 변경의 재확인·성공 재사용을 허용한다. 누락 의심·새 코드·관측 오류·symlink·새 실패와 R-086 일반 선행 승인 뒤의 실제 전체 실행 조건을 유지한다.
- 실제 runtime 지침 변경은 계속 planning·Worker·Review 입력과 호출별 전달 해시에 반영된다. verify 목록에서 제외했다는 이유로 Agent 성공·Review 승인을 재사용하거나 지침 품질 확인을 자동 verify가 보장한다고 설명하지 않는다. 모든 명세·필수 검증·읽기 전용 Review·게시 gate는 유지한다.

## Implementation

- [ ] 확인한 `tests/test_loop_manager.py:PublicFacadeTest.test_worker_and_reviewer_prompts_define_test_execution_boundary`, `tests/test_verify_contract.py:EntrypointAndGuidanceTests.test_role_prompts_only_link_declaration_and_support_context`, `tests/test_agent_invocation.py:CommonGuidanceInvocationTests.test_real_worker_prompts_deliver_static_rules_only_in_pinned_instruction`의 실제 원문 읽기와 정책 문구 assertion을 정리한다. 혼합 테스트의 결과 형식·미실행 사유·후보별 검증 근거·prompt delta 검사는 보존하고, 소유한 fixture 내용의 전달/비중복·provider 요청을 검사한다. 순수 정책 검사만 제거할 때는 제거 대상과 남은 기능 회귀를 대응시킨다.
- [ ] 확인한 `tests/support/fixed_samples.py`의 `PROJECT_FILES`·`use_fixed_guidance()`, `tests/support/workspace.py:pin_common_guidance()`와 `tests/fixtures/project-files/docs/AGENTS.*.md`를 우선 재사용한다. `CommonGuidanceInvocationTests`가 이미 만드는 임시 runtime과 `tests/test_support_fixtures.py`의 원본 읽기 경계 회귀도 재사용한다. 부족한 입력은 필요한 짧은 고정 값만 보완하며 실제 원문 복사·언어별 지침 사본·새 fixture 엔진을 만들지 않는다.
- [ ] 확인한 `loop_manager_internal/agents/common_guidance.py`의 `RUNTIME_ROOT`·`resolve()`·전체 원본/실제 전달 해시 소비자를 테스트 준비부터 후속 해시·재개까지 대조한다. 제품의 현재 runtime 원문 선택은 바꾸지 않고 테스트만 소유한 입력을 사용한다. 다른 테스트의 원문 소비가 발견되면 같은 목표에 필요한 부분만 함께 전환한다.
- [ ] 확인한 `scripts/verify-inputs.json`과 `tests/test_verify_inputs.py:test_repository_manifest_covers_execution_inputs_and_fixed_samples`를 소비 제거 결과에 맞춘다. `loop_manager_internal/verification/inputs.py`·`manager_adapter.py`, `loop_manager_internal/execution/result_validation.py`·`execution_ports.py`의 현재 관측·제외 재확인·Worker 근거 전달과 `loop_manager_internal/storage/verify_receipts.py`의 실제 run/CAS를 재사용하며 일반 verify 재사용을 다시 구현하지 않는다.
- [ ] PRODUCT·ARCHITECTURE와 `docs/VERIFY.md`의 소유 절에 실제 입력 범위와 지침 기능 검증/정책 문구 검토의 경계를 통합한다. 지침 파일 자체의 정책이나 Requirement·다른 ExecPlan은 변경하지 않는다. G-227의 Reviewer prompt 중복 제거와 G-183의 최종 Review 재사용은 해당 작업에 남긴다.

## Validation

- Worker는 위 세 소비 테스트와 `tests/test_verify_inputs.py`·`tests/test_support_fixtures.py`의 변경 관련 최소 unit case를 실행한다. 실제 저장소 지침 읽기를 거부하는 기존 mock 경계를 재사용해 준비·provider 요청·후속 해시까지 고정 샘플만 소비하는지 확인한다. 실제 source 파일을 변경하거나 실제 Agent를 호출하지 않는다.
- 같은 호출 fixture에서 common/역할/Ponytail 합성 bytes·해시·Worker 한 번 추가·다른 역할 제외, 신규/재개/새 세션·현재 원본 오류와 후보 편집본 미채택을 계속 검사한다. `CommonGuidanceInvocationTests`의 기존 역할 변경·원본 누락·과거 snapshot 보존/미사용 회귀와 `tests/test_agent_invocation_state.py`를 재사용하며 같은 조건/assertion을 복제하지 않는다.
- `tests/test_verify_inputs.py`의 실제 임시 파일 관측에서 지침 prose만의 bytes/mode 변경은 코드 지문을 바꾸지 않고 현재 제외 근거를 갱신하는지 확인한다. 반대로 고정 샘플·실행 코드·manifest·진입점/판정 규칙 변경과 새 목록 밖 코드·불명확한 소비는 기존 선언/성공 재사용을 거부해야 한다. 입력 범위 축소 전의 token을 새 기준 성공으로 옮기지 않는다.
- `tests/test_verify_receipts.py`의 production port·실제 SQLite fixture로 같은 작업 주기의 원래 run 연결, 문서 재확인, 새 실패/중단·경쟁 거부를 유지한다. planning/Worker/Review의 지침 입력 갱신과 verify의 코드 입력 비교를 구별하고 Review 승인·invocation·전체 실행 성공을 합성하지 않는다. 관련 대상만 보강하며 기존 전체 receipt·가드·수집/fixture·필수 compile 회귀는 유지한다.
- 제거한 정책 문구 assertion과 계속 검증하는 전달·오류·protocol case를 Worker 결과에 남긴다. 실제 지침의 내용·정합성은 변경 diff와 명세를 읽는 Review 대상으로 유지하고 자동 테스트에 문구 검사를 다시 넣지 않는다.

## Progress

- [x] 지정 revision의 PRODUCT·ARCHITECTURE와 활성 Requirement 29개 전문, 선택 adapter/환경·지원 계약, 관련 비종료 계획과 위 코드·테스트를 정적으로 대조했다. 실제 원문 소비 세 곳과 입력 목록 6개를 확인했으며 구현·테스트·전체 verify는 실행하지 않았다.
- [ ] 실제 지침 원문 소비와 정책 문구 assertion의 고정 샘플 전환
- [ ] 입력 목록·문서 정합화 및 기능/재사용 회귀 보존

## Decision Log

- G-241에 반영된 코드 입력 관측·제외 재확인·원래 run 재사용을 다시 계획하지 않는다. 이 작업은 최신 코드에도 남은 R-072의 실제 지침 prose 의존성과 그 목록 범위만 맡는다. catalog의 G-219는 Ponytail 원문·사용 안내, G-227은 Reviewer prompt의 정적 규칙 중복 제거, G-183은 마지막 최종 승인 재사용으로 목표가 다르다. 독립적으로 구현·검증·Merge할 수 있어 선행 dependency는 없다.
- G-227은 같은 전달 fixture를 소비하므로 허용 snapshot에서 고정 샘플 사용 조건과 선행 관계를 함께 개정한다. 지침 prose 검사 제거가 실제 instruction·권한·검증 책임의 제거로 되지 않게 한다. G-226 보존 후보와 수동 G-303의 코드·문서·실행 기록은 읽기 전용이며 수정하거나 완료로 판단하지 않는다.
- complexity medium(최초 level 3): 기존 fixture·제품 경계를 재사용하되 여러 테스트의 준비/호출/후속 해시와 입력 목록을 함께 대조해야 한다. risk medium: 잘못된 소비 제외는 verify 재사용 범위를 넓힐 수 있으므로 먼저 소비를 제거하고, 목록 변경에 따른 기존 증거 무효화·실제 run·새 소비 거부를 검증한다. priority normal은 기존 핵심 검증 계약의 잔여 목표 기준이다. Model·Effort는 Manager 설정을 따른다.
- 선택 adapter는 unittest version 1·launcher Python 3.11–3.14, 모듈별 독립 실행과 필수 compile이다. 이 결과는 full planning의 미충족 작업 구성이며 자동 검증 성공·Requirement 충족·프로젝트 최종 전문 확인을 뜻하지 않는다.
