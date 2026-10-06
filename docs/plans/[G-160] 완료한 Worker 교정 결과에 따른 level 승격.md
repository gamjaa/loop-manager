---
id: G-160
title: 완료한 Worker 교정 결과에 따른 level 승격
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-052] Worker Classification and Initial Level Calibration.md
depends_on:
  - G-159
complexity: high
risk: high
priority: high
---

## Purpose

최초 Review FAIL과 완료된 Worker 교정 뒤의 FAIL을 구분해 R-052의 승격 조건을 적용한다. 현재 result_validation의 지문 누적은 최초 FAIL도 반복 횟수에 포함한다.

## Acceptance Criteria

- level별 최초 유효 FAIL은 기준선이다. 같은 issue가 두 번의 완료한 Worker 교정 뒤 남으면 다음 level로 승격하고, issue가 바뀌어도 교정 뒤 연속 세 번 FAIL이면 승격한다. 한 Review의 여러 issue는 한 교정 결과다.
- CLI 재시도·프로토콜 교정·Review만 재실행·재시작은 교정 완료로 세지 않는다. Worker→verify→유효 Review의 결합과 per-level 기준선·미완료 교정·effective level을 중단 뒤 보존한다.
- level 8을 넘기거나 최종 Review 반복 횟수로 계획을 차단하지 않는다. 실행별 교정 한도는 작업·session을 보존한 대기이며 다음 실행에서 이어간다. verify 실패 지문 정책과 사용자 승인·실제 안전 차단은 유지한다.
- 구현 PR의 PRODUCT에는 G-159가 Merge한 최초 level 산식(complexity 1·3·5와 critical risk만 +1, 최초 1~6), 설정에 따른 profile 선택과 이 계획의 교정 후 승격 조건이 함께 반영된다. 이전 risk 가산점·최초 7 배정·최초 FAIL의 issue 수에 따른 승격 설명을 남기지 않는다.

## Implementation

- execution/result_validation.py와 Review·Worker handler의 기존 전이, storage의 실패·교정 기록을 확장한다. 교정 결과 identity를 실제 완료 경계에 결합하고 실패 지문 정규화·세션 전환 helper를 재사용한다.
- 기존 기록에 없는 기준선·완료 여부를 추측하지 않는 호환 처리를 두고 변화 없는 재관측의 중복 승격을 막는다. Worker 호출 경계와 Review 교정 예산을 서로 다른 책임으로 유지한다.
- R-052의 장기 규칙을 코드와 같은 PR에서 docs/PRODUCT.md의 Model과 Effort 선택·실패와 재시도 절에 반영한다. 이미 구현된 최초 산식과 명시적 profile 검증은 재구현하지 않는다.
- 문서·권한·게시와 검증의 공통 경계는 AGENTS.md, docs/ARCHITECTURE.md, docs/PLANS.md를 따른다. 필요한 기존 helper와 관련 테스트를 우선 재사용하고 새 루트 항목을 만들지 않는다.

## Validation

