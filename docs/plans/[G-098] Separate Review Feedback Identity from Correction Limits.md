---
id: G-098
title: Separate Review Feedback Identity from Correction Limits
sources:
  - docs/requirements/[R-044] Separate Review Feedback Identity from Correction Limits.md
depends_on:
  - G-056
  - G-096
complexity: high
risk: high
priority: critical
---

## Purpose

Review의 서로 다른 결함이 같은 ISSUE_ID에 묶이면 수정이 진전돼도 반복 한도를 소진한다. 계획 미비는 기존 구조화 결정 요청으로 처리하고, Review 교정의 실행 한도는 ISSUE_ID와 독립적으로 적용해 정상 수정과 자원 중단을 구분한다.

## Acceptance Criteria

- Review FAIL은 기존 ISSUE_ID·진단·해결 이력과 level 승격 자료를 기록하지만, Review 지문 발생 횟수만으로 `review_retry_exhausted` 최종 차단이나 같은 효과의 우회 차단을 만들지 않는다. `transition_validation_failure`와 후속 `WORKER_CORRECTION` 복구 판정 모두 이 경계를 따른다. verify 지문 한도는 유지한다.
- 한 외부 실행의 계획별 Review 유발 Worker 교정은 최대 3회다. 여러 issue가 있는 FAIL도 하나의 교정 turn으로 처리한다. 세 번째 교정 결과의 verify·Review 성공은 게시로 진행하고, 추가 교정이 필요하면 다음 Worker 호출 전에 작업을 보존하는 일시 중단 결과를 반환한다.
- 예산은 외부 실행 identity와 plan에 결합한다. 같은 실행의 재진입·등록 프로젝트 재배정, 후보 revision·provider·session 변경에 의해 초기화되지 않으며, CLI 재시도·session fallback·중단된 동일 turn 복구는 중복 소비하지 않는다. 새 외부 실행에서 같은 Worker·Reviewer 세션과 미전달 피드백을 재개한다.
- 한도 중단은 현재 실행 투영과 계획 phase, branch·worktree·소유권, 변경·승인·session·검증 결과를 보존한다. 최종 차단, checkpoint push, BLOCKED·차단 해제·재개 알림을 생성하지 않으며 기존 지속 실행 실패 진단과 알림 경로를 사용한다. 중단 뒤 verify·Review 성공 전에는 게시하지 않는다.
- `needs_user_decision`과 `SPEC_BLOCKED`는 G-096/G-056의 문서 근거 검증과 기존 원인별 해제 조건을 유지한다. 안전 차단, verify 반복, rebase 교정, Reviewer 프로토콜·외부 CLI 재시도 및 Worker level 승격 정책은 이 Review 예산과 분리한다.
- legacy `review_retry_exhausted`는 저장된 최종 원인과 감사·작업물·피드백을 보존해 원인별로 재평가한다. 횟수 차단만 남고 현재 workspace·branch·원격 OID와 실행 증거가 안전하면 같은 교정 경로로 재개한다. 다른 유효한 결정·안전 원인이나 미확인 rebase 소유권이 있으면 그 근거를 보존한다. G-092 없이 detached HEAD를 포괄 허용하거나 G-088의 보안 issue를 해결됐다고 처리하지 않는다.

## Implementation

- `loop_manager.py`의 `transition_validation_failure`에서 Review 피드백·실패 감사·level 승격 갱신과 최종 차단 결정을 분리한다. 기존 verify 분기는 유지하고, `validation_feedback.py`/`validation_feedback_state.py`의 ISSUE_ID 기반 delta 전달·해결 기록은 그대로 사용한다.
- Review 교정 예산을 `recovery_state.py`/`execution_state.py`의 좁은 저장 경계와 `execution_handlers.py`의 Review→Worker 전이에 연결한다. 기존 외부 실행 identity와 plan별 논리 교정 turn을 사용하고 호출 시작 전 예약·시작 증거를 호환 가능한 저장으로 남겨 재진입·중단·session fallback의 중복 집계를 막는다. 모든 지문을 묶은 새 hash나 무조건 초기화한 기존 failure history로 예산을 구현하지 않는다.
- `recovery_runtime.py`의 Review 유발 `WORKER_CORRECTION`은 ISSUE_ID occurrence에 따른 최종 차단 대신 예산의 계속/일시 중단 결정을 따른다. 같은 이유 code를 사용하는 verify·Worker 결과 형식·rebase 경로는 명시적인 단계 문맥으로 분리해 기존 한도를 보존한다. 한도 중단의 cause·실행 identity·사용 횟수·남은 Review 진단을 기존 복구·지속 실패 알림에 전달한다.
- `scoped_blocking.py`와 계획별 복구 경계에 legacy Review 횟수 차단의 재평가를 연결한다. 변경된 정책의 원인만 해제하고 과거 실패 history·block 사건·checkpoint 게시 OID는 감사로 유지한다. 현재 작업의 소유권·Git·승인 검사가 실패하면 재개하지 않으며 다른 계획·planning 상태를 변경하지 않는다.
- Reviewer prompt의 서로 다른 결함에 다른 ID를 쓰는 계약과 G-056의 `SPEC_BLOCKED`, Worker의 G-096 결과 선언을 유지한다. 횟수 한도 진단에는 문서 수정이 필요한 계획 미비라고 쓰지 않는다. 새로운 의미 판정 protocol이나 Agent 호출을 추가하지 않는다.
- 구현 PR에서 R-044의 장기 규칙을 `docs/PRODUCT.md`에 반영하고 Review 한도와 기존 verify·level 승격 정책을 구분한다. R-044는 사용자가 승인한 Review 정책 변경의 근거이며 기존 PRODUCT/ARCHITECTURE의 Review 반복 최종 차단 문구를 계속 구현하라는 뜻으로 해석하지 않는다. Linux·Windows verify의 검증 범위는 유지한다.

