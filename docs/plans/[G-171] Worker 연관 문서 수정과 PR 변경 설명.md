---
id: G-171
title: Worker 연관 문서 수정과 PR 변경 설명
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-061] Agent Roles and Draft ExecPlan Decisions.md
depends_on:
  - G-147
  - G-170
complexity: high
risk: high
priority: high
---

## Purpose

R-061이 허용한 연관 문서 수정 경계와 최종 PR 문서 변경 설명을 구현한다. 현재 Worker prompt와 scope는 Requirement/ARCHITECTURE 변경을 일괄 제한한다.

## Acceptance Criteria

- 선택 목표에 필요한 PRODUCT·ARCHITECTURE·Requirement·관련 ExecPlan·공통 지침을 각 문서 역할과 허용 범위 안에서 함께 갱신할 수 있다. 새 목표·사용자 의미 변경·Non-Goal·AGENTS 승인·실행/소유 계획 보호를 문서 수정으로 우회하지 않는다.
- 원래 사용자 요구·Acceptance Criteria와 결정/실패 보존 조건을 유지한다. 문서 사이 불일치나 사용자 결정 필요 사항은 명시적으로 보류하며 요구를 약화해 구현 성공으로 만들지 않는다.
- PR은 최종 실제 문서별 경로·내용·이유·영향과 diff 링크를 제공한다. 수정/rename/delete·rebase 후 보고 누락·오래된 내용은 Manager가 실제 diff와 대조해 보완하고 Reviewer가 읽기 전용으로 검토한다.
- 기존 ExecPlan/source의 immutable 링크·Purpose·Summary와 G-147 공통 지침 보고를 재사용한다. 진행 중 Worker/Reviewer의 고정 공통 버전과 Requirement 이동의 Manager 책임을 유지한다.

## Implementation

- agents/agent_prompts.py·scope 검증의 문서 책임 분류와 documents/pr_description.py의 최종 diff 보고 경계를 확장한다. 기존 provenance·허용 경로·원격 변경·사용자 결정 검사를 재사용한다.
- 현재 AGENTS의 변경이 실제 필요하면 Worker가 선택 계획의 Decision Log에 정확한 제안과 빈 User Decision을 기록하고 중단한다. Planner나 Manager가 권한을 자체 확장하지 않는다.
- 공통 경계는 AGENTS.md, docs/ARCHITECTURE.md와 docs/PLANS.md를 참조한다. 기존 helper·관련 테스트를 재사용하고 지정 루트 경로를 보존한다.

## Validation

- scope·Worker result·PR description·guidance 테스트에서 필요한 연관 변경/무관 변경 거부, 승인 필요 경계, 다문서 rename/delete와 최종 rebase 보고의 최소 unit case를 실행한다.
- Manager 전체 verify는 원래 기준 보존, source immutable 링크, 공통 버전, draft·retirement 및 Reviewer 읽기 전용 경계를 검증한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 문서 권한과 최종 보고의 검증 경계가 연결되어 complexity high, 요구 약화·범위 확대 영향으로 risk high다. R-061의 명시적 변경만 구현한다.
