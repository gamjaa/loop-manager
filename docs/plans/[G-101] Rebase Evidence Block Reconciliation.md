---
id: G-101
title: Rebase Evidence Block Reconciliation
sources:
  - docs/requirements/[R-045] Phase-Bound Managed Rebase Validation and Recovery.md
depends_on:
  - G-100
complexity: medium
risk: high
priority: critical
---

## Purpose

구 완료 검사의 오탐으로 저장된 `status_failed` 재시도, `PLAN_BLOCKED` 또는 `PROJECT_BLOCKED`가 남을 수 있다. rebase 완료 복구만으로 기존 차단 사건을 무조건 지우지 않고, R-045 원인에 한해 증거를 재평가해 실행을 이어가게 한다.

## Acceptance Criteria

- 저장된 실패·계획/프로젝트 차단의 구조화 원인과 대상 rebase intent를 확인한다. G-100의 완료 증거가 현재 Git·checkpoint·원격/PR 소유권과 일치하고 다른 유효한 차단 원인이 없을 때만 이 결함의 retry·차단을 해제한다. 불완전한 legacy 증거, 별도 Review 실패·사용자 결정·Agent 범위 위반과 다른 계획·planning 오류는 유지한다.
- 실제 해제와 실행 상태 전이는 기존 사건·알림·실패 감사를 보존하며 일관되게 기록한다. 같은 미해결 원인의 반복 관측은 새 BLOCKED 알림을 만들지 않고, 확인된 재개 뒤 새 실패는 기존 사건 규칙을 따른다.
- 복구 뒤 기존 Worker session·작업물·승인·게시 OID를 유지하고 필요한 Worker 확인·targeted test·전체 verify·Review와 최신 원격 lease 검사를 거쳐 게시한다. 이 계획의 해제가 R-023의 무관한 shared ref·외부 worktree 감시나 범용 `default_worktree_mutation` 해제를 허용하지 않는다.
- 진단은 충돌 해결 tree, 후속 재생, 최종 HEAD/ref/등록, 원격 lease 및 legacy 증거 중 실패한 단계와 기대·관측 지문을 식별한다. 실제 Git 이력의 G-085 재발 조건에서 충돌 해결 뒤 여러 후속 commit이 같은 파일을 변경해도 재시작 후 Worker·verify·Review 경로에 도달한다.

## Implementation

- `scoped_blocking.py`, `loop_manager.py`의 계획별·프로젝트별 차단 재검토 및 `execution_handlers.py`의 저장 실패 복구에 R-045 전용 원인 판정을 연결한다. G-100의 검증된 완료 결과를 사용하고 상태·사건 변경은 기존 transaction과 소유 실행 자리 경계 안에서 수행한다.
- G-093~G-095의 planning slot·오류·incident 보존과 G-092의 Agent 기준선/일반 차단 해제는 건드리지 않는다. 서로 다른 차단 원인이 섞인 경우 해제 가능한 원인만 해소하고 나머지는 유지한다. R-045의 결함 복구·진단 규칙은 구현 PR에서 `docs/PRODUCT.md`에 반영한다.

## Validation

- Worker는 직접 변경한 `tests/test_scoped_blocking.py`, `tests/test_recovery_runtime.py`, `tests/test_execution_handlers.py`, `tests/test_loop_manager.py` 중 관련 targeted unit test만 실행한다.
- G-085 형식의 실제 Git commit 이력으로 충돌 해결 commit 다음 여러 commit이 같은 파일을 변경하는 경우와 재시작을 재현한다. Manager 전체 verify는 복구 뒤 Worker·verify·Review 진행, 기존 PR/PR 없는 경로, 잘못된 staged tree·후속 commit 변조·원격 OID 변경·다른 session·legacy 증거 부족의 차단 유지를 확인한다.
- 별도 Review/사용자 결정/Agent 범위 문제와 planning 상태, 실패·알림 감사가 보존되고 같은 미해결 사건이 중복 통지되지 않는지 확인한다. 정상 차단 없는 rebase와 기존 알림 전송 실패 재시도도 유지한다.

## Progress

- [ ] R-045 증거 실패의 저장된 retry·차단 원인별 재평가
- [ ] 차단 사건·진단 및 재검증 연결
- [ ] 실제 Git 이력과 중단·재시작 회귀

## Decision Log

- G-092는 R-023의 Agent Git 범위와 Manager 기준선 차단을 담당한다. 이 계획은 R-045의 완료 증거 오탐으로 남은 실패·차단만 다룬다.
