---
id: G-129
title: Separate Planning Rebase Conflict Correction
sources:
  - docs/PRODUCT.md
depends_on:
  - G-128
complexity: medium
risk: high
priority: normal
---

## Purpose

source revision `f9c10ed921ed8fe39757a36c3ca27b77a0539ee8`의 `planning/workspace_service.py`는 workspace 생성·소유권, 원격 base 반영·catalog 권한 재구성과 함께 rebase 충돌 prompt 작성, Planner session/scope, 파일·ID 충돌 해소 검증, 교정 기록 저장과 stage/continue 재개를 담당한다. `_prepare_workspace`가 `_resolve_rebase_conflict`를 호출하면 workspace 준비 도중 별도 Agent turn과 Manager Git 쓰기가 시작된다. 교정 검증 변경과 workspace/게시 복구 변경이 같은 service에 모이며, 교정만 확인하는 실제 Git 테스트도 private workspace method를 통해야 한다.

G-118 이후 추가된 이 책임 혼합을 별도 planning 충돌 교정 경계로 분리한다. G-128의 scoped-turn 절차와 기존 `planning_rebase.py`의 durable journal/교정 identity 검증을 그대로 사용한다. 새 Git 저장 시스템이나 실행 상태는 만들지 않는다. compact catalog의 비종료 네 계획은 이 경계를 분리하지 않으며 먼저 실행해야 할 READY 계획은 없다.

## Acceptance Criteria

- workspace service는 소유권·원격 관측·base 채택·catalog 권한과 기존 publication intent 재검증을 소유한다. 충돌 교정 경계는 제한된 입력, 기존 Agent/Git/event port 및 session 저장 callback만 받아 prompt·Agent turn·파일 검증·교정 재개를 담당한다. coordinator 전체, root module 또는 가변 `_PlanningRun`을 넘기지 않는다.
- 사용자 동작, 공개 계약과 저장 형식을 보존한다. 현재 Planner profile·역할·session 및 작업물 재사용, outcome과 진단, 공개 helper 서명, CLI·prompt/result protocol, SQLite schema·transaction, planning rebase JSON의 nonce·phase·target·head·correction/digest와 stash identity는 그대로다.
- 저장된 검증 교정은 Agent를 다시 호출하기 전에 읽고 현재 HEAD·파일·index digest가 같은 경우에만 재개한다. 새 충돌 또는 drift를 이전 교정으로 승인하지 않는다. stage 허용 경로와 `--` pathspec, 현재 rebase 판별, `continue`와 stash conflict 수용의 구분을 보존한다.
- symlink·잘못된 파일 형식·남은 conflict marker와 역할 밖 충돌은 거부한다. untracked plan ID 충돌에서는 원격 파일을 보존하고 별도 ID/파일의 로컬 결과를 요구한다. 기존 일반 planning의 FUTURE_IDEAS 병합 조건을 유지하되 barrier 등 현재 mode의 변경 권한을 넓히지 않는다.
- 교정 저장 → stage/continue 또는 stash 수용 → 재관측 → SQLite base 반영 → stash/journal 정리 순서를 보존한다. 실패·중단에서 stash·작업물·session·scope·원래 planning 오류를 보존하고 성공 지문·catalog 적용·차단 해제를 조기 확정하지 않는다.
- G-128의 scope helper를 사용하되 충돌 교정의 기존 오류 처리 결과와 재시도 정책은 그대로다. 충돌 없는 준비에서는 교정 Agent를 호출하지 않는다. 실제 구현은 하나이며 확인된 호출/테스트 소비에 필요한 얇은 위임만 허용한다.

## Implementation

1. `_resolve_rebase_conflict` 19행과 `_finish_rebase_correction` 136행, `_prepare_workspace`의 호출/완료 순서를 실제 소비 테스트와 대조한다. G-128가 Merge된 최신 원격에서 공통 turn 경계를 확인한다.
2. `loop_manager_internal/planning/` 안에 충돌 교정 책임을 둔다. 작은 `conflict_correction.py`가 필요한 경우 이 package 안에만 추가하고 두 verify의 기존 compile 목록에 포함한다. 새 루트 항목이나 추가 설정은 없다. 별도 repository·general-purpose workflow/port 계층은 만들지 않는다.
3. prompt와 역할·session 입력, unmerged/collision path 확인, 교정 검증 및 Manager stage/continue orchestration을 이동한다. journal의 읽기·저장·digest·stash 복원은 `planning_rebase.py` 및 기존 Git port에 계속 위임한다. 소유권 검증과 base 채택·catalog/intent transaction은 workspace service에 남긴다.
4. 현재 private method를 직접 쓰는 테스트를 새 경계 또는 실제 위임에 연결하고 fake Git/Agent port로 교정만 독립 검증한다. 실제 Git 중단·복원 시나리오의 assertion과 단일 수집은 보존한다.

