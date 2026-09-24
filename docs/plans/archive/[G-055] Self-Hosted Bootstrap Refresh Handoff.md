---
id: G-055
title: Self-Hosted Bootstrap Refresh Handoff
sources:
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
depends_on:
  - G-051
  - G-052
  - G-074
complexity: high
risk: high
priority: critical
---

## Purpose

현재 source bootstrap은 이전 revision이어도 최신 runtime 자식을 바로 시작한다. 이 경우 process lock과 인자 선택은 구 코드가 소유해 새 self-hosted 변경의 안전 계약이 적용되기 전에 자동 실행이 진행될 수 있다.

## Acceptance Criteria

- process lock 안에서 등록된 self `<base-branch>`를 해석하고 `origin/<base-branch>`를 먼저 fetch한다. source SHA가 원격 SHA와 같으면 dirty 여부와 관계없이 같은 SHA의 runtime 자식을 시작한다.
- source가 behind-only이고 G-074의 safe fast-forward가 가능하면 staged·unstaged·untracked·ignored 중 원격 변경과 겹치지 않는 로컬 변경을 보존해 갱신한 뒤 runtime·PR 동기화 없이 정상 종료한다. 다음 외부 실행에서 새 bootstrap이 시작된다.
- overlap dirty, ahead/diverged, 다른 branch, Git operation, 동시 변경 또는 Git 거부는 사용자 상태를 보존하고 진단 가능한 실패로 종료한다. 구 bootstrap으로 최신 runtime을 시작하지 않는다.
- runtime 자식 시작 시 bootstrap·runtime·원격 SHA가 일치한다. 기존 CLI 인자, config 경로, lock, SQLite 저장 형식과 일반 프로젝트의 원격 기준 계약은 보존한다.

## Implementation

- self bootstrap의 base branch 선택과 fetch를 runtime 준비보다 앞으로 옮기고 `main` 상수 경로를 등록 상태로 해석한다.
- G-074의 dirty 보존 safe fast-forward 검사와 Git 충돌 검출을 재사용해 source 전용 handoff 결과를 명시한다. reset·stash·강제 checkout은 사용하지 않는다.
- architecture의 bootstrap 순서와 달라지는 부분은 R-023에 근거한 사용자 승인 상태를 Worker 시작 전에 확인하고, 승인 전에는 Architecture Change Policy에 따라 중단한다.

## Validation

- Worker는 `tests/test_origin_isolation.py`의 bootstrap/runtime, `tests/test_loop_manager.py`의 safe fast-forward 관련 targeted unit test만 실행한다.
- equal SHA + dirty, behind-only + staged·unstaged·untracked·ignored non-overlap dirty, overlap·ahead·diverged·다른 branch·Git operation·race·Git 실패, `develop` base branch와 runtime 미시작을 검증한다.
- 전체 verify에서 source checkout 보존, process lock, config 전달과 self-hosted scope 복구가 유지돼야 한다.

## Progress

- [ ] self base 해석과 갱신 handoff 도입
- [ ] 안전·실패 경로 targeted test 추가

## Decision Log

- R-023은 현재 ARCHITECTURE의 `origin/main` runtime 즉시 실행 순서를 바꾼다. Worker가 해당 변경을 시작할 때 사용자 승인 및 사용자 소유 ARCHITECTURE 정합성을 확인해야 한다.
