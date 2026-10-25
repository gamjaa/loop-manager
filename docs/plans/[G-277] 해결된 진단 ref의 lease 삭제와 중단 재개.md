---
id: G-277
title: 해결된 진단 ref의 lease 삭제와 중단 재개
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-083] 충돌 해결 후 원격 진단 브랜치 정리.md
depends_on:
  - G-276
complexity: high
risk: high
priority: normal
---

## Purpose

R-083의 충돌 해결 진단 ref에 한해, 원인 해소·해결 결과의 정상 원격 반영·다른 원인과 진행 중 게시의 사용 종료·로컬 원문 복구 가능성을 확인한 뒤 저장된 게시 OID의 정확한 lease로 삭제한다. PR Merge는 추가로 기다리지 않으며 원래 작업·base·사용자·다른 작업 branch는 보존한다. 경쟁·보존 실패·사용 중·불확실한 대상은 남기고, 중단 뒤 실제 원격과 저장 근거를 대조해 재개한다. 확인된 사용자 결정의 범위를 구현하며 draft 정리·대기 해제와 게시 확정은 Manager가 검증한다.

## Acceptance Criteria

- 승인된 R-083 대상만 첫 안전한 실행 경계에서 정리한다. 삭제 직전 origin/게시 대상·작업/원인·정상 원격 반영·SQLite 해소·모든 ref 사용자·자료 보존과 자원 소유를 재검증한다. PR Merge를 추가 조건으로 요구하거나 다른 차단·실행 gate를 함께 해제하지 않는다.
- `diagnostic_cleanup_preparations`의 `ready`·`basis`는 재관측할 입력이며 삭제 승인이 아니다. 현재 원인별 사용·generation·호출 예약·checkpoint·origin·자료·정상 branch 게시가 준비 시점과 달라지면 낡은 준비로 삭제하지 않는다. WAITING 해제는 같은 target의 현재 COMPLETE가 확인돼야 하고, 해소 때 남은 작업물은 정상 원격 게시가 별도로 확인돼야 한다.
- 정확한 ref의 현재 OID가 저장된 게시 성공 OID와 같을 때만 그 값의 lease로 해당 ref를 삭제한다. 원래 작업·base·사용자·다른 작업 branch는 변경하지 않는다. 처음부터 다른 OID와 조회/삭제 사이 경쟁은 보존하며 현재 OID를 새 기대값으로 채택하지 않는다.
- 원격 부재가 확인되면 push·재생성 없이 정리 완료로 처리한다. 조회 실패·불명확 응답은 부재가 아니며 삭제 뒤 확인 실패는 미확정으로 유지한다. 원문 자료·과거 ref/OID·게시·실패·session·감사는 삭제하지 않고 현재 ref 존재와 과거 게시 성공을 구분한다.
- 삭제 전 intent, 삭제 성공 뒤, 부재 확인 뒤 및 SQLite 확정 전의 중단은 실제 ref·현재 안전 조건·저장 근거를 대조해 멱등 재개한다. 진행 중 보존/게시와 같은 ref를 동시에 변경하지 않는다.
- 일시 조회·삭제 실패는 다음 정상 실행에서 재검증해 제한적으로 재시도한다. 반복 경쟁·권한/보호 정책 거부·소유권 또는 자료 부족은 같은 근거로 삭제 push를 반복하지 않는다. 실패가 작업 재차단·후속 진행 중단·중복 알림을 만들지 않는다.
- 충분한 과거 기록의 대상도 같은 정리에 연결하고 부족한 기록은 보존한다. 정리된 과거 intent의 재처리가 ref를 되살리거나 현재 원격 보존 성공으로 보고하지 않으며 새 충돌은 독립 원인으로 정상 보존한다.
- PRODUCT·ARCHITECTURE의 진단 수명·원격 안전 경계는 승인 후 실제 구현한 범위에 맞게 통합한다. G-261의 작업 자원 정리나 로컬 자료 수명·일반 force push·자동 Merge·검증/Review 조건을 확대하지 않는다.

## Implementation

