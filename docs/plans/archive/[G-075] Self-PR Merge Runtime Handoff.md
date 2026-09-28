---
id: G-075
title: Self-PR Merge Runtime Handoff
sources:
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
depends_on:
  - G-055
  - G-073
  - G-074
complexity: high
risk: high
priority: critical
---

## Purpose

현재 runtime 자식은 자기 저장소의 PR Merge를 새로 확인해도 같은 구 revision에서 다음 Agent·verify·publication 또는 다른 프로젝트 작업을 시작할 수 있다. R-023의 최신 bootstrap gate와 PR 상태 선동기화 뒤, 검증된 self Merge가 현재 runtime보다 새 프로그램 revision을 제공하면 상태를 먼저 영속화하고 다음 외부 실행에 제어를 넘긴다.

## Acceptance Criteria

- runtime 자식은 부모가 검증해 전달한 canonical source checkout, runtime SHA, 실제 Git repository identity와 origin 및 등록된 `<base-branch>`를 대조해 self 프로젝트를 결정한다. 같은 표시명·origin의 다른 clone, 경로 prefix, symlink alias 또는 runtime 경로를 self source로 오인하지 않는다. self 등록이 없을 때의 bootstrap 기본 branch 선택은 유지한다.
- 현재 실행에서 self PR의 새 Merge 완료를 확인하면 해당 `<base-branch>`의 최신 `origin/<base-branch>` fetch, PR·ExecPlan 전이, cleanup 결과와 알림 outbox를 먼저 durable하게 기록한다. G-073 순서와 G-074의 dirty 허용 조건으로 source sync를 평가·기록한 뒤 현재 runtime SHA와 fetch된 SHA를 비교한다.
- 두 SHA가 같으면 이미 최신 runtime에서 정상 실행을 계속한다. 원격 SHA가 현재 runtime의 fast-forward 후속이면 source sync 성공·skip·fail과 무관하게 성공 코드로 종료하고 새 Planner·Worker·Reviewer·전체 verify·publication·다른 프로젝트 상태 변경을 시작하지 않는다. 다음 실행은 G-055의 bootstrap freshness gate에서 같은 등록 base의 원격 SHA를 다시 조회한다.
- 최신 fetch 실패, repository/origin identity 불확실 또는 두 SHA의 fast-forward 관계 불명확 시 성공 handoff로 취급하지 않고 기존 일시 오류·repository safety 경계에서 세션, retry, workspace와 상태를 보존한다.
- self planning commit 또는 일반 외부 push만 관측했거나 다른 등록 프로젝트의 PR Merge만 확인한 경우에는 handoff하지 않는다. handoff 판정은 병렬 프로젝트 작업을 새로 배정하기 전의 안전한 scheduling boundary에서 완료하며, 이미 영속화한 결과를 되돌리지 않는다.
- handoff 로그에는 등록 base, 현재·원격 SHA, self repository, Merge PR과 source sync 결과를 비밀값 없이 남기고 BLOCKED 오류나 알림 사건으로 분류하지 않는다. 장기 실행 규칙은 구현 PR의 `docs/PRODUCT.md`에 반영하며 Architecture Change Policy에 해당하는 경계는 승인 상태를 먼저 확인한다.

## Implementation

- `loop_manager.py`의 runtime child 인자·검증 및 `project_registry.py` 등록 정보를 사용해 self identity와 일관된 base branch를 산출한다. G-055의 부모 선택 SHA와 현재 자식 SHA를 판정 입력으로 삼고 Git common directory·origin을 실제 조회로 확인한다.
- `synchronize_waiting_pull_requests`의 새 self Merge 결과가 durable한 뒤 source sync·remote ancestry 판정과 handoff disposition을 만든다. 같은 SHA 재확인이나 중단 재개는 동일 사건을 다시 상태 변경·알림하지 않는다.
- `schedule_registered_projects`에서 self PR reconciliation과 handoff를 병렬 작업 제출 전의 단계로 배치한다. handoff 필요 시 pool에 새 프로젝트 작업을 제출하지 않고, 필요 없을 때 기존 `max_workers` 동시성과 독립 프로젝트 처리를 유지한다. 프로젝트 처리 중 새 Agent·verify 시작 경계에도 handoff 신호를 확인한다.

## Validation

- Worker는 `tests/test_origin_isolation.py`의 runtime child identity, `tests/test_loop_manager.py`의 PR Merge·scheduler 관련 targeted unit test만 실행한다.
- `main` 및 대체 base, self Merge 후 ahead runtime의 정상 종료, source sync skip·fail 뒤 종료, 이미 같은 SHA인 runtime의 계속 실행, fetch·identity·ancestry 오류, planning push 비트리거, 다른 clone·origin·symlink·runtime 경로 거부와 다른 프로젝트 Merge를 검증한다.
- 다중 프로젝트에서 self Merge가 병렬 작업 제출 직전에 확인되면 신규 Agent·verify·publication 및 다른 프로젝트 상태 변경이 시작되지 않고, 다음 외부 실행에서 저장된 세션·worktree·retry가 복구됨을 검증한다. 전체 verify는 process lock, exact-SHA bootstrap, PR 복구와 기존 동시 실행 한도를 유지해야 한다.

## Progress

- [ ] self repository·base identity 및 Merge handoff 판정
- [ ] 병렬 scheduling 전 안전 경계와 정상 종료 연결
- [ ] 재시작·다중 프로젝트·거부 사례 targeted test 추가

## Decision Log

- handoff의 원인은 현재 실행에서 새로 확인한 self PR Merge뿐이다. 원격 SHA 관측만으로 실행을 끝내지 않는다.
