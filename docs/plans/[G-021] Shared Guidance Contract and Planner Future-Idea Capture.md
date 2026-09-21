---
id: G-021
title: Shared Guidance Contract and Planner Future-Idea Capture
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-007] Controlled Shared Agent Guidance Changes.md
depends_on:
  - G-020
complexity: medium
risk: high
priority: high
---

## Purpose

G-020의 승인 흐름을 사용해 공통 Agent 지침과 제안 형식을 사용자 승인 아래 활성화하고, Planner가 현재 범위 밖 아이디어를 구현 계획과 섞지 않은 채 `docs/FUTURE_IDEAS.md`에 보존할 수 있게 한다.

## Acceptance Criteria

- Worker는 이 ExecPlan에 필요한 기준 `AGENTS.md`의 정확한 변경안을 G-020 형식으로 먼저 제안하고, 사용자가 동일 ExecPlan의 `User Decision`에서 승인하거나 대체 patch를 확정하기 전에는 실제 파일을 수정하거나 이후 검증·게시 단계로 진행하지 않는다.
- 승인된 기준 `AGENTS.md`는 Planner가 새 ExecPlan과 함께 `docs/FUTURE_IDEAS.md`를 생성·수정할 수 있음을 명시하고, Planner와 Reviewer의 `AGENTS.md` 변경 및 승인 전 Worker 직접 변경 금지를 유지한다.
- 공통 `docs/PLANS.md`에 `Decision Log`의 guidance 제안과 `User Decision` 형식, 승인·거절·대체 patch 작성 방법 및 결정란 밖의 변경으로는 승인 BLOCKED가 해제되지 않는다는 규칙을 문서화한다.
- Planner prompt와 scope 검증은 새 `docs/plans/*.md` 및 `docs/FUTURE_IDEAS.md` 변경만 허용한다. 기존 ExecPlan 수정·삭제와 그 밖의 파일 변경은 기존 planning validation 오류로 거부한다.
- `docs/FUTURE_IDEAS.md`는 현재 PRODUCT/Requirement 범위 밖이지만 보존 가치가 있는 아이디어만 기록하며, 그 항목을 같은 계획 주기의 ExecPlan 범위나 미충족 작업의 근거로 사용하지 않는다.
- Planner 결과 검증은 FUTURE_IDEAS 변경을 ExecPlan으로 파싱하거나 새 ID 연속성·source·dependency 검증 대상에 포함하지 않고, 같은 batch의 새 ExecPlan만 기존 catalog 규칙으로 검증·등록한다.
- FUTURE_IDEAS와 새 ExecPlan이 함께 있는 유효한 계획 결과는 하나의 planning commit으로 게시되고, FUTURE_IDEAS만 수정됐다는 이유로 가짜 ExecPlan 또는 SQLite 실행 상태를 만들지 않는다.
- Planner가 FUTURE_IDEAS 이외의 보호 파일을 변경하거나 기존 계획을 수정하면 FUTURE_IDEAS의 유효한 변경도 함께 게시하지 않고 같은 Planner 세션의 correction 흐름을 사용한다.
- 승인된 공통 `AGENTS.md`는 이후 새 프로젝트 설정의 기준 원본이 되지만 이미 등록된 프로젝트에는 자동 복사, commit, push 또는 PR을 만들지 않는다.

## Implementation

- G-020이 제공한 형식으로 기준 `AGENTS.md` patch를 제안해 사용자 결정을 받은 뒤 확정된 patch만 적용하고, 공통 `docs/PLANS.md`에 동일한 제안·결정 계약을 추가한다.
- Planner prompt에 현재 범위 밖 아이디어의 FUTURE_IDEAS 분리 규칙을 추가하고, Git scope validator에서 해당 경로만 기존 파일 수정 또는 새 파일 생성을 허용한다.
- planning result 검증을 허용된 보조 문서 변경과 새 ExecPlan 목록으로 분리해 계획 ID·frontmatter·dependency 검증 및 SQLite 등록에는 ExecPlan만 전달한다.
- planning commit, 입력 fingerprint와 완료 알림 경계에서 FUTURE_IDEAS가 활성 Requirement나 실행 상태로 오인되지 않도록 기존 제외 규칙을 유지한다.

## Validation

