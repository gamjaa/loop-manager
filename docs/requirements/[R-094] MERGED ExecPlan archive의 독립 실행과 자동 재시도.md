# [R-094] MERGED ExecPlan archive의 독립 실행과 자동 재시도

## Summary

실제 PR Merge와 SQLite MERGED가 확정된 ExecPlan의 archive를 프로젝트 전체 유휴 상태와 분리한다. Manager의 기존 조율 경계에서 실제 공유 Git 자원·게시·소유권 충돌을 확인해 처리하고, 연속 작업 배정이나 재시작 뒤에도 미완료 후보를 다시 확인한다. 내용 보존 이동·fast-forward 게시·중단 복구를 유지하며, 보류 이유를 확인할 수 있게 하고 archive 미완료가 다른 작업이나 프로젝트 완료를 막지 않게 한다.

## Context

MERGED ExecPlan이 활성 경로에 계속 남으면 사용자가 미완료 작업과 완료 작업을 구분하기 어렵다. 현행 archive는 PR 상태 동기화의 후속 처리에 연결돼 있고, 프로젝트 phase·Planner 점유·다른 활성 ExecPlan·Agent 작업 공간 상태·유지보수 cycle을 확인하는 공통 gate가 닫히면 후보 처리 전에 반환한다. 이 반환에는 archive 보류 사유가 남지 않는 경로가 있어, 다른 작업을 계속 진행하는 프로젝트에서 정리가 반복적으로 밀릴 수 있다.

이는 `loop_manager_internal/catalog/archive_service.py`와 `loop_manager_internal/application/project_service.py`에서 확인한 구조적 문제다. 참조 대화에서 보고된 누적 사건의 운영 DB·실행 로그를 이번 작성에서 확인하지 않았으므로, 특정 gate가 실제 누적의 유일한 원인이라고 확정하지 않는다.

