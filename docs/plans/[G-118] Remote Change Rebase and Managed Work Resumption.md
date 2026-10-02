---
id: G-118
title: Remote Change Rebase and Managed Work Resumption
sources:
  - docs/requirements/[R-051] Remote Change Rebase and Managed Work Resumption.md
depends_on: []
complexity: high
risk: high
priority: critical
---

## Purpose

원격 base 변경 시 일반·유지보수 Planner 결과를 폐기하는 대신 관리 작업과 세션을 보존해 rebase·재검증 후 재개한다. 이전 Planner가 READY를 수정한 상태의 재개에서 시작 revision의 원본과 작성 결과를 혼동해 발생하는 원본 불일치 오류를 해결한다. Worker·Reviewer에도 동일한 원격 반영 원칙을 적용하되 문서 재확인은 R-051의 단계별 관련 문서 변경에 한정한다.

## Acceptance Criteria

- 일반·유지보수 planning의 작성·교정·검증·게시 및 구현·verify·Review·게시·차단 복구에서 관측한 원격 전진을 해당 관리 worktree에 반영한다. Agent·verify 사용 중 worktree를 변경하지 않고 다음 안전한 경계에서 반영한다.
- staged·unstaged·untracked 허용 변경, 기존 세션·결과·실패 감사를 손실 없이 보존해 rebase한다. 원격이 그대로이고 허용된 READY 개정만 남은 경우 원본 불일치로 거부하지 않고 미완료 단계부터 재개한다.
- Planner는 시작 revision의 원본, 작성 결과, 최신 원격 catalog와 SQLite 대상 권한을 구분한다. 동일 계획 변경·새 ID 충돌·dependency 변경과 대상 착수·소유·종료를 재검증하며 과거 snapshot 권한으로 덮어쓰지 않는다.
- Planner의 문서 재확인은 현재 작성·평가 중인 계획의 근거·목표·범위·검증 조건·dependency에 영향을 주는 문서 내용 변경에 한정한다. Worker·Reviewer는 현재 진행 중인 ExecPlan의 내용이 바뀐 경우에만 해당 변경과 작업 적합성을 확인한다. 무관한 문서 변경·내용 보존 이동·revision 전진으로 재확인을 반복하지 않는다.
- 후보 HEAD 변경 시 기존 전체 verify·Review 승인을 무효화하고 최종 후보에서 필요한 Worker targeted test, Manager 전체 verify와 읽기 전용 Review를 거친다. 변경 없는 유지보수 검토 근거는 원본 identity와 관계의 유효성을 확인해 재사용한다.
- rebase 충돌은 기존 역할·허용 범위 안에서 해결하고 미해결 상태를 보존한다. 원격 재작성·삭제, origin·소유권·범위 불명확과 독립 차단·승인 대기는 기존 정책에 따라 처리하며 rebase 완료만으로 해제하지 않는다.
- 게시 직전 원격을 재확인하고 재전진은 보존·재시도로 처리한다. 안정되지 않는 원격은 일시 중단하며 planning fast-forward, 구현 lease·PR·중간 checkpoint 규칙을 유지한다.
- rebase와 push 전후 중단은 현재 Git·원격·저장 상태를 대조해 멱등하게 복구한다. 이미 반영된 base, 완료 호출·검증·게시와 catalog 적용을 중복 확정하지 않는다.
- 기본 checkout의 파일·index·HEAD, 프로젝트별 단일 변경 자리, dependency·유지보수 장벽과 Agent 권한을 보존한다. 로그로 이전·새 base, 후보, 재개 단계와 남은 원인을 확인할 수 있다.

## Implementation

