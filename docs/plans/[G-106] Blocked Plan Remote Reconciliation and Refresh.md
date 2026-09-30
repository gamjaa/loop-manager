---
id: G-106
title: Blocked Plan Remote Reconciliation and Refresh
sources:
  - docs/requirements/[R-046] Blocked Plan Remote Reconciliation and Checkpoint Refresh.md
  - docs/requirements/[R-048] Evidence-Free Managed Rebase Recovery.md
depends_on:
  - G-101
  - G-104
  - G-105
  - G-110
complexity: high
risk: critical
priority: critical
---

## Purpose

`PLAN_BLOCKED` 계획의 원격 작업 branch 또는 base가 바뀌면 안전한 사용자 commit과 최신 base를 관리 worktree에 반영하고, 원래 차단 원인을 현재 입력에서 다시 판단한다.

## Acceptance Criteria

- 계획별 복구는 같은 프로젝트의 변경 실행 자리가 비었거나 차단된 동일 계획이 그 자리를 점유한 때만 시작한다. 다른 활성 계획, PR 충돌 복구, planning publication 및 유지보수 작업을 선점하지 않고 관측한 입력을 다음 실행까지 보존한다.
- 작업 직전 원격 ref·PR·계획·branch·worktree·로컬 작업 범위를 재확인한다. 원격 작업 branch가 마지막 게시 checkpoint의 안전한 후속 commit이면 R-039의 보존 조건으로 먼저 채택한다. 삭제·재작성·분기, 기존 PR, 예상 밖 로컬 변경이나 fetch 경쟁은 사용자 commit과 로컬 작업물을 보존하고 정확한 이유로 중단한다.
- base가 전진했으면 계획·Requirement 내용 지문이 그대로여도 해당 관리 worktree에서 최신 base로 rebase한다. 작업 branch와 base가 모두 바뀐 경우 branch 변경을 먼저 채택한다. G-109의 공통 판정으로 현재 계획·repository·branch·worktree·target base ancestry·원격 기대값·index·작업 범위를 확인하며, 자동·수동 해결 모두 과거 Worker turn·stage 승인·receipt·replay·journal의 누락만으로 거부하지 않는다. 충돌은 기존 교정으로 넘기거나 미해결 상태를 보존하고 게시하지 않는다.
- PR 없는 기존 원격 작업 branch의 rebase 완료 결과는 G-104의 중간 checkpoint가 게시되거나 안전하게 거부된 뒤 원인을 재평가한다. 원래 차단 원인만 현재 계획·근거 문서·작업물에 대해 검사하고, 다른 사용자 결정·PR reopen·소유권·안전 차단과 다른 계획의 상태는 유지한다.
- 원인이 해결됐으면 기존 세션·branch·worktree와 실패 감사를 보존해 재개하고 변경된 후보 revision에 필요한 Worker 확인·targeted test·전체 verify·Review를 다시 수행한 뒤에만 PR을 허용한다. 원인이 남으면 `PLAN_BLOCKED`와 이유를 유지하면서 안전한 현재 작업물을 기존 checkpoint 경계로 게시한다. 원격에 같은 HEAD가 이미 있으면 빈 commit·반복 push를 만들지 않는다.
- 관측, fetch, rebase, 원인 재검증, checkpoint commit·push와 SQLite 반영 사이의 중단에서 현재 Git 상태·저장 identity·원격 기대값·마지막 처리 입력을 대조해 중복 작업이나 잘못된 해제를 막는다. 완료된 rebase의 과거 작업 증거를 재구성하지 않으며 기존 journal·실패 감사는 보존한다. 동일 입력·동일 원인의 반복은 rebase·검증·차단 알림을 반복하지 않는다.

## Implementation

- G-105의 계획별 관측 결과를 `loop_manager.py`의 차단 해제 선택과 연결하되, 현재 `_plan_block_selected_for_release`의 단일 mirror 가정에 의존하지 않고 대상 계획 ID를 명시한다. 원격 후속 commit 채택에는 기존 R-039 검사와 `checkpoint_ownership.py`를 재사용한다.
- 차단 rebase는 G-109의 현재 상태 수용·복구 경계를 사용하고 rebase 원인의 수동 해제는 G-110을 소비한다. `execution_checkpoint_state.py`의 현재 수용 결과와 `blocked_checkpoint.py`의 기존 branch checkpoint 게시, `scoped_blocking.py`의 원인별 해제·사건 기록을 연결한다. 이 계획에서 과거 rebase 증거 검사나 별도의 수동 복구 판정을 추가하지 않고 저장 단계별 멱등 전이를 둔다.
- 관련 R-046 장기 복구 규칙은 G-107과 함께 구현 PR에서 `docs/PRODUCT.md`에 반영한다.

