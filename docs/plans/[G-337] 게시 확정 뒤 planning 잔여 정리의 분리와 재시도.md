---
id: G-337
title: 게시 확정 뒤 planning 잔여 정리의 분리와 재시도
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
  - docs/requirements/[R-076] 운영 명령어와 상태 조회 및 Skill 지원.md
depends_on:
  - G-336
  - G-236
complexity: high
risk: high
priority: high
---

## Purpose

원격 게시·catalog/DB 적용과 writer 종료가 확인된 planning에서, 안전하게 별도 보존할 수 있는 worktree 제거 실패를 잔여 정리로 남기고 최종 자리 해제와 유효 작업 재개를 허용한다. 잔여 경로는 후속 planning이 재사용하지 않으며 기존 Manager의 안전 경계와 재시작에서 정리를 다시 시도한다.

현재 `_complete_publication_release()`는 remove 성공 뒤에만 최종 DB CAS를 호출한다. 게시 적용·외부 정리·CAS 분리와 제거 후 중단 복구는 이미 구현돼 있다. 이 작업은 그 구현을 소비해 제거 실패의 독립 보존을 추가하며 G-278의 미게시 실패 suspend나 G-282의 수동 경합 batch 분리를 대신하지 않는다.

## Acceptance Criteria

- R-082·R-055 B03: 원격 commit/게시 경로의 현재 보존, 적용된 catalog·목적별 기준과 publication identity, 현재 소유권·writer 종료를 확인한 뒤에만 잔여 정리를 분리한다. 미게시 변경·진행 중 operation·종료 불명 writer·분리 불가능한 공유 자원에는 기존 보류를 유지한다.
- 제거 실패를 분리할 때 소유 경로·publication/operation·정리 단계·보존 근거·다음 조건을 durable하게 남기고 planning 자리 해제를 CAS로 확정한다. 이미 적용된 성공 기준을 되돌리지 않으며 Requirement 점검·부분 게시를 일반/프로젝트 최종 확인 성공으로 바꾸지 않는다. 해제 전에는 원래 작업을 재개하지 않는다.
- push 후 DB 미적용, 적용 후 분리 기록 전후, 자리 해제 전후와 제거 전후의 중단에서 같은 publication을 멱등 재개한다. 이미 끝난 Planner·commit·push·batch 적용·제거를 반복하지 않는다. 현재 owner/catalog/cycle/intent/수동 membership 경쟁에는 낡은 해제만 거부하고 경쟁 변경·원래 작업/session/오류·checkpoint를 보존한다.
- 정리 대기가 남아도 후속 planning은 그 경로·등록·예약을 덮어쓰거나 새 workspace로 채택하지 않는다. 현재 fingerprint 기반 경로/소유 identity를 유지하고, 같은 경로가 필요한 planning만 정리 조건을 기다리며 무관한 유효 작업은 계속한다. 기존 조율의 안전 경계·다음 실행에서 소유권/실제 경로·Git 등록·종료 근거를 다시 확인해 남은 정리만 수행한다. 새 사용자 편집·소유권 불명에는 삭제하지 않고 이유를 남긴다. 광범위 clean/reset·상주 정리 thread·새 CLI는 추가하지 않는다.
- G-236의 전체/프로젝트 pause를 분리 기록·해제·재시도 효과 진입에서도 준수한다. resume 명령 자체는 정리나 실행을 시작하지 않으며 다음 자동 실행만 남은 단계를 확인한다. 외부 관측/제거는 DB 밖, 확정은 기존 공통 DB 요청의 짧은 CAS로 수행한다.

## Implementation