- [x] 확인한 `loop_manager_internal/recovery/diagnostic_cleanup.py`의 `_snapshot`·`_observe`·`assess_ref`와 `prepare_recorded`를 재사용해 삭제 직전의 자격 재검증을 연결한다. `diagnostic_ref_uses`·`diagnostic_cause_resolutions`·게시 intent의 현재 근거를 소비하고 원인 기록·bundle 검증·준비 엔진을 다시 만들지 않는다. acceptance의 repo identity와 등록 origin은 현재 계약대로 별도로 대조하며, WAITING 후 COMPLETE·미게시 작업물·복수 사용의 보존 조건을 유지한다.
- [x] 확인한 `application/project_runner.py:run_project_result`와 `application/bindings.py`·`recovery/bindings.py`의 준비 callback을 출발점으로 첫 안전한 정리 경계에 삭제 handler를 연결한다. 준비 기록만 보고 삭제하거나 PR Merge까지 미루지 않으며, PR 대기에서도 같은 현재 조건을 적용한다. 대상 자원의 진행 중/종료 불명 호출·게시와 경합하면 해당 정리만 보류하고 runtime handoff에서는 새 관측·삭제를 시작하지 않는다. 외부 관측·push 동안 DB 접근을 반환하고, 짧은 적용 transaction에서 소유권·사용 generation·basis를 다시 확인한다.
- [x] 확인한 `recovery/checkpoint_adapter.py`의 `_validated_push_url`·`_frozen_push_target`과 `git/manager_git_journal.py`의 `parse_remote_branch_oid`·`exact_branch_lease`를 재사용한다. 저장된 게시 성공 OID를 lease로 고정한 진단 ref 삭제 효과를 추가하되 생성 전용 `push_diagnostic_checkpoint`의 의미는 유지한다. hooks·tracking ref·다른 ref 보호와 기존 자원 조율을 지킨다. 현재 journal의 일반 허용 전이를 진단 삭제 성공으로 가장하지 않고, 삭제 intent·실제 원격 부재·완료 확정을 이 정리의 저장 근거로 연결한다.
- [x] 확인한 `storage/blocked_checkpoint_state.py`의 기존 세 준비 테이블과 게시/보존 기록을 유지하며 삭제 intent·기대 OID·사용/basis·시도 결과·미확정/완료·재평가 사유를 호환 가능하게 저장한다. 기존 payload/감사를 덮어 정리 완료를 표현하지 않는다. 원격 효과와 SQLite 확정을 원자 작업으로 가정하지 않고, 삭제 성공 뒤 기록 실패도 실제 부재와 현재 안전 조건을 대조해 재개한다.
- [x] 확인한 `recovery/diagnostic_preservation.py:preserve`, `project_blocked_recovery.py`의 진단 `_publish`, `storage/blocked_checkpoint_state.py`의 보존·게시 결과 조회를 삭제 수명과 연결한다. 정리된 과거 intent/receipt는 ref 재생성이나 현재 원격 보존 성공으로 보고하지 않고 원문·과거 성공·정리 감사를 함께 유지한다. 새 충돌은 독립 원인으로 기존 보존 정책을 적용하며 부족한 과거 근거를 원격 namespace 탐색이나 추정 backfill로 보충하지 않는다.
- [x] 확인한 `tests/test_diagnostic_cleanup.py`·`tests/support/diagnostic.py`, `tests/test_project_diagnostic_publication.py`, `tests/test_diagnostic_preservation.py`, `tests/test_blocked_checkpoint_state.py`의 fixture와 실제 판단·저장 함수를 재사용해 아래 삭제 고유 회귀를 추가한다. 이미 있는 준비·원문 검증 assertion은 유지하고 같은 조건을 복제하지 않는다. PRODUCT의 현재 준비 전용 설명과 ARCHITECTURE의 정리 준비 근거 절을 실제 삭제·보존·실패/재개 계약으로 통합한다. 별도 테스트 파일이 필요하면 예정 경로로 명시한다.

## Validation