## Validation

- Worker는 직접 변경한 `tests/test_loop_manager.py`, `tests/test_blocked_checkpoint.py`, `tests/test_execution_handlers.py`, `tests/test_scoped_blocking.py` 중 관련 targeted unit test만 실행한다.
- 실제 Git 이력으로 base만 전진, branch 후속 commit만 전진, 동시 변경, 원인 지속·해소, 동일 입력 반복, 자동·수동 충돌 해결·원격 경쟁·중단 재시작을 검증한다. 현재 상태가 정상인 증거 없는 rebase는 진행하고 현재 소유권·범위·원격 불일치는 거부한다. 사용자 결정·닫힌 PR·별도 안전 차단과 다른 계획의 차단은 유지돼야 한다. 실제 Git integration case는 Worker가 실행하지 않고 Manager 전체 verify에 맡긴다.
- 전체 verify는 재개 후 최종 후보의 Worker·verify·Review·PR gate와 기존 checkpoint lease·알림 중복 방지, 기본 worktree 보존을 확인한다.

## Progress

- [x] 실행 자리와 후속 commit 채택·관리 rebase 연결
- [x] 원인별 재검증·checkpoint 갱신 및 단계별 복구
- 2026-09-30: `blocked_plan_reconciliation.py`에 계획별 직렬 복구와 저장 단계 전이를 구현했다. 기존 후속 commit 채택, 현재 상태 rebase 판정, 중간 checkpoint 게시와 차단 원인 검사 경계를 재사용한다. 기존 session·실패 감사와 다른 계획의 상태를 보존하며 후보 검증 토큰을 무효화한다.
- 2026-09-30: Worker targeted unit test 최종 결과는 다음과 같다. 모든 Git·verify·Agent 외부 효과를 fake/mock으로 대체한 class만 실행했다.
  - `python3 -m unittest tests.test_scoped_blocking.BlockedPlanRemoteRefreshTests`: 26개 통과.
  - `python3 -m unittest tests.test_blocked_checkpoint.BlockedCheckpointRefreshUnitTests`: 7개 통과.
  - `python3 -m unittest tests.test_execution_handlers.EvidenceFreeBlockedScopeTests`: 5개 통과.
- 초기 fixture의 필수 SQLite 필드 누락과 마지막 진단 보호 변경의 빈 project 전이 값 오류를 수정했다. 실패한 명령과 동일한 명령을 재실행했으며 최종 실행에는 실패가 없다.
  - FAILED_COMMAND: python3 -m unittest tests.test_scoped_blocking.BlockedPlanRemoteRefreshTests
  - RERUN: python3 -m unittest tests.test_scoped_blocking.BlockedPlanRemoteRefreshTests
- 2026-09-30: 관리 rebase 결과 수용 뒤 현재 ExecPlan·R-046·R-048·PRODUCT·ARCHITECTURE를 다시 읽었다. 현재 HEAD와 `origin/main`은 `c16a98c1f4fe84291a9f28f28b8f4cea9600c6ba`이며, 작업 공간 변경과 신규 모듈을 포함한 base 대비 최종 diff 전체를 확인했다. 이전 후보의 성공을 재사용하지 않고 위 세 targeted unit test 명령을 현재 후보에서 새로 실행해 각각 26개·7개·5개가 실패 없이 통과했다. 전체 verify와 Review는 이후 Manager가 수행한다.
- 2026-09-30: Worker 결과 검증 오류의 원인은 `COMMIT_TITLE`과 `PR_SUMMARY` 사이의 빈 줄이었다. 기존 파서가 두 필드의 인접한 줄을 요구하므로 최종 출력에서 빈 줄을 제거했으며, 실제 `parse_worker_result`로 동일한 응답 초안이 통과하는 것을 확인했다. 현재 입력·base 대비 전체 diff를 다시 확인하고 위 세 targeted unit test 명령을 다시 실행해 26개·7개·5개가 모두 통과했다. 파서나 제품 동작을 추가 변경하지 않았다.
- 2026-09-30: Manager의 verify 실패 7개·error 1개에 대한 저장된 진단을 읽었다. 등록 origin이 아직 없는 legacy 프로젝트의 관측을 새 원격 복구가 가로채 기존 문서·Agent 범위 복구를 막고 원래 오류를 덮는 원인을 수정했다. 이 경우 기존 복구 경로로 넘기며, 등록 origin이 있는 프로젝트의 원격 조회 실패는 계속 차단한다. 격리된 Planner import fixture에 새 모듈을 포함하고, Linux 검증 스크립트의 기존 단일 compile 명령 안에 새 모듈을 넣어 전체 기존 검사 목록을 유지했다.
- 2026-09-30: 현재 HEAD `a0e207d720dc64a090953e326d2a16357bbca8f2`의 후보를 base `origin/main` (`c16a98c1f4fe84291a9f28f28b8f4cea9600c6ba`)와 비교해 최종 diff 전체와 현재 ExecPlan·R-046·R-048·PRODUCT·ARCHITECTURE를 다시 확인했다. 현재 후보에서 targeted unit test를 새로 실행해 아래 결과를 얻었다. origin 미등록 기존 복구 경로, 등록 origin 조회 실패 차단, 다른 계획 보존과 스크립트 wiring 회귀를 포함한다.
  - `python3 -m unittest tests.test_scoped_blocking.BlockedPlanRemoteRefreshTests`: 29개 통과.
  - `python3 -m unittest tests.test_blocked_checkpoint.BlockedCheckpointRefreshUnitTests`: 7개 통과.
  - `python3 -m unittest tests.test_execution_handlers.EvidenceFreeBlockedScopeTests`: 5개 통과.
  - `python3 -m unittest tests.test_catalog_checkpoint_wiring.CatalogCheckpointWiringTest`: 2개 통과. 스크립트 내용만 읽고 실행하지 않는다.
