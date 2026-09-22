---
id: G-047
title: ExecPlan Runtime State Projection Boundary
sources:
  - docs/PRODUCT.md
depends_on: []
complexity: high
risk: high
priority: normal
---

## Purpose

현재 활성 ExecPlan 실행 상태는 `exec_plans`와 호환용 `projects` row에 함께 저장되며, `update_project`, `_mirror_project_update_to_execplan`, hydrate·clear helper와 여러 직접 SQL이 phase 이름 변환 및 두 projection의 원자성을 나누어 책임진다. Worker·검증·게시·차단 경로가 이 암묵적 mirror 규칙을 각자 알아야 하므로 새 durable field나 중단 복구 전이를 추가할 때 한쪽만 갱신하거나 서로 다른 commit 경계를 사용하는 구조적 위험이 있다.

기존 SQLite schema와 저장값을 그대로 사용하는 typed runtime-state repository 경계를 도입해 plan 실행 row, project compatibility projection과 notification outbox 갱신을 한 곳에서 원자적으로 다룬다. 이 계획은 현재 상태 의미와 사용자 동작을 바꾸지 않으며 후속 planning·execution coordinator가 raw row와 mirror 규칙 대신 하나의 저장 책임에 의존할 기반만 만든다.

## Acceptance Criteria

- 활성 ExecPlan의 phase, branch, worktree, session, Worker·verify·Review 결과, publication marker와 Agent scope marker를 읽고 갱신하는 책임이 별도 runtime-state repository 모듈에 모인다.
- `WORKING`과 `IMPLEMENTING`의 기존 compatibility mapping, `VERIFYING`, `REVIEWING`, `PUBLISHING`, `PR_OPEN`, `PLAN_BLOCKED` 및 clear/hydrate 의미를 그대로 보존한다.
- 하나의 lifecycle 전이는 `exec_plans` source row와 `projects` compatibility projection을 같은 SQLite transaction에서 갱신하며, 중간 예외를 주입해도 두 row 중 하나만 commit되지 않는다.
- plan activation, active-state hydrate, 일반 field update, validation retry, final block, PR open과 active projection clear의 기존 호출 경로가 직접적인 이중 table 갱신 대신 repository operation을 사용한다.
- planning-only project field와 등록·migration·catalog metadata는 실행 projection과 명시적으로 분리하고, repository가 Git, Agent CLI, verify 또는 GitHub 명령을 실행하지 않는다.
- notification outbox row는 현재와 같은 상태 transaction 안에서 생성되고 실제 전송은 계속 transaction 밖의 기존 dispatcher가 담당한다.
- 기존 helper를 외부 또는 테스트가 호출하는 경우 얇은 compatibility wrapper로 유지해 공개 Python 호출 계약과 예외 의미를 보존한다.
- SQLite table·column·index, 저장된 phase/status 문자열, NULL/default 표현과 기존 DB migration 결과를 추가·삭제·변경하지 않는다.
- CLI 출력, 작업 선택, retry·BLOCKED 판정, worktree·branch·PR 이름, commit·push 순서와 사용자 동작을 변경하지 않는다.

## Implementation

- 새 runtime-state 모듈에 immutable execution snapshot, 저장 phase/status 변환과 repository operation을 정의하고 기존 `sqlite3.Row` 접근을 경계 안에서 typed 값으로 정규화한다.
- 현재 `FIELDS` 중 plan runtime과 project-only planning field를 분류하고, mirror 대상 목록과 phase 변환을 한 선언에서 사용하게 한다.
- `activate_ready_execplan`, `hydrate_execplan_project`, `_mirror_project_update_to_execplan`, `_clear_active_in_current_transaction`, validation failure와 block helper의 persistence 부분을 repository transaction으로 이관한다.
- block scope·재시도·복구 가능성 같은 도메인 판단은 현재 호출자에 보존하고 repository에는 이미 결정된 전이와 expected plan/status 조건만 전달한다.
- 기존 schema bootstrap과 legacy migration은 유지하되 migration 뒤 snapshot을 repository로 읽고 동일한 compatibility projection을 재현하는 회귀 경계를 둔다.

## Validation

- Worker는 새 runtime-state repository test module과 이관한 compatibility wrapper의 직접 관련 테스트만 targeted unit test로 실행한다.
- 각 phase/status mapping, activation·hydrate·clear, session/result/publication field round trip과 project-only field 비간섭을 검증한다.
- 두 table 갱신 사이, outbox enqueue 전후 및 expected-status 불일치에 실패를 주입해 전체 transaction rollback과 재시도 멱등성을 검증한다.
- 기존 DB fixture를 migration한 뒤 table·column과 저장값이 바뀌지 않고 활성 작업이 같은 phase, session, workspace와 PR 정보로 재개되는지 검증한다.
- 전체 verify에서 catalog 선택, scoped blocking, guidance approval, validation retry, notification 중복 방지, PR conflict recovery와 maintenance cycle 회귀가 없어야 한다.

## Progress

- [ ] runtime snapshot·phase mapping과 repository 모듈 추가
- [ ] lifecycle dual-write·hydrate·clear 경로 이관
- [ ] compatibility wrapper와 project-only field 분리
- [ ] transaction rollback·migration·resume 회귀 테스트 추가

## Decision Log

- 이 계획은 `exec_plans`와 `projects` 중 하나를 삭제하거나 schema를 재설계하지 않는다. 기존 저장 형식을 보존한 채 이중 projection을 유지하는 단일 책임만 만든다.
- recovery disposition과 새 durable field의 제품 의미는 후속 기능 계획이 정의한다. 이 계획은 현재 의미를 typed persistence API로 표현할 뿐 새 상태를 도입하지 않는다.
