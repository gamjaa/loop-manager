---
id: G-177
title: 변경 중심 planning과 최종 명세 전문 확인
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-057] Change-Focused Planning and Final Full Verification.md
  - docs/requirements/[R-061] Agent Roles and Draft ExecPlan Decisions.md
depends_on:
  - G-170
  - G-176
complexity: high
risk: high
priority: critical
---

## Purpose

일반 planning의 변경 검토와 프로젝트 완료 전 최종 전체 문서 확인을 별도 목적·근거로 기록한다. 현재 prompt는 매번 전체 읽기를 요구하고 최종 확인을 별도로 증명하지 않는다.

## Acceptance Criteria

- 최초/유효 기준선 없음은 전체 입력을 읽는다. 이후 일반 planning은 마지막 성공 원격 입력과의 변경·삭제/rename 및 영향받는 문서/코드·catalog를 우선 검토하고 필요한 문맥을 읽는다. 무관한 변경으로 같은 판단을 반복하지 않는다.
- 빠른 점검·변경 planning·추가 계획 없음은 최종 제품 충족을 대신하지 않는다. 기존 실행/PR/READY/draft·차단 gate가 모두 해소된 완료 후보에서 일반 Planner가 활성 명세 전문과 최신 구현·검증을 최종 확인한다.
- 최종 확인의 입력 identity·결과를 보존해 같은 입력/재시작에서 재사용한다. 미충족이면 source에 연결된 후속 계획, 사용자 결정 필요면 draft로 남기고 완료·archive 성공을 합성하지 않는다.
- 선택된 기존 작업·유지보수 종료를 선점하거나 최종 확인을 유지보수 post-revalidation 대체로 호출하지 않는다. 입력 변화·불완전한 근거·게시 실패는 해당 성공 기준을 확정하지 않는다.

## Implementation

- planning 입력 지문·성공 cycle·역할 선택과 agents/agent_prompts.py의 일반 planning 문맥을 확장한다. R-068의 Requirement별 judgment를 재사용하되 프로젝트 전체 최종 전문 확인과 혼동하지 않는다.
- 변경 기준선·최종 receipt를 실제 원격 revision과 명세 내용에 결합하고 delta에서 불확실한 영향은 필요한 원문 확인으로 보완한다. 성공 receipt와 활성 작업 소유권을 분리한다.
- 공통 경계는 AGENTS.md, docs/ARCHITECTURE.md와 docs/PLANS.md를 참조한다. 기존 helper·관련 테스트를 재사용하고 지정 루트 경로를 보존한다.

## Validation

- tests/test_planning_selection.py와 planning publication/requirement retirement 테스트에서 최초·delta·삭제/rename·최종 확인·후속/draft·동일 입력 no-call의 최소 unit case를 실행한다.
- Manager 전체 verify는 변경 입력 처리, READY/PR 순서, 완료 통지·retirement·게시 실패와 유지보수 종료 후 선택의 회귀를 검증한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 성공 기준선·역할 선택·완료 gate가 연결되어 complexity high, 누락된 목표의 조기 완료 영향으로 risk high다.
