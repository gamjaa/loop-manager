---
id: G-008
title: Descriptive Worker Commits and Immutable PR Context
sources:
  - docs/requirements/[R-004] Descriptive Worker Commits and PR Context.md
depends_on:
  - G-006
complexity: medium
risk: medium
---

## Purpose

Worker의 최종 결과에서 실제 변경을 설명하는 commit 제목과 요약을 받아 검증하고, 계획 당시 commit에 고정된 ExecPlan·source 링크와 함께 PR에 기록해 Git 이력과 Review 문맥을 장기적으로 추적할 수 있게 한다.

## Acceptance Criteria

- Worker prompt는 targeted unit test를 마친 최종 diff 전체를 설명하는 한 줄 commit 제목과 PR Summary를 명시적인 결과 형식으로 반환하도록 요구한다.
- commit 제목이 비어 있거나 여러 줄이거나 ExecPlan 제목을 그대로 반복하거나 `Implement <ExecPlan>` 및 그 밖의 일반적인 변경 문구뿐이면 commit하지 않고 같은 Worker 세션에 구체적인 검증 오류를 전달해 다시 요청한다.
- verify 또는 Review 실패 수정으로 Worker가 다시 실행되면 새 결과의 commit 제목과 Summary가 수정 후 전체 diff를 설명해야 하며, 이전 결과나 ExecPlan 제목으로 대체하지 않는다.
- 유효한 Worker commit 제목을 실제 Worker 변경 commit에 사용하고 Planner의 `Add ExecPlan <ID>: <title>` commit 제목 규칙은 변경하지 않는다.
- PR 본문에는 Worker가 보고한 실제 변경 Summary, ExecPlan ID·제목·링크와 ExecPlan Purpose의 짧은 설명, 모든 `sources` 경로와 링크를 포함한다.
- ExecPlan과 source 링크는 Worker branch의 최신 base가 아니라 해당 계획을 게시한 commit SHA에 고정한다.
- GitHub blob 링크의 경로는 공백, 대괄호와 비 ASCII 문자를 포함해 UTF-8 percent-encoding하며 저장소 경로 구분자는 유지한다.
- Requirement가 이후 archive로 이동하거나 base branch의 파일 경로가 바뀌어도 이미 생성된 PR의 계획 및 source 링크는 계획 당시 내용을 계속 가리킨다.
- 기존 branch의 PR을 복구하는 흐름은 중복 PR을 만들거나 본문을 임의로 덮어쓰지 않고 현재 동작을 유지한다.

## Implementation

- Worker prompt와 결과 파싱 경계에 commit 제목, PR Summary 및 기존 targeted test 증거를 구분하는 작은 구조화 계약을 추가하고, 누락·여러 줄·ExecPlan 제목 반복·알려진 일반 문구를 결정적으로 거부한다.
- Worker 결과가 유효하지 않으면 worktree 변경을 보존한 채 `WORKING`에 머물고 검증 오류를 `last_error`로 저장해 기존 `worker_session_id`로 재개하며, 유효한 제목과 Summary가 생기기 전에는 전체 verify나 commit으로 진행하지 않는다.
- verify 또는 Review 수정 뒤의 마지막 유효 Worker 결과를 게시 메타데이터의 원본으로 사용하고, commit 생성 시 하드코딩된 `Implement ...` 제목 대신 검증된 Worker 제목을 전달한다.
- 계획이 base에 게시돼 Worker 작업이 시작될 때의 commit SHA를 프로젝트 작업 상태에 별도 저장하는 호환 가능한 SQLite migration을 추가하고, 이후 base rebase로 `active_sha`가 바뀌어도 이 값은 유지하다가 활성 작업 정리 시 함께 비운다.
- GitHub origin에서 저장소 식별자를 정규화하고 계획 commit SHA 및 percent-encoding된 프로젝트 상대 경로로 immutable blob URL을 만드는 부작용 없는 helper를 추가한다.
- 검증된 ExecPlan 본문에서 `Purpose`를 읽고 Worker Summary, 계획 정보와 `sources` 목록을 조합하는 PR 본문 생성 함수를 분리한 뒤 기존 `gh pr create --body`의 정적 문구를 이 결과로 교체한다.

## Validation

- Worker 결과 파서가 정상 제목·Summary·targeted test 증거를 보존하고, 빈 제목, 여러 줄 제목, ExecPlan 제목 반복과 일반 문구를 거부하는 targeted unit test
- 잘못된 제목 뒤 같은 Worker 세션 재개, verify 및 Review 수정 뒤 최신 제목 사용, 유효 결과 전 verify·commit 미실행을 검증하는 상태 전이 targeted unit test
- Worker 제목을 commit에 사용하면서 Planner 계획 commit 제목은 유지하는 targeted unit test
- 계획 SHA가 base rebase 뒤에도 유지되고 기존 SQLite DB migration 및 활성 작업 정리에서 올바르게 저장·해제되는 targeted unit test
- 공백, 대괄호와 비 ASCII 경로의 immutable GitHub URL 및 Worker Summary·ExecPlan Purpose·전체 source 링크를 포함하는 PR 본문 targeted unit test
- 전체 verify에서 기존 Worker→VERIFYING→REVIEWING→PR 생성 순서, 기존 PR 복구와 Planner 게시 흐름이 회귀하지 않는지 확인

## Progress

- [ ] Worker 게시 메타데이터 결과 계약과 동일 세션 재요청 구현
- [ ] 검증된 Worker 제목을 commit 생성에 연결
- [ ] 계획 commit SHA 영속화와 immutable GitHub 링크 생성
- [ ] 실제 변경·ExecPlan·source 문맥을 포함한 PR 본문 생성
- [ ] 결과 검증, 상태 전이, migration, URL과 PR 본문 단위 테스트 추가

## Decision Log

- G-006에서 도입한 Worker 세션과 결과 저장을 재사용해 commit 제목 검증 실패도 코드 변경을 잃지 않고 같은 세션에서 교정한다.
- 링크 기준은 PR 생성 시점의 base가 아니라 ExecPlan이 계획된 commit으로 고정해 Requirement archive와 후속 경로 변경의 영향을 받지 않게 한다.
- commit 제목과 PR Summary는 같은 최종 Worker 결과에서 받되 각각의 사용 목적에 맞게 검증하고, 유효하지 않을 때 ExecPlan 제목이나 정적 문구로 fallback하지 않는다.