1. 최신 원격에서 `planning_runtime.py`의 오류 gate·workspace 준비·결과 검증·게시·복구 경로, `loop_manager.py`의 READY snapshot 및 planning Git/validation ports, 실행 handler와 관리 rebase·게시 경계를 조사한다. 기존 모듈 분리가 먼저 Merge됐으면 실제 책임 소유 모듈로 경로를 대응하고 이 계획의 목표·범위를 보존한다.
2. 원격 관측과 반영 판단을 Agent 실행·변경 자리·origin·workspace identity 검사와 분리된 테스트 가능한 경계로 구성한다. 원격 base, 후보 HEAD, 작업 branch 및 PR 상태를 구분하고 관측만으로 성공·차단 해제 상태를 바꾸지 않는다. 같은 대상 전진은 한 번만 반영한다.
3. planning 재개용 원본 snapshot을 저장된 시작 revision의 Git blob과 SQLite 권한에 결합해 복원하고 작성 결과는 별도 diff로 검증한다. 처음 workspace를 준비하는 원본 일치 검사와 이미 Planner가 변경한 결과를 재개하는 검사를 분리한다. 보존된 세션·호출·scope 근거를 확인하고 범위 밖 결과를 채택하지 않는다.
4. 미커밋 작업 보존, 관리 rebase 시작·충돌·완료와 재개 판정을 기존 Git 복구 경계에 연결한다. 보존 자료와 현재 worktree의 대응을 검증하고 완료 전에 자료를 삭제하지 않는다. 현재 상태 판정을 재사용하며 새 Worker 호출·receipt를 Git 완료 조건으로 추가하지 않는다.
5. 최신 원격 catalog와 대상 상태·소유권으로 planning 권한을 재검증한다. ID 충돌·동시 계획 개정은 같은 Planner 세션에 필요한 교정만 전달한다. maintenance mode·cycle 및 source-bound 파일 검토 근거를 검증하고 최신 입력에 유효한 근거만 유지한다.
6. 변경된 문서의 내용 identity와 현재 작업의 관련성을 판단하는 좁은 경계를 둔다. Planner의 관련 근거·의존성 변경과 Worker·Reviewer의 현재 ExecPlan 변경을 구분하고, 무관한 문서 변경은 추가 확인 호출을 만들지 않는다. 현재 계획의 변경된 승인·dependency 조건은 기존 gate에 전달한다.
7. 후보 변경에 필요한 targeted test·전체 verify·Review 무효화와 재실행을 기존 실행 단계에 연결한다. PR Merge·Close 및 이미 게시된 planning 결과를 먼저 확인해 종료 작업을 재개하지 않는다. 게시 경쟁·fetch 실패는 기존 일시 재시도 경계를 사용한다.
8. PRODUCT의 장기 규칙을 구현 결과에 맞게 반영한다. ARCHITECTURE의 planning 폐기·재시작 경로는 사용자가 정확한 diff를 승인해 별도 문서 commit으로 반영했다. Worker는 ARCHITECTURE.md를 직접 수정하지 않고 승인된 설계를 기준으로 구현·검증을 이어간다. 추가 설계 변경이 필요하면 diff와 사유를 Decision Log에 기록해 사용자 결정을 받으며, 비호환 schema·권한 확대 등 별도 승인 변경은 포함하지 않는다.

## Validation

- Worker는 수정한 경계에 대한 최소 targeted unit test만 작성·실행한다. `tests/test_planning_runtime.py`, `tests/test_maintenance_planning.py`와 해당 snapshot·rebase·execution test의 실제 소유 모듈에서 아래 시나리오의 개별 case를 선택한다.
- 동일 원격에서 READY 개정이 남은 재개, 범위 밖 개정 거부, 작성·교정·검증·게시 중 원격 전진, staged·unstaged·untracked 보존, 충돌 및 단계별 중단·재시작을 검증한다.
- 동일 READY 동시 변경, ID·dependency 충돌, 대상 착수·소유·종료, 유지보수 mode·원본 검토 근거 보존, 관련·무관 문서 변경과 내용 보존 이동의 확인 호출 횟수를 검증한다.
- Agent·verify 사용 중 rebase 지연, 후보 변경 시 verify·Review 무효화, 독립 차단 보존, 반복 전진·fetch 실패·원격 삭제·재작성·게시 경쟁 및 push 후 SQLite 확정 전 중단을 검증한다.
- Worker는 현재 구조를 따라 전체 검증 스크립트를 유지하고 직접 실행하지 않는다. Manager 전체 verify는 기존 origin 격리·권한·catalog·planning publication·관리 rebase·중간 checkpoint·Review 회귀와 새 재개 시나리오를 확인한다. Reviewer는 파일 수정·테스트 재실행 없이 전달된 결과와 diff를 검토한다.

## Progress

- [x] 기존 planning·execution 원격 반영 및 재개 경계 조사
- [x] 원본 snapshot과 Planner 결과 분리 및 보존 rebase 연결
- [x] 단계별 관련 문서 확인·검증 무효화·게시 복구 구현
- [x] targeted unit test 및 Manager 전체 검증 경로 유지
- [ ] Manager 전체 verify, 독립 Review 및 사용자 Merge

### 구현 경계 대응

