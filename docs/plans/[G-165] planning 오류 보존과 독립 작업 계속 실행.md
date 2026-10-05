---
id: G-165
title: planning 오류 보존과 독립 작업 계속 실행
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-055] Blocking Path Audit and Minimal Interruption.md
depends_on:
  - G-163
complexity: high
risk: high
priority: critical
---

## Purpose

preserved planning_error가 실행 자리를 계속 점유하는 현재 gate를 바꿔 안전한 독립 구현과 PR 복구를 계속한다.

## Acceptance Criteria

- 실패한 planning의 변경 자원을 안전하게 비운 뒤 유효한 독립 READY·리팩터링 계획을 순차 실행하고 기존 PR 관측·충돌 복구를 계속한다. 같은 프로젝트의 실제 변경 실행은 여전히 하나다.
- planning 오류·session·작업 공간·미게시 결과·실패 피드백은 다른 작업의 진행·중단·재시작에도 보존된다. 동일 입력의 신규 Planner 반복과 프로젝트 완료·관련 Requirement 이동은 보류한다.
- planning 복구와 구현 슬롯을 구분하며 공유 자원·미확정 Git 쓰기·소유권 불확실에서는 안전한 release를 합성하지 않는다. 원인 해소 후 기존 planning 작업을 재검증·게시하고 해당 오류만 해제한다.

## Implementation

- planning/selection_service.py의 preserved-error gate와 storage/runtime_state.py의 슬롯 소유 판단을 명시적인 보존 오류와 활성 변경 상태로 분리한다. 기존 실행·checkpoint·session 저장소를 재사용한다.
- planning 실패의 결과·게시 intent를 구현 phase 필드로 덮지 않도록 책임 경계를 정리하고 scheduler가 대기·실패·진행을 정확히 반환하도록 연결한다.
- 문서·권한·게시와 검증의 공통 경계는 AGENTS.md, docs/ARCHITECTURE.md, docs/PLANS.md를 따른다. 필요한 기존 helper와 관련 테스트를 우선 재사용하고 새 루트 항목을 만들지 않는다.

## Validation

- tests/test_planning_selection.py, test_runtime_state.py와 planning 복구 테스트에서 오류 후 독립 Worker·PR 충돌·재시작·미확정 쓰기·같은 입력 no-call의 최소 unit case를 실행한다.
- Manager 전체 verify는 프로젝트별 단일 실행, planning 작업물·session 보존, 독립 프로젝트 지속과 완료 gate를 검증한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 여러 durable 상태와 실행 슬롯을 분리해야 해 complexity high, 오류 작업물과 실행 순서 손실의 영향으로 risk high다. 원격 진단 보존 자체는 G-168에서 구현한다.
