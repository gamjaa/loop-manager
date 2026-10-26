---
id: G-320
title: base 문서 유입 뒤 단계별 성공 근거 보존과 재연결
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-072] 입력 동일성이 증명된 구현 후보 성공 결과 재사용.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
  - docs/requirements/[R-086] 영향 기반 부분 검증과 일반 선행 Review.md
depends_on:
  - G-182
  - G-241
  - G-242
  - G-315
complexity: high
risk: high
priority: high
---

## Purpose

최신 base에만 유입된 무관한 Requirement·ExecPlan 변경 뒤에도 원래 Worker·targeted test·전체 verify·최종 Review 근거를 보존하고, 단계별 입력을 확인해 현재 후보에 다시 연결한다. 이 작업은 관련 입력의 비교·근거 보존과 Worker/verify의 재연결을 담당한다. 마지막 최종 Review의 승인 receipt 생성·동일성 판단·재사용은 기존 G-183이 이 결과를 소비해 완성한다.

현재 clean rebase의 Worker 보존과 `verify_receipts`의 동일 코드 입력 재사용은 구현돼 있다. 남은 문제는 `managed_rebase_adoption.revalidation_values()`의 일괄 초기화, revision gate의 base/HEAD token 변화에 따른 단계 복귀와 `VerifyHandler` 재사용 경로의 Review 근거 삭제다. 또한 `stage_documents.related_paths()`와 source 지문만으로는 직접 sources 밖의 실제 관련성과 무관한 base 유입을 모두 증명하지 못한다. 이미 구현된 재사용 엔진을 다시 만들지 않고 이 소비 경계를 연결한다.

## Acceptance Criteria

- R-072/R-082: 원래 후보와 그 base, 현재 후보와 그 base를 각각 연결해 선택 작업의 실제 변경·경로·kind·mode 및 구현 조건을 비교한다. 최신 base에만 유입된 다른 Requirement·ExecPlan의 추가·수정·archive를 선택 작업의 변경으로 합산하지 않는다. 다른 ID·직접 sources 밖이라는 이유만으로 무관함을 확정하지 않으며 dependency·사용자 결정·적용 제품 규칙과 공유 구현/검증 조건을 확인한다.
- 프로젝트의 유효하게 반영된 일반 planning 결과와 작업의 마지막 반영 조건을 재사용한다. 명세 변경의 planning·게시·catalog·DB 확정을 유지하되 같은 관련성 판단을 별도 Planner/모델 호출로 반복하지 않는다. 과거 입력·관계·실제 소비를 확인할 수 없으면 해당 단계만 기존 재확인·교정 경로로 돌아간다.
- rebase 시작·Git 완료·revision gate·저장 단계 복구에서 원래 성공과 입력의 출처·작업 주기·session·실패 이력을 보존한다. 낡은 token을 현재 승인으로 쓰지 않으면서도 원래 성공을 지워 불필요한 호출을 강제하지 않는다. 현재 후보 연결은 관측과 소유권/generation 재확인 후 확정한다.
- 유효한 완료 Worker·targeted test와 관련 구현 입력이 같으면 무관한 base 문서 변경 및 문서 충돌의 최신 base 자동 채택 뒤 Worker·완료한 targeted test 호출이 0이다. 구현 조건·충돌 해결 결과가 달라졌거나 교정이 남으면 보존된 session에서 필요한 작업을 수행한다.
- 같은 프로젝트·ExecPlan·작업 주기의 실제 전체 verify 성공이 있고 현재 코드 입력과 문서 제외/누락 확인이 유효하면 원래 run을 재연결하며 verify 호출이 0이다. 코드·테스트·고정 자료·검증 선언/진입점 변경, 비교 실패·성공 부재와 새 실패는 기존 전체 실행을 유지한다. 설치 환경·외부 동적 입력 미확인을 새 gate로 추가하지 않는다.
- 최종 Review의 원래 승인·실제 호출 identity·검토 입력은 G-183이 비교할 수 있게 보존하되 이 작업만으로 현재 후보의 Review를 승인하지 않는다. R-086의 미최종 일반 선행 PASS 뒤에는 실제 전체 verify를 실행하고 문서 선행 PASS/VERIFY_REQUIRED·부분 PASS·수동 면제를 최종 성공으로 바꾸지 않는다.
- 중단·재시작·게시 직전 경쟁에서 원래 근거와 현재 연결을 다시 검증한다. 다른 작업/주기 성공·미완료·불완전 입력은 승계하지 않으며 실행 생략으로 새 invocation·성공 실행·절감 수치·실패 초기화를 만들지 않는다. 최신 lease·PR identity·dependency·사용자 승인과 최종 게시 gate는 유지한다.