- Worker는 Planner scope/result validation과 guidance 계약 문서 변경에 직접 관련된 test class 또는 test module만 targeted unit test로 실행한다.
- 새 ExecPlan과 FUTURE_IDEAS의 동시 생성, 기존 FUTURE_IDEAS 수정, 보조 문서의 ExecPlan 파싱 제외와 ExecPlan만의 연속 ID 등록을 검증한다.
- FUTURE_IDEAS 외 경로 변경, 기존 ExecPlan 수정·삭제 및 범위 밖 아이디어를 같은 ExecPlan에 포함한 잘못된 결과가 게시되지 않는지 검증한다.
- FUTURE_IDEAS 변경이 planning fingerprint, Requirement 활성 목록, SQLite ExecPlan 상태와 프로젝트 완료 알림을 새 작업으로 오인시키지 않는지 검증한다.
- G-020 흐름으로 승인 전 기준 `AGENTS.md`가 불변이고, 승인·대체 patch만 정확히 적용되며 기존 프로젝트에 자동 배포되지 않는지 검증한다.
- 전체 verify에서 Planner correction retry, 계획 commit publication과 Worker/Reviewer 보호 범위가 회귀하지 않아야 한다.

## Progress

- [ ] 기준 AGENTS 변경 제안과 사용자 승인 반영
- [ ] docs/PLANS guidance 제안·결정 계약 문서화
- [ ] Planner FUTURE_IDEAS prompt·scope·result validation 구현
- [ ] planning publication, fingerprint와 자동 배포 방지 회귀 테스트 추가

## Decision Log

- 공통 지침을 먼저 바꾸면 현재 Worker가 승인되지 않은 규칙의 영향을 받으므로, G-020 Merge 후 같은 ExecPlan에서 정확한 patch 승인을 받는 순서를 강제한다.
- FUTURE_IDEAS는 계획 입력이나 실행 상태가 아니며, 현재 범위의 ExecPlan을 대체하거나 자동 승격하는 저장소로 사용하지 않는다.

### Agent Guidance Proposal: shared-guidance-future-ideas
Target: `AGENTS.md`
Scope: `common`
Reason: 현재 ExecPlan은 Planner가 새 ExecPlan과 함께 범위 밖 아이디어를 `docs/FUTURE_IDEAS.md`에 분리해 보존하도록 허용하면서도, Planner와 Reviewer의 `AGENTS.md` 변경 및 승인 전 Worker의 직접 변경을 공통 지침에서 명시적으로 금지해야 한다.
Impact: 적용하지 않으면 Planner의 FUTURE_IDEAS 변경이 현재 공통 역할 범위를 위반하고, AGENTS 변경 제안·승인 경계가 후속 Agent 실행에 일관되게 적용되지 않아 현재 ExecPlan의 Planner scope와 보호 조건을 구현할 수 없다.
Patch:
```diff
diff --git a/AGENTS.md b/AGENTS.md
--- a/AGENTS.md
+++ b/AGENTS.md
@@ -30,5 +30,8 @@
 구현 전 관련 문서를 확인한다.
-
-Planner는 `docs/plans/*.md`만 생성하거나 수정한다.
-
+
+Planner는 새 `docs/plans/*.md`를 생성하고, 현재 PRODUCT/Requirement 범위 밖이지만 보존 가치가 있는 아이디어만 `docs/FUTURE_IDEAS.md`에 생성하거나 수정할 수 있다. 기존 ExecPlan을 수정·삭제하거나 그 밖의 파일을 변경하지 않는다.
+Planner는 `docs/FUTURE_IDEAS.md`의 항목을 같은 계획 주기의 ExecPlan 범위나 현재 미충족 작업의 근거로 사용하지 않는다.
+Planner와 Reviewer는 `AGENTS.md` 변경을 제안하거나 적용하지 않는다.
+Worker는 현재 ExecPlan에 꼭 필요한 `AGENTS.md` 규칙 변경을 발견해도 승인 전에 파일을 직접 수정하지 않고, 선택된 ExecPlan의 `Decision Log`에 `docs/PLANS.md`가 정한 제안과 빈 `User Decision`을 기록한 뒤 작업을 중단한다.
+
 Worker는 구현, 관련 targeted unit test 작성·실행과 전체 검증 스크립트 생성을 담당한다.
```

### Agent Guidance User Decision: shared-guidance-future-ideas
Decision: `approve`
Revision: `user-approval-2026-09-21-v1`
Patch:
```diff
```
