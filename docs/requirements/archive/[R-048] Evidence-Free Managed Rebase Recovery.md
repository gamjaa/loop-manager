# [R-048] Evidence-Free Managed Rebase Recovery

## Context

G-088의 관리 rebase가 충돌 중 멈춘 뒤 ExecPlan 입력이 바뀌었다. 사용자가 관리 worktree의 충돌을 직접 해결하더라도 현재 완료 판정은 각 해결에 대응하는 Worker 교정 turn, 승인된 stage tree와 Manager journal의 재생 증거를 요구한다. 수동 복구에는 이런 과거 Agent 작업 증거가 생성되지 않으므로 Git 결과가 정상이어도 `managed_worktree_conflict` 차단을 해제할 수 없다.

R-045는 자동 rebase의 단계별 증거를 필수 조건으로 정의했다. 사용자는 2026-09-29에 차단 복구에만 예외를 두는 대신 **모든 관리 rebase에서 작업 수행 증거를 완료 조건에서 제거**하기로 결정했다. 충돌 해결을 Worker가 했는지 사용자가 했는지에 따라 완료 판정이 달라지지 않아야 한다.

## Requirement

Loop Manager는 모든 관리 rebase의 진행·완료·중단 복구에서 Worker 교정 turn, 승인된 stage tree, Git 명령 실행 receipt, 원본 commit별 재생 대응 및 과거 journal의 완전성을 필수 완료 증거로 요구하지 않는다. 관리 worktree의 충돌을 사용자가 수동으로 해결하거나 rebase를 계속·완료한 결과도 같은 현재 상태 검사로 수용한다. 기존 증거와 실패 감사는 삭제하거나 성공 증거로 꾸며내지 않고 보존한다.

Manager는 현재 Git 상태에서 대상 repository·계획·관리 worktree·소유 branch와 원격 기대값을 확인한다. rebase가 진행 중이면 해당 worktree의 충돌과 Git operation을 보존하고, 해결 뒤에는 최종 HEAD·branch ref·등록 상태·target base 포함 관계·index·작업 파일 범위·원격 lease를 다시 검사한다. 이 검사가 실패하거나 결과를 현재 계획의 소유 변경으로 한정할 수 없으면 차단을 유지한다.

Git 결과의 수용은 구현 완료 또는 게시 승인이 아니다. rebase 중 ExecPlan이나 근거 입력이 바뀌었거나 후보 revision이 달라졌으면 기존 검증·Review를 무효화하고 Worker가 현재 입력과 최종 변경을 다시 확인하며 관련 targeted test, Manager 전체 verify와 Reviewer 검토를 통과한 뒤에만 게시한다.

## Acceptance Criteria

- 일반 구현, 차단 해제, PR 충돌 복구, PR 없는 checkpoint branch의 관리 rebase에 같은 판정을 적용한다. 자동 Worker 교정과 사용자 수동 충돌 해결 모두 과거 교정 turn·stage 승인·명령 receipt·commit별 replay 증거가 없다는 이유만으로 거부하지 않는다.
- 저장된 rebase가 detached HEAD와 미해결 충돌에 멈춰 있어도 plan·repository·worktree·branch·target base·원격 기대 OID와 현재 Git operation의 관계가 일치하면 같은 worktree에서 해결·재개할 수 있다. 일치하지 않거나 다른 Git operation이 섞이면 기존 상태와 작업물을 보존하고 차단한다.
- rebase 완료 시 소유 branch의 HEAD·ref·worktree 등록, target base ancestry, clean index와 미해결 operation 부재, 허용된 파일·index 변경 범위, 현재 원격 branch/PR head의 기대 OID를 검사한다. 과거 작업 기록이 없다는 사실을 소유권 불일치로 간주하지 않지만 실제 상태 불일치나 범위 밖 변경을 허용하지 않는다.
- 충돌 해결 과정에서 commit 내용·개수·순서가 바뀌어도 작업 증거가 없다는 이유로 완료 판정을 막지 않는다. 최종 변경의 제품 적합성은 현재 ExecPlan 입력을 기준으로 Worker 확인, targeted test, 전체 verify, Review에서 다시 판단한다. 어느 단계도 생략해 구현 완료로 처리하지 않는다.
- G-088처럼 차단 후 ExecPlan 입력이 바뀐 사례에서 안전한 현재 Git 상태가 확인되면 `managed_worktree_conflict` 원인만 해제하고 필요한 충돌 해결·재검증을 재개한다. 별도의 Review 실패, 사용자 결정, Agent 범위 위반, 다른 계획·planning 차단과 기존 실패·알림 감사는 보존한다.
- Git 완료와 SQLite 반영 사이에 중단되거나 Manager가 재시작돼도 같은 현재 상태를 다시 검사해 완료를 한 번만 반영한다. 원격 경쟁, branch 재작성·삭제, worktree identity 불명확, 범위 밖 변경 또는 미해결 충돌은 자동 게시하지 않는다.
- 실제 Git 이력으로 자동·수동 충돌 해결, 여러 충돌, 계획 입력 변경, PR 유무, 중단·재시작과 원격 경쟁을 검증한다. 작업 증거가 없는 정상 결과는 진행하고, 상태·소유권·범위가 어긋난 결과는 차단하며, 재개 뒤 필요한 targeted test·전체 verify·Review가 다시 수행되는지 확인한다.

## Product Impact

- 사용자는 차단된 관리 worktree의 rebase 충돌을 직접 해결할 수 있으며, 과거 Worker 작업 증거가 없다는 이유만으로 복구가 막히지 않는다.
- 모든 관리 rebase는 현재 Git 상태와 최종 변경을 재검증한다. 기존 감사와 원격 게시 조건은 유지된다.

## Non-Goals

- 기본 source checkout에서 rebase하거나 Agent에게 Git 쓰기·push·PR 권한을 부여하지 않는다.
- 다른 계획의 branch 또는 worktree 변경, 범위 밖 파일·index 변경, 원격 경쟁을 자동으로 수용하지 않는다.
- 전체 검증·Review를 건너뛰거나 기존 실패 감사·checkpoint·journal을 삭제하지 않는다.
- 일반 강제 push, 자동 Merge, 비가역 migration 또는 새 infrastructure를 도입하지 않는다.

## Open Questions

- 없음