- tests/test_review_profile_escalation.py, test_execution_retry_policy.py, test_execution_review_phase.py에서 기준선·같은/변경 issue·다중 issue·verify 중간 실패·재시작·중복 재관측·낡은 level의 Review 결과 관련 최소 unit case를 실행한다.
- Manager 전체 verify는 A→B→A, level별 전환, provider·session 교체, 실행별 대기와 기존 verify 지문 복구를 회귀 검증한다.
- 문서 대조에서 최초 산식의 12개 조합·설정 기반 profile 선택·완료한 교정의 승격 조건을 실제 구현 및 관련 테스트와 확인한다. Manager 전체 verify에서 G-159의 최초 level·명시 설정 회귀도 유지한다.
- Review는 Worker·verify·Review의 결합 identity와 원자 집계, 중단 뒤 중복 승격 방지 및 기존 effective level·감사 보존을 대조한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [x] 2026-10-06 기준 029067f의 G-159 Merge 결과·PRODUCT·R-052와 관련 테스트를 대조해 기존 목표 안에 제품 규칙 정합성 조건을 보완함. 구현·테스트 실행은 하지 않음.
- [x] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 완료한 교정 identity·level별 기준선·중단 재관측의 원자 집계를 설계해야 하므로 complexity high다. 집계 오류는 여러 계획의 저장된 effective level·실제 profile 호출·session 전이를 잘못 승격시킬 수 있고 적용된 level을 자동으로 낮출 수 없어 risk high다. 원문·결합 identity·기존 이력을 보존해 새 집계를 교정·재검증하며 이미 발생한 호출 비용을 코드 rollback으로 복구한다고 가정하지 않는다. 최초 FAIL을 교정 실패로 소급 간주하지 않는다.
- G-159 Merge(6c01d0e) 뒤에도 PRODUCT의 최초 level 설명에 이전 산식이 남아 있다. 별도 구현 계획을 만들지 않고 같은 R-052의 후속 G-160에서 이미 Merge된 산식과 새 승격 규칙을 함께 문서화한다. ID·sources·dependency와 원래 승격 목표는 보존한다.

- 구현은 기존 ISSUE_ID 지문, 검증 원문 감사, Review 교정 예약과 profile/session 전이를 재사용한다. 호환 가능한 `exec_plans.review_correction_state` JSON 열에 현재 level의 기준선·완료 교정 집계·미완료 turn·verify 후보 HEAD를 저장하며, 기존 level별 지문 이력에서 완료 횟수를 소급 추정하지 않는다. `storage/review_correction_state.py`는 이 저장 경계만 담당한다.
- Worker의 정상 결과와 VERIFYING 전이, 성공한 후보 verify와 검증 토큰, 유효 FAIL의 교정 소비와 level 갱신을 기존 중첩 transaction helper로 묶는다. 낡은 level 후보의 Review, CLI/프로토콜 재시도와 교정 없는 재관측은 승격하지 않는다. 기준선·교정의 원본 Review ID·검증 HEAD·결과 지문·적용 level을 감사에 보존하며 PASS는 진행 중인 집계만 끝낸다. 정상 사용자 차단 해제만 새 교정 상태도 초기화한다.
- PRODUCT의 최초 12개 level 조합과 명시 설정·교정 후 승격 규칙을 반영했다. 기존 `scripts/verify.sh`는 unittest manifest 자동 탐색으로 변경된 unit test를 포함하므로 수정할 필요가 없다. Worker는 전체 verify를 실행하지 않았으며 Manager 검증·읽기 전용 Review·사용자 Merge는 남아 있다.

- Manager verify 진단에서 공통 `update_project`의 commit 동작을 바꾼 영향으로 rebase 완료가 열린 외부 transaction을 거절하는 회귀를 확인했다. 기존 호출의 commit 의미를 유지하고 새 Worker·verify의 결합 저장에서만 `preserve_transaction=True`로 기존 transaction helper를 사용한다. 유효 Review의 level·후보 HEAD는 전이 인자로 전달해 임시 관측 상태 저장을 제거했다. 재현한 unit case, 관련 복구 경계와 원자 rollback을 targeted 검증하며 전체 verify 재실행은 Manager에 맡긴다.

- Reviewer 교정: `review_result`가 rebase로 사라져도 source ID와 별개인 Worker 후보의 실제 level·완료 여부·검증 HEAD를 저장한다. 이 후보를 새 기준선에도 보존해 승격 직후 첫 검증된 FAIL을 해당 level의 기준선으로 집계하고, 교정 없는 낡은 후보 재관측은 계속 제외한다. 원격 checkpoint의 정상 차단 해제와 Review 이력 초기화 경계는 새 교정 상태도 같은 transaction에서 초기화한다. 승격→rebase→Worker→verify→Review 뒤 두 교정의 승격, 기존 집계가 있는 checkpoint 재개 뒤 첫 FAIL, 낡은 후보와 rollback 회귀를 targeted 검증했다. phase fixture도 유효한 초기 Worker level을 명시한다.
