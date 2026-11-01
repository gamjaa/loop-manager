---
id: G-302
title: verify 재개 전 targeted 근거 확인
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
depends_on: []
complexity: medium
risk: high
priority: high
---

## Purpose

저장된 VERIFYING 단계에서 Worker를 다시 호출하지 않아도 해당 작업의 targeted 기록을 재수집하고, 세 번째 실패 또는 미소비 근거를 처리한 뒤 검증을 진행한다. PRODUCT의 재수집 후 차단·작업별 대기를 기존 검증 진입에 연결하는 국소 교정이며 호출 결합·collector·실패 집계를 다시 구현하지 않는다.

현재 `ExecutionPhaseCoordinator.dispatch()`는 VERIFYING을 `VerifyHandler.verify()`로 바로 전달한다. 이 handler는 저장된 Worker 보고를 검사한 뒤 부분 실행·전체 receipt 재사용·전체 실행을 선택하지만 `TargetedTestRepository.reconcile()`·`repeated()`·`pending()`을 확인하지 않는다. 시작의 `InvocationRepository.reconcile_interrupted()`는 결과를 DB에 수집할 뿐 차단을 확정하지 않는다. 재수집/도달 표식 소비는 `WorkerHandler`에만 연결돼 있어 저장된 검증 단계의 독립 진입을 보호하지 못한다. 기존 회귀의 “verify 전에 차단”은 Worker 경로에서 verify가 호출되지 않았다는 assertion이다.

## Acceptance Criteria

- R-055/PRODUCT: 저장된 VERIFYING의 재개·재시작과 검증 배정 대기 뒤 실제 진입에서 현재 repository·ExecPlan의 결합된 미소비 자료를 재관측한다. 세 번째 실패가 확인되면 부분/전체 verify나 성공 receipt 재사용 전에 기존 `targeted_test_exhausted`로 해당 작업을 차단한다. 도달 표식 소비와 차단 확정은 같은 transaction이며 rollback 뒤에도 다시 처리할 수 있다.
- 종료 불명·살아 있는 수동 writer·경로 불일치·읽기/해석/저장 오류로 자료가 남으면 해당 검증만 일시 중단한다. 원문·호출 결합·저장된 Worker 결과·session·후보·검증 재개 문맥과 기존 감사를 보존하며, 일반/rebase 보고 오류나 verify 실패 횟수를 소비하지 않는다. 재관측이 끝나면 필요한 기존 검증 단계로 돌아가며 수집 대기만으로 새 Worker를 요구하지 않는다.
- R-091의 수집 대기는 다음 외부 실행 전용 종료가 아니다. 실제 미충족 조건을 재확인해 해소되면 같은 실행에서 저장된 검증 진입을 다시 준비한다. 일반 교정/대기 배정은 G-297/G-313이 소비하며 이 작업은 기존 pending·종료·원자 차단 결과를 정확히 전달한다. 새 테스트 근거 없는 Worker 실패 보고의 재교정 정책은 별도이고 미종료 writer·수집 오류를 새 Worker로 우회하지 않는다.
- 기록 부재·부분 마지막 줄·중복 재관측과 서로 다른 테스트의 실패는 성공·실패·차단을 합성하지 않는다. 이미 소비된 도달 표식은 다시 차단하지 않으며 확인된 기존 결과를 재집계하거나 초기화하지 않는다.
- R-091: 현재 작업에만 검사·결과 전이를 적용한다. 다른 작업의 투영·예약·실패·session·후보와 독립 배정은 유지한다. 배정 후 검증을 시작하지 않은 대기는 미시작 근거로 해당 자리만 반환하며, 실행 중 검증을 선점하거나 실제 종료 불명 예약을 회수하지 않는다.
- 현재 선언·source·dependency·사용자 판단·sandbox·문서 보고·수동 제출/면제의 기존 gate를 유지한다. 일반 선행 PASS 뒤 실제 전체 실행과 부분/전체 결과 구분은 바꾸지 않으며, 수집 대기 해소를 검증·Review·게시 성공으로 사용하지 않는다.

## Implementation

