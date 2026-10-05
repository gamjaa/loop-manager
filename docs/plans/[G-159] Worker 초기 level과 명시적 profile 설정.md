---
id: G-159
title: Worker 초기 level과 명시적 profile 설정
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-052] Worker Classification and Initial Level Calibration.md
depends_on: []
complexity: medium
risk: medium
priority: high
---

## Purpose

분류와 설정의 결정적 정책을 최신 R-052에 맞춘다. 현재 catalog의 초기 level 계산은 risk 전체 점수를 더하고 planner_check 설정은 planner로 fallback한다.

## Acceptance Criteria

- 신규 계획의 초기 level은 complexity 1·3·5에 critical risk만 1을 더한 1~6이다. 기존 실행 중 계획의 effective level·session·승격 이력을 소급 변경하지 않는다.
- 선택 provider의 worker_1~8과 planner·planner_check를 명시적으로 검증한다. maintenance의 허용된 planner 상속, reviewer 설정과 level 8의 지원 profile 선택을 보존하고 잘못된 설정은 모델 시작 전에 구체적으로 진단한다.
- 분류 근거는 남은 설계·구현 난도와 실패 영향으로 설명한다. 코드 parser, 설정 예제와 안내가 같은 규칙을 사용하며 새 설정 오류를 묵시적 대체로 숨기지 않는다.

## Implementation

- storage/execplan_catalog_state.py의 초기 계산과 agents/agent_profiles.py의 profile 해석을 기존 검증 경계에서 고친다. 과거 catalog 호환성과 최초 등록·재선택의 구분을 유지한다.
- config의 기존 예제·설정 안내와 docs/PLANS.md의 분류 지침을 갱신한다. 지원 모델·effort는 구현 시 공식 provider 자료로 확인하고 별도 모델 목록이나 전역 설정을 추가하지 않는다. Reviewer 설정은 보존하고 R-052에 명시한 planner·planner_check 예제도 실제 지원 조합으로 함께 반영한다.
- 문서·권한·게시와 검증의 공통 경계는 AGENTS.md, docs/ARCHITECTURE.md, docs/PLANS.md를 따른다. 필요한 기존 helper와 관련 테스트를 우선 재사용하고 새 루트 항목을 만들지 않는다.

## Validation

- tests/test_catalog_metadata.py, test_execplan_catalog_state.py, test_provider_profiles.py 중 계산 조합·누락 profile·상속·기존 실행 이력 관련 최소 unit case를 실행한다.
- Manager 전체 verify는 기존 계획 호환성, provider 선택·설정 거부, session과 retry 이력 보존을 검증한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 2026-10-05 게시 전 번호 검증 오류 교정: Manager parser의 활성·archive 바로 아래 계획 및 제공 catalog 기준 최대 ID는 G-158이다. 중첩 `docs/plans/manual/`은 관리 catalog 입력이 아니므로 번호 집계에 포함하지 않는다. 아직 게시·등록되지 않은 이 batch 31개를 G-159~G-189로 연속 배정하고 dependency·본문 참조를 같은 대응으로 갱신했다. 기존 snapshot ID·계획 목표·사용자 문서는 변경하지 않았다.

- 계산·설정은 프로그램의 결정적 책임이다. Review 교정 결과에 따른 승격은 G-160에서 따로 다룬다. 난도는 제한된 정책 경계 변경, 위험은 잘못된 호출 선택의 영향으로 분류했다.
- 이동 영수증의 rename 관측 오류는 별도 G-190으로 추가했다. 최종 신규 batch는 G-159~G-190의 32개이며 기존 ID를 재사용하지 않는다.
