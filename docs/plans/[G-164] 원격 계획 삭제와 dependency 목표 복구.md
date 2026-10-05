---
id: G-164
title: 원격 계획 삭제와 dependency 목표 복구
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

원격에서 확인한 미착수 삭제·archive 이동·번호 공백을 구분하고 삭제된 선행 계획의 목표를 보존하는 자동 복구를 제공한다.

## Acceptance Criteria

- 번호 공백을 실패나 삭제의 근거로 쓰지 않는다. 실제 원격 삭제·archive 이동·삭제와 새 계획 추가를 구분하고 확인된 미착수 취소 감사, source·ID·실행/PR 이력을 보존한다. 삭제 ID를 재사용하지 않는다.
- 삭제 계획을 참조하는 관계는 최신 제품 목표에 맞는 적격 READY 개정·새 ID 대체·관계 정리를 먼저 시도한다. 아직 필요한 선행 목표를 dependency 제거로 우회하지 않는다.
- 유효한 최종 관계를 하나의 검증된 원격 게시와 catalog 반영으로 확정한다. 목표·소유권·영향 범위를 확정할 수 없으면 그 범위만 보류하며 독립 작업·PR 관측은 계속한다.
- 중단·재시작·경쟁에서 같은 취소·대체·게시를 중복하지 않는다. 실행된 계획·소유 작업 공간·기존 PR과 snapshot 밖 파일을 자동 개정하지 않는다.

## Implementation

- catalog/catalog_service.py·project_catalog_refresh.py의 원격 reconciliation과 취소 저장 경계를 재사용한다. 삭제 관측은 실제 tree·이력과 채택 revision에 결합한다.
- planning의 일반 mutable READY snapshot·결과 검증·게시 절차로 목표 복구를 요청한다. 의미가 불명확한 대체를 결정적 코드로 합성하지 않으며 복구 전후 의존 graph 전체를 검증한다.
- 문서·권한·게시와 검증의 공통 경계는 AGENTS.md, docs/ARCHITECTURE.md, docs/PLANS.md를 따른다. 필요한 기존 helper와 관련 테스트를 우선 재사용하고 새 루트 항목을 만들지 않는다.

## Validation

- catalog refresh·batch·cancellation 관련 기존 테스트에서 번호 공백, archive, 미착수 삭제, 실행 이력, 신규 추가 동시 관측, 선행 목표 보존의 최소 unit case를 실행한다.
- Manager 전체 verify는 삭제 복구의 게시/SQLite 중단·경쟁, 유효 dependency gate와 독립 계획 지속을 검증한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 원격 관계·목표 판단·원자적 채택이 연결되어 complexity high, 선행 목표 손실의 영향으로 risk high다. 취소를 Merge나 Requirement 충족으로 간주하지 않는다.