- [x] `VerifyHandler.verify()`의 Worker 결과·사용자 결정 gate 뒤에서 작업별 targeted 자료를 재관측하고, 전체 receipt 재사용·부분/전체 verify보다 먼저 기존 Worker 경계의 원자 차단과 `targeted_test_correction` 대기를 적용했다. `_maybe_partial()`과 최종 verify 진입 모두 같은 gate를 통과한다.
- [x] verify 효과는 `scheduled_call("verify")`가 실제 자리를 배정한 뒤 process receipt 준비 전에 다시 확인한다. 차단·대기 시 기존 호출 확인 규칙으로 미시작 자리만 반환하고 새 verify 실패 receipt를 만들지 않으며, Worker 결과·session·후보와 다른 작업 상태를 보존한다. 공정 배정·한도·일반 상태 재검사는 기존 경계에 남겼다.
- [x] 기존 `_targeted_test_block()`·`TargetedTestRepository.reconcile()`·`pending()` 및 좁은 port 조립을 재사용했다. 파일/프로세스 관측은 쓰기 transaction 밖에서 수행하고 적용 전에 작업 context를 재확인한다. 새 저장소·runner·복구 계층은 추가하지 않았다.
- [x] 실제 SQLite·JSONL·호출 receipt fixture로 저장 VERIFYING의 세 번째 실패 차단, 미수집 자료의 대기/재개, slot 배정 뒤 새 자료가 도착하는 경계를 회귀 검증했다.

## Validation

- Worker는 위 두 테스트 모듈의 새 검증 진입 case와 실제 변경한 공유 경계의 최소 기존 case만 실행한다. 임시 JSONL·실제 SQLite·receipt 종료 관측 double을 사용하고 실제 Agent·Git·전체 verify를 실행하지 않는다.
- 유효한 저장 Worker 보고와 VERIFYING 상태를 두고, 이미 DB에 세 번째 실패가 남은 경우와 시작 재수집으로 세 번째 실패가 들어오는 경우를 각각 확인한다. 부분 verify·전체 실행·receipt 재사용이 호출/확정되지 않고 대상 차단만 생겨야 한다. 차단 transaction 실패 뒤에는 도달 표식·기존 결과를 유지하고 재시도에서 한 번만 소비해야 한다.
- pending/error·종료 불명·live 수동 writer에서는 원문·호출 결합·검증 재개 문맥과 모든 교정 횟수를 보존한다. writer 종료/수집 오류 해소 뒤 같은 기록을 한 번만 반영하고 새 Worker 없이 필요한 검증으로 이어지는지 확인한다. 배정 대기 중 새 미소비 자료나 작업 상태 경쟁이 생기면 오래된 준비 결과로 검증을 시작하지 않아야 한다.
- pending → 확인된 writer 종료/수집 복구 → 같은 실행의 검증 재진입을 기존 단계 fixture에서 확인한다. 조건이 남으면 verify/새 Worker 호출은 0회이며, 해소 뒤 기존 결과는 한 번만 반영한다. G-297/G-313의 대기열·시간 배정 구현과 동일 assertion은 복제하지 않는다.
- 기존 `TargetedHistoryTests`·`TargetedRecoveryTests`의 테스트별 초기화·중복/상충·부분 기록과 `WorkerTargetedFailureTests`의 Worker/CLI retry·원자 차단을 대응 근거로 유지한다. 같은 조건/assertion의 collector·native 종료 회귀를 복제하지 않는다. 선택 작업 B와 대표 투영 A를 구별하고 B의 대기/차단 중 A와 독립 작업의 진행·예약이 보존되는 최소 연결을 확인한다.
- 기존 저장 사용자 결정·수동 제출/면제·문서 보고·부분/전체 receipt의 의미를 유지한다. 선택 adapter는 version 1 unittest, launcher Python 3.11–3.14이며 독립 모듈 수집·필수 compile·Agent guard를 축소하지 않는다.

## Progress

