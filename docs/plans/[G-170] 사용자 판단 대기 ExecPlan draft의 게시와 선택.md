---
id: G-170
title: 사용자 판단 대기 ExecPlan draft의 게시와 선택
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-061] Agent Roles and Draft ExecPlan Decisions.md
depends_on: []
complexity: high
risk: high
priority: high
---

## Purpose

결정이 필요한 유효한 계획을 planning 실패 대신 draft로 게시하고, 독립 작업은 계속할 수 있게 한다. 기존 document_drafts는 문서 수정 승인으로서 이 기능을 대신하지 않는다.

## Acceptance Criteria

- draft 계획과 실행 가능한 계획을 검증한 한 batch로 게시·catalog에 반영한다. draft는 질문·선택 이유·필요한 결정을 담으며 Worker/branch/worktree/session/PR을 예약하거나 시작하지 않는다.
- draft와 영향받는 의존 계획은 실행하지 않는다. 독립 READY·planning·PR 관측은 계속하며 draft를 planning 오류·프로젝트 차단·완료 또는 Requirement 충족으로 취급하지 않는다.
- 사용자가 원격에서 결정하고 draft 표시를 제거한 유효한 계획만 최신 원격·dependency·소유권 확인 뒤 READY 실행 대상이 된다. Agent의 자기 승인·묵시적 해제와 같은 질문의 반복 게시를 거부한다.
- 질문과 원격 링크·대기 상태를 보존하고, 게시 중단·원격 수정·재시작에서 중복 draft·결정 손실·불완전한 catalog 채택이 없다.

## Implementation

- catalog parser·계획 snapshot·storage와 planning 게시/selection의 책임 경계에 draft metadata와 사용자 결정 대기를 연결한다. 기존 계획 상태·취소·source·ID 고유성의 의미를 보존한다.
- 일반 Planner prompt·결과 검증과 retirement/완료 gate에서 draft를 유효한 사용자 판단 대기로 다룬다. 기존 문서 draft 승인과 AGENTS User Decision은 별도 계약으로 유지한다.
- 공통 경계는 AGENTS.md, docs/ARCHITECTURE.md와 docs/PLANS.md를 참조한다. 기존 helper·관련 테스트를 재사용하고 지정 루트 경로를 보존한다.

## Validation

- catalog metadata·planning selection·catalog batches·requirement retirement의 draft parsing, batch 혼합, dependency, 원격 해제, 자기 승인·재시작 관련 최소 unit case를 실행한다.
- Manager 전체 verify는 draft 무예약·무호출, 독립 계획/프로젝트 지속, 실제 게시·catalog 확정과 기존 문서 draft·guidance 계약을 검증한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- catalog·게시·선택·완료에 걸쳐 새 대기 의미를 연결하므로 complexity high, 오실행·자기 승인 영향으로 risk high다. 알림 표현은 G-175에서 연결한다.
