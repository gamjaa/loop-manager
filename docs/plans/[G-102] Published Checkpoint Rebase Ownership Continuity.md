---
id: G-102
title: Published Checkpoint Rebase Ownership Continuity
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-045] Phase-Bound Managed Rebase Validation and Recovery.md
depends_on:
  - G-099
complexity: medium
risk: high
priority: critical
---

## Purpose

PR 없는 게시 checkpoint branch가 관리 rebase를 완료하면 로컬 HEAD는 새 base 위의 commit으로 바뀌지만 원격 branch는 마지막 게시 OID에 남는다. 현재 실행 경로는 rebase 완료 checkpoint를 기록한 뒤 이 차이를 소유권 상실로 판단해 `publication_ownership_uncertain`으로 프로젝트 전체를 차단한다. 검증된 rebase 결과와 기존 원격 lease를 함께 보존해 정상 실행을 계속한다.

## Acceptance Criteria

- 게시 checkpoint의 마지막 채택 원격 OID가 그대로이고 PR이 없으며, 같은 plan·branch·worktree·Worker session의 관리 rebase intent, 단계별 journal 결과, checkpoint generation과 현재 clean HEAD·branch ref·worktree 등록·target base가 일치하면, rebase 후 로컬 HEAD가 원격 OID와 달라도 기존 게시 소유권을 유지한다. 재시작 직후와 rebase 완료 결과를 SQLite에 반영한 뒤의 재진입에 같은 판정을 적용한다.
- rebase 완료 판정과 게시 소유권 판정을 하나의 지속 가능한 실행 상태로 연결한다. 완료 checkpoint의 `operation` 정리 때문에 소유 증거가 사라지거나, 근거 없는 `blocked_resume` 표식을 만들어 소유권을 우회하지 않는다. 확인된 원격 OID를 향후 push lease로 사용하고, 후보 revision 변경 시 Worker 확인·targeted test·전체 verify·Review를 다시 거친다.
- 원격 OID/PR head의 예상 밖 변경, 누락·변조된 journal 또는 checkpoint, 다른 session, 불일치한 HEAD·ref·index·작업 파일·worktree identity는 계속 안전 차단한다. 원격을 덮어쓰거나 게시 checkpoint를 새 로컬 HEAD로 간주하지 않는다.
- 소유권이 증명된 완료 rebase는 `PROJECT_BLOCKED`를 만들지 않고 보존된 Worker session과 작업물에서 진행한다. 기존 `worker_output_invalid` 같은 독립적인 계획 차단은 별도 원인으로 유지하며 다른 READY 계획의 선택을 막지 않는다.

## Implementation

- `execution_handlers.py`의 게시 checkpoint 원격 소유권 검사와 rebase 완료 전이를 G-099의 검증된 journal 결과에 연결한다. `execution_checkpoint_state.py`의 완료 기록과 `loop_manager.py`의 재시작 복구 경로가 완료 후에도 같은 before HEAD, after HEAD, target base, accepted remote OID 및 실행 identity를 검증하도록 한다.
- 기존 원격 lease, checkpoint 원본 및 실패 감사는 유지한다. 증거 없는 과거 checkpoint를 자동 채택하거나 새 강제 push 경로를 추가하지 않는다. 장기 제품 규칙에 해당하는 변경은 구현 PR에서 `docs/PRODUCT.md`에 반영한다.

## Validation

- Worker는 직접 변경한 `tests/test_execution_handlers.py`, `tests/test_execution_checkpoint_state.py`, `tests/test_loop_manager.py` 중 관련 targeted unit test만 실행한다.
- 게시 checkpoint OID와 다른 로컬 HEAD가 소유 rebase 완료로 증명되는 경우, Git 완료 직후·checkpoint 확정 직후·재시작 뒤에 프로젝트 차단 없이 Worker 확인 및 후속 검증으로 진행하는지 확인한다. 원격 변경, PR 출현, journal 누락·변조, session 불일치, 오염된 worktree는 거부하는 회귀를 포함한다.
- Manager 전체 verify는 일반 재개와 PR 없는 게시 checkpoint 경로, 원격 lease 보존, 별도 계획 차단 및 READY 선택이 유지되는지 확인한다.

## Progress

- [ ] rebase 완료 증거와 게시 소유권의 지속적 연결
- [ ] 재시작·원격 경쟁·별도 계획 차단 회귀

## Decision Log

- 2026-09-28: G-100의 기존 `worker_output_invalid` 차단은 작업물과 게시 checkpoint를 보존해 유예한다. 이 계획은 현재 Merge된 실행 경로의 게시 소유권 검사를 독립적으로 수정하며 G-100의 구 형식 rebase 복구를 선행 조건으로 요구하지 않는다.