| 계획의 조사 대상 | 현재 책임 소유 경계와 재사용 근거 |
| --- | --- |
| `planning_runtime.py`의 선택·원격 반영 | coordinator의 기존 순서를 유지하고 `selection_service.py`, `workspace_service.py`에서 소유 workspace를 보존한다. detached planning의 index·untracked 복원 journal은 `planning_rebase.py`에 둔다. |
| READY 원본·최신 권한 | 기존 `planner_validation.py`의 revision blob snapshot과 SQLite CAS 검사를 재사용하고 `maintenance_validation.py`에서 신규 checkout 검사와 재개 원본 복원을 구분한다. |
| Planner 호출·검증·게시 | `assessment_service.py`에서 완료 호출을 재사용하고 `publication_service.py`에서 미게시 후보를 보존한다. 기존 publication intent 복구가 이미 게시된 결과를 먼저 확정한다. |
| 관련 문서·유지보수 근거 | `planning_document_relevance.py`가 source·dependency의 blob을 비교한다. `maintenance_file_review.py`는 원본 identity가 같은 잠정 파일 검토만 새 revision에 재검증해 재사용하며 관계 검증은 기존 source-bound 경계를 따른다. |
| Worker·Reviewer 재확인 | `execution_revision.py`, `execution_revision_handler.py`에서 source 내용과 경로 이동을 Worker 재확인 입력에서 제외한다. 기존 후보 HEAD 토큰과 managed rebase acceptance가 targeted test·verify·Review를 무효화한다. |

루트 항목 추가·이동은 없으며 새 모듈과 테스트는 기존 패키지·테스트 경로에 둔다. Linux verify의 명시적 compile 대상에 새 모듈을 추가하고 전체 검증 실행은 Manager에 맡긴다. R-051은 사용자 지정 경로로 원문을 보존해 archive 이동한다.

재개 후보의 관련 unit test 101개가 통과했다. 잠정 검토 재사용과 충돌하는 기존 cycle 변경 테스트를 갱신한 뒤 같은 명령을 재실행했다. `test_planning_error_recovery.py`의 프로젝트 복구 시나리오는 폐기·새 세션 기대값을 보존·재개로 갱신했으며 통합 테스트 실행은 Manager 전체 verify에 남긴다. Worker는 전체 verify·Review·게시를 실행하지 않았다.

## Decision Log

- 2026-10-02: 사용자는 원격 전진을 어느 단계에서 관측하든 관리 작업을 rebase 후 이어가고, 단계별 관련 문서가 변경됐을 때만 재확인하도록 요청했다. Worker·Reviewer의 추가 문서 확인 대상은 현재 진행 중인 ExecPlan이다.
- 2026-10-02: 사용자는 R-051과 이 G-118 구현 계획의 main 게시 및 중단된 Manager 작업 재개를 승인했다. 이는 계획 게시와 운영 재개 승인이고 구현 PR의 자동 Merge 승인이 아니다.
- 2026-10-02: 현재 catalog의 마지막 예약 번호 G-117 뒤에 G-119~G-127이 게시돼 연속 ID 검사에서 중단됐다. R-051의 실제 구현 범위를 G-118로 정의해 누락을 해소한다. 기존 계획의 ID·본문·dependency는 변경하지 않는다.
- 새로운 운영상 장애의 안전한 복구를 우선해 priority를 critical로 둔다. 다른 계획과 충돌하는 코드·테스트 추출은 본 작업에 필요한 책임 경계로 제한한다.

- 2026-10-02: 사용자가 G-118의 ARCHITECTURE diff를 확인하고 반영을 승인했다. 이 정확한 문서 변경은 별도 사용자 결정 commit으로 게시하며 Worker는 해당 문서를 다시 수정하지 않는다. 기존 작업물·session과 범위 검사, Manager 전체 verify·Review 및 사용자 Merge 조건을 유지한다.
- 2026-10-02: 재개 세션에서 현재 HEAD 대비 후보 전체를 재검토했다. prepared commit 위의 미커밋 변경은 rebased commit에 먼저 복원하고 soft reset으로 검증 후보를 구성해 staged·unstaged 구분을 보존한다. stash apply·reset 직후 중단, 완료된 no-change 호출 재사용과 내용 보존 ExecPlan 이동을 targeted unit test로 검증한다. 보존 stash는 SQLite의 새 base 확정과 작업물 identity 확인 뒤에만 제거한다.