- 새 회귀 test fixture의 mock 적용 대상과 비교 기준선 초기화 오류를 수정하고 동일 명령을 재실행했다. 마지막 실행은 29개 모두 실패 없이 통과했다. Manager가 보고한 실제 Git 사례와 전체 verify는 Worker가 실행하지 않았으며 이후 Manager 검증을 기다린다.
  - FAILED_COMMAND: python3 -m unittest tests.test_scoped_blocking.BlockedPlanRemoteRefreshTests
  - RERUN: python3 -m unittest tests.test_scoped_blocking.BlockedPlanRemoteRefreshTests
- 2026-09-30: 재개 세션에서 최신 계획·R-046·R-048·PRODUCT·ARCHITECTURE와 HEAD `be95a79f4938394db06460ee05e3cf5be5b0bac6`의 `origin/main` 대비 전체 변경을 다시 확인했다. Review 지적 세 건을 수정했다. 안전한 원격 후속 commit의 선택 branch/worktree 비교 기준선을 execution checkpoint에 별도로 저장해 Agent 원본·결과 snapshot을 보존한다. base·후속 branch fetch와 원격 조회의 일시 실패는 입력을 소비하지 않고 재시도한다. 등록 origin·관측 row가 있는 기존 Review 횟수 차단도 기존 소유권 검사를 먼저 거쳐 교정을 재개한다.
- 2026-09-30: 현재 후보의 최소 unit test 최종 결과는 52개 통과다. `BlockedPlanRemoteRefreshTests` 36개, `BlockedCheckpointRefreshUnitTests` 7개, `EvidenceFreeBlockedScopeTests` 5개, `CatalogCheckpointWiringTest` 2개 및 `ScopedBlockingTest`의 `test_published_review_checkpoint_reconciles_before_legacy_count_release`, `test_legacy_review_count_block_releases_only_owned_non_rebase_workspace` 두 사례를 실행했다. 원격 채택 뒤 실제 Agent snapshot 보존·guidance 재시작·후속 범위 위반 거부와 동일 입력 fetch 실패 후 성공을 포함한다. 새 fixture의 schema 초기화와 index 형식·fake 검사 포트를 수정한 뒤 동일 명령의 마지막 실행은 36개 모두 통과했다.
  - FAILED_COMMAND: python3 -m unittest tests.test_scoped_blocking.BlockedPlanRemoteRefreshTests
  - RERUN: python3 -m unittest tests.test_scoped_blocking.BlockedPlanRemoteRefreshTests
