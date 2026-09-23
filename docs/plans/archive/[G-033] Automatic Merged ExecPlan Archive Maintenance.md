---
id: G-033
title: Automatic Merged ExecPlan Archive Maintenance
sources:
  - docs/requirements/[R-014] Automatic Completed ExecPlan Archiving.md
depends_on:
  - G-018
  - G-032
  - G-049
complexity: high
risk: critical
priority: high
---

## Purpose

GitHub Merge와 SQLite MERGED 전이가 확인된 뒤 Loop Manager 코드가 별도 임시 worktree에서 완료 계획을 내용 변경 없이 `docs/plans/archive/`로 일괄 이동하고 최신 원격 base에 fast-forward push한다. 정리 실패는 안전한 다른 후보와 다음 READY·Planner·완료 처리를 막지 않으며, 중단·원격 경쟁·push 실패 뒤에도 중복 commit이나 잘못된 덮어쓰기 없이 재시도한다.

## Acceptance Criteria

- Worker, verify, Reviewer, publication과 PR_OPEN 동안 선택된 ExecPlan은 최초 active path에 남고 구현 branch 또는 구현 PR에는 plan archive 이동이 포함되지 않는다.
- GitHub Merge 확인과 해당 ExecPlan의 SQLite MERGED transaction이 완료된 뒤에만 top-level active 파일을 archive 후보로 계산한다.
- 정리 단계는 AI Agent를 호출하지 않고 최신 `origin/<base-branch>`의 별도 detached 임시 worktree에서 실행하며 archive 전용 ExecPlan, 장기 branch 또는 PR을 만들지 않는다.
- 후보 source는 regular file이고 저장된 원본 content hash와 byte-for-byte 같아야 하며, archive destination은 존재하지 않아야 한다. symlink, 누락, hash 불일치, 목적지 충돌과 Merge 불확실성은 덮어쓰지 않는다.
- 안전한 모든 후보를 하나의 내용 보존 rename-only commit으로 묶되, 문제가 있는 후보는 archive pending reason을 저장하고 다른 안전한 후보의 이동을 막지 않는다.
- archive commit diff는 `docs/plans/`의 검증된 source 삭제와 같은 파일명의 `docs/plans/archive/` 추가만 포함한다. 제품 코드, Requirement와 계획 본문 변경이 섞이면 게시하지 않는다.
- commit 직전과 push 직전에 원격 base SHA를 다시 확인한다. 기준이 바뀌면 rebase·merge·force push하지 않고 임시 worktree를 제거한 뒤 최신 기준에서 후보와 pending reason을 다시 계산한다.
- 정리 commit은 `origin/<base-branch>`에 fast-forward로 직접 push하며 branch protection, 권한 또는 일시 오류를 우회하지 않는다. 실패한 후보와 commit 상태는 다음 실행에서 멱등하게 재평가한다.
- commit 뒤 또는 push 뒤 중단돼도 원격 tree와 저장된 archive operation identity를 대조해 같은 파일을 다시 이동하거나 빈·중복 commit을 만들지 않는다.
- archive 성공과 실패는 이미 확인된 MERGED 상태를 되돌리지 않고 다음 READY 선택, Planner와 프로젝트 완료 판정을 막지 않는다. archive pending은 프로젝트 block이나 planning error가 아니다.
- active source가 이미 없고 기대 archive가 동일 hash로 원격에 있으면 성공한 정리로 수렴한다. source와 destination이 함께 있거나 내용이 다르면 자동 삭제·덮어쓰기를 하지 않는다.
- Planner/Worker/maintenance 입력과 실행 후보는 top-level active 계획으로 제한하고 G-032의 full catalog는 ID, dependency, recovery와 audit 검증에만 사용한다.
- archive-only plan 이동은 planning input fingerprint 변화, 새 일반·유지보수 계획 주기, completion cycle 재활성화 또는 새 프로젝트 완료 알림을 만들지 않는다.
- 기능 도입 PR은 G-033 자신이나 과거 MERGED 계획을 이동하지 않는다. Merge 후 새 runtime의 첫 정리 단계가 특정 ID 목록 없이 SQLite·GitHub 상태로 모든 안전한 기존 후보를 계산한다.
- Requirement archive eligibility와 구현 PR 안의 Requirement 이동 규칙은 변경하지 않는다.
- 장기 제품 규칙을 R-014에 맞게 `docs/PRODUCT.md`에 갱신하되 Requirement와 ARCHITECTURE는 수정하지 않는다.

## Implementation

