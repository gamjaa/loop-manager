---
id: G-178
title: 게시 전 새 계획 batch의 ID 충돌 교정
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-071] Remaining Manager-Owned Deterministic Work.md
depends_on:
  - G-170
  - G-208
  - G-209
  - G-210
  - G-211
  - G-212
  - G-213
  - G-215
  - G-216
  - G-217
complexity: medium
risk: high
priority: normal
---

## Purpose

고유 대상이 확정된 미게시 새 계획 batch의 ID/path 충돌을 Manager가 교정해 형식만 고치기 위한 Planner 재호출을 줄인다.

## Acceptance Criteria

- 아직 게시/등록하지 않은 새 계획의 충돌을 활성·archive·등록·취소·예약 ID를 피한 새 ID/path로 교정하고 확정된 batch 내부 구조 dependency만 갱신한다. 기존 계획·사용자 파일과 draft 질문의 의미를 보존한다.
- 서로 다른 목표의 같은 ID, 대상이 모호한 본문 참조·dependency는 기계적으로 선택하지 않고 필요한 Planner/사용자 판단으로 남긴다. 기존 실행·snapshot 밖 계획의 ID를 바꾸지 않는다.
- 배정 결과와 실제 편집을 원격·snapshot·소유권에 결합해 전체 graph를 검증·게시한다. 중단/경쟁에서 같은 배정을 중복하거나 다른 게시자의 ID를 덮지 않는다.

## Implementation

- planning/planner_validation.py와 catalog batch·게시의 기존 번호/고유성 검사에 deterministic normalization을 앞단으로 연결한다. 허용 새 파일과 구조적 참조만 처리한다.
- 원래 결과·배정 mapping·확정 revision을 보존해 재시작 시 실제 diff와 대조한다. 모호함의 검증 실패는 성공으로 보충하지 않는다.
- 자동 역할·권한·검증은 docs/AGENTS.common.md와 해당 역할의 docs/AGENTS.*.md를, 프로젝트 규칙·설계·계획 작성은 AGENTS.md·docs/ARCHITECTURE.md·docs/PLANS.md를 참조한다. 기존 helper·관련 테스트를 재사용하고 지정 루트 경로를 보존한다.

## Validation

- tests/test_catalog_batches.py와 planner validation 테스트에서 archive/취소/예약 충돌, batch 내부 관계, 모호한 목표/본문, draft·원격 경쟁의 최소 unit case를 실행한다.
- Manager 전체 verify는 ID 고유성·전체 dependency 검증, 게시 재개·scope 거부와 기존 source/실행 이력 보존을 검증한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [x] 2026-10-08 새 계획 batch의 ID/path·확정 dependency 정규화와 publication manifest의 배정 대응 검증을 구현하고 targeted unit case를 통과함. draft·기존 User Decision과 애매한 충돌 보존도 확인함.
- [x] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 미게시 batch의 정형 편집으로 complexity medium, 잘못된 목표 연결/ID 재사용 영향으로 risk high다.