- 전체 verify·실제 Git 사례·Review는 Manager에 맡기며 Worker는 실행하지 않았다. `tests/test_loop_manager.py`의 기존 격리 import fixture는 실제 Git 저장소를 생성하는 사례이므로 읽기만 했으며 Worker 실행 대상에서 제외했다. commit·push·PR 생성, AGENTS·PLANS·Requirement 변경은 하지 않았다.
- 2026-09-30: Worker 결과 검증 오류를 재현한 파서 조건을 확인했다. 직전 응답은 `FAILED_COMMAND`와 `RERUN` 사이에 실패 결과가 없어 미해결 이력으로 판정됐다. 최종 보고를 실패 명령·실패 결과·동일 재실행 명령·최종 PASS 순서로 구성하고 기존 `parse_worker_result`가 수용함을 확인했다. 파서와 제품 코드는 추가 변경하지 않았다. 최신 근거 문서와 현재 HEAD/base 대비 diff를 재확인하고 위 최소 unit test 5개 명령을 새로 실행해 36·7·5·2·2개, 총 52개가 모두 통과했다. 전체 verify·실제 Git 검증·Review는 이후 Manager가 수행한다.
- 2026-09-30: Manager verify의 legacy Agent 범위 검사 오류를 수정했다. 원격 채택 기록이 없는 프로젝트 전용 SQLite에서도 `exec_plans`를 무조건 조회한 것이 원인이었다. 채택 기록과 catalog 존재 여부를 확인한 경우에만 추가 계획 조회를 수행한다. 실제 Git 없이 동일 SQLite/snapshot 조건을 재현한 `LegacyBlockedScopeUnitTests`에서 정상 snapshot 허용과 Agent의 다른 소유 ref 변경 거부를 확인했다. 최초 fixture에 빠진 worktree 등록을 보완하고 동일 명령을 재실행해 1개 통과했다.
  - FAILED_COMMAND: python3 -m unittest tests.test_scoped_blocking.LegacyBlockedScopeUnitTests
  - FAILED: 새 fixture의 관리 worktree 등록 정보 누락을 수정했다.
  - RERUN: python3 -m unittest tests.test_scoped_blocking.LegacyBlockedScopeUnitTests
  - PASS: 최종 재실행 1개 통과.
- 최신 계획과 R-046·R-048·PRODUCT·ARCHITECTURE를 다시 확인하고 HEAD `e2340102201b83201b8cb00bdfaf196b6941dada` 및 `origin/main` 대비 변경을 검토했다. 현재 후보에서 `BlockedPlanRemoteRefreshTests` 36개, `EvidenceFreeBlockedScopeTests` 5개, `BlockedCheckpointRefreshUnitTests` 7개, `CatalogCheckpointWiringTest` 2개를 새로 실행해 모두 통과했다. 새 legacy 사례를 포함한 최종 unit 결과는 51개 통과다. Manager가 보고한 `test_blocked_resume_does_not_excuse_agent_owned_ref_change`는 실제 Git 저장소·commit을 사용하는 integration 사례이므로 Worker는 실행하지 않았다. 전체 verify와 해당 사례 재검증·Review는 Manager에 맡긴다.
- 2026-09-30: Review의 guidance 재개·원격 채택 중단 경계 두 건을 수정했다. guidance 재개는 저장된 rebase 수용이 있으면 공통 현재 상태 서비스에서 완료·소유권을 다시 확인한 뒤 `accepted_rebase` 범위 검사를 사용한다. 실제 pending/approve 결정과 원본 Agent snapshot을 사용하는 unit 회귀를 추가했다. fast-forward 전에 정확한 요청·후속 OID·branch/worktree·Agent 원본/결과·비교 snapshot을 실행 checkpoint intent에 저장한다. Git 효과 직후 완료 SQLite 기록 전 중단에서도 해당 clean HEAD·index·등록 상태를 대조해 기존 R-039 채택 검사를 재개하며, 후속 shared ref·staged 변경은 거부한다.
- 최신 근거 입력과 HEAD `73cd54d0cf6a584d071e0e92c518c91c50b8df62`의 base 대비 후보 변경을 다시 확인했다. 현재 후보에서 `python3 -m unittest tests.test_scoped_blocking.BlockedPlanRemoteRefreshTests` 39개, `python3 -m unittest tests.test_execution_handlers.EvidenceFreeBlockedScopeTests` 6개, `python3 -m unittest tests.test_blocked_checkpoint.BlockedCheckpointRefreshUnitTests` 7개, `python3 -m unittest tests.test_scoped_blocking.LegacyBlockedScopeUnitTests` 1개, `python3 -m unittest tests.test_catalog_checkpoint_wiring.CatalogCheckpointWiringTest` 2개가 모두 통과했다. 이번 실행에 targeted 실패는 없었다. 실제 guidance 사용자 결정 후속 commit·base rebase·중간 checkpoint·승인 재개를 함께 거치는 Git 회귀를 추가했으며 Worker는 실행하지 않았다. 전체 verify·실제 Git 회귀·Review는 Manager가 수행한다.
- 2026-09-30: Manager가 보고한 guidance 후속 결정·base rebase 사례의 tracking ref 오탐을 수정했다. Agent 실행 이후의 base tracking 변경은 현재 관측과 새 원격 조회의 정확한 OID가 일치할 때만 허용한다. 선택 branch tracking은 원격 후속 commit과 별도로 기존 게시 checkpoint의 소유권·identity로 확인하며 실제 Agent 결과에 기록된 ref 변경은 허용하지 않는다. 후속 commit 채택과 게시 lease 검사는 그대로 유지한다. 최신 근거 입력과 HEAD `5bb3fe7661e338faaa215e2fac214ab94b19d2d2`의 `origin/main` 대비 전체 diff를 확인했다. 현재 후보에서 `python3 -m unittest tests.test_scoped_blocking.BlockedPlanRemoteRefreshTests` 43개, `python3 -m unittest tests.test_execution_handlers.EvidenceFreeBlockedScopeTests` 6개, `python3 -m unittest tests.test_blocked_checkpoint.BlockedCheckpointRefreshUnitTests` 7개, `python3 -m unittest tests.test_scoped_blocking.LegacyBlockedScopeUnitTests` 1개, `python3 -m unittest tests.test_catalog_checkpoint_wiring.CatalogCheckpointWiringTest` 2개가 모두 통과했다. 이번 실행의 targeted 실패는 없다. 실제 Git 회귀와 전체 verify·Review는 Manager가 수행한다.
- [ ] Manager 전체 verify와 실제 Git 회귀 사례 검증. base만 변경·후속 commit만 변경·동시 변경·원인 지속/해소·Git 효과 뒤 중단 재시작·수동 충돌 해결 사례를 작성했으며 Worker는 실행하지 않았다. Linux 전체 검증 스크립트에 새 모듈의 compile 검사를 추가했으나 스크립트·전체 suite·integration·lint·build·type check는 실행하지 않았다.
- [ ] Reviewer 검토 및 사용자 Merge. Worker는 commit·push·PR을 생성하지 않았다.

