---
id: G-163
title: 잘못된 catalog 선언의 계획별 격리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-055] Blocking Path Audit and Minimal Interruption.md
depends_on: []
complexity: high
risk: high
priority: critical
---

## Purpose

현재 catalog refresh의 전체 소비자 중단을, 식별·영향 범위를 확정할 수 있는 계획별 오류에서는 최소 범위 중단으로 바꾼다.

## Acceptance Criteria

- 고유하게 식별한 잘못된 선언·source·dependency와 해당 계획의 영향받는 의존 계획을 중단한다. 검증된 독립 READY의 선택·planning 및 기존 PR 관측을 계속하고 다른 프로젝트를 막지 않는다.
- ID 충돌·공유 자원·소유권·범위가 실제로 불확실하면 관련 실행을 거부하며 원인과 영향 범위를 남긴다. 유효하지 않은 전체 catalog를 부분 성공으로 저장하거나 dependency gate를 우회하지 않는다.
- 안전한 원격 개정 뒤 실제 원인만 재검사·해제하고 독립 승인·차단·실행 이력은 보존한다. 중단·재시작·원격 경쟁에서도 검증한 catalog와 보류 오류의 결합이 유지된다.

## Implementation

- catalog/execplan_catalog.py의 오류 정보, project_catalog_refresh.py와 storage/execplan_catalog_state.py의 채택 경계를 분리한다. 독립성이 증명된 실행 view와 보류된 오류를 같은 원격 revision에 결합한다.
- 기존 scoped_blocking·PR preflight·선택 gate를 재사용한다. 모든 catalog 소비자가 계획별 영향 집합을 일관되게 적용하도록 하고 임의의 깨진 문서 parsing으로 신원을 추정하지 않는다.
- 문서·권한·게시와 검증의 공통 경계는 AGENTS.md, docs/ARCHITECTURE.md, docs/PLANS.md를 따른다. 필요한 기존 helper와 관련 테스트를 우선 재사용하고 새 루트 항목을 만들지 않는다.

## Validation

- tests/test_project_catalog_refresh.py, test_execplan_catalog.py, test_scoped_blocking.py 중 독립/간접 의존·ID 모호함·공유 문제·동시 개정·재시작의 최소 unit case를 실행한다.
- Manager 전체 verify는 잘못된 입력의 비채택, 유효 독립 작업·등록 프로젝트 scheduler·PR 관측 지속과 안전 차단 보존을 검증한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- graph·채택·선택 경계를 함께 변경하므로 complexity high, 잘못된 독립성 판단의 영향으로 risk high다. 원격 삭제와 dependency 목표 복구는 G-164로 분리한다.
