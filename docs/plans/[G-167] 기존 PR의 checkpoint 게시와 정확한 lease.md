---
id: G-167
title: 기존 PR의 checkpoint 게시와 정확한 lease
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-060] Collaborative Git Recovery and Remote Blocked Snapshots.md
depends_on:
  - G-166
complexity: high
risk: critical
priority: critical
---

## Purpose

PR이 존재한다는 이유로 checkpoint를 거부하는 현재 경로를 실제 PR identity와 정확한 원격 lease를 검증하는 게시 경계로 바꾼다.

## Acceptance Criteria

- 기존 PR 작업에도 검증된 checkpoint를 같은 작업 branch에 게시할 수 있다. 검증된 동일 branch rebase의 갱신은 실제 원격 head의 정확한 lease와 저장/조회한 PR identity에 결합한다.
- 동시 사용자 push·PR Close/Merge·게시 경쟁·origin/branch 변경에서 예상 밖 commit을 덮지 않는다. 충돌 없는 일반 push와 필요한 lease 갱신을 구분하고 새 PR·Draft PR을 만들지 않는다.
- 후보 변경은 오래된 verify·Review 승인으로 게시하지 않는다. 게시 intent·실제 원격 결과·SQLite 확정의 중단/재시작에서 같은 checkpoint를 중복하지 않고 기존 작업과 독립 차단을 보존한다.

## Implementation

- recovery/blocked_checkpoint.py·checkpoint_recovery.py와 git/managed_rebase_publication.py의 기존 checkpoint·PR 조회·push intent를 연결한다. blanket PR 거부를 안전 조건별 판정으로 대체한다.
- 정확한 lease는 게시 직전 관측과 실제 후보에 결합하고, 게시 후 확인 및 durable receipt를 기존 transaction 경계에 둔다. 미확정 게시를 강제 push 성공으로 추정하지 않는다.
- 문서·권한·게시와 검증의 공통 경계는 AGENTS.md, docs/ARCHITECTURE.md, docs/PLANS.md를 따른다. 필요한 기존 helper와 관련 테스트를 우선 재사용하고 새 루트 항목을 만들지 않는다.

## Validation

- blocked checkpoint·publication base·PR 충돌 테스트에서 기존 PR, 동일 branch rebase, 동시 push, PR 종료, 게시 전후 중단의 최소 unit case를 실행한다.
- Manager 전체 verify는 실제 원격 lease 거부·중복 push 방지, PR identity 보존과 최종 후보 검증 무효화를 검증한다.