## Decision Log

- G-101은 R-045의 기존 오탐 차단만 복구한다. 이 계획은 새 원격 입력으로 다시 평가해야 하는 R-046의 계획별 복구를 맡는다.
- 2026-09-30: 제공된 mutable READY snapshot의 기준 revision·원본 hash를 확인해 R-048을 반영했다. G-110 뒤에 새 원격 입력 반영을 구현하고 G-104의 중간 게시 순서를 유지한다. rebase 완료는 R-045 작업 증거 대신 G-109의 현재 상태로 판정한다.
- 2026-09-30: 복구 단계와 원래 차단 원인·검증 artifact는 별도의 additive SQLite 기록에 저장한다. rebase 수용으로 기존 검증 결과가 무효화돼도 저장된 원인을 검사할 수 있으며, 완료된 Git 효과나 완료된 원인 검사를 재시작에서 반복하지 않는다. 후속 checkpoint identity를 갱신할 때 이전 게시 identity는 감사 이력으로 보존한다.
- 2026-09-30: 대상 계획 ID를 명시해 mirror에 의존하지 않고 복구한다. 문서 입력 해제는 원래 revision과 차단 당시 worktree 기준선을 비교해 기존 로컬 제안을 새 사용자 결정으로 오인하지 않는다. 복구 도중 실행 자리 소유자가 바뀌면 다른 계획의 투영·진단을 보존한다. 프로젝트 차단 복구·진단 branch는 G-107 범위로 남기며 이 계획에는 추가하지 않았다.
- 2026-09-30: 등록 origin이 없는 legacy 관측은 유효한 원격 갱신 입력으로 간주하지 않는다. 기존 문서·Agent 범위 복구 경계를 유지하고 원래 차단·진단과 미처리 관측을 보존한다. 등록 origin이 있는 프로젝트에는 원격 identity·조회 오류 검사와 현재 원격 복구 gate를 그대로 적용한다.

- 2026-09-30: 원격 채택 비교 기준선은 R-039가 확인한 선택 branch·worktree 상태와 정확한 후속 OID에만 결합한다. 원래 Agent snapshot과 별도로 저장하며 이후 로컬 HEAD·공유 ref·config 변경을 자동으로 흡수하지 않는다. 일시적 외부 검사 실패는 영구 거부 입력으로 기록하지 않는다. 기존 Review 횟수 해제는 원격 복구의 완료 여부가 아니라 기존 소유권·PR·rebase·별도 차단 검사에 의해 결정한다.

- 2026-09-30: 미완료 fast-forward의 비교 intent는 Git 효과 전에 저장하고 완료 채택 기준선과 구분한다. 현재 HEAD가 정확한 의도 결과이고 index·작업물이 clean인 경우에만 선택 상태를 비교에서 수용하며 다른 공유 metadata와 원본 Agent 감사는 보존한다. guidance 승인 검사는 rebase 현재 상태 수용과 별도로 계속 수행한다.
