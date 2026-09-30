---
id: G-109
title: Unified Managed Rebase Adoption and Revalidation
sources:
  - docs/requirements/[R-048] Evidence-Free Managed Rebase Recovery.md
depends_on:
  - G-108
complexity: high
risk: critical
priority: critical
---

## Purpose

G-108의 현재 상태 판정을 일반 구현, 관리 rebase 재시작, PR 충돌 복구와 PR 없는 checkpoint branch에 일관되게 연결한다. 과거 작업 증거 없이 해결·완료한 결과를 채택하되 현재 입력과 최종 후보의 Worker 확인·targeted test·전체 verify·Review를 다시 거치게 한다.

## Acceptance Criteria

- 모든 기존 관리 rebase의 시작·충돌 관측·stage/continue·완료·재진입 경로는 G-108의 현재 상태 기준을 사용한다. 자동 Worker 해결과 사용자의 파일 해결·stage·`rebase --continue`·완료가 같은 결과로 처리된다. 과거 Worker 교정 turn·승인 tree·receipt·reflog 표식·commit별 replay 또는 완전한 journal이 없다는 이유로 진행·완료를 거부하지 않는다.
- 소유 rebase가 진행 중이면 현재 충돌·operation·staged 내용을 보존한다. 해결 대기는 기존 Worker 세션의 교정 경로로 보내고, 현재 범위 검사를 통과한 해결 내용은 Agent 호출 밖의 Manager가 필요한 path만 stage하고 계속한다. 이미 사용자가 stage·continue·완료한 단계는 반복하지 않는다. 미해결 autostash 복원도 완료·게시로 처리하지 않고 기존 보존·교정 경계를 유지한다.
- 일반 handler와 직접 복구 경로, 기존/신규 checkpoint 형식에서 같은 현재 결과를 채택한다. journal seal 전·후 또는 SQLite 반영 전에 중단돼도 현재 상태를 다시 검사해 완료를 한 번만 반영한다. 과거 증거 부족에 의한 반복 `status_failed`만 남기거나 동일 rebase·stage·Worker 교정을 반복하지 않는다. 범위·identity·target·원격 기대값이 불명확하면 작업물과 실패 감사를 보존한다.
- PR 없는 게시 checkpoint의 원격 OID가 rebased HEAD의 조상이 아니어도 동일 계획의 게시 identity·마지막 채택 원격 기대값·현재 소유 branch/worktree·target·허용 최종 변경을 검사해 재개한다. 원격 후속 commit·삭제·재작성·PR head 변경은 이 예외로 수용하지 않는다. 게시 ownership 검사에 rebase journal/replay의 완전성을 다시 요구하지 않으며 정확한 원격 lease와 기존 PR 게시 조건을 유지한다.
- rebase 결과 HEAD 또는 선택 ExecPlan·근거 입력이 바뀌면 이전 Worker 후보 확인과 verify·Review·publication 증거를 무효화한다. 기존 또는 정상 fallback Worker 세션에서 현재 입력과 최종 diff를 확인하고 관련 targeted test 결과를 받은 뒤 Manager 전체 verify와 Reviewer를 실행한다. 중단·재시작·최신 base 재전진 뒤에도 이전 후보의 성공을 재사용하지 않는다. 현재 선언의 dependency·역할별 범위 검사는 기존 revision 계약을 유지하며 현재 입력 확인을 생략하는 근거로 사용하지 않는다.
- G-104가 담당할 R-047의 중간 checkpoint는 작업물 보존 목적의 별도 게시 경계다. 그 경로를 최종 검증 성공·PR 준비·차단 해제로 표시하지 않는다. 이 계획은 조기 push 또는 새 branch 게시 기능을 추가하지 않으며, 최종 PR/게시의 기존 검증·Review·원격 lease 조건을 유지한다.
- rebase로 수용한 현재 결과만 선택 branch·worktree의 기준선에 반영하고 Agent 원래 snapshot·session·허용 변경과 과거 감사를 보존한다. 수동 해결을 Manager 실행 또는 Agent 격리 증거로 꾸미지 않는다. 다른 Manager Git 작업, 실제 Agent 범위 위반, 사용자 결정·Review 실패 및 다른 계획·planning 차단은 별도 기준으로 유지한다.

## Implementation