보관된 [R-014](archive/%5BR-014%5D%20Automatic%20Completed%20ExecPlan%20Archiving.md)는 기본 archive 도입의 과거 근거다. 현재 [archive 계약](../ARCHITECTURE.md#merged-execplan-archive)을 유지하면서, 프로젝트 전체 유휴 조건을 실제 자원 충돌 판단으로 바꾸고 독립 실행 기회·재시도·보류 관측을 보강한다. Requirement 완료 판단과 archive는 별도 흐름으로 유지한다.

## Requirement

### 후보와 실행 조건

- GitHub에서 실제 Merge를 확인하고 SQLite에 MERGED를 반영한 뒤, 활성 경로에 남아 있는 ExecPlan을 archive 후보로 유지한다. PR Open·미Merge Close·Worker 완료·verify 또는 Review PASS를 Merge로 간주하지 않는다.
- 다른 Worker·verify·Reviewer의 진행, 독립 계획의 차단, Planner 실행 자리 점유나 유지보수 cycle의 존재만으로 archive를 금지하지 않는다. 해당 archive가 사용할 공유 Git 자원·원격 base 게시·소유권과 실제 경합하는지를 판단한다.
- 같은 원격 base의 다른 Manager 게시·미확정 Git 쓰기, archive 대상 자원의 수동 소유나 실행 중·종료 불명 writer 등으로 안전성을 확인할 수 없으면 해당 archive 쓰기를 보류한다. 기존 자원 잠금·소유권·작업물 보존과 실제 프로젝트 안전 차단을 우회하지 않는다. 다른 작업의 worktree·index·후보·session·검증 근거를 archive를 위해 수정하거나 초기화하지 않는다.
- 함께 호출되는 Requirement 식별 정보 교정 게시의 기존 안전 조건은 유지한다. 교정의 source·draft·planning 예약·writer 보호를 archive와 함께 완화하지 않으며, 교정의 실행 불가를 archive 전체의 실행 불가로 그대로 적용하지 않는다. 두 처리 사이의 실제 공유 자원 충돌은 계속 보류한다.

### 실행 기회와 자동 재시도

- PR 동기화로 MERGED를 반영한 뒤뿐 아니라 Manager의 기존 실행 조율 중 안전한 경계에서도 미완료 archive를 확인한다. 새 작업이 연속 배정돼도 실행 가능한 archive를 프로젝트 전체 작업·유지보수 cycle 종료까지 미루지 않는다.
- 자원 경합으로 보류했으면 해당 자원 사용 종료 뒤 다음 안전한 조율 경계에서 자동 재확인한다. 원격 조회·게시 실패는 기존 제한 재시도와 이후 실행의 재관측으로 이어가며, 같은 원인으로 진전 없는 반복을 만들지 않는다.
- 중단·Manager 재시작 뒤에는 저장된 MERGED 상태·현재 원격 catalog·archive 작업 근거로 미완료 후보를 자동 재발견하고 남은 처리를 이어간다. 앞선 실행에서 archive 진입 전 반환해 후보별 기록이 없더라도 대상에서 빠지지 않는다.
- 이 처리는 기존 Manager 조율 안에서 수행한다. 별도 Agent·ExecPlan·PR이나 상주 스케줄러·새 실행 명령을 추가하지 않는다.

### 게시 안전성과 복구

- 최신 `origin/<base-branch>`의 detached 임시 worktree에서 같은 파일명의 `docs/plans/archive/`로 이동한다. 현재 catalog의 최종 문서 hash·revision을 확인하고, 내용과 mode를 보존한 rename-only commit을 원격 base에 fast-forward 게시한다. 기본 checkout이나 진행 중 작업 공간을 편집하지 않는다.
- 원본 hash 불일치·목적지 충돌·양쪽 파일 부재·동일 ID 충돌·비일반 파일·revision 부재는 후보별 보류로 남기고 덮어쓰지 않는다. 안전한 다른 후보는 분리해 처리한다. 원본이 없고 목적지의 최종 내용·이동 근거가 일치하면 이미 완료된 원격 결과로 확인한다.
- commit 준비와 게시 직전에 원격 base·후보·소유권을 다시 확인한다. 원격이 전진하거나 후보가 바뀌면 낡은 결과를 게시하지 않고 최신 관측으로 다시 계산한다. force push·정리 commit의 자동 rebase·보호 정책 우회를 하지 않는다.
- 준비 commit·게시 결과·DB 반영이 중단돼도 기존 작업 근거와 실제 원격 결과를 대조한다. 같은 base·후보에서 유효한 준비 commit은 재사용하고, 이미 게시된 이동은 새 commit·push 없이 catalog·SQLite 확정을 마친다. 게시됐는지 불확실하면 성공이나 미게시로 추정하지 않고 재관측한다.

### 보류 관측과 비차단 처리

후보가 있는데 처리하지 못하면 해당 후보 또는 archive 실행에 연결된 이유와 재시도 조건을 기존 상태·로그에서 확인할 수 있어야 한다. 실행 경계에 진입하지 못한 경우도 포함하며, 다음을 구분한다.

| 보류 범위 | 확인할 이유 |
|---|---|
| 실행 기회 | 후보가 있으나 아직 안전한 실행 경계를 확보하지 못함 |
| 공유 자원 | 충돌하는 Git 자원·게시·소유권 또는 미확정 쓰기 |
| 원격 관측 | 원격 조회 실패·경쟁·게시 여부 미확인 |
| 개별 후보 | 파일·hash·경로·revision 등 후보 검증 실패 |
| 게시·확정 | commit 준비·push·catalog 또는 SQLite 확정 실패 |

- 동일 사유를 조율 경계마다 과도하게 출력하지 않으며, 사유 변경·실제 시도 결과·완료를 구분한다. Telegram은 [R-053](%5BR-053%5D%20이벤트%20시점에%20맞춘%20진행%20상황%20알림.md)의 사용자 행동 필요 조건을 유지한다.
- archive 미완료는 MERGED를 되돌리거나 READY 실행·Planner·dependency 충족·유지보수 종료·프로젝트 완료를 막지 않는다. archive 대기를 실행 실패나 새 사용자 판단 대기로 승격하지 않으며 실제 다른 차단은 유지한다.
- 내용 보존 archive 자체로 Planner 호출·새 작업 주기·유지보수 Merge 수·프로젝트 완료 알림을 만들지 않는다. 이동한 문서를 실행 후보로 되살리지 않고 ID·source·dependency·PR 불변 링크와 감사의 기존 호환성을 유지한다.

차단·복구 범위는 [R-055](%5BR-055%5D%20차단%20경로%20전수%20조사와%20불필요한%20작업%20중단%20축소.md), 병렬 조율과 공유 자원 보호는 [R-091](%5BR-091%5D%20프로젝트%20공정%20배정과%20우선순위%20기반%20병렬%20작업.md), 유지보수 종료는 [R-074](%5BR-074%5D%20유지보수%201회%20평가와%20즉시%20종료.md)를 따른다. 보류 관측은 [R-076](%5BR-076%5D%20운영%20명령어와%20상태%20조회%20및%20Skill%20지원.md)의 읽기 전용 조회와 연결할 수 있으며 별도 운영 명령을 요구하지 않는다.

## Acceptance Criteria

- 다른 Worker·verify·Reviewer가 진행 중이거나 독립 계획이 차단돼 있어도 실제 공유 자원 충돌이 없으면 MERGED ExecPlan을 archive한다. Planner 자리 점유·활성 유지보수 cycle만으로 전체 종료를 기다리지 않는다.
- 같은 base의 다른 Manager 게시·미확정 Git 쓰기와 충돌하면 양쪽 작업을 보존하고 이유를 남기며, 자원 반환 뒤 다음 안전한 조율 경계에서 자동 재시도한다. Requirement 교정 게시의 기존 안전 조건도 유지된다.
- 연속된 새 작업 배정 중에도 실행 가능한 안전 경계에서 이전 MERGED 후보를 처리한다. PR 동기화 직후 기회를 놓친 경우와 후보 기록 없이 진입 전 보류된 경우도 포함하며, Manager 재시작 뒤 자동 재발견한다.
- 실제 Merge·SQLite MERGED가 확인되지 않은 문서는 이동하지 않는다. 최신 원격 detached worktree에서 최종 hash·revision과 내용·mode를 보존한 이동만 fast-forward 게시하며 진행 중 작업 공간은 보존된다.
- hash 불일치·목적지 충돌·파일 부재 등 문제가 있는 후보와 안전한 후보를 분리한다. 문제 후보의 원인을 보존하면서 안전한 다른 후보는 archive한다.
- 준비·게시 직전 원격 경쟁, push 실패, push 직후 중단과 SQLite 확정 누락에서 실제 원격 결과로 재개한다. 이미 게시된 이동은 중복 commit·push하지 않고, 낡은 후보·불확실한 결과를 성공으로 확정하거나 다른 문서를 덮어쓰지 않는다.
- 후보별·실행별 보류 이유와 재개 조건을 조회할 수 있고 실행 기회·자원 경합·원격 조회·후보 검증·게시 및 확정 실패가 구분된다. 같은 사유의 반복 출력이나 사용자 조치가 필요 없는 반복 Telegram이 없다.
- 실패·보류 중에도 MERGED·독립 실행·Planner·dependency·유지보수 종료·프로젝트 완료와 기존 기록이 유지된다. 내용 보존 이동만으로 새 planning·작업 주기·Merge 수·완료 알림을 만들지 않으며 별도 Agent·ExecPlan·PR·상주 스케줄러를 추가하지 않는다.
