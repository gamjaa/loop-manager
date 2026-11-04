---
id: G-354
title: 프로젝트 Git 순번의 요청 재등록과 반환
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
  - docs/requirements/[R-094] MERGED ExecPlan archive의 독립 실행과 자동 재시도.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
depends_on:
  - G-353
complexity: high
risk: high
priority: high
---

## Purpose

프로젝트별 Git FIFO와 프로세스 자원 잠금 대기를 유한 요청으로 조율한다. 같은 프로젝트의 접수 순번을 유지하며 차례·자원이 없으면 반환하고, 다른 프로젝트의 Git 및 독립 실행은 계속한다. 현재 `_git_turn()`과 `resource_locks()`의 condition 대기를 G-353의 재등록 경계에 연결한다.

## Acceptance Criteria

- R-091/R-094: fetch·commit·push·rebase·archive가 같은 프로젝트 FIFO에 참여한다. 앞선 유효 archive/복구를 뒤의 요청이 추월하지 않으며 같은 요청 재확인은 identity·순번을 바꾸지 않는다. 새 후속 Git 요청은 실행 작업의 높은 우선순위와 관계없이 뒤에 접수한다.
- 차례 미도래·실제 잠금/소유 자원 대기는 thread나 열린 DB transaction 없이 반환하고 조건/재확인 시점에 재등록한다. A의 대기 동안 B의 Git·Agent·verify·DB/알림 조율이 진행하며 무변화 busy loop가 없다.
- 순번 확보 후 기존 `git:project` 및 구체 프로세스 잠금·writer 종료·원격 OID/정확한 lease·journal·소유권을 재검사한다. 순번이 게시 권한이나 operation 성공이 되지 않는다. 같은 thread/실행 구간의 중첩 scope는 실제 열린 lease만 재사용하고 모델/verify 대기에 Git 순번을 유지하지 않는다.
- planning 중 준비용 base-fetch 예외·pause·handoff 취소와 대기 시간 비소비를 유지한다. 재시작은 checkpoint/journal·실제 operation부터 복구하고 새 자원 요청은 FIFO를 다시 취득한다. 메모리 순번 유실로 완료/실패를 합성하거나 완료 commit/push를 반복하지 않는다.

## Implementation

- [ ] 확인한 `runtime/resource_lock.py:_git_turn()`·`resource_locks()`·`git_command_turn()`과 `application/task_scheduling.py`의 프로젝트별 git_waiters를 G-353의 유한 요청/완료 수집에 연결한다. 기존 context manager의 외부/중첩 호출 계약을 유지하면서 자동 작업의 대기는 반환 가능하게 한다. 프로세스 잠금과 메모리 순번의 책임을 혼동하지 않는다.
- [ ] 확인한 `git/manager_git_journal.py`·`application/project_service.py:_archive_scheduling_boundary()`의 기존 operation/예약·archive 재시도 근거를 소비한다. 새 FIFO 저장소나 archive 게시 엔진을 만들지 않고 요청 취소·반환·효과 확정 뒤 자원 수명을 연결한다. G-299/G-324의 fetch ref 보호·경쟁 통합은 범위 밖이다.
- [ ] `tests/test_task_scheduling.py`의 Git FIFO·DB transaction 거부·중첩 lease·취소/base-fetch 회귀와 `tests/test_execplan_archive.py`의 존재하는 archive 효과 회귀를 재사용한다. 새 조율 경계에 필요한 최소 조건을 연결하고 PRODUCT·ARCHITECTURE의 Git 순번 절을 통합한다.

## Validation

- Worker targeted unit은 기존 FIFO 관련 case와 변경한 자원 연결이다. 제어된 lease/실행과 실제 SQLite로 archive→fetch/게시 접수 순서, 다른 프로젝트 병렬, 같은 요청 재등록·새 후속 요청 순번, DB transaction을 보유한 대기 거부를 확인한다.
- 잠금 미확정·writer 종료 불명·planning drain/handoff/pause·취소/예외에서 한 조율 단계가 반환하고 예약/원래 순번 또는 취소 결과가 일관되는지 검사한다. 사라진 요청·중복 실행·유효 순번 추월과 대기 timeout 소비가 없어야 한다.
- 중단 후 이미 완료한 Git 효과는 기존 journal·원격 관측으로 확정만 이어간다. 실제 worktree/index·lease·archive rename-only 효과는 기존 최소 Git 통합을 유지하고 정책 행렬을 실제 저장소로 반복하지 않는다.

## Progress

- [x] 검토 revision의 Git FIFO·프로세스 잠금·archive 조율과 관련 회귀를 정적으로 확인했다. 구현·테스트는 실행하지 않았다.
- [ ] Git 요청의 유한 접수·대기 반환·재등록/완료 연결
- [ ] FIFO·중단·실제 잠금/operation 보존 회귀와 설계 반영

## Decision Log

- 기존 FIFO 자체는 구현돼 있다. 남은 목표는 thread 대기 제거와 재등록에서도 같은 순번/실제 소유권을 보존하는 것이다. R-094의 archive 독립 처리와 기존 네트워크/게시 계약을 재구현하지 않는다.
- complexity high·최초 level 5: 중첩 lease와 외부 프로세스 잠금·소유 operation·요청 수명을 중단 후에도 맞춰야 한다. risk high: 잘못된 차례/해제는 경쟁 Git 쓰기와 작업물 훼손으로 이어질 수 있다. 정확한 lease·기존 journal·소유권 재검사·FIFO/중단 회귀로 제한한다. priority high, Model·Effort는 Manager 설정을 따른다.