- `execution_handlers.py`의 `_record_rebase_intent`, `_begin_rebase_continuation`, `_rebase_journal_checks`, `_complete_rebase_intent`, `_owned_detached_rebase`, `_owned_completed_rebase`, `_owned_persisted_rebase`와 `recover`가 공통 판정을 소비하게 한다. 현재 scope 검사와 Worker 결과 protocol은 유지하면서 replay·교정 turn·승인 stage의 역사 검증을 필수 gate에서 제거한다.
- `loop_manager.py`의 `_finish_owned_direct_rebase`, `_direct_rebase_history_proven`, `_complete_direct_rebase_checkpoint`, `resume_pr_conflict_rebase`도 같은 서비스에 위임한다. `manager_git_journal.py`의 완료·재조회 및 `checkpoint_ownership.py`의 소유권 호출에 남은 receipt·range-diff·patch 개수·session 일치 요구가 현재 rebase 수용을 가로막지 않게 한다. rebase 이외의 fetch·worktree·commit·push 증거 검사는 축소하지 않는다.
- 현재 상태 조회·판정·저장 orchestration은 공통 서비스로 묶고 handler와 직접 경로에는 adapter만 둔다. `managed_rebase_evidence.py`의 과거 형식 reader와 감사 자료는 보존하되 신규 완료를 위해 원본별 재생을 재구성하지 않는다.
- `execution_revision.py`, `runtime_state.py`와 기존 Worker/validation 경계에 최종 후보·입력에 결합된 재확인과 검증 무효화를 연결한다. rebase 완료를 곧바로 VERIFYING으로 보내 Worker의 현재 후보 확인을 생략하는 경로를 없앤다. 동일 후보에서 재확인이 완료되면 재호출을 반복하지 않는다.
- R-048의 일반 rebase·게시 ownership·재검증 장기 규칙을 같은 구현 PR의 `docs/PRODUCT.md`에 반영한다. ARCHITECTURE, Requirement와 AGENTS.md는 수정하지 않는다.

## Validation

- Worker는 `tests/test_execution_handlers.py`, `tests/test_execution_checkpoint.py`, `tests/test_managed_mutation_evidence.py`, `tests/test_manager_git_journal.py`, `tests/test_execution_revision.py`에서 직접 바뀐 판정·phase·ownership·후보 무효화 unit case만 실행한다. 실제 Git integration case는 작성하고 Manager 전체 verify에 맡긴다.
- 과거 증거 없는 자동·수동 해결과 변경된 commit 내용·개수·순서, journal 없는 진행/완료, 완료 뒤 원격 base 재전진 및 PR 유무를 같은 unit 판정으로 다룬다. 실제 scope 위반·잘못된 metadata·원격 경쟁은 계속 거부한다. 구 증거 gate를 기대하던 테스트는 새 승인 정책의 수용/거부 경계로 갱신하며 검증·Review 자체를 삭제하지 않는다.
- `tests/test_managed_rebase_lifecycle.py` 등 실제 Git/SQLite lifecycle에서 여러 충돌·같은 파일의 후속 변경·수동 stage/continue/완료, Git 완료 후 저장 전 중단과 재시작을 검증한다. 완료가 한 번만 반영되고 현재 후보 Worker 확인·targeted test 결과·Manager verify·Review 뒤에만 최종 게시 단계에 도달해야 한다.
- Manager 전체 verify는 기본 checkout과 다른 계획 보존, 기존 PR의 정확한 lease·중복 PR 방지, checkpoint ownership, 실제 flags·범위 밖 변경 거부 및 최종 후보 변경 후 재검증을 확인한다.

## Progress

- [ ] handler·직접 경로의 공통 현재 상태 채택
- [ ] checkpoint 소유권·중단 복구와 감사 보존
- [ ] 최종 후보 Worker 확인·검증 무효화·실제 Git 회귀 사례

## Decision Log

- 일반 구현과 PR 충돌 복구까지 적용하는 R-048의 전역 정책이다. legacy 오탐 차단만 고쳤던 G-101을 다시 계획하지 않는다. 저장된 최종 차단을 원인별로 해제하는 추가 연결은 G-110이 담당한다.
- G-104의 중간 게시 순서와 G-106·G-107의 차단 중 원격 갱신은 기존 계획에 남긴다. 이 계획의 수용 결과를 그 계획들이 사용하며 과거 replay 증거를 게시 gate로 재도입하지 않는다.