- Worker는 `tests/test_diagnostic_cleanup.py`의 삭제 판정·run 경계, `tests/test_project_diagnostic_publication.py:ProjectDiagnosticPublicationUnitTests`의 삭제 인자/응답 검사와 저장·과거 intent 재사용을 변경한 최소 unit case만 실행한다. 실제 정책·SQLite·원문 파일과 모의 Git 관측을 사용하며 기존 `DiagnosticCleanupTests`의 준비 조건·`ArtifactRecoveryTests`의 독립 복구 검증은 재사용한다.
- 준비 뒤 origin·자료·원인 사용/generation·checkpoint·호출 예약이 바뀌면 삭제 push가 0회인지 확인한다. 같은 target의 WAITING→COMPLETE, 해소 때 남은 작업의 clean 게시 후속 commit, 복수 원인/ref·진행 중 게시, PR 대기·별개 차단 보존을 실제 삭제 진입과 연결한다. 원격 처리 중 다른 DB 연결의 쓰기가 가능하고 경쟁 변경 뒤 낡은 결과 적용만 거부되는지 검사한다.
- 임시 로컬 원격의 최소 Git 통합 회귀는 Manager 검증에 둔다. 기존 `tests/support/git.py`의 seed부터 격리된 fixture를 사용해 저장 OID의 정확한 lease 삭제와 삭제 직전 경쟁 거부, 원래/base/사용자/tracking ref 불변·로컬 원문 복구를 확인한다. 기존 생성 전용 회귀만으로 증명할 수 없는 삭제 효과를 검사하며 정책 조합별 Git 실행은 반복하지 않는다.
- 삭제 전·push 후·부재 확인 후·DB 확정 전 중단, 이미 없는 ref, 삭제 후 조회 실패, 일시 오류와 반복 경쟁/권한 거부를 검사한다. 미확정 보존·다음 정상 실행 재시도·동일 근거의 반복 push 방지·중복 재생성/알림 없음과 독립 진행을 확인한다. 정리 실패와 runtime handoff에서 기존 run 결과·다른 차단·session·작업물이 바뀌지 않아야 한다.
- 과거 보존 intent·감사 조회, 충분/불충분한 과거 ref, 정리된 intent 재시도와 새 충돌 보존, checkpoint/rebase/PR gate를 후속 소비자까지 확인한다. 과거 게시 성공과 현재 원격 부재를 구별하고 원격 정리 결과를 검증·Review·Merge 성공으로 사용하면 실패해야 한다. 현재 unittest version 1·launcher Python·독립 tests와 필수 compile 계약을 유지하며 native 원격/OS 성공을 mock 결과로 대신하지 않는다.

## Progress

후속 Worker 결과 검증에서 TARGETED_TESTS의 테스트 설명 “조회 실패 재개 회귀” 중 “실패”가 미해결 결과로 해석된 보고 문제를 확인했다. 직전 호출의 원본 targeted JSONL에서 신규 2개·기존 2개 모두 success임을 확인했고, 설명을 “조회 복구 회귀”로 교정한 전체 보고를 실제 parse_worker_result가 수용했다. 재삭제 교정·회귀·assertion은 그대로 보존하며 제품 코드나 이미 통과한 테스트를 반복 변경·실행하지 않았다.

Reviewer의 `completed-intent-redelete` 피드백을 교정했다. 완료 기록은 현재 basis에 한정하지 않고 같은 repo·ref·저장 게시 OID의 과거 complete에서 원인·입력·origin·원래 branch·tree/commit·확정 OID로 게시 사용을 대조한다. 모든 현재 사용이 이미 완료됐으면 무관한 차단이나 generation 변경으로 basis가 달라져도 복원된 ref를 다시 삭제하지 않는다. 새로운 원인·게시 사용이 있으면 기존 현재성·해소 조건을 모두 확인한 뒤 별도로 정리한다. 프로젝트 Python과 지정 targeted runner로 복원 후 basis 변경·새 사용의 후속 정리 신규 회귀 2개와 단계별 중단·삭제 후 조회 실패 재개 기존 회귀 2개를 실행해 4개 통과했다. PRODUCT·ARCHITECTURE의 기존 재삭제 금지 계약과 schema·필수 검증은 유지하며 전체 verify·Review는 Manager에 인계한다.

