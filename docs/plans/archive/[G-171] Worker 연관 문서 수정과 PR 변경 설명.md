---
id: G-171
title: Worker 연관 문서 수정과 PR 변경 설명
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-061] Agent Roles and Draft ExecPlan Decisions.md
  - docs/requirements/[R-064] 모델 호출을 통한 공통 Agent 지침 전달.md
  - docs/requirements/[R-066] AGENTS 변경 사전 승인.md
  - docs/requirements/[R-069] User Decision 경계와 입력 양식.md
depends_on:
  - G-147
  - G-170
complexity: high
risk: high
priority: high
---

## Purpose

R-061이 허용한 연관 문서 수정 경계와 최종 PR 문서 변경 설명을 구현한다. 현재 Worker 역할 지침·작업 prompt·scope는 Requirement/ARCHITECTURE 변경을 제한한다.

## Acceptance Criteria

- 선택 목표에 필요한 PRODUCT·ARCHITECTURE·Requirement·관련 ExecPlan·공통·역할별 지침을 각 문서 역할과 허용 범위 안에서 함께 갱신할 수 있다. 새 목표·사용자 의미 변경·Non-Goal·일반 사용자 결정·실행/소유 계획 보호를 문서 수정으로 우회하지 않는다. R-066·R-069에 따라 지침 파일 변경에 별도 승인·patch 대조·승인 대기를 요구하지 않는다.
- 원래 사용자 요구·Acceptance Criteria와 결정/실패 보존 조건을 유지한다. 문서 사이 불일치나 사용자 결정 필요 사항은 명시적으로 보류하며 요구를 약화해 구현 성공으로 만들지 않는다.
- PR은 최종 실제 문서별 경로·내용·이유·영향과 diff 링크를 제공한다. 수정/rename/delete·rebase 후 보고 누락·오래된 내용은 Manager가 실제 diff와 대조해 보완하고 Reviewer가 읽기 전용으로 검토한다.
- 기존 ExecPlan/source의 immutable 링크·Purpose·Summary와 공통·역할별 지침의 최종 diff 보고를 재사용한다. 진행 중 Worker/Reviewer는 같은 원본 묶음 버전에서 각 역할 지침을 합쳐 사용하며, 작업 중 지침 파일 수정으로 현재 실행 권한을 바꾸지 않는다. Requirement 이동의 Manager 책임을 유지한다.

## Implementation

- `docs/AGENTS.worker.md`의 문서 권한, agents/agent_prompts.py의 작업별 문서 문맥과 scope 검증의 책임 분류를 정합하게 갱신하고 documents/pr_description.py의 최종 diff 보고 경계를 확장한다. 상위 instruction의 권한 제한을 사용자 prompt만 고쳐 우회하지 않는다. 기존 provenance·허용 경로·원격 변경·사용자 결정 검사와 공통·역할별 지침 보고를 재사용하며 이미 구현된 보고를 중복하지 않는다.
- 루트 AGENTS.md와 docs/AGENTS.*.md 변경은 현재 역할·목표의 일반 파일 수정 권한과 최종 PR 보고를 따른다. Manager가 프로젝트 루트 지침을 별도로 읽거나 제안·사전 승인·patch 대조·승인 대기를 만들지 않는다. 과거 승인·문서·사용자 편집본은 보존하되 현재 권한이나 적용 patch로 사용하지 않는다. 일반 사용자 판단이 필요한 목표 변경은 기존 대기로 처리하며 Planner나 Manager가 권한을 자체 확장하지 않는다.
- 자동 역할·권한·검증은 고정된 docs/AGENTS.common.md와 해당 역할의 docs/AGENTS.*.md instruction을 사용하고, 설계·계획 작성은 docs/ARCHITECTURE.md·docs/PLANS.md를 참조한다. 프로젝트 지침 해석은 Agent CLI에 맡기며 Manager 호출에 루트 지침의 명시적인 읽기·승인·복원 지시를 추가하지 않는다. 기존 helper·관련 테스트와 R-064의 예약 경로를 유지한다.

## Validation

- scope·Worker result·PR description·guidance 테스트에서 역할 instruction·작업 prompt·허용 경로의 일치, 필요한 연관 변경/무관 변경 거부, 지침 파일에 대한 별도 승인 gate 부재와 일반 사용자 결정 보존, 다문서 rename/delete와 최종 rebase 보고의 최소 unit case를 실행한다. 루트 지침 읽기·과거 승인 patch 대조를 요구하지 않는지 확인한다.
- Manager 전체 verify는 원래 기준 보존, source immutable 링크, 공통·역할별 원본 묶음 버전, draft·retirement 및 Reviewer 읽기 전용 경계를 검증한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 문서 권한과 최종 보고의 검증 경계가 연결되어 complexity high, 요구 약화·범위 확대 영향으로 risk high다. R-061의 명시적 변경만 구현한다.
- 2026-10-07 지침 분리 정합화: 향후 문서 권한 변경은 Worker 역할 instruction과 실제 scope를 함께 고쳐야 한다. 이번 계획 개정 자체는 Worker 권한을 확대하거나 진행 중 지침 버전·승인을 변경하지 않는다.
- 2026-10-09 사용자 요청에 따른 최신 명세 정합화: R-064·R-066·R-069의 별도 파일 승인 제거를 적용해 루트 승인 제안·중단·검증 요구를 제거했다. 일반 문서 권한·사용자 판단·고정 instruction·PR 보고 목표와 기존 dependency·실행 이력은 유지한다. 문서 개정이며 구현·검증·대기 해제 완료를 의미하지 않는다.