- Worker targeted unit 검증: `python3 -m unittest tests.test_blocked_checkpoint.ExistingPrCheckpointPublicationTests tests.test_blocked_checkpoint_state.BlockedCheckpointRepositoryTest tests.test_execution_immediate_rebase_publication_phase.ImmediateRebasePublicationTests tests.test_execution_checkpoint_state.RebasePublicationRepositoryTests` — 50개 PASS. 외부 Git·PR 효과를 대체한 unit 경계에서 lease 경쟁, PR 종료·identity 변경, 후보 승인 무효화, 독립 차단 보존 및 저장 중단 후 재사용을 검증했다. `BlockedCheckpointPolicyTest`도 별도 targeted 실행에서 통과했다.
- verify 피드백에 따른 targeted unit 검증: `python3 -m unittest tests.test_blocked_checkpoint.ExistingPrCheckpointPublicationTests tests.test_execution_immediate_rebase_publication_phase.ImmediateRebasePublicationTests tests.test_managed_rebase_lifecycle.RebaseObservationFailureTests tests.test_execution_publication_phase.ExternalOwnershipObservationTests.test_project_checkpoint_lineage_retries_failed_owner_observation tests.test_pr_conflict_corrections.LivePullRequestObservationTests` — 최종 41개 PASS. 새 재시도 fixture의 필수 인자 누락과 외부 상태 port 참조를 수정한 뒤 동일 명령으로 재검증했다. 닫힌 PR 차단 보존, rebase adoption 후 게시 실패·재시도와 live PR head 관측을 외부 효과를 대체한 unit 경계에서 검사했다. 실제 Git 복구 시나리오의 fixture도 live head·순차 lease에 맞췄으며 실행은 Manager 전체 verify에 맡긴다.
- 결과 보고 검증: 위 41개 unit test를 동일 명령으로 재확인했다. 초기 fixture 실행의 ERROR·FAIL 결과를 `FAILED_COMMAND:` 뒤에 명시하고 동일 명령의 `RERUN:` 뒤에 최종 PASS를 둔 실제 보고 문구를 기존 `parse_worker_result`에 전달하는 단일 unit test도 통과했다. 결과 파서·공통 지침은 변경하지 않는다.
- Review 피드백 targeted unit 검증: `python3 -m unittest tests.test_execution_immediate_rebase_publication_phase.ImmediateRebasePublicationTests tests.test_checkpoint_adoption.BlockedCheckpointRefreshUnitTests` — 최종 60개 PASS. 초기 실행에서 비투영 계획에 빈 project 갱신을 전달한 오류와 fixture의 planning gate·원격 기대값 설정을 수정하고 동일 명령으로 재검증했다. 정상 `_run_project_step` 진입점의 게시 확정 전 중단·PR 변경 거부·일시 실패 재시도, 사용자 push 채택→base rebase 완료→미검증 재개, PR head/receipt 원자성 및 독립 projection 보존을 외부 Git·PR·사전 catalog 효과를 대체한 unit 경계에서 검사했다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [x] 기존 PR identity·정확한 원격 lease·게시 재확인과 승인 무효화, 중단 후 같은 checkpoint 재사용을 구현하고 관련 targeted unit 검증을 수행함.
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- PR·Git·저장 확정이 연결되어 complexity high, 원격 사용자 commit 덮어쓰기의 영향으로 risk critical다.

- R-060의 명시된 기존 PR checkpoint 확장을 적용했다. PR URL은 checkpoint 요청과 rebase intent에 보존하고, Open 상태·branch·live head를 게시 전후 및 SQLite 확정 경계에서 대조한다. 정상 후속 push와 검증된 rebase lease를 구분하며 기존 차단·session·작업 공간을 보존한다.
- 기존 PR의 후보가 바뀌면 PR head와 lease를 함께 전진시키고 후보 승인 토큰을 무효화한다. commit/push 뒤 중단된 요청은 저장된 commit identity·tree·lease를 유지해 중복 commit·push를 피한다. 최종 verify·Review를 마친 후보가 이미 같은 PR checkpoint에 있으면 최신 PR·원격·후보 검증을 거쳐 push 없이 재사용한다.
- 새 루트 항목·Requirement·ARCHITECTURE·AGENTS·PLANS 변경은 없다. `scripts/verify.sh`는 기존 verification runner로 전체 검증을 수행하고 실패 코드를 전달하므로 그대로 유지한다. 전체 검증·Review·Merge는 Manager와 사용자의 후속 경계다.
- 닫힌 PR로 이미 차단된 요청은 historical agent scope 없이도 PLAN_BLOCKED를 보존하고 PR 재개 경계에 맡긴다. rebase adoption과 게시 효과를 연결한 mutating helper는 게시 실패 시에도 저장된 snapshot을 호출자에 반영하여 같은 intent·lease의 재시도를 가리지 않는다. 정상 복구 fixture는 저장된 최초 head를 수정하지 않고 실제 원격 head를 조회하므로, 두 checkpoint의 순차 lease와 최종 중복 push 생략을 검사한다.
- Project 진입점은 저장된 PR rebase 게시 intent를 기존 PR head 검사보다 먼저 기존 direct 게시기로 확정한다. PR·원격·후보 검사를 생략하지 않으며, 일시 관측/게시 오류는 같은 intent로 재시도하고 identity 변화는 후속 후보 작업 전에 거부한다.
- 사용자 원격 후속 commit 채택은 기존 transaction에서 게시 lease와 PR head, 해당 PR의 충돌 복구 head를 함께 갱신하고 후보 승인 토큰을 무효화한다. 확정 직전 저장된 head도 다시 대조하며, 비투영 계획은 기존 source-only 저장 API를 사용해 다른 실행의 projection을 보존한다.