- [ ] 확인한 `planning/publication_service.py:_complete_publication_release()`·`_recover_publication()`, `planning/state_ports.py:pending_publication_release()`·`validate_publication_release()`·`complete_publication_release()`와 `storage/planning_publication_state.py:retain_planning_after_publication()`의 적용·해제 identity를 재사용한다. G-336의 scoped gate를 소비하고 실제 제거 실패와 현재 안전 미확정을 구분한다.
- [ ] 확인한 `planning/planning_workspace.py`의 경로/identity와 `planning/workspace_service.py`의 준비·보존 경계에 남은 정리의 소유 근거·경로 배제를 연결한다. durable 정리 근거는 기존 publication/workspace 보존·저장 구조에 호환 확장하고 별도 상태 원본이나 비호환 migration을 만들지 않는다. 현 구조로 안전한 분리가 불가능한 사례는 보류한다.
- [ ] `application/project_service.py`·`planning/planning_runtime.py`의 기존 조율에 남은 정리 재확인을 연결한다. 새 planning·원래 작업 재개의 자격 판단과 정리만의 결과를 구분하고, 정리 조회를 위해 Git/DB 잠금을 장시간 보유하지 않는다. G-236이 Merge한 pause의 소유 scope와 저장 gate를 재사용한다.
- [ ] 확인한 `tests/test_planning_publication.py`, `tests/test_incremental_planning.py`, `tests/test_tiered_publication.py`, `tests/test_planning_selection.py`의 release·경쟁·workspace fixture와 G-236의 `tests/test_pause.py`를 확장한다. PRODUCT·ARCHITECTURE의 게시 완료/자리 해제·작업 공간 보존 절에 실제 구현만 통합한다.

## Validation

- Worker targeted unit은 변경한 release·경로 재사용·정리 재진입의 최소 case다. 실제 SQLite와 임시 파일을 사용하고 Git/프로세스 관측·remove 실패만 double로 제공한다. 기존 `test_applied_publication_recovers_after_cleanup_before_db_release`의 제거 후 CAS 복구, 독립 DB 쓰기와 이미 완료한 효과 0회 assertion을 유지한다.
- remove 실패에도 안전한 분리 기록→최종 해제→유효 작업 선택에 도달하고 다른 fingerprint의 planning이 독립된 소유 경로를 쓰는지 확인한다. 같은 경로를 요청한 planning은 덮어쓰지 않고 이유를 남기되 무관한 실행은 유지해야 한다. 재시작 뒤 잔여 정리 성공·새 사용자 편집/등록 경쟁·기록 손상은 각각 남은 삭제만 수행/보존해야 한다. old publication의 정리가 새 소유 상태를 해제하면 실패다.
- push·DB 적용·분리 기록·최종 CAS·제거의 각 중단을 기존 fixture에 연결한다. 일반/변경 없음 완료·Requirement 목적·유지보수 restart의 공통 코드는 재사용하고 목적별 identity 차이만 검사한다. 필수 writer/원격/소유 근거 부재에는 해제·새 호출 0회, 적용된 catalog·기준과 오류 감사는 불변이어야 한다.
- pause 접수·resume·다음 자동 실행에서 추가 게시/Planner·중복 제거가 없고, pause 범위 밖 독립 작업은 유지된다. 실제 Git worktree 등록/제거 효과는 기존 최소 통합 회귀에 남기고 정책 행렬마다 실제 repository를 준비하지 않는다. 정리 대기만으로 Telegram·실패/교정 소비·성공 receipt를 만들지 않는다.

## Progress

- [x] `9dd75b5d`의 게시 적용·외부 제거·최종 CAS와 기존 중단 회귀를 정적으로 확인했다. 제거 실패의 비차단 분리·후속 경로 배제/재시도는 미충족이며 구현·테스트는 실행하지 않았다.
- [ ] 잔여 정리 보존·최종 해제와 경로 재사용 방지
- [ ] 안전 경계/재시작·pause·경쟁/중단 회귀와 문서 정합성

## Decision Log

- G-336의 자원 판단과 G-236의 실제 pause 구현을 선행 소비한다. G-236의 진행 중 후보에 미Merge 정리 기능을 선행 요구하지 않고, 이 후속 작업이 새 분리 상태와 pause의 연결을 소유한다. 미게시 실패의 의미 판단·독립 선택은 G-278, 부분 게시의 보류 원문 보존은 G-282에 남긴다.
- complexity high: 원자적이지 않은 파일 제거·소유 예약·DB 해제를 재시작과 후속 planning에도 연결해야 한다. risk high: 조기 해제/경로 재사용은 중복 writer나 보존 작업물 훼손을 만들 수 있다. durable 소유 근거·현 형상 재검사·CAS·경쟁 거부와 효과 무반복으로 완화한다. 자동 후속 진행을 막는 정리 장벽의 교정으로 priority high, 최초 level 5다.