## Implementation

- [ ] 확인한 `loop_manager_internal/execution/execution_revision.py`·`execution_revision_handler.py`·`plan_validation.py`와 `stage_documents.py`의 definition/source/문서 변화 경계를 통합한다. base 유입과 각 후보의 실제 작업 변경을 구분하고, 이미 확정된 프로젝트 planning 및 관련 입력 근거를 소비한다. 선언·source·dependency·사용자 판단·적용 규칙·실제 코드 소비를 확인한 관계만 비교에 사용한다. 새 범용 의존성 그래프나 작업별 영향 Planner를 추가하지 않는다.
- [ ] 확인한 `git/managed_rebase_adoption.py`·`execution/execution_rebase_handler.py`·`execution_workspace_handler.py`와 `execution_state.py`의 기존 checkpoint/CAS에 원래 성공 근거 보존·단계별 재판정을 연결한다. 직접 구현된 `retain_clean_rebase_worker()`·완료 rebase 선처리·공식 선언 복원·Worker checkpoint 게시 순서를 재사용한다. 진행 중 operation·target·사용자 index/파일을 초기화하거나 중간 형상을 최종 후보로 승인하지 않는다.
- [ ] 확인한 `execution/execution_verify_handler.py`·`storage/verify_receipts.py`와 기존 입력 관측을 재사용해 코드가 같은 후보의 원래 run 연결을 유지한다. verify 재연결 중 G-183이 필요한 마지막 승인 자료를 삭제하지 않되 현재 Review token은 별도 승인 확인 전 사용할 수 없게 한다. 호환 근거가 없는 과거 행은 현재 값으로 보충하지 않고 필요한 실제 단계를 실행한다.
- [ ] 확인한 `tests/test_execution_revision_gate.py`·`tests/test_verify_receipts.py`·`tests/test_execution_current_rebase_adoption_phase.py`의 모의 Git/실제 SQLite fixture와 `tests/test_managed_rebase_lifecycle.py`의 효과 회귀를 재사용한다. G-315의 prose 원본 의존성 제거·G-241의 코드 입력 receipt·G-242의 문서 반영을 중복 구현하지 않는다. PRODUCT의 `#### 후보 갱신과 성공 결과 재사용`, ARCHITECTURE의 단계별 문서 반영·rebase·성공 receipt 설명은 실제 구현에 맞게 소유 절에 통합한다.

## Validation

- Worker는 위 revision/receipt/현재 rebase의 변경 관련 최소 unit case만 실행한다. G-A에 완료 Worker·targeted test·실제 full PASS를 준비하고 무관한 G-B ExecPlan/Requirement 추가·수정·archive를 모의 base로 제공한다. 원래 근거·새 후보 연결, Worker/targeted/verify 호출 0, session·실패 이력·checkpoint 보존을 검사한다. 최종 Reviewer 호출 0을 포함한 통합 판정은 G-183이 담당한다.
- 문서 충돌의 자동 최신본 채택, Progress만의 변화, 선택 작업 직접 문서 수정·경로/mode 변경, sources 밖의 실제 관련 조건·dependency/결정 변화와 비교 불가를 구별한다. 조건이 달라진 단계만 재판정하며 구현 교정이나 필수 검증을 무관한 문서로 숨기지 않아야 한다.
- 기존 `test_proven_success_reuses_original_run_without_new_invocation_or_audit`와 production-port 문서 재관측 회귀를 유지한다. 코드 입력 변경·목록 누락·새 실패/중단·다른 주기에는 재사용을 거부하고 일반 선행 PASS 뒤에는 같은 코드여도 실제 전체 run이 있어야 한다. 보존된 최종 Review 자료만으로 게시가 허용되면 실패다.
- 저장 전후 경쟁·rebase 완료 뒤 재시작·verify 재연결 뒤 재시작에서 원래 run/승인 자료와 현재 관측을 다시 결합한다. 실제 Git이 필요한 효과는 기존 lifecycle fixture에 최소 사례로 연결하고 정책 조합마다 rebase·push를 반복하지 않는다. 기존의 무조건 verify/Review 초기화를 기대하는 assertion은 새 조건에 맞게 교체하되 변경 입력·실패·소유권 거부 검증은 유지한다.

