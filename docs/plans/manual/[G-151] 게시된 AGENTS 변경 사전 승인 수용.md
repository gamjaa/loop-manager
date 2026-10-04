---
id: G-151
title: 게시된 AGENTS 변경 사전 승인 수용
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-066] AGENTS 변경 사전 승인.md
  - docs/requirements/[R-061] Agent Roles and Draft ExecPlan Decisions.md
depends_on: []
complexity: high
risk: high
priority: high
---

## Purpose

사용자가 원격 기준에 게시한 ExecPlan의 정확한 AGENTS.md 변경 결정을 Worker 시작 전에 인식한다. 기존 형식, SQLite 승인 기록과 적용·게시 검사를 재사용하고 새 승인 저장소나 포괄적 권한을 만들지 않는다.

## Acceptance Criteria

- 채택된 계획 revision의 Proposal·User Decision과 같은 worktree 결정만 사전 승인으로 등록한다.
- 승인 시점의 AGENTS.md 내용·mode를 비교하고 변경 시 기존 guidance 승인 대기로 넘기며 Worker 호출을 막는다.
- 재시작·거절·대체·여러 제안·혼합된 미결정·로컬 위조·실제 결과 불일치 및 기존 pending 흐름을 검증한다.
- 공통 `docs/PLANS.md`를 갱신해 사전 승인 게시 조건과 기존 pending 절차를 함께 설명한다.
- Worker prompt와 실행 범위는 등록된 승인만 사용하며 실제 AGENTS.md를 이 구현에서 수정하지 않는다.

## Implementation

guidance_preapproval에 Worker 호출 전 승인 준비 단계를 추가한다. current_revision_sha 또는 최초 plan_sha에서 읽은 계획과 명시적 Baseline commit의 파일을 신뢰 원본으로 사용하고 Baseline의 ancestry, Scope와 일반 파일 mode를 확인한다. 승인과 기준 파일의 정규화된 내용을 대조하되 기존 최종 patch 검사에서 내용 차이를 숨기지 않는다.

기존 guidance_approvals 행과 문맥을 사용하며 새 schema와 decision 종류를 만들지 않는다. 변경된 기준은 기존 무효화·BLOCKED·User Decision 재개 절차로 연결한다. WorkerHandler의 호출 전 경계와 명시적 effect/port 연결을 갱신한다.

docs/PLANS.md와 PRODUCT의 지속 규칙, Worker prompt를 구현에 맞게 갱신한다. 공통 지침 주입의 G-147과 기존 프로젝트 본문 정리는 별도 범위로 남긴다.

## Validation

Worker는 tests.test_guidance_approval, tests.test_guidance_preapproval 및 tests.test_execution_guidance의 관련 unit test를 실행한다. 실제 임시 Git과 SQLite로 원격 기준 승인 수용, 기준 변경 후 재승인, 승인 없는 로컬 수정 거부, 재시작 보존과 LF/CRLF를 확인한다. 기존 verify 스크립트가 새 test 모듈을 discovery하는지 확인하고 전체 verify는 실행하지 않는다.

## Progress

- [x] 현재 승인·Worker 경계와 사용자 요청을 확인했다.
- [x] 사전 승인 수용과 실행 경계를 구현했다.
- [x] targeted unit test와 문서 검증을 마쳤다.

관련 unit test 총 39개가 통과했다(사전 승인 12, 기존 승인 17, Worker 경계 10). 테스트 process에서만 Git 전역·system 설정을 분리해 개인 설정과 별개로 검증했고, CRLF patch helper는 실제 설정에 의존하지 않도록 처리했다. 계획 metadata·sources와 변경 Python 파일 syntax, git diff --check를 확인했다. 전체 verify, Reviewer와 PR Merge는 이 로컬 구현에서 수행하지 않았다.

## Decision Log

- 2026-10-04: 사용자가 로컬에서 작업한 계획을 서버 자동 실행 입력에서 제외하도록 요청했다. 이 파일은 직접 구현 기록으로 `docs/plans/manual/`에 보존한다. 코드 구현과 targeted 검증은 마쳤지만 전체 verify·읽기 전용 Review·사용자 Merge 전이므로 archive나 SQLite MERGED 상태로 취급하지 않는다.
- 2026-10-04: 사용자가 사전 승인 기능의 즉시 구현과 별도 worktree 작업을 요청했다. 새로운 Requirement와 호환 가능한 승인 동작 확장의 근거로 사용한다.
- 2026-10-04: 원격 기준에서 채택한 계획 revision을 승인 원본으로 사용한다. Worker가 기록한 신규 결정에는 기존 pending 규칙을 유지한다.
- 2026-10-04: 사전 결정의 Baseline SHA를 decision hash에 결합한다. 승인 문구가 유지된 채 원격 기준이 바뀌는 경우와 승인 등록 직후 재시작 시 기준이 바뀌는 경우에도 새 파일을 자동 승인하지 않는다. 기존 Baseline 없는 결정의 identity와 schema는 유지한다.
