---
id: G-048
title: Planning Lifecycle Coordinator Extraction
sources:
  - docs/PRODUCT.md
depends_on:
  - G-047
complexity: high
risk: high
priority: normal
---

## Purpose

현재 `_run_project_step`의 planning 구간은 일반·유지보수 scheduling, 입력 fingerprint, planning worktree 소유권, Planner session 호출, Agent scope 검사, 결과 validation, commit·fast-forward push, SQLite catalog 등록과 중단 복구를 한 제어 흐름에서 직접 수행한다. 순수 validation과 maintenance state 모듈이 이미 있어도 상위 흐름이 raw SQLite, Git과 Agent 전역 함수에 동시에 결합되어 base race, validation retry와 publication interruption을 독립적으로 검증하기 어렵다.

planning lifecycle을 명시적인 coordinator 모듈과 주입 가능한 저장·Git·Agent port 경계로 추출한다. 일반 planning과 maintenance barrier/revalidation의 현재 분기 및 결과를 보존하면서 `_run_project_step`은 준비된 project context를 전달하고 typed outcome을 해석하는 상위 dispatch만 담당하게 한다.

## Acceptance Criteria

- 일반 planning과 maintenance assessment·barrier·post-refactor revalidation의 시작, 재개, validation 및 publication이 별도 planning coordinator 책임으로 이동한다.
- coordinator는 fingerprint skip, planning error 보존·해제, deterministic worktree 생성·소유권, Planner session 재개, scope validation retry, no-change 완료, commit·push 및 push 후 catalog/cycle 적용 순서를 현재와 동일하게 유지한다.
- prerequisite, no-change, refactoring, unsafe revalidation, validation retry, remote-base restart, transient pause와 publication recovery를 문자열이나 정수 분기 대신 typed outcome으로 반환하고 최상위 adapter가 기존 process result와 로그 의미로 변환한다.
- 일반 Planner의 허용 파일, maintenance mode별 eligible snapshot과 revision 보존, combined planning 제한 및 `docs/FUTURE_IDEAS.md` 분리 계약을 변경하지 않는다.
- Codex가 Agent turn 추적을 위해 생성·갱신·정리하는 `refs/codex/turn-diffs/*`는 게시 대상이나 workspace 소유권을 변경하지 않는 provider 소유의 비권위 임시 ref로 다룬다. 현재 turn과 중단 전에 저장된 Planner·Worker·Reviewer scope snapshot 비교에서 이 namespace의 순변경만으로 `PROJECT_BLOCKED`를 만들지 않는다. 소유 branch, remote ref, tag와 그 밖의 공유 ref 변경 보호는 그대로 유지한다.
- coordinator의 상태 접근은 G-047 repository와 maintenance repository를 사용하고, Git·Agent·clock/log 동작은 명시적인 port를 통해 호출해 fake 구현으로 독립 unit test가 가능하다.
- coordinator 모듈은 `loop_manager.py`를 import하지 않으며 runtime bootstrap, Worker lifecycle, GitHub PR과 notification 전송 책임을 가져오지 않는다.
- 기존 prompt 내용, Agent provider·model·effort 선택, session ID, retry 한도, planning commit message, fast-forward-only push와 원격 경쟁 처리 결과를 보존한다.
- planning worktree 경로, SQLite schema·저장 형식, ExecPlan 파일 형식, 공개 CLI와 사용자 동작을 변경하지 않는다.

## Implementation

- planning 입력·현재 cycle·workspace identity를 담는 context와 next action을 표현하는 outcome type을 새 planning runtime 모듈에 정의한다.
- fingerprint/selection, workspace preparation, Agent turn, validation, publication을 작은 phase handler로 나누고 각 handler가 durable 전이 후 필요한 외부 port만 호출하게 한다.
- 기존 `maintenance_planning`, `maintenance_orchestration`, `maintenance_state`의 pure validator와 repository는 재사용하고 같은 규칙을 coordinator 안에 중복 구현하지 않는다.
- Agent scope ref 비교 경계에서 비권위 `refs/codex/turn-diffs/*`를 비교 양쪽에서 제외해, 이전 버전이 저장한 snapshot과 현재 snapshot 사이의 생성·삭제·갱신도 같은 의미로 정규화한다. 정확한 namespace 밖 ref는 기존 구조화된 차단 원인으로 계속 분류한다.
- `_run_project_step`의 project safety 및 PR synchronization 이후 planning 관련 분기를 coordinator 호출로 교체하고 현재 helper entry point가 필요하면 adapter로 유지한다.
- planning publication의 commit-before-push, remote 확인-before-catalog 적용과 stale-base restart 순서를 characterization test로 고정한 뒤 코드를 이동한다.

## Validation

- Worker는 새 planning coordinator test module과 변경된 top-level dispatch adapter의 직접 관련 테스트만 targeted unit test로 실행한다.
- 일반 no-change/새 계획/FUTURE_IDEAS-only, Planner validation 3회 한도, session 재개, scope 위반과 planning error fingerprint 변화를 fake port로 검증한다.
- Planner·Worker·Reviewer의 현재 실행과 저장된 scope 재개에서 `refs/codex/turn-diffs/*`의 생성·갱신·삭제는 통과하고, 동시에 branch·remote ref·tag 또는 다른 공유 ref가 바뀌면 기존 project-scope 원인으로 차단하는지 검증한다.
- maintenance prerequisite/refactoring/no-change/unsafe 결과, barrier dependency publication, prepared intent 중단 복구와 post-refactor revalidation을 독립 시나리오로 검증한다.
- commit 전·push 전 base race, push 실패, push 후 SQLite 적용 전 중단과 이미 게시된 batch 재관측에서 기존 멱등 결과를 검증한다.
- 전체 verify에서 기존 Planner prompt·scope, maintenance planning/state/orchestration, origin isolation, catalog registration과 project completion 회귀가 없어야 한다.

## Progress

- [ ] planning context·port·outcome 계약 추가
- [ ] 일반 및 maintenance planning phase handler 추출
- [ ] top-level dispatch와 compatibility adapter 연결
- [x] Codex turn-diff ref 정규화와 공유 ref 보호 회귀 테스트 추가
- [ ] base race·validation·publication 중단 회귀 테스트 추가

## Decision Log

- planning validation 규칙과 maintenance state machine은 이미 분리돼 있으므로 다시 작성하지 않고, 이 계획은 그 경계를 호출하는 orchestration 책임만 이동한다.
- runtime-state 저장 경계를 먼저 G-047에서 고정해 coordinator 추출 중 기존 dual-write 규칙을 복제하지 않는다.
- 2026-09-22 사용자 차단 해제 결정: Codex Agent 실행 중 provider가 자동 생성한 `refs/codex/turn-diffs/captures/*`를 Worker의 공유 Git ref 변경으로 잘못 귀속해 `turn_diff_ref_mutation` 프로젝트 차단이 발생했다. `refs/codex/turn-diffs/*`는 Agent 결과와 게시 의미가 없는 provider 임시 ref로 분류해 scope 비교에서 제외하되, 다른 공유 Git 상태 보호는 완화하지 않는다.
