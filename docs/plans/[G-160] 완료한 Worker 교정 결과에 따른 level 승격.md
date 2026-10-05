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

## Implementation

- execution/result_validation.py와 Review·Worker handler의 기존 전이, storage의 실패·교정 기록을 확장한다. 교정 결과 identity를 실제 완료 경계에 결합하고 실패 지문 정규화·세션 전환 helper를 재사용한다.
- 기존 기록에 없는 기준선·완료 여부를 추측하지 않는 호환 처리를 두고 변화 없는 재관측의 중복 승격을 막는다. Worker 호출 경계와 Review 교정 예산을 서로 다른 책임으로 유지한다.
- 문서·권한·게시와 검증의 공통 경계는 AGENTS.md, docs/ARCHITECTURE.md, docs/PLANS.md를 따른다. 필요한 기존 helper와 관련 테스트를 우선 재사용하고 새 루트 항목을 만들지 않는다.

## Validation

- tests/test_review_profile_escalation.py, test_execution_retry_policy.py, test_execution_review_phase.py에서 기준선·같은/변경 issue·다중 issue·verify 중간 실패·재시작 관련 최소 unit case를 실행한다.
- Manager 전체 verify는 A→B→A, level별 전환, provider·session 교체, 실행별 대기와 기존 verify 지문 복구를 회귀 검증한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 다단계 완료·재시작 결합 때문에 complexity high, 잘못된 승격과 작업 중단의 영향 때문에 risk high다. 최초 FAIL을 교정 실패로 소급 간주하지 않는다.
