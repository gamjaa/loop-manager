---
id: G-165
title: planning 오류 보존과 독립 작업 계속 실행
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-055] Blocking Path Audit and Minimal Interruption.md
depends_on:
  - G-163
complexity: high
risk: high
priority: critical
---

## Purpose

preserved planning_error가 실행 자리를 계속 점유하는 현재 gate를 바꿔 안전한 독립 구현과 PR 복구를 계속한다.

## Acceptance Criteria

- 실패한 planning의 변경 자원을 안전하게 비운 뒤 유효한 독립 READY·리팩터링 계획을 순차 실행하고 기존 PR 관측·충돌 복구를 계속한다. 같은 프로젝트의 실제 변경 실행은 여전히 하나다.
- planning 오류·session·작업 공간·미게시 결과·실패 피드백은 다른 작업의 진행·중단·재시작에도 보존된다. 동일 입력의 신규 Planner 반복과 프로젝트 완료·관련 Requirement 이동은 보류한다.
- planning 복구와 구현 슬롯을 구분하며 공유 자원·미확정 Git 쓰기·소유권 불확실에서는 안전한 release를 합성하지 않는다. 원인 해소 후 기존 planning 작업을 재검증·게시하고 해당 오류만 해제한다.

## Implementation

- planning/selection_service.py의 preserved-error gate와 storage/runtime_state.py의 슬롯 소유 판단을 명시적인 보존 오류와 활성 변경 상태로 분리한다. 기존 실행·checkpoint·session 저장소를 재사용한다.
- planning 실패의 결과·게시 intent를 구현 phase 필드로 덮지 않도록 책임 경계를 정리하고 scheduler가 대기·실패·진행을 정확히 반환하도록 연결한다.
- 문서·권한·게시와 검증의 공통 경계는 AGENTS.md, docs/ARCHITECTURE.md, docs/PLANS.md를 따른다. 필요한 기존 helper와 관련 테스트를 우선 재사용하고 새 루트 항목을 만들지 않는다.

## Validation

- tests/test_planning_selection.py, test_runtime_state.py와 planning 복구 테스트에서 오류 후 독립 Worker·PR 충돌·재시작·미확정 쓰기·같은 입력 no-call의 최소 unit case를 실행한다.
- Manager 전체 verify는 프로젝트별 단일 실행, planning 작업물·session 보존, 독립 프로젝트 지속과 완료 gate를 검증한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [x] 구현 및 관련 targeted unit 검증 (선택한 23개 case와 보존 작업 재검증 1개 case 통과)
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 여러 durable 상태와 실행 슬롯을 분리해야 해 complexity high, 오류 작업물과 실행 순서 손실의 영향으로 risk high다. 원격 진단 보존 자체는 G-168에서 구현한다.

- 기존 runtime 전이·planning workspace 소유권·Agent scope·Manager Git journal을 재사용한다. `projects.planning_preserved_state`는 additive TEXT 필드이며 실패한 planning의 projection·session·미게시 결과·피드백을 보존한다. 작업 공간을 이동·삭제하거나 새 루트 파일을 만들지 않는다.
- Worker projection 정리·PR 전이·Merge·차단 복구는 보존된 planning과 오류 incident를 변경하지 않는다. 입력 변경 때 빈 실행 자리에서 기존 projection을 복원하며, 이후 기존 workspace 재검증·교정·게시 경계가 해당 오류의 실제 해소를 판정한다.