### 원본 코드맵

| 파일 / 원본 SHA256 | 책임·진입점 및 직접 관계 |
| --- | --- |
| `planning/workspace_service.py` / `40165df8040b2f8f8b72b915ede3a8a93ae8420380ee83bb51a88c3fa6e7076a` | coordinator가 `_prepare_workspace`를 소비한다. 그 안에서 충돌 때 `_resolve_rebase_conflict`가 Agent와 파일 검증을 실행하고 `_finish_rebase_correction`이 stage/continue를 소유한다. `tests/test_planning_recovery.py` 및 `tests/test_planning_rebase.py`가 직접 검증한다. |
| `planning/planning_rebase.py` / `9e12007ec671513d2fd14d0c62f00c4f0015e16b15eacbf2d87610efabfcddc6` | `planning_correction/save_planning_correction/accept_resolved_stash_conflict/complete_planning_rebase`와 atomic journal·stash 복원을 담당한다. `planning/git_ports.py`와 workspace service가 소비하며 저장 책임은 이동하지 않는다. |
| `tests/test_planning_recovery.py` / `737afdc71a52cf5a802aee9b6568b79126479e591c9dd7d0eb2e25e97ea8cfe4` | fake ports로 session 보존, scope 거부, plan 충돌과 untracked ID 충돌을 검증한다. 교정 입력/출력·효과 경계의 독립 unit test를 여기에 추가할 수 있다. |
| `tests/test_planning_rebase.py` / `fcd5ea8f75c6c592bad42514332cc5fe667b7f490f22a2db784b455b08dd7ab9` | 실제 Git으로 stage/reset 뒤 중단·재개, 교정 후 drift, stash 충돌·ID collision과 staged/unstaged/untracked 보존을 검증한다. Worker targeted 실행 대상과 Manager 전체 verify 대상을 구분한다. |

표의 `planning/` 경로는 `loop_manager_internal/planning/` 아래다. 루트 항목의 실제 참조·소유권 및 유지 목적지는 G-128의 루트 배치 확인을 따른다. 감사·사용자 파일이나 기존 계획을 삭제·rename하지 않는다.

## Validation

- Worker는 `tests.test_planning_recovery.PlanningConflictCorrectionUnitTests`에 추가할 targeted unit test와 기존 `PlanningRecoveryTest.test_planner_resolves_owned_plan_file_rebase_conflict_in_saved_session`, `test_untracked_plan_id_collision_uses_saved_planner_session`, `test_saved_scope_violation_blocks_rebase_without_discarding_result` 중 변경한 경계의 최소 case만 실행한다. fake Git/Agent port를 사용한다.
- 저장된 교정의 재개에서 Agent 중복 호출 없음, HEAD/digest drift 거부, scope·session callback 오류 후 Git 쓰기 없음, 허용 path만 stage, rebase continue 실패의 보존과 stash 수용 분기를 검증한다. symlink·marker·역할 밖 파일·불완전 ID 분리도 거부해야 한다.
- workspace wiring unit test는 base/cycle/catalog/intent의 기존 순서, 원래 오류 지문 보존과 SQLite base 확정 뒤 stash 정리 호출을 확인한다. 일반/maintenance 역할과 충돌 없는 준비의 무호출을 확인한다.
- Manager 전체 verify는 `tests/test_planning_rebase.py`의 실제 Git 회귀 전체를 포함한다. stage·apply·soft-reset·base 저장·stash drop 사이 중단에서 작업물과 index를 보존하고 같은 Planner 교정을 반복하지 않아야 한다. 일반·barrier·revalidation planning, 원격 재작성·source drift, 성공 기준/lease 및 최종 Worker verify/Review gate를 보존한다.
- Reviewer는 교정과 workspace의 책임 분리, 소유권 검증 누락 부재, journal/transaction 보존과 테스트·verify 범위가 약화되지 않았는지 읽기 전용 검토한다. Worker는 실제 Git integration test와 전체 verify를 실행하지 않는다.