- 새 `execplan_archive.py`에 candidate validation, rename-only tree 계산과 typed dispatcher outcome을 두고, 새 `execplan_archive_state.py` repository가 plan identity, base SHA, final hash, operation/commit OID, push 결과와 pending reason을 additive table에 저장한다. `runtime_state.py`의 active execution projection이나 `exec_plans.path` 의미는 바꾸지 않는다.
- archive는 활성 ExecPlan phase가 아니므로 `ExecutionPhaseCoordinator`나 `execution_handlers.py`에 phase를 추가하지 않는다. PR Merge reconciliation 뒤와 project 선택 전의 상위 housekeeping adapter가 dispatcher를 호출하고 success/pending/transient 결과를 scheduler 성공과 분리한다.
- deterministic archive workspace와 remote compare-and-swap은 주입된 Git port로만 수행한다. G-022 ownership helper를 adapter에서 재사용하되 archive domain service는 subprocess, Agent, notification 또는 raw project row를 직접 다루지 않는다.
- candidate별 순수 validation이 반환한 안전 subset만 명시적 pathspec으로 stage하고 commit 전후 tree mode·blob hash·rename-only diff를 재확인한다. base race와 중단은 repository operation identity로 재계산하며 rebase·merge·force push 경로를 만들지 않는다.
- planning input fingerprint, completion cycle과 maintenance due가 공유하는 archive-only change 판정을 `execplan_catalog.py`의 명시적 classifier로 두어 세 호출자가 tree diff 규칙을 중복 구현하지 않게 한다.
- `loop_manager.py`에는 archive state/Git port 조립과 typed housekeeping outcome의 기존 process result 변환만 남긴다. 알림은 저장된 결과를 읽는 기존 outbox 경계가 담당한다.

## Validation

- Worker는 새 `tests/test_execplan_archive.py`, `tests/test_execplan_archive_state.py`와 planning/completion classifier의 직접 관련 targeted case만 실행한다.
- PR 미Merge·Open·Closed와 verify/Review 중에는 이동하지 않고 Merge+SQLite 전이 뒤에만 후보가 되는지 검증한다.
- 단일·복수 및 기존 MERGED 후보, G-033 자체의 Merge 후 첫 정리와 특정 ID 없는 bootstrap을 검증한다.
- 내용 보존 rename-only diff, source hash 변조, symlink, 목적지 충돌, source 누락과 일부 후보 실패 격리를 검증한다.
- commit 전·push 전 base race에서 재계산하고 rebase/force push하지 않는지 검증한다.
- push 실패, commit 후 중단, push 후 DB 기록 전 중단, 이미 원격에 반영된 archive와 빈 후보에서 멱등하게 수렴하는지 검증한다.
- 상위 housekeeping adapter fake로 archive pending이 실행 slot, READY·Planner·완료를 막지 않고 active execution coordinator를 호출하지 않는지 검증한다.
- archive-only classifier가 planning fingerprint, maintenance count와 notification completion cycle을 바꾸지 않는지 각 소비자의 targeted test로 검증한다.
- active/archived ID 연속성, archived dependency와 recovery는 G-032 계약을 유지하고 Requirement archive 테스트가 바뀌지 않는지 검증한다.
- 전체 verify에서 PR Merge cleanup, origin isolation, notification outbox, READY 소진 및 maintenance scheduling 회귀가 없어야 한다.

## Progress

- [ ] archive candidate/dispatcher와 operation repository 경계 구현
- [ ] 주입형 Git port 기반 별도 worktree·rename-only 검증 구현
- [ ] fast-forward CAS 게시와 원격 경쟁·중단 복구 구현
- [ ] project housekeeping adapter와 공통 archive-only classifier 통합
- [ ] archive domain/state/scheduler targeted 회귀 테스트 추가

## Decision Log

- archive는 구현 산출물이 아니라 MERGED 이후의 housekeeping이므로 Worker branch와 PR에서 분리하고 Loop Manager 코드가 직접 수행한다.
- 한 후보의 손상이나 충돌이 확인된 Merge 상태 또는 다른 안전한 계획의 정리를 되돌리지 않도록 후보별 진단과 batch publication을 분리한다.
- 별도 bootstrap ExecPlan과 ID allowlist를 만들지 않는다. 기능 Merge 후 현재 상태에서 계산한 모든 MERGED 후보를 같은 일반 dispatcher가 처리한다.
- Post-refactor revalidation: archive housekeeping은 G-049의 active execution phase와 다른 수명주기이므로 `execution_handlers.py`에 누적하지 않는다. 독립 dispatcher/repository와 상위 project adapter로 두어 실행 coordinator 변경 충돌을 제한한다.