| 경로 | 변경·유지 이유와 검증 |
|---|---|
| `planning/selection_service.py` preserved-error gate | 같은 입력은 신규 Planner를 호출하지 않는다. 확인된 실패 작업을 보존해 슬롯을 비운 뒤 독립 READY 선택 또는 대기를 반환한다. 활성 Worker·PR 교정은 선점하지 않는다. |
| `planning/git_ports.py`, `planning/state_ports.py`, `git/manager_git_journal.py` | 소유권·현재 Agent 범위·미종료 호출·planning worktree Git operation·미해결 index·미확정 공유 쓰기를 검사한다. 기본 checkout 전용 rebase·bisect는 실행 슬롯을 점유하지 않는다. prepared publication과 확인할 수 없는 workspace는 release하지 않으며 원인·결과를 보존한다. |
| `storage/runtime_state.py`, `storage/database.py`, `storage/execplan_catalog_state.py` | 보존과 실행 투영을 transaction으로 분리하고 catalog CAS에 보존 상태를 포함한다. 재시작·중단 rollback·Worker/PR/Merge 정리 뒤 원래 planning을 복원한다. 손상된 보존 상태나 stale snapshot은 슬롯 해제 근거가 아니다. |
| `catalog/project_selection.py`, `application/project_runner.py` | planning 오류에서도 catalog hold·dependency·PROJECT_BLOCKED·활성 실행 gate를 유지한 채 독립 READY를 순차 선택한다. post-refactor 재검증 대상과 일반 점검의 미확정 대상 및 전이 의존 계획은 목표 보존을 확정하기 전까지 선택하지 않는다. 보존된 planning 대기는 WAITING, 실제 Worker 오류는 실패로 반환하며 완료 판정은 보류한다. |
| `recovery/project_block_service.py`, `storage/runtime_state.py` rebase 복구 | 별도 planning 오류의 존재만으로 독립 복구를 막던 중복 gate를 슬롯 소유 판정으로 통일한다. 미해제 planning 슬롯과 대상 계획의 기존 소유권·dependency·scope·checkpoint 검사는 유지한다. |

- 관련 unit 검증은 `tests/test_planning_selection.py`, `tests/test_runtime_state.py`, `tests/test_catalog_scheduling.py`, `tests/test_manager_git_journal.py`의 변경한 class·case에 한정했다. 실행 결과 경계에서 누락된 import를 발견해 수정하고 같은 23개 case 명령을 재실행해 모두 통과했으며, 기존 session 복원 후 오류 해제를 별도 1개 case로 확인했다.
- 기존 `scripts/verify.sh`는 프로젝트 verification runner의 test discovery를 사용하므로 이번 테스트도 포함한다. 스크립트 변경이 필요하지 않으며 전체 verify는 Manager가 수행한다.
- Manager 진단에서 미확정 재검증 대상을 Worker로 시작하는 오류를 확인해 위 대상 gate를 추가했다. unit case는 MERGED 경유 전이 의존 계획까지 보류하고, 독립 READY 선택·planning 보존·복구 후 대상 선택 재개를 검증한다. 유지보수 fixture는 실제 cycle row를 생성하고 phase를 확인하도록 수정했다.
- 교정한 3개 unit case가 통과했다. 같은 실패 입력에서 반환값은 안전한 보존 전이 후 WAITING이며, 기존 session·결과·피드백과 Planner 호출 횟수는 유지됨을 확인했다. 전달된 maintenance integration 실패는 로그와 원본으로 분석했으며 직접 재실행하지 않았다. 전체 경로 재검증은 Manager가 수행한다.
- Review의 일반 planning 대상 누락은 기존 `planner_checks`의 미수용 결과를 scheduler에 연결해 수정했다. `needs_revision`·대상 ID가 있는 `uncertain`은 해당 대상과 MERGED 경유 전이 의존 계획을 보류한다. 대상 없는 불확실·미확정·손상된 점검은 미착수 READY의 유효성을 확정하지 않는다. 입력 변경·독립 실행만으로 인계·superseded 대상 보류를 해제하지 않는다. `storage/planning_input_state.py`의 일반 planning 성공 baseline transaction에서만 점검을 accepted로 확정하고 비교 revision을 갱신하며 결과·session 감사는 보존한다.
- 기본 checkout의 operation metadata는 그 checkout의 상태로 취급한다. 관리 planning worktree의 operation·index·소유권, Agent의 공유 ref·설정 범위와 Manager Git journal 검사는 유지한다. 로컬 Git port unit case에서 사용자 rebase·bisect metadata·HEAD·index를 보존하면서 안전한 release를 허용하고 미확정 공유 쓰기는 거부함을 확인했다.
- Review 대응 targeted unit 10개가 통과했다. 새 인계 테스트의 오류 반환·재시도 예상값을 실제 validation 소진 흐름에 맞게 수정한 뒤 동일 명령을 재실행했다. 성공 baseline과 대상 해제의 transaction rollback도 확인했다. 전체 verify와 integration 경로는 직접 실행하지 않았다.
- Worker 결과의 추가 거부는 보고 형식에서 발생했다. `TARGETED_TESTS`의 실패 시나리오 설명이 미해결 테스트 실패로 해석됐고 실패·재실행 이력은 해당 절 밖에 있었다. 기존 parser를 수정하지 않고 이력을 같은 절에 배치해 수용됨을 확인했다. 동일 targeted 명령을 다시 실행해 10개 모두 OK·종료 코드 0을 확인했다.
