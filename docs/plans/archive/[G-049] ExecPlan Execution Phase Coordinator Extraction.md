---
id: G-049
title: ExecPlan Execution Phase Coordinator Extraction
sources:
  - docs/PRODUCT.md
depends_on:
  - G-048
complexity: high
risk: high
priority: normal
---

## Purpose

현재 `_run_project_step`의 활성 ExecPlan 구간은 workspace 복구, plan/archive 입력 계산, Worker session, Agent scope, rebase correction, 전체 verify, Reviewer protocol, validation history, branch push와 PR 생성·복구를 한 함수에서 공유 `row`와 전역 helper로 조정한다. `VERIFYING`과 `PUBLISHING`이 latest-base rebase 및 결과 무효화를 각자 수행하고, 여러 예외 경로가 책임이 혼합된 채 직접 persistence·BLOCKED·retry·로그를 선택하므로 이후 recovery 기능들이 같은 핵심 제어 흐름을 반복 변경하게 된다.

`IMPLEMENTING`부터 `PR_OPEN`까지의 실행 lifecycle을 phase별 coordinator와 외부 port 경계로 추출한다. 현재 retry와 즉시 차단 의미까지 그대로 보존하되 workspace, Agent, validation, latest-base 및 publication 결과가 typed phase outcome으로 돌아오게 해 후속 catalog revision·session ledger·recovery 계획이 각 책임 경계에 결합될 수 있게 한다.

## Acceptance Criteria

- workspace preparation, Worker turn, verify, Review와 publication이 명시적인 phase handler로 분리되고 최상위 project runner는 현재 phase의 handler 하나를 dispatch한다.
- 각 handler는 G-047 runtime-state repository를 통해 expected plan/status에 결합된 전이를 저장하고 raw `projects`/`exec_plans` dual-write를 수행하지 않는다.
- workspace/Git, Agent invocation, Requirement archive eligibility, verify, Reviewer parsing, blocking 및 GitHub publication은 주입 가능한 port로 표현되어 phase handler를 실제 subprocess나 GitHub 없이 독립 unit test할 수 있다.
- Worker 결과 이후 archive 이동 검증, verify 성공 뒤 commit, latest-base rebase 뒤 재검증, Review PASS/FAIL, publication preflight와 기존 PR 복구 순서를 현재와 동일하게 보존한다.
- `VERIFYING`과 `PUBLISHING`의 중복 latest-base 검사·rebase·validation 무효화는 하나의 내부 boundary를 사용하되, 현재 어느 조건에서 WORKING·VERIFYING·BLOCKED·retry로 돌아가는지는 변경하지 않는다.
- Agent scope와 guidance approval, validation fingerprint/Worker level, Requirement archive, PR conflict recovery 및 force-with-lease 안전 판정은 기존 전용 helper의 결정을 재사용하고 coordinator에서 다시 구현하지 않는다.
- handler outcome은 success, stable wait, retry, transient pause, usage pause와 현재 final block 결과를 구분하며 top-level adapter가 기존 반환 코드, 로그와 notification 시점을 보존한다.
- branch/worktree/PR naming, prompt와 Agent session 계약, commit 제목·PR body, Git 명령 권한과 순서, SQLite schema·저장 형식, 공개 CLI 및 사용자 동작을 변경하지 않는다.

## Implementation

- 실행 identity, plan metadata, workspace 및 현재 validation/publication marker를 담는 immutable context와 phase outcome을 새 execution runtime 모듈에 정의한다.
- workspace preflight와 각 phase handler를 추출하고, handler가 외부 동작 전에 저장해야 하는 intent와 외부 동작 뒤 확인해야 하는 observation을 명시적으로 반환·적용하게 한다.
- 공통 latest-base gate는 기존 rebase 시작·충돌·성공·no-change 처리와 verify/Review invalidation을 그대로 캡슐화하고 향후 checkpoint/recovery policy를 주입할 seam만 제공한다.
- `_run_project_step`에는 project safety, PR synchronization, planning coordinator와 active execution coordinator 사이의 상위 scheduling만 남긴다.
- 기존 function-level helper와 test patch point가 외부 계약으로 사용되는 경우 얇은 adapter를 유지하고 phase별 새 테스트로 동일 호출 순서를 고정한다.

## Validation

- Worker는 새 execution phase coordinator test module과 top-level dispatch adapter의 직접 관련 테스트만 targeted unit test로 실행한다.
- IMPLEMENTING→VERIFYING→REVIEWING→PUBLISHING→PR_OPEN 정상 전이와 각 phase 중단 후 같은 workspace/session에서의 재개를 fake repository와 port로 검증한다.
- Worker 형식 오류, verify·Review 실패 진행/반복 차단, Reviewer protocol 교정, guidance 승인, Agent scope·workspace 위반의 기존 전이를 검증한다.
- base 변경 전·후 rebase, 충돌, no-change, archive 후보 변화, push 전·후 중단, 기존 PR 발견과 PR conflict recovery에서 외부 호출 순서와 저장된 marker를 검증한다.
- phase handler가 subprocess·GitHub 없이 독립 실행되고 같은 expected snapshot에 대한 반복 호출이 중복 commit, push, PR 또는 상태 전이를 만들지 않는지 검증한다.
- 전체 verify에서 Worker targeted-test 계약, Requirement archive, validation escalation, scoped blocking, origin isolation, publication lease, PR conflict recovery와 concurrent scheduler 회귀가 없어야 한다.

## Progress

- [ ] execution context·port·outcome 계약 추가
- [ ] workspace·Worker·verify·Review·publication handler 추출
- [ ] 공통 latest-base gate와 top-level dispatch 연결
- [ ] phase 중단·실패·publication 회귀 테스트 추가

## Decision Log

- recovery disposition, catalog revision invalidation, usage ledger와 progress-aware rebase의 새 제품 의미는 G-037 및 G-039~G-045가 각 phase port에 추가한다. 이 계획은 현재 동작만 이동해 그 기능을 선행 구현하지 않는다.
- planning coordinator를 G-048에서 먼저 분리해 같은 `_run_project_step` 본문을 두 리팩터링 계획이 병렬로 수정하지 않게 한다.
