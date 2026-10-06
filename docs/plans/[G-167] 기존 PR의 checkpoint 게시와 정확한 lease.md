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

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [x] 기존 PR identity·정확한 원격 lease·게시 재확인과 승인 무효화, 중단 후 같은 checkpoint 재사용을 구현하고 관련 targeted unit 검증을 수행함.
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- PR·Git·저장 확정이 연결되어 complexity high, 원격 사용자 commit 덮어쓰기의 영향으로 risk critical다.

- R-060의 명시된 기존 PR checkpoint 확장을 적용했다. PR URL은 checkpoint 요청과 rebase intent에 보존하고, Open 상태·branch·live head를 게시 전후 및 SQLite 확정 경계에서 대조한다. 정상 후속 push와 검증된 rebase lease를 구분하며 기존 차단·session·작업 공간을 보존한다.
- 기존 PR의 후보가 바뀌면 PR head와 lease를 함께 전진시키고 후보 승인 토큰을 무효화한다. commit/push 뒤 중단된 요청은 저장된 commit identity·tree·lease를 유지해 중복 commit·push를 피한다. 최종 verify·Review를 마친 후보가 이미 같은 PR checkpoint에 있으면 최신 PR·원격·후보 검증을 거쳐 push 없이 재사용한다.
- 새 루트 항목·Requirement·ARCHITECTURE·AGENTS·PLANS 변경은 없다. `scripts/verify.sh`는 기존 verification runner로 전체 검증을 수행하고 실패 코드를 전달하므로 그대로 유지한다. 전체 검증·Review·Merge는 Manager와 사용자의 후속 경계다.