## Validation

- Worker는 직접 변경한 `tests/test_loop_manager.py`, `tests/test_execution_handlers.py`, `tests/test_recovery_runtime.py`, `tests/test_recovery_state.py`, `tests/test_validation_feedback_state.py`, `tests/test_scoped_blocking.py`의 targeted unit test만 실행한다. 구조화 결정·알림 경계를 변경하면 해당 `tests/test_worker_outcome.py` 또는 `tests/test_notifications.py`의 직접 관련 unit case만 추가한다.
- 같은 ID의 진단 변경, 다른 ID의 연속 FAIL과 복수 issue FAIL에서 최종 차단이 없고 동일한 교정 예산을 쓰는지 검증한다. 세 번째 교정 뒤 PASS→게시와 FAIL→네 번째 Worker 미호출·일시 중단을 대조한다.
- 같은 외부 실행의 coordinator 재진입·프로젝트 재배정, 호출 예약/시작/결과 저장 사이 중단, CLI 재시도·provider/session fallback과 새 외부 실행 재개를 검증한다. 동일 논리 turn은 한 번만 소비하고, 실행 identity가 바뀌기 전에는 추가 예산이 생기지 않아야 한다.
- 한도 중단 전후 SQLite의 phase·session·승인·피드백 cursor 및 Git 작업물을 비교하고 BLOCKED·checkpoint·차단 재개 알림이 없음을 검증한다. 기존 지속 실패 알림의 횟수·중복 제거와 새 외부 실행의 교정 재개를 유지한다.
- legacy Review 횟수 차단만 있는 정상 workspace의 재개와 별도 결정·범위 위반·PR 종료·소유권 불명확·진행 rebase가 섞인 상태의 보존을 검증한다. 다른 계획의 차단·planning 상태 및 과거 감사는 변경하지 않는다.
- 전체 verify에서 Worker 결정 요청과 Reviewer `SPEC_BLOCKED`가 검증·게시 전에 차단되고, 기존 verify/rebase/protocol 한도·Worker level 승격·PR 소유권·최종 검증/Review 조건과 알림 회귀가 없어야 한다.

## Progress

- [ ] Review 지문 감사·피드백과 최종 차단 결정 분리
- [ ] 외부 실행별 Review 교정 예산과 중단·재개 경계
- [ ] legacy 횟수 차단 재평가와 독립 안전 조건 보존
- [ ] 한도·중단·결정 요청·다른 단계의 회귀 검증

## Decision Log

- 2026-09-27: 사용자는 ISSUE_ID 누적만으로 자동 차단하는 방식을 제거하고 명시적인 결정 요청과 별도 교정 한도로 역할을 나누는 계획 작성·main 게시를 요청했다. 새 정책은 R-044에서 시작하며 PRODUCT의 현재 동작을 문서 게시만으로 바꾸지 않는다.
- 교정 한도는 기존 3회 규모를 외부 실행별 계획 단위로 사용한다. 한도 소진은 상태 보존 일시 중단이며 새로운 ISSUE_ID로 예산을 늘리거나 같은 ID라는 이유로 영구 차단하지 않는다. 신규 비용 설정과 전체 실행 시간 제한은 추가하지 않는다.
- 기존 Worker level 승격에 쓰는 Review 통계는 유지한다. 이번 변경은 그 통계의 최종 차단 효과를 제거하며 모델 선택 정책을 함께 바꾸지 않는다.
- G-056과 G-096은 이미 Merge된 구조화 결정 경로다. 차단된 G-088이나 그 후속 G-089/G-092에 의존하지 않아야 이 정책 변경을 먼저 구현할 수 있다. G-088의 실제 mount 별칭 결함과 G-092의 rebase 오탐은 각각의 계획 범위로 유지한다.