## Progress

- [x] revision `296da23b69e3bc76df27264eeb978c46d7caa100`의 R-072/R-082 delta, 기존 Worker/verify 재사용과 남은 초기화·revision 소비 경계를 정적으로 대조했다. 구현·테스트·verify는 실행하지 않았다.
- rebase 채택의 기존 `rebase_revalidation_audit`에 원래 definition·base·작업/session·Agent 입력/결과 snapshot과 checkpoint generation, 선택 verify cycle 및 실제 receipt를 함께 보존했다. 현재 후보 token 초기화와 소유권/CAS 경계는 유지하며 과거 자료가 없으면 현재 값으로 보충하지 않는다. 이 중간 변경은 재사용 승인을 추가하지 않는다.
- targeted runner로 `tests.test_runtime_state.CurrentRebaseRuntimeTests`의 `test_adoption_preserves_original_stage_provenance_without_reapproving_it`, `test_current_acceptance_rolls_back_checkpoint_plan_project_and_outbox`, `test_current_acceptance_is_atomic_idempotent_and_never_execution_proof`와 `tests.test_execution_current_rebase_adoption_phase.CurrentRebaseAdoptionTests.test_manual_completion_without_journal_invalidates_worker_and_validation_once`를 각각 실행해 4개 모두 PASS했다. 새 회귀는 성공·실패·미완료·면제·기록 부재와 새 cycle에서 과거 성공을 승격하지 않는 조건을 확인한다. 전체 verify는 Worker에서 실행하지 않았다.
- 남은 구현은 rebase 시작 전 근거 보존, 일반 planning의 관련성 근거 소비, 후보별 실제 변경/입력 비교, revision gate·verify 재연결 및 중단 복구다. PRODUCT·ARCHITECTURE의 최종 동작 반영과 완료 회귀는 해당 연결 구현 뒤 수행한다.
- [ ] 관련 입력 비교와 원래 성공 근거 보존
- [ ] Worker/verify 재연결·중단 복구 및 최소 회귀

## Decision Log

- G-183의 기존 최종 Review 목표를 중복 계획하지 않는다. 이 작업은 그 승인 원문을 보존하는 기반과 Worker/verify의 연결을 먼저 Merge할 수 있고, G-183은 실제 Reviewer identity·선택 작업 PR 변경·최종 승인과 게시를 결합한다. G-315가 제거하는 자체 verify의 prose 의존성을 선행으로 소비하며 단순 clean rebase Worker 보존·코드 receipt 재사용을 소급 작업으로 만들지 않는다.
- complexity high: 관련성·후보별 base·단계별 근거 수명과 rebase/DB 중단 복구를 함께 유지해야 한다. risk high: 잘못된 동일성 판정이 변경된 후보를 미검증 상태로 게시할 수 있다. 출처 보존·보수적 fallback·단계별 CAS와 경쟁/실패 회귀로 완화한다. priority high는 불필요한 자동 호출을 줄이면서 필수 승인 경계를 보존하는 핵심 자동화다. 최초 level은 5이며 Model·Effort는 Manager 설정을 따른다.