Manager의 전체 verify 피드백 `verify:961fc1cb84519e2c64325de33b660d665b10f7a0a5e6ff973b935eefdc6012e7`에서 compatibility export가 effects 주석을 모듈 내 타입 이름 문자열로 조회하는 계약을 확인했다. 새 `cleanup_recorded`의 주석만 기존 `prepare_recorded`와 같은 문자열 형식으로 교정했다. 기존 export 회귀 `tests.test_project_services.ProjectServiceCompositionTests.test_compatibility_exports_bind_the_matching_named_effect_contract`를 지정 targeted runner로 실행해 1개 통과했다. runtime 동작·검사 assertion·Requirement·frontmatter는 변경하지 않았으며 전체 verify 재실행은 Manager에 남긴다. 아래 결과는 이전 구현·교정 호출의 이력이다.

G-276의 준비 판정·원문 복구·run 경계를 재사용해 자원 잠금 아래 삭제 직전 재관측, 저장 OID의 정확한 lease 삭제, 원격 부재 확인과 중단 재개를 구현했다. `diagnostic_cleanup_attempts`에 basis별 intent·시도 예약·단계/실패 감사를 보존하고 같은 근거의 일시 시도는 최대 3회, lease/정책 거부는 재push하지 않는다. 보존·프로젝트 진단 게시가 같은 ref 잠금을 공유하며 정리된 과거 원인의 재생성과 현재 원격 게시 성공 오보를 막는다. PRODUCT·ARCHITECTURE에 제품 수명·호환 저장 계약을 반영했다. 기존 사용자 판단·선언은 변경하지 않았다.

Worker targeted 결과: 삭제/준비·보존·게시 인자 단위 61개, 기존 프로젝트 게시 단위 11개, 삭제 직전 generation 경쟁·과거 프로젝트 intent·알림 경계 3개, pending handoff 1개를 실행했다. 추가로 복수 원인·미게시 작업물·WAITING→COMPLETE·독립 차단의 실제 삭제 경로 4개를 좁혀 검증했다. 실패한 두 실행은 각각 새 checkpoint fixture 필수 인자 누락과 프로젝트 fixture tree 불일치였으며 교정 뒤 동일 명령으로 통과했다. 최초 34개 준비/삭제 실행의 재실행도 통과했다. 전체 verify와 Windows/실제 Git 실행 성공을 주장하지 않는다. 후속 결과 검증에서 최종 보고의 FAILED_COMMAND와 RERUN 사이에 본래 실패 결과를 빠뜨린 형식 오류를 확인했다. 원본 targeted JSONL의 76개 test ID를 시각순으로 대조해 두 과거 오류 모두 최신 success임을 확인했고, 실패 결과를 명령 뒤·재실행 앞에 명시한 전체 보고를 실제 parse_worker_result가 수용함을 확인했다. 제품 코드나 검증 조건은 바꾸지 않았으며 이미 성공한 unit test는 반복하지 않았다.

Manager 검증용 `tests/test_diagnostic_cleanup.py:DiagnosticCleanupGitTests`는 기존 격리 seed와 실제 orphan 자료를 사용해 단일 lease 삭제·삭제 직전 경쟁 거부·원래/base/사용자/다른 branch·tracking ref·로컬 원문 보존을 검사한다. Worker는 이 native 통합 회귀와 전체 verify를 실행하지 않았다. 새 파일·dependency·Requirement 변경은 없다.

- [x] 2026-10-16 사용자 요청 “277은 권장 방향으로 적용해주고”에 따라 선택지 A 승인을 위 User Decision에 기록했다.
- [x] 승인 범위의 삭제·재개·보존 재사용 교정
- [x] targeted 검증과 제품·설계 정합성 (전체 verify·native Git 통합 회귀·Review는 Manager 대기)

## Decision Log