- [x] revision `937b03f1dd9e182e0e48e565ea093dc560f2de5b`의 호출 결합·재수집·Worker gate와 저장된 verify dispatch·관련 테스트를 정적으로 대조했다.
- [x] 저장된 검증 진입의 작업별 재관측·차단·대기를 연결하고 부분/전체 실행 및 receipt 재사용보다 먼저 검사한다.
- [x] 초기 후보에서 verify slot 배정 뒤 새 자료 도착을 포함한 targeted unit test 4개를 실행했다. 네 테스트 모두 통과했다.
- [x] 현재 후보 `1120bd4d8d132e3ca33a50985f05c701d43adb6d`의 Manager verify delta 7개 case는 모두 Worker session 시작으로 projection이 갱신된 뒤 직접 호출용 `_post_effect_guard()`가 이전 context를 반환해 실패했다. guard의 직접 호출 경로도 저장된 최신 context를 다시 구성하고 회귀 case를 추가했다. 동일한 7개 case, 새 context 회귀 case, 저장된 verify 재개 관련 4개 case가 현재 후보에서 모두 통과했으며 전체 verify는 실행하지 않았다.
- [x] 관리 rebase 반영 뒤 현재 후보에서 저장 verify 재개 관련 targeted case 4개와 `_post_effect_guard()`의 직접 호출 context 회귀 1개를 다시 실행했고 모두 통과했다. 전체 verify는 실행하지 않았다.
- [x] 이전 후보의 Manager verify에서 2 CPU 환경의 nested runner 통합 회귀 두 건이 30초 제한에 도달했다. 현재 고정 base에는 bootstrap runner와 verify 90초·외부 process 120초 예산이 이미 포함되어 있으므로 더 큰 예산과 진단을 보존하고, full-suite 동시 작업이 2 CPU에서 기다릴 수 있다는 설명을 테스트에 추가했다. 실제 nested verify·Manager integration인 두 case는 Worker에서 실행하지 않았으며 Manager 재검증이 필요하다.
- [x] 현재 HEAD `f61d872387a7fdb28362211254ae882a0b525d05`에서 rebase 소유·차단 관련 unit case 3개와 저장 verify 재개·직접 호출 context 관련 unit case 4개를 targeted 실행해 모두 통과했다. nested verify 통합 case와 전체 verify·Review는 Worker에서 실행하지 않았다.
- [x] 현재 후보에서 `plan_input_invalid`와 저장 rebase intent가 소유한 원래 HEAD의 안전한 START 상태를 다시 확인하면 입력 차단만 해제하고 intent를 보존해 재개하도록 했다. START의 완료·검증·게시 증거 오용은 막고 `managed_worktree_conflict` 거부는 유지한다. 새 `plan_input_invalid → START → resume` unit case가 통과했다.

## Decision Log

- 이번 R-055/R-091 delta의 비차단 재개는 수집 대기의 반환 의미에 반영한다. snapshot의 started ID·경로·기존 조사/수행 기록·dependency를 보존하며 collector·native 분류·실패 집계를 다시 구현하지 않는다. 대기 해소의 같은 실행 재진입만 추가 검증하고 실제 후보나 verify/Review 성공은 확정하지 않았다.

- 이번 delta에서 원격에 반영된 G-291의 결합·재수집을 기존 구현으로 소비한다. 기반 구현이 이미 있으므로 새 선행 작업을 만들지 않는다. G-290의 native 분류와 G-292의 Claude 전달은 별도 경계이며 이 수정은 현재 Codex/공통 저장 자료로 독립 구현·Merge할 수 있다. G-189는 이 교정 뒤 알림·복구·다음 선택의 후속 소비자를 감사한다.
- complexity medium: 기존 작업별 저장·재관측·복구 경계를 검증의 세 선택 분기에 연결하는 책임이 명확하다. risk high: 누락된 확인은 필수 차단을 우회하고 오래된 검증 근거를 다음 승인에 전달할 수 있다. 원자 차단/소비·원문과 재개 문맥 보존·실제 단계/SQLite 회귀로 완화하며 기존 감사로 수동 복구할 수 있게 한다. priority high는 필수 실행 근거 확인을 검증 재개에도 유지하기 위한 것이다. 최초 level은 3이며 Model·Effort는 Manager의 명시 설정을 따른다.
- snapshot token `e108f535e66d7a1ec6bbe0c12265d2b209bdfe14e05e4bc20c155efd5dc80e7c`의 관련 계획과 대조해 미충족 검증 진입만 분리했다. 새 제품 정책·권한·adapter·외부 서비스·비호환 migration은 요구하지 않는다. 이번 작업 작성은 delta 검토이며 프로젝트 최종 전문 확인이나 실행 성공이 아니다.
