---
id: G-271
title: Git 관측 비용 회귀의 테스트 환경 격리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-080] Git Mock Defaults for Tests.md
depends_on: []
complexity: low
risk: low
priority: normal
---

## Purpose

`tests/test_git_snapshot_cost.py`의 실제 `manager.git`·`capture_git_snapshot` 호출에 기존 공통 테스트 환경 격리를 적용한다. seed와 fixture의 `git_command`는 격리돼 있지만 이 테스트 class는 `isolated_git_test`를 적용하지 않아 후속 운영 adapter가 호출자 global/system 설정과 XDG ignore·attributes를 읽을 수 있다. 실제 Git 비용·index 보존 계약은 그대로 검사한다.

## Acceptance Criteria

- 실제 Git을 사용하는 비용 회귀의 seed·후속 `manager.git`·snapshot 및 필요한 자식이 `tests/support/git.py`의 같은 격리 환경을 사용한다. 사용자 hook·서명·template·fsmonitor·global/system 및 XDG 입력은 명시하지 않은 기본 fixture에 개입하지 않는다.
- 소유권 관측 공유와 확정 재관측의 6/4 snapshot·102/68 명령 비교, attached/detached worktree의 수집 대응, stale index와 실제 파일 변경·index bytes 보존 assertion 및 준비 비용 계측을 유지한다. 비용 계약을 맞추기 위해 실행 범위나 assertion을 줄이지 않는다.
- 테스트 범위 종료·setUp 실패 뒤 호출자 환경을 복원하고 사용자 설정 파일·운영 checkout 설정을 변경하지 않는다. 명시적인 설정 상속 검사는 기존 통제 입력 회귀대로 동작한다.
- 연결된 실제 Git 비용 회귀에 격리 누락이 없는지 확인하되 mock만 사용하는 테스트에 실제 Git 실행을 추가하지 않는다. 전체 수집·compile·skip·timeout 조건을 유지한다.

## Implementation

기존 `isolated_git_test`의 setUp부터 cleanup까지의 환경 전달을 재사용해 비용 테스트의 운영 adapter까지 연결한다. 기존 공통 helper·hostile 입력 회귀를 보강할 필요가 있으면 해당 소비 경계만 확인하며 운영 Git adapter의 환경 정책이나 사용자 설정을 바꾸지 않는다. 새 환경/fixture 체계는 만들지 않는다.

## Validation

- Worker는 격리 전달의 최소 unit case를 기존 `tests/test_git_fixture_seed.py`의 환경 double로 검사한다. 후보 비용 class의 운영 adapter 호출에도 통제된 환경이 전달되며 정상·실패 후 복원됨을 확인한다. 실제 Git 비용 회귀 실행은 Manager 전체 verify에 맡긴다.
- Manager 전체 verify는 `test_git_snapshot_cost.py`의 실제 비용·index·worktree 계약과 기존 seed/adapter/자식 hostile 입력·명시 설정 상속 회귀를 포함한다. Reviewer는 생산 코드 변경·필수 비용 assertion 제거·설정 상속 우회가 없는지 확인한다.

## Progress

- [x] 2026-10-13 기준 `20e6f0fbfdb6ceb5384d42c83f3904490b441356`의 공통 격리 helper와 비용 class를 대조했다. 다른 실제 Git 소비 class의 기존 격리 적용을 재사용하며 비용 class의 후속 운영 adapter 경계에 적용이 빠져 있다.
- [x] 코드·테스트가 같은 저장된 전체 verify `64fd60600657b242f833a1b1355aaab0`의 PASS는 해당 Linux 환경의 실행 근거다. hostile 사용자 입력에서 이 비용 class가 격리됐다는 증거로 사용하지 않는다.
- [ ] 소비 경계 격리와 최소 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- complexity low: 이미 구현된 환경 decorator를 누락된 소비 경계에 적용하는 국소 변경이다. risk low: 테스트 환경에 한정되며 기존 복원 helper와 환경 전달 unit 회귀로 확인하고 되돌릴 수 있다.
- 공통 seed 격리와 실제 비용 측정 자체는 이미 구현됐다. 환경 격리의 잔여 연결만 계획하며 mock 성공·기존 PASS를 사용자 설정 격리의 완료로 대신하지 않는다.
