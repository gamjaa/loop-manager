---
id: G-104
title: Immediate Managed Rebase Checkpoint Publication
sources:
  - docs/requirements/[R-047] Immediate Remote Checkpoint After Managed Rebase.md
  - docs/requirements/[R-048] Evidence-Free Managed Rebase Recovery.md
depends_on:
  - G-100
  - G-101
  - G-109
complexity: high
risk: critical
priority: critical
---

## Purpose

PR이 없는 동일 계획의 기존 원격 작업 branch를 관리 rebase한 뒤, 검증과 Review를 기다리는 동안 재작성된 로컬 HEAD가 원격 checkpoint와 어긋나 있다. 완료된 rebase 결과를 즉시 원격에 보존하고 다음 base rebase보다 먼저 게시 기준을 갱신한다.

## Acceptance Criteria

- G-109의 현재 상태 판정으로 계획·repository·관리 worktree·소유 branch·HEAD·ref·등록, target base ancestry, clean index·미해결 operation 부재, 작업 범위와 원격 기대값을 확인한 경우에만 중간 게시 intent를 만든다. 자동·수동 해결 모두 과거 Worker turn·stage 승인·receipt·commit별 replay·journal의 누락 자체로 거부하지 않는다. 충돌, detached HEAD, 미해결 Git operation, 현재 소유권 불확실성 또는 범위 밖 변경은 push 없이 작업물과 이유를 보존한다.
- 게시 직전 정확한 원격 작업 ref와 PR 부재를 다시 확인한다. 마지막으로 채택한 원격 OID가 그대로일 때만 fast-forward하거나 그 OID를 명시한 `--force-with-lease`를 사용한다. 원격 삭제·재작성·후속 commit, PR 출현, 권한·보호 정책 거부는 기존 ref를 덮지 않는다.
- 안전한 rebase 완료 직후, 다음 Worker·전체 verify·Review·추가 base rebase보다 먼저 게시 또는 안전한 거부를 확정한다. 그 사이 base가 다시 전진해도 먼저 완료된 결과의 게시를 처리한다. 이미 같은 OID가 원격에 있으면 commit·push를 반복하지 않는다.
- push 전후와 SQLite 반영 전 중단에서 저장된 게시 intent, 실제 원격 OID와 G-109의 현재 소유 상태·수용 결과를 재대조해 한 번만 완료 기록을 적용한다. 일시적 실패는 같은 intent로 재개하며 예상 밖 원격 경쟁에서는 lease를 다시 추정하지 않는다. 게시 intent와 현재 원격 기대값은 계속 필요하지만 과거 rebase 명령 receipt나 journal을 복구해 만드는 것은 조건이 아니다.
- G-100처럼 게시되지 않은 rebase가 여러 개 이어진 legacy 상태도 동일 계획의 게시 identity·마지막 채택 원격 OID와 현재 branch·worktree·target·최종 변경 범위를 검사해 처리한다. 과거 실행별 journal/replay 연쇄는 요구하지 않는다. 원격 HEAD가 로컬 HEAD의 조상이 아니라는 사실만으로 거부하지 않고, clean 상태나 공통 base 조상 하나만으로 승인하지도 않는다. 현재 소유 범위 또는 원격 기대값을 확정할 수 없으면 게시하지 않는다.
- 중간 게시는 구현 완료·차단 해제·검증 성공·PR head 승인으로 취급하지 않는다. 이후 base·Worker 변경·후보 HEAD가 달라지면 최종 후보에 필요한 Worker 확인, targeted test, 전체 verify와 Review를 다시 거친다. 기존 PR, 신규 원격 branch 및 다른 계획의 branch는 이 경로에서 게시하지 않는다.

## Implementation

- `execution_handlers.py`의 관리 rebase 완료·재진입 경계에서 중간 게시를 우선 처리하고, G-109의 공통 현재 상태·checkpoint 소유권 결과를 소비한다. 단일·누적 rebase 모두 같은 판정을 사용하며 이 계획에서 별도 replay/journal validator를 만들지 않는다.
- `execution_checkpoint_state.py`와 관련 SQLite 저장 경계에 게시 intent, 마지막 채택 원격 OID, 현재 상태 수용 identity·지문과 게시 결과를 지속 저장한다. 기존 journal·게시·실패 감사는 보존한다. 원격 조회·push는 Agent 호출 밖의 Manager가 수행하고, `publication_resume.py`의 최종 PR 게시 판단은 중간 게시와 독립적으로 유지한다.
- R-047의 장기 게시 순서와 검증 경계는 구현 PR에서 `docs/PRODUCT.md`에 반영한다.

## Validation

- Worker는 직접 변경한 `tests/test_execution_handlers.py`, `tests/test_execution_checkpoint_state.py`, `tests/test_manager_git_journal.py` 중 게시 intent·순서·lease에 관련된 targeted unit case만 실행한다. 실제 Git lifecycle test는 작성하고 Manager 전체 verify에 맡긴다.
- 실제 Git 이력으로 게시 checkpoint → 첫 rebase → 중간 게시 → 새 base 전진 → 두 번째 rebase를 검증한다. 첫 push 전·후 중단, 자동·수동 충돌 교정, 원격 OID 경쟁, lease·권한 거부 및 미게시 다중 rebase를 다룬다. 과거 rebase 증거가 없는 정상 현재 결과는 게시 대상으로 인정하고 현재 소유권·범위·원격 불일치는 계속 거부한다.
- 전체 verify는 중간 게시 이후 최종 후보의 검증·Review·PR gate가 유지되고 기존 PR 및 신규 branch가 조기 게시되지 않는지 확인한다.

## Progress

- [x] 현재 상태의 완료 rebase 수용과 중간 게시 intent 검증
- [x] 원격 lease 게시·중단 복구와 최종 검증 경계 연결

- Targeted unit 검증: `ImmediateRebasePublicationTests`와 `RebasePublicationRepositoryTests` 16개 및 기존 rebase 수용·소유권 case 4개 통과. 실제 Git lifecycle 테스트와 전체 verify는 Worker가 실행하지 않고 Loop Manager 검증에 맡긴다.

## Decision Log

- G-100·G-101은 R-045의 rebase 완료·오류 차단 복구를 맡는다. 이 계획은 그 결과를 게시하는 R-047의 새 순서와 legacy 다중 rebase 게시 증거만 다룬다.
- 2026-09-30: mutable READY snapshot token `5d5162255574800d9d989ed1db28373663ca028d91b2faab321d45e609690e8e`, 기준 revision `d9769d7a5b437ceddc2efdeb87f9cc3bda2c74da`와 원본 hash 일치를 확인해 개정했다. R-048은 위 과거 Decision의 완료 증거 정책을 대체한다. G-109에 의존해 현재 상태 수용 결과를 게시에 사용하고 R-047의 검증 전 중간 게시 순서는 유지한다.

- 2026-09-30: G-109 현재 상태 수용 결과를 재검사하는 `managed_rebase_publication.py`에 중간 게시를 분리했다. `rebase_publish` intent는 최종 PR 게시와 독립적이며 정확한 원격 lease와 후보 HEAD를 고정한다. 기존 checkpoint JSON 저장과 runtime transaction을 사용해 비호환 SQLite schema 변경 없이 게시 감사·마지막 채택 OID를 함께 반영한다.
- 2026-09-30: 게시 뒤에도 기존 blocked checkpoint identity와 rebase 감사는 보존한다. 원격 OID 갱신 외 phase·Worker·verify·Review·차단 상태를 게시 완료로 승인하지 않는다. 실제 Git lifecycle 시나리오는 전체 verify용으로 작성했고 Worker는 실행하지 않았다.
