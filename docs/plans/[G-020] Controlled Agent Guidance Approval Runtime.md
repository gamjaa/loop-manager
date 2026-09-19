---
id: G-020
title: Controlled Agent Guidance Approval Runtime
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-007] Controlled Shared Agent Guidance Changes.md
depends_on:
  - G-014
  - G-016
complexity: high
risk: critical
priority: high
---

## Purpose

Worker가 현재 ExecPlan에 꼭 필요한 Agent 지침 변경을 실제 `AGENTS.md`에 먼저 적용하지 않고 정확한 patch로 제안하게 하며, 명시적인 사용자 결정과 승인된 patch 일치 검증을 거친 경우에만 같은 작업 공간과 세션에서 안전하게 재개할 수 있는 런타임을 만든다.

## Acceptance Criteria

- Worker prompt는 `AGENTS.md`를 직접 수정하지 않고 선택된 ExecPlan의 `Decision Log`에 대상 경로, 프로젝트 전용·공통 범위, 이유, 미적용 영향과 적용할 정확한 patch를 포함한 제안을 기록하도록 지시한다.
- 모든 등록 프로젝트의 Worker는 현재 프로젝트의 `AGENTS.md` 변경만 제안할 수 있고, Loop Manager 자체 저장소의 Worker만 향후 프로젝트 설정에 사용하는 기준 `AGENTS.md` 공통 변경을 제안할 수 있다.
- Planner와 Reviewer의 제안 또는 `AGENTS.md` 변경, 승인 전 Worker의 직접 변경 및 허용 대상 밖의 patch는 기존 역할 범위 위반으로 차단한다.
- 완전하고 유효한 제안을 감지하면 구현 diff, 선택된 ExecPlan과 Worker 세션을 보존한 채 commit, 전체 verify, Review, push와 PR 생성 전에 현재 ExecPlan을 전용 사유의 `BLOCKED`로 전환한다.
- BLOCKED 원인과 알림 데이터에는 ExecPlan ID·경로, 제안 위치와 사용자가 편집할 `User Decision` 위치가 포함된다.
- 사용자는 보존된 관리 worktree의 동일 ExecPlan에서 각 제안의 `User Decision`만 승인, 거절 또는 정확한 대체 patch로 수정할 수 있다. 다른 ExecPlan 본문이나 source 문서 변경만으로는 이 승인 BLOCKED가 해제되지 않는다.
- 프로그램은 결정이 참조하는 제안, 정규화된 대상 경로와 사용자가 확정한 patch의 결정적 hash를 SQLite 승인 기록에 결합하고 프로세스 중단 뒤에도 복구한다.
- 승인 또는 대체 결정이면 같은 Worker 세션과 worktree에서 확정된 patch만 적용하도록 재개하고, 거절이면 `AGENTS.md`를 변경하지 않은 채 재개한다.
- 재개 뒤 실제 `AGENTS.md` diff가 승인된 대상과 patch와 정확히 다르거나 새로운 지침 변경이 필요하면 기존 승인을 무효화하고 최신 정확한 제안에 대한 새 사용자 결정을 기다린다.
- 거절된 규칙 없이 Current Goal을 달성할 수 없다고 Worker가 판단하면 이유와 새 결정 사항을 남기고 다시 `BLOCKED`로 전환할 수 있다.
- 승인된 변경은 일반 Worker 변경과 동일한 targeted test, 전체 verify, 읽기 전용 Review와 G-014의 최신-base 게시 검증을 거쳐 같은 구현 PR에 포함하며 자동 Merge하지 않는다.
- 승인된 기준 파일은 이후 프로젝트 설정의 공통 원본으로만 사용하고 기존 등록 프로젝트에는 자동 배포하지 않는다.

## Implementation

- 선택된 ExecPlan의 `Decision Log`에서 제안과 `User Decision`을 결정적으로 파싱·정규화하는 작은 형식 모듈을 만들고, 불완전하거나 모호한 결정은 적용하지 않는다.
- G-013의 지속 세션·재시도 상태와 G-016의 프로젝트 registry를 사용해 프로젝트 전용 대상, Loop Manager 자체 저장소와 기준 `AGENTS.md` 대상을 구분한다.
- 일반 source-hash BLOCKED와 분리된 guidance-approval 원인, 제안 identity, 결정, 대상 및 patch hash를 호환 가능한 SQLite migration으로 저장하고 전용 해제 판정을 구현한다.
- Worker 종료 직후 범위 검증과 Manager Git 변경 전에 제안 감지 경계를 두고, 보존·차단·재개 과정에서 Agent가 만든 기존 diff나 세션을 잃지 않게 한다.
- 승인 재개 시 적용 전후 파일 내용을 exact patch 결과와 비교하는 scope 검증을 추가하고, 불일치 시 commit 이전에 다시 차단한다.
- 후속 계획이 공통 계약 문서를 갱신할 수 있도록 사용자 승인 대상 `AGENTS.md`와 명시적으로 계획된 공통 `docs/PLANS.md` 변경을 구분하는 Worker 범위 경계를 제공하되, 다른 보호 문서에 대한 일반 쓰기 권한은 열지 않는다.

## Validation

- Worker는 guidance 제안 parser, 승인 상태 전이와 Agent scope에 직접 관련된 test class 또는 test module만 targeted unit test로 실행한다.
- 프로젝트 전용·공통 제안 권한, Planner/Reviewer 제안 거부, 승인 전 파일 불변과 commit·verify·Review·push 미실행을 검증한다.
- 승인, 거절과 대체 patch가 같은 세션·worktree에서 재개되는 경우 및 제안란 밖의 변경으로는 해제되지 않는 경우를 검증한다.
- 대상 경로·patch hash의 결정성, 프로세스 재실행 보존, 승인 후 exact diff, 불일치·새 제안에 따른 재승인과 거절 후 재차단을 검증한다.
- 기존 구현 diff와 세션이 BLOCKED 동안 보존되고 승인된 변경만 기존 publication 흐름을 통과하는지 검증한다.
- 기존 등록 프로젝트에 대한 자동 배포가 없고, 일반 BLOCKED 해제와 닫힌 PR BLOCKED 동작이 회귀하지 않는지 전체 verify에서 확인한다.

## Progress

- [ ] guidance 제안·User Decision 형식 parser와 identity 구현
- [ ] 전용 BLOCKED·승인 persistence 및 해제 상태 전이 구현
- [ ] 정확한 patch 적용·불일치 차단과 Worker 세션 재개 구현
- [ ] 프로젝트 전용·공통 권한 및 publication 회귀 테스트 추가

## Decision Log

- 현재 런타임은 Worker의 `AGENTS.md`와 `docs/PLANS.md` 변경을 모두 차단하므로, 승인 엔진과 안전한 scope 경계를 먼저 Merge한다. 실제 공통 지침 및 형식 변경은 이 런타임을 사용하는 G-021에서 별도 검증·승인한다.
- 공통 지침 제안 권한은 G-016의 registry에서 Loop Manager 자체 저장소임을 확인한 Worker에만 부여한다.