- G-276의 `ready`·`basis`와 두 번의 외부 관측은 준비 근거이며, 실제 삭제 직전의 현재성·자원 소유·정확한 lease를 대신하지 않는다. 이번 개정은 그 구현을 소비하도록 Implementation·Validation을 구체화하며 기존 ID·경로·sources·G-276 dependency·분류·priority와 사용자 판단 기록을 보존한다. 준비 기능이나 launcher fixture 교정을 소급 계획하지 않고 실제 삭제의 미충족 범위만 유지한다.
- 질문 해소 검토: 제공된 verified 사용자 변경 `d7a199007b8f7959611d5f7e7fbe7a070f24f5f1`의 새 Answer는 R-083의 대상·원인별 사용 종료·정상 원격 반영·로컬 복구·등록 origin/소유권·저장 OID lease·중단 재개를 모두 선택한다. PR Merge 대기 제외와 불확실한 대상 보존도 명시해 기존 질문 전체를 해소한다. 이를 위 Purpose와 Implementation에 통합했고 실제 ref 삭제·검증은 미완료로 유지한다. 최신 Architecture Change Policy도 이 승인 범위를 반복 승인 대상으로 만들지 않는다. Draft Decision·User Decision·draft 표시는 Manager의 출처·현재 입력·게시 검증을 위해 원문 그대로 남긴다.
- R-083의 목표를 원격 삭제 없이 완료로 처리하지 않는다. 원인/자료 준비와 실제 삭제를 나누어 독립 작업은 진행한다. 2026-10-16의 사용자 요청으로 선택지 A가 승인됐으며, 문서의 draft 표시와 저장된 대기는 사용자 출처·질문 해소를 검증하는 기존 Manager 전환에서 정리한다. 이 승인으로 구현·검증 성공이나 다른 범위의 승인을 만들지 않는다.
- complexity high: ref 사용·원인 해소·실제 게시의 최신 근거를 삭제 직전 경쟁 및 Git/SQLite 사이 중단에 결합하고 과거 intent 재생 방지까지 지켜야 한다.
- risk high: 잘못된 삭제는 사용 중 원격 진단이나 경쟁 commit의 복구 경로를 잃게 한다. 검증한 로컬 원문/감사·정확한 게시 OID lease·자원 조율·미확정 보존으로 복구 가능성을 유지한다. 실제 ref 복원은 보존본과 감사에 따른 수동 복구이며 코드 rollback으로 대체하지 않는다. 위 경쟁·자료 손상·중단 사례에서 보존을 확인한다.
- priority normal: 기존 복구를 막지 않는 필수 진단 수명 정리다. 승인 범위는 충돌 진단 ref에 한정하며 새로운 정리 CLI·기간 정책·비호환 migration·외부 서비스를 도입하지 않는다.
- 기존 사용자 결정 유효성 재검토 (6c2ed6e6c114fb3bffaececc2820a08089174ce4e623e5c81d8257a16fe7c197): 기존 결정의 충돌 진단 ref 한정·원인 해소·정상 원격 반영·로컬 자료 보존·저장 OID의 정확한 lease·중단 재개 조건이 현재 R-083 및 계획 본문과 일치한다.
- 기존 사용자 결정 유효성 재검토 (7d40103008f271a133344175e8d5872871df4358a2f305439e6d81e459abc7fe): 최신 R-083과 계획 본문의 진단 ref 범위·원인 해소·정상 원격 반영·자료 보존·정확한 lease·중단 재개 조건이 기존 결정과 일치하며, 새 검증 launcher는 승인 범위를 바꾸지 않는다.
- 기존 사용자 결정 유효성 재검토 (890f84bcf99a4efd3c15fd1bb1a0e88205be6bbc1a6947fa2fb800de937f8ead): 현재 R-083과 계획의 대상 제한·원인 해소·정상 원격 반영·로컬 자료 보존·정확한 OID lease·중단 재개 조건이 기존 결정과 일치하며 이번 delta는 승인 범위를 바꾸지 않는다.
- 기존 사용자 결정 유효성 재검토 (d7f6d335d3008ed0cb9c8b0ab9d93a87386f6c8ce6820b1c36dda5359919a9b7): ARCHITECTURE의 archive 참조·draft 취소 교정은 기존 R-083의 진단 ref 한정·원인 해소·정상 원격 반영·자료 보존·정확한 OID lease·중단 재개 조건을 변경하지 않는다.
- 기존 사용자 결정 유효성 재검토 (9d8deac37bdbd53e1f73075de5895108ee5b1cf32626fd3dbfdd90ac9db2737d): 현재 R-083과 계획의 충돌 진단 ref 한정·원인 해소·정상 원격 반영·로컬 자료 보존·정확한 게시 OID lease·중단 재개 조건이 기존 결정과 일치하며 이번 delta는 승인 범위를 변경하지 않는다.
- 기존 사용자 결정 유효성 재검토 (6ec7af30810674c09f22c8958ec9df65f4d01b0dd84bbcde0fa72e244c9a814e): 현재 R-083과 계획의 원인 해소·정상 원격 반영·ref 사용 종료·로컬 자료 보존·정확한 게시 OID lease·중단 재개 조건이 기존 결정과 일치하며 이번 transaction 교정은 승인 범위를 변경하지 않는다.
- 기존 사용자 결정 유효성 재검토 (385d73052228fa442c57e7af31f1ad16ff95ce4c2d9248f025d70d58e99efb00): 현재 R-083과 계획의 충돌 진단 ref 한정·원인 해소·정상 원격 반영·ref 사용 종료·로컬 자료 보존·정확한 게시 OID lease·중단 재개 조건이 기존 결정과 일치한다. 이번 변경은 유효성 기록 추가로 승인 범위를 변경하지 않는다.
- 기존 사용자 결정 유효성 재검토 (d792203628b231e447a4abd87a27b10a36d9d87d83c63c8e7714c8e34e2b309a): 현재 R-083과 계획의 대상 제한·원인 해소·정상 원격 반영·사용 종료·로컬 자료 보존·정확한 게시 OID lease·중단 재개 조건이 기존 결정과 일치한다. 이번 기록 추가는 승인 범위를 변경하지 않는다.
- 기존 사용자 결정 유효성 재검토 (b5edba57bc2559260388cbf274d9fc5a4c617aba746e306b9446adfb6eac1244): 현재 R-083과 계획의 대상 제한·원인 해소·정상 원격 반영·사용 종료·자료 보존·정확한 게시 OID lease·중단 재개 조건이 기존 결정과 일치하며 이번 교정은 승인 범위를 변경하지 않는다.
- 기존 사용자 결정 유효성 재검토 (bd3300438f4203ba77a3c30d00f51baff5ce1082264228b080be87697298bae4): 현재 R-083과 계획의 충돌 진단 ref 한정·원인 해소·정상 원격 반영·사용 종료·로컬 자료 보존·정확한 게시 OID lease·중단 재개 조건이 기존 결정과 일치한다. 이번 삭제 source 증명 및 PR 경합 변경은 승인 범위를 변경하지 않는다.
- 기존 사용자 결정 유효성 재검토 (161d29ea36792429c6d294e5acd1d9d1cd83c6797082098d952f2182a830fa67): 현재 R-083과 계획의 대상 제한·원인 해소·정상 원격 반영·사용 종료·로컬 자료 보존·정확한 게시 OID lease·중단 재개 조건이 기존 결정과 일치한다. 선택된 ExecPlan의 문서 보고 제외는 삭제 승인 범위를 변경하지 않는다.
- 기존 사용자 결정 유효성 재검토 (8319ca307c8331610e28d28968786d4a9f5b1a6b5a673b425bf0eb5cee0a2c7d): 현재 R-083과 계획의 대상 제한·원인 해소·정상 원격 반영·자료 보존·정확한 게시 OID lease·중단 재개 조건이 기존 결정과 일치하며 추가 코드 변경은 삭제 승인 범위를 바꾸지 않는다.
- 기존 사용자 결정 유효성 재검토 (a0c4dcb9f7c926bfa641a14a39d75b729d743ec65f2835af5a9010069fabbb1c): 현재 R-083과 계획의 진단 ref 한정·원인 해소·정상 원격 반영·사용 종료·로컬 자료 보존·정확한 게시 OID lease·중단 재개 조건이 기존 결정과 일치하며 이번 delta는 삭제 승인 범위를 변경하지 않는다.
- 기존 사용자 결정 유효성 재검토 (8dc65069532704f63be4035c4e7ef4cb96c5e399af3eca7d444a7d80db1701b2): 현재 R-083과 계획은 충돌 진단 ref 한정·원인 해소·정상 원격 반영·모든 사용자와 게시의 사용 종료·로컬 자료 보존·정확한 게시 OID lease·중단 재개 조건을 유지한다. R-091의 병렬 배정은 삭제 승인 범위를 변경하지 않는다.
- 기존 사용자 결정 유효성 재검토 (4b8b81ff1e86e52daf34923b99d75b9f6d3d41339a44ea97c22b656bdf90d237): 현재 R-083과 계획의 충돌 진단 ref 한정·원인 해소·정상 원격 반영·사용 종료·로컬 자료 보존·정확한 게시 OID lease·중단 재개 조건이 기존 결정과 일치한다. 이번 verify 계약 변경은 삭제 승인 범위를 바꾸지 않는다.
- 기존 사용자 결정 유효성 재검토 (cbb70259dda877f5259e746d46adf3b7327eea36db66bcc7ca83b5afc85e302f): 현재 R-083과 계획의 충돌 진단 ref 한정·원인 해소·정상 원격 반영·사용 종료·로컬 자료 복구·정확한 게시 OID lease·중단 재개 조건이 기존 결정과 일치하며 이번 명세 변경은 승인 범위를 바꾸지 않는다.
- 기존 사용자 결정 유효성 재검토 (448cd9302696427d5d8af049d7d0802cab45379319273533f12534f0dc2b9f6c): 현재 R-083과 계획은 충돌 진단 ref 한정·원인 해소·정상 원격 반영·모든 사용 종료·로컬 자료 보존·정확한 게시 OID lease·중단 재개 조건을 유지한다. R-092의 일괄 fetch와 공유 잠금 변경은 기존 원격 진단 ref 삭제 승인 범위를 변경하지 않는다.
- 기존 사용자 결정 유효성 재검토 (05322994014d78c05c2f4d26040e3fa77e78a73767f45bf73ab4e6cd9d308017): 현재 R-083과 계획의 진단 ref 한정·원인 해소·정상 원격 반영·사용 종료·자료 보존·정확한 게시 OID lease·중단 재개 조건이 기존 결정과 일치하며 이번 변경은 승인 범위를 확대하지 않는다.
- 기존 사용자 결정 유효성 재검토 (9487bcfc997e704ad060f8b89fdc3fc3eb5615b840b08c92940277d16c7c1e91): 현재 R-083과 계획의 진단 ref 한정·원인 해소·정상 원격 반영·사용 종료·자료 보존·정확한 게시 OID lease·중단 재개 조건이 기존 결정과 일치한다. 이번 targeted 기록 결합·재수집 변경은 삭제 승인 범위를 변경하지 않는다.
- 기존 사용자 결정 유효성 재검토 (60f74d929e679f20d47e5ff2a6a67d78eff7dd07b2fb13ed99922d9bfe908b80): 현재 R-083과 계획의 대상 제한·원인 해소·정상 원격 반영·사용 종료·자료 보존·정확한 게시 OID lease·중단 재개 조건은 기존 결정과 일치한다. 이번 DB 접근 변경은 삭제 승인 범위를 바꾸지 않으며 미검증 change-3을 새 승인 근거로 사용하지 않았다.
- 기존 사용자 결정 유효성 재검토 (04983ff0ac7a0f96f86b294bb1ef42b48d1fe4267173fc29f1990b3502a19746): 현재 R-083과 계획의 대상 제한·원인 해소·정상 원격 반영·사용 종료·자료 보존·정확한 게시 OID lease·중단 재개 조건이 기존 결정과 일치한다. 이번 DB·배정 변경은 승인 범위를 바꾸지 않으며 미검증 change-3은 새 승인 근거로 사용하지 않았다.