### 구현 대응표와 검증 기록

| 경계 | 구현 및 보존한 책임 |
| --- | --- |
| `planning/conflict_correction.py` | `PlanningConflictCorrection.resolve`가 repo·worktree·target·detail·role·session·저장 callback과 기존 Git/Agent/event port만 받는다. prompt, unmerged/collision 검증, 교정 저장과 stage/continue·stash 수용을 담당한다. state port·coordinator·가변 run을 받지 않는다. |
| `planning/workspace_service.py` | 기존 실제 Git 테스트가 소비하는 `_resolve_rebase_conflict`만 얇게 위임한다. 소유권·fetch·재관측·cycle/intent·오류 지문 보존·SQLite base 채택·catalog 권한 재구성 및 base 저장 뒤 journal 정리 순서는 유지한다. 소비가 없는 `_finish_rebase_correction`은 별도 교정 경계의 `finish`로 이동했다. |
| `planning/agent_ports.py`, `planning/planning_rebase.py` | 기존 scoped-turn helper와 journal identity/digest 검증·저장을 그대로 재사용한다. prompt·오류 우선순위·session callback 전파·JSON 및 stash 형식을 변경하지 않는다. |
| `tests/test_planning_recovery.py` | 독립 교정 unit class 14 cases와 기존 workspace 연결 3 cases를 fake ports로 검증했다. 실제 Git 회귀는 기존 얇은 위임에서 같은 구현을 소비하며 assertion·수집을 변경하지 않았다. |
| `scripts/verify.sh`, `scripts/verify.ps1` | 기존 compile 목록에 내부 교정 모듈만 추가했다. unittest discover와 전체 검증 범위는 유지한다. |

- targeted 명령: `python3 -m unittest tests.test_planning_recovery.PlanningConflictCorrectionUnitTests tests.test_planning_recovery.PlanningRecoveryTest.test_planner_resolves_owned_plan_file_rebase_conflict_in_saved_session tests.test_planning_recovery.PlanningRecoveryTest.test_untracked_plan_id_collision_uses_saved_planner_session tests.test_planning_recovery.PlanningRecoveryTest.test_saved_scope_violation_blocks_rebase_without_discarding_result`
- 첫 실행에서 유지보수 wiring fixture가 일반 입력 지문을 사용해 실패했다. 기존 `maintenance_planning_fingerprint`로 fixture를 수정한 뒤 같은 명령으로 17 tests가 모두 통과했다.
- 저장된 실제 journal을 fake Git port와 연결해 같은 교정의 Agent 무호출, HEAD/file/index/target drift 거부와 새 rebase 충돌의 별도 turn을 확인했다. scope·session 저장 실패 후 Git 쓰기 없음, 잘못된 파일·marker·역할 밖 경로 거부, ID 충돌 원격 보존 및 별도 로컬 파일 요구, FUTURE_IDEAS 병합과 maintenance scope 거부, continue 실패와 stash 수용을 검증했다.
- workspace wiring은 일반/유지보수 역할, 오류·session·publication intent 보존과 교정 → 재관측 → cycle/intent → 진단 보존 → base 저장 → 정리 순서를 확인했다. 충돌 없는 준비는 교정 경계를 생성하지 않는다.
- Worker는 실제 Git integration, 전체 suite·lint·build·type check와 전체 verify를 실행하지 않았다. Manager 전체 verify·Review·Merge는 별도 검증이다. 루트 이동·추가 없이 G-128의 루트 배치 확인을 유지하며 PRODUCT·Requirement·ARCHITECTURE·AGENTS·PLANS는 수정하지 않았다.

## Progress

- [x] 지정 revision의 교정·workspace·journal 및 관련 테스트 경계 확인
- [x] G-128 공통 scope helper를 사용한 충돌 교정 경계 분리·연결
- [x] 관련 targeted unit test
- [ ] Manager 전체 verify, 독립 Review 및 사용자 Merge

## Decision Log

- 2026-10-03: 이 계획은 G-128 후 독립 검증·Merge하며 새 DAG의 유일한 말단이다. barrier 대상 G-066/G-112/G-113/G-114에는 기존 dependency 뒤에 G-129만 추가한다.
- 원격 변경 정책, 사용자 수동 복구 허용 범위, source checkout, journal·SQLite 형식과 역할 권한은 변경하지 않는다. 불확실한 동작을 통일하거나 문서의 정책 모순을 이 계획에서 선택하지 않는다.
