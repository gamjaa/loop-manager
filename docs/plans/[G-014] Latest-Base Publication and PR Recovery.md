---
id: G-014
title: Latest-Base Publication and PR Recovery
sources:
  - docs/PRODUCT.md
depends_on:
  - G-013
complexity: high
risk: high
priority: critical
---

## Purpose

PR 생성 직전까지 최신 원격 base를 반영하고 검증된 commit만 게시하며, push나 PR 기록 사이에 중단돼도 동일 branch와 PR을 중복 없이 복구한다.

## Acceptance Criteria

- PR 게시 직전에 `origin/<base-branch>`를 다시 fetch하고 Review에 사용한 base와 달라졌으면 관리 branch를 rebase한 뒤 전체 verify와 Review를 다시 수행한다.
- 최종 verify 또는 Review 중 base가 다시 바뀌면 최신 base가 고정된 한 시점에서 두 단계가 모두 성공할 때까지 게시 단계로 진행하지 않는다.
- rebase 충돌은 같은 Worker 세션에 충돌 내용을 전달하고, 해결된 변경 범위와 결과를 다시 검증하며 해결할 수 없으면 `BLOCKED`로 만든다.
- rebase가 Worker 변경을 모두 제거하면 PR을 만들지 않고 현재 ExecPlan을 진단 가능한 원인과 함께 `BLOCKED`로 만든다.
- branch push 또는 PR 생성 뒤 SQLite 기록 전에 중단돼도 같은 head branch의 Open, Closed 또는 Merged PR을 조회해 기존 PR에 다시 연결하고 중복 PR을 만들지 않는다.
- 동일한 Loop Manager 소유 branch임을 SQLite와 Git 상태로 확인한 PR 미생성 복구에서만 `--force-with-lease`를 허용하며, 일반 force push나 소유권이 불확실한 원격 branch 갱신은 거부한다.
- PR이 Merge 없이 닫히면 `BLOCKED`로 전이하고 같은 PR이 다시 열렸을 때 `PR_OPEN`으로 복구한다. 새 PR로 대체하지 않는다.
- Merge 확인은 GitHub 상태와 저장된 PR head OID를 사용하고, MERGED 상태와 worktree 정리는 원격 base fetch 이후에 반영한다.
- PR 제목 규칙, Worker의 설명적 commit 제목, Summary, immutable ExecPlan/source 링크와 기존 R-004 동작을 유지한다.
- 최신 base에 대한 전체 verify와 Review가 모두 성공하지 않은 commit으로 PR을 생성하지 않는다.

## Implementation

- 현재 `run_project`의 rebase, verify, Review와 publish 코드를 명시적인 publication loop와 복구 가능한 상태 전이로 분리한다.
- 검증 기준 base SHA를 SQLite 실행 상태에 기록하고 게시 직전 비교한다.
- 원격 branch와 PR 조회 결과를 소유권 정보와 함께 판정하는 idempotent publication helper를 추가한다.
- rebase 충돌 수정은 Worker 범위 검증과 재시도 예산을 그대로 사용하며 새 임시 PR이나 branch를 만들지 않는다.

## Validation

- Worker는 publication state와 PR recovery 테스트 class만 targeted unit test로 실행한다.
- verify 후 base 변경, Review 후 base 재변경, rebase 충돌과 변경 소실을 검증한다.
- push 후 중단, PR 생성 후 DB 기록 전 중단, 기존 Open/Closed/Merged PR과 원격 branch만 남은 경우를 검증한다.
- 전체 verify에서 R-004 PR 본문, R-002 Merge 방식별 cleanup과 기본 worktree 안전성이 유지돼야 한다.

## Progress

- [ ] 최신 base publication loop와 검증 기준 저장 구현
- [ ] rebase 충돌 Worker 복구와 변경 소실 차단 구현
- [ ] branch push 및 PR 생성 idempotent 복구 구현
- [ ] 게시 race와 PR 상태 회귀 테스트 추가

## Decision Log

- PR 생성 직전의 원격 변경은 이전 검증을 무효화하므로 verify와 Review를 모두 다시 수행한다.
- force push 예외는 PR이 없는 동일 소유 branch의 rebase 복구에만 한정한다.
