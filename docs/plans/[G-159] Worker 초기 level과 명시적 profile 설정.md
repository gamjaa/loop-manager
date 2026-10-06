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

- 초기 level과 기존 계획의 initial level cache는 complexity 1·3·5에 critical risk만 1을 더한 1~6이다. 산식 전환에서는 실행 이력이 있는 계획의 effective level·session·작업물·승격 및 실패 감사 기록을 보존하며 level 불일치 오류나 자동 차단 해제를 만들지 않는다. 기존의 정상적인 BLOCKED 해제에서만 새 산식으로 최초·effective level과 승격 이력·집계를 초기화한다. 새 최초 배정에서 level 7·8은 사용하지 않으며 이후 승격은 G-160에서 다룬다.
- 선택 provider의 worker_1~8과 planner·planner_check의 Model·Effort·참조 alias를 모두 명시적으로 검증한다. 누락·빈 값·잘못된 형식·미지원 조합은 오류 위치와 항목을 남기고 모델 시작 전에 거부하며 다른 profile·CLI 기본값으로 보충하지 않는다. 미선택 provider의 설정은 요구하지 않고 기존 작업물·session·실패 이력을 보존한다.
- level 8의 특정 Model·Effort 강제를 제거하고 다른 level과 같은 지원 조합 검증을 적용한다. 여러 level의 동일 profile, maintenance의 planner 연결과 기존 Reviewer의 실제 Model·Effort를 보존한다.
- config.ini.example에는 R-052의 Codex·Claude planner·planner_check와 Worker 1~8, 참조 alias가 모두 실제 값으로 채워진다. Claude Worker는 R-052의 Sonnet 5.5·Opus 5.5 단계표를 따르고 필요한 CLI 버전을 안내한다. 지원 CLI 환경에서는 예시를 복사한 뒤 모델 설정 보완 없이 사용할 수 있으며 예시를 운영 설정의 fallback으로 읽지 않는다.
- 분류 근거는 남은 설계·구현 난도와 실패 영향으로 설명한다. 코드 parser, 설정 예제와 안내가 같은 규칙을 사용하며 새 설정 오류를 묵시적 대체로 숨기지 않는다.

## Implementation

- storage/execplan_catalog_state.py의 초기 계산과 agents/agent_profiles.py의 profile 해석을 기존 검증 경계에서 고친다. catalog 전환, 저장된 실행 level 검증과 recovery/blocked_plan_service.py의 정상 차단 해제를 구분해 새 초기 cache와 보존된 effective level을 일관되게 취급한다. 과거 catalog 호환성과 최초 등록·재선택의 구분을 유지한다.
- config의 기존 예제·설정 안내와 docs/PLANS.md의 분류 지침을 갱신한다. R-052에 명시한 Planner와 Claude Worker 단계표·CLI 최소 버전을 구현 환경의 지원 정보와 대조하고, 현재 alias 변경이 Reviewer의 실제 Model·Effort까지 바꾸지 않게 한다. 별도 모델 목록이나 전역 설정을 추가하지 않는다. 초기 cache 재계산과 effective level 보존은 기존 catalog transaction·재개 경계에 반영한다.
- 문서·권한·게시와 검증의 공통 경계는 AGENTS.md, docs/ARCHITECTURE.md, docs/PLANS.md를 따른다. 필요한 기존 helper와 관련 테스트를 우선 재사용하고 새 루트 항목을 만들지 않는다.

## Validation

- tests/test_catalog_metadata.py, test_execplan_catalog_state.py, test_provider_profiles.py와 기존 차단 해제 fixture를 재사용해 complexity·risk 12개 조합, initial cache 재계산·effective level 보존, 산식 전환만으로 차단을 해제하지 않음과 정상 BLOCKED 해제의 새 최초 level·승격 이력·집계 초기화를 검증하는 최소 unit case를 실행한다. 선택 provider의 필수 profile·alias 누락과 빈 값·미지원 조합 거부, level 8의 지원 대체 profile·동일 profile 허용, Planner fallback 제거와 maintenance 연결도 변경 경계에서 확인한다.
- Manager 전체 verify는 예시의 두 provider별 Planner·Worker 전체 profile 해석, Claude 단계표·버전 안내와 Reviewer 실제 설정 보존, 기존 설정 호환성을 검증한다. 산식 전환·설정 오류·중단 뒤 session·작업물·실패 감사 기록이 보존되고, 정상 BLOCKED 해제에서만 새 최초·effective level과 승격 이력·집계가 초기화되는 회귀 조건을 포함한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [x] 2026-10-06 a1db7e8와 제공된 mutable READY snapshot의 원본 hash를 대조하고 최신 R-052의 명시 설정·Claude 예시·기존 실행 level 보존 조건을 보강함. 구현·테스트 실행은 하지 않음.
- [x] 2026-10-06 최신 기준 49a7400과 새 mutable READY snapshot의 원본 hash를 대조함. G-193의 선택 변경은 이 목표를 대체하지 않으며 R-052의 산식 전환 보존과 정상 BLOCKED 해제 초기화를 구분한 완료·회귀 조건을 유지함. 구현·테스트 실행은 하지 않음.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 2026-10-06: snapshot token 6739ea8ea5a43e56da15b0733a8bc892154efa877fafb9f48d1dd17936a5fb63 안의 G-159를 같은 ID·경로·목표로 개정한다. R-052는 산식 전환 시 실행 이력을 보존하되 정상 BLOCKED 해제는 새 최초 level로 초기화하도록 구분한다. 기존 해제 구현과 관련 테스트를 재사용하고 두 상황을 함께 보존으로 처리한 이전 완료 조건을 바로잡는다. 현재 level 8의 일반 지원 조합 검증도 이미 구현된 helper·회귀 테스트를 유지하며 별도 구현을 중복하지 않는다.
- 2026-10-06: 최신 R-052의 profile·예시·initial cache 규칙은 기존 계산·설정 목표 안의 미충족 조건이다. 같은 G-159에 반영하고 Review 교정 집계·승격 구현은 G-160에 유지한다. provider 지원 확인은 구현 시 수행하며 문서에 적힌 모델명을 코드의 강제 profile로 만들지 않는다.

- 2026-10-05 게시 전 번호 검증 오류 교정: Manager parser의 활성·archive 바로 아래 계획 및 제공 catalog 기준 최대 ID는 G-158이다. 중첩 `docs/plans/manual/`은 관리 catalog 입력이 아니므로 번호 집계에 포함하지 않는다. 아직 게시·등록되지 않은 이 batch 31개를 G-159~G-189로 연속 배정하고 dependency·본문 참조를 같은 대응으로 갱신했다. 기존 snapshot ID·계획 목표·사용자 문서는 변경하지 않았다.

- 계산·설정은 프로그램의 결정적 책임이다. Review 교정 결과에 따른 승격은 G-160에서 따로 다룬다. 난도는 제한된 정책 경계 변경, 위험은 잘못된 호출 선택의 영향으로 분류했다.
- 이동 영수증의 rename 관측 오류는 별도 G-190으로 추가했다. 최종 신규 batch는 G-159~G-190의 32개이며 기존 ID를 재사용하지 않는다.
