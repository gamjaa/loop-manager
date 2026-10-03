---
id: G-107
title: Project Blocked Remote Recovery and Diagnostic Publication
sources:
  - docs/requirements/[R-046] Blocked Plan Remote Reconciliation and Checkpoint Refresh.md
  - docs/requirements/[R-048] Evidence-Free Managed Rebase Recovery.md
depends_on:
  - G-106
complexity: high
risk: critical
priority: critical
---

## Purpose

계획과 연결된 `PROJECT_BLOCKED` 중에도 검증 가능한 대상 계획의 원격 변경 복구와 안전한 작업물 보존을 수행한다. 기존 작업 branch의 게시 계보가 불확실하면 그 ref를 덮지 않는 진단 branch를 사용한다.

## Acceptance Criteria

- 프로젝트 차단은 계획별 읽기 관측과 소유권이 증명된 복구 Git 쓰기의 일괄 금지 조건이 아니다. 해당 계획이 기존 변경 자리를 점유했거나 자리가 비어 있고 작업 범위를 독립적으로 검증한 경우에만 G-106의 후속 commit 채택·rebase·원인 재검증·checkpoint 갱신을 실행한다. 다른 계획의 실행, 새 Agent 시작, 일반 Planner 및 기본 worktree 쓰기는 기존 project 안전 gate를 유지한다.
- 원래 작업 branch의 게시 계보와 마지막 원격 OID가 확인되면 관측 OID에 결합된 fast-forward 또는 PR 없는 동일 소유 branch의 검증된 rebase에 한한 정확한 lease로 게시한다. 원래 프로젝트 차단이 남으면 상태와 이유를 유지하고 원격 checkpoint 결과만 기록한다.
- 기존 branch의 계보를 증명할 수 없어도 대상 작업물의 범위·Git 상태·rebase 완료를 검증할 수 있으면 원래 ref를 덮지 않는 결정적 진단 branch에만 게시한다. 같은 plan·작업물 identity의 동일 OID는 재사용하고 다른 OID가 이미 있으면 갱신하지 않는다. 진단 ref·원본 branch·base·원인·내용 OID를 durable하게 연결하며 PR을 만들거나 원래 branch 소유권·차단 해제 근거로 삼지 않는다.
- 작업 범위·Git 상태를 검증하지 못하거나 충돌 중이면 commit·push하지 않고 작업물·세션과 구체적인 local-only 이유를 보존한다. 원격 삭제·재작성·분기, 권한·branch protection 오류, 원격 경쟁 및 현재 plan·repository·worktree·target·원격 기대값을 확정할 수 없는 legacy 상태는 예상하지 못한 ref의 force push로 우회하지 않는다. 과거 rebase turn·stage 승인·receipt·replay·journal의 누락은 G-109의 현재 상태 판정을 거부할 사유가 아니다.
- 원래 차단 원인만 현재 입력으로 재평가하고 독립된 프로젝트 안전 차단·사용자 결정·다른 계획 차단은 유지한다. 원인 또는 원격·작업물 입력이 같은 반복 실행은 재게시·재검증·알림을 중복하지 않고, 입력이 바뀌면 다시 처리한다.
- 진단 branch 생성·checkpoint commit·push·SQLite 기록 사이에서 중단돼도 실제 원격 OID와 저장 intent를 대조해 같은 작업물을 한 번만 게시 완료로 확정한다. 알림에는 확인된 ref·OID 또는 실제 local-only 사유를 포함한다.

## Implementation

- `loop_manager.py`의 project 안전 gate에 대상 계획과 실행 자리로 한정한 복구 경로를 두고 G-106의 원격 채택·rebase·원인 검사 서비스를 재사용한다. rebase 진행·완료와 수동 해결은 G-109·G-110의 현재 상태 기준을 소비하며 project 예외용 journal/replay gate를 추가하지 않는다. `scoped_blocking.py`에서는 원인별 해제를 적용하고 관련 없는 project block을 자동으로 지우지 않는다.
- `blocked_checkpoint.py`와 저장 모듈에 원래 branch 게시와 분리된 진단 branch intent·결과를 추가한다. `notifications.py`의 차단 사건은 저장된 게시 결과를 참조한다. 원격 ref 생성은 Agent 밖 Manager의 정확한 이름·부재 검사로 제한한다.
- R-046의 project 차단 예외와 안전한 게시 규칙을 구현 PR에서 `docs/PRODUCT.md`에 반영한다.

## Validation

- Worker는 직접 변경한 `tests/test_loop_manager.py`, `tests/test_blocked_checkpoint.py`, `tests/test_blocked_checkpoint_state.py`, `tests/test_scoped_blocking.py`, `tests/test_notifications.py` 중 관련 targeted unit test만 실행한다.
- 실제 Git 이력으로 project 차단 중 base·branch 변경과 재개, 자동·수동 rebase 결과, 동일 ref checkpoint, 게시 계보 불확실 시 진단 branch, 진단 ref 충돌, 원격 경쟁, 충돌 중 local-only 및 push 후 SQLite 반영 전 재시작을 검증한다. rebase 수행 증거가 없어도 현재 상태가 정상인 작업물은 같은 보존 경계를 사용하고, 실제 소유권·범위 불명확은 local-only로 남겨야 한다. 실제 Git integration case는 Worker가 실행하지 않고 Manager 전체 verify에 맡긴다.
- 전체 verify는 다른 계획의 상태·실행 자리와 기본 worktree 보존, 진단 branch의 PR 미생성·소유권 비승격, 알림 중복 방지 및 최종 검증 gate를 확인한다.

## Progress

- [x] project 차단 중 계획별 안전 복구 경계
- [x] 진단 branch 게시·local-only 결과 및 중단 복구
- Worker: 현재 상태 수용·직렬 실행 자리·진단 ref 재사용·충돌·중단 복구·알림의 targeted unit test 통과. 실제 Git 회귀 테스트와 전체 verify는 Manager 실행 대기.
- Manager의 600초 verify 시간 초과를 진단해 Git fixture class의 전역 import로 중복 수집되던 실제 Git 테스트 72개를 제거했다. 기존 고유 검증은 모두 유지하고 수집 회귀 unit test와 Linux verify의 개별 case 진행 출력을 추가했다. 전체 verify 재실행은 Manager 대기.
- 중복 제거 후 Manager verify는 1,639개 case를 535.144초에 마쳤으며 세 건의 실패를 전달했다. legacy catalog 누락, 중간 게시 완료 후 lease 선택과 import fixture 누락을 수정하고 관련 최소 범위 unit test 5개를 검증했다. 실제 Git 회귀와 전체 verify 재실행은 Manager 대기.
- Reviewer의 읽기 전용 Git 실행 전 sandbox 오류를 재현했다. 승인 검토를 거친 읽기 전용 명령으로 기존 구현 diff와 근거 문서를 열 수 있음을 확인했다. 코드 결함 피드백은 없으며 Reviewer 재검토는 대기 중이다.
- 관리 rebase 수용 후 현재 HEAD `4ac0c98`과 공통 base `0d8b0a3`의 최종 diff 전체 및 현재 근거 입력을 다시 확인했다. 이번 후보에서 복구·게시·저장·알림·fixture 수집의 최소 범위 unit test 37개를 새로 실행해 모두 통과했다(0.815초). R-046 이동의 byte 보존을 확인했다. 확인 중 별도로 전진한 `origin/main`의 반영과 최종 verify·Review는 Manager 대기 중이다.
- runtime hotfix가 반영된 현재 HEAD `1e1e7c0`을 base `f26cb14` 대비 재확인했다. 현재 근거 입력과 최종 diff 전체를 다시 읽고 관련 최소 범위 unit test 37개를 새로 실행해 모두 통과했다(0.780초). 이번 후보의 Manager 전체 verify와 Review는 대기 중이다.
- Worker 결과 형식 재검증: 같은 현재 후보에서 관련 unit test 37개를 새로 실행해 PASS(0.703초)를 확인했다. 성공 보고 문구를 명확히 하고 기존 결과 parser의 수용을 확인했으며 Manager 전체 verify와 Review는 대기 중이다.
- Reviewer의 project-only 원격 채택 거부를 수정했다. 별도 PLAN_BLOCKED가 없어도 연결된 프로젝트 원인·계획·실행 자리와 현재 작업 범위를 확인해 후속 commit을 채택하고 base 반영·원인 재검증을 계속한다. 새 회귀 unit test 5개와 일반 계획 채택 회귀 3개를 포함한 현재 후보의 최소 범위 unit test 45개가 PASS(0.904초)이며 Manager 전체 verify와 Review는 대기 중이다.

## Decision Log

- `PROJECT_BLOCKED`의 일반 실행 제한은 유지한다. 이 계획은 R-046에 명시된 검증 가능한 대상 작업의 복구·보존만 예외로 허용한다.
- 2026-09-30: mutable READY snapshot의 기준 revision·원본 hash를 확인해 R-048 완료 정책을 연결했다. G-106을 통해 G-110·G-109를 선행하며 과거 rebase 작업 증거를 project 복구 또는 진단 게시의 필수 조건으로 재도입하지 않는다. 기존 branch의 게시 identity·원격 lease 검증은 유지한다.
- 2026-10-01: project 복구 orchestration은 `project_blocked_recovery.py`로 분리하고 G-106 원격 관측·후속 commit 채택 및 현재 rebase 상태 서비스를 재사용한다. 진단 intent·결과는 원래 branch 게시 소유권과 분리된 additive SQLite 테이블에 저장한다. 진단 ref는 검증된 push URL과 정확한 부재 lease로 생성해 원래 ref와 tracking ref를 변경하지 않는다. 원래 project 원인·투영·다른 plan 및 감사 기록은 보존하고, 현재 후보 검증 토큰만 무효화한다.
- 2026-10-01: project 예외 rebase는 명령별 `core.hooksPath`와 `rebase.updateRefs=false`로 hook과 다른 branch의 자동 갱신을 막는다. project 예외 push도 hook을 비활성화한다. 기본 worktree 및 다른 계획 ref의 쓰기 경계는 유지한다.
- 2026-10-01: verify 피드백 `8262527e49a77b20169ba26cc971a4d307032fe79ff83eb5b88e46bd1258594e`를 읽기 전용으로 조사했다. 04:56:03에 600초 제한으로 종료됐으며 마지막 04:55:38 runtime 출력 뒤 22개 case가 완료됐다. 그 다음 순서의 `test_final_assessment_uses_independent_pages_and_durable_evidence`는 단독 unit 실행에서 1.469초에 통과했다. 실행 없는 discovery에서 기존 1,710개 중 고유 case는 1,638개이고 `BlockedCheckpointGitAdapterTest`의 72개가 전역 fixture import 때문에 두 번 수집됨을 확인했다. fixture를 모듈 경유로 참조해 고유 검증을 보존하고, 재발 방지 unit test와 `-v` 진행 출력을 추가한다. 시간 상한은 변경하지 않고 중복 제거 후 전체 검증을 Manager에 맡긴다.
- 2026-10-01: verify 피드백 `5840f78b5b77c598bb47c64daf5306bcd816ff75610c1280afaf5bdaf5c7bbdd`의 세 실패를 진단했다. catalog에 없는 legacy 계획은 dependency 검사 전에 원래 project 차단을 보존한다. G-104 게시 완료는 원래 rebase 수용보다 checkpoint generation·원격 OID를 전진시키므로, intent·확인된 현재 acceptance·plan 게시 OID가 모두 일치할 때만 그 lease를 현재 범위 검사와 멱등 복구에 사용한다. 미확정·변조·원격 불일치는 허용하지 않으며 실제 현재 Git 상태 검사는 유지한다. planner import fixture는 누락된 `project_blocked_recovery.py`와 `runtime_scope.py`를 포함하고 직접 import 의존성 목록을 unit test로 검사한다.
- 2026-10-01: `review:agents.md::reviewer::review-environment-blocked`는 Git 명령 실행 전에 발생한 환경 오류다. 현재 Worker에서도 기본 실행이 같은 오류로 실패했으며 `/proc/self/mountinfo`와 경로 metadata에서 `/tmp/codex-daemon-1001`이 현재 사용자 소유이지만 mode `000`인 읽기 전용 tmpfs로 가려져 있음을 확인했다. 이 mount는 Worker에서 권한을 복구할 수 없다. `require_escalated`로 승인 검토를 거친 `git status`, `git log`, 실제 코드 diff 및 ExecPlan 읽기는 정상 종료했고 해당 명령 안에서도 차단 mount는 유지됐다. Reviewer는 승인 가능한 읽기 전용 명령으로 재시도하거나 Manager가 Agent 실행 환경을 재생성한 뒤 재검토해야 한다. sandbox·AGENTS 규칙과 검증 항목은 변경하지 않으며 이번 진단은 문서 기록만 추가하므로 테스트·verify를 재실행하지 않는다.
- 2026-10-01: 관리 rebase 뒤 재확인을 시작할 때 `origin/main`은 `0d8b0a39854901ffdc735816caab5ecbc0345846`, 확인 후보 HEAD는 `4ac0c98`이다. AGENTS·ExecPlan·PRODUCT·R-048·관련 ARCHITECTURE와 이 base의 활성 R-046 원본을 다시 읽고 base 대비 16개 파일의 최종 변경을 확인했다. 실제 Git integration은 실행하지 않고 `ProjectBlockedRemoteRecoveryTests`, `ProjectDiagnosticPublicationUnitTests`, `ProjectCheckpointNotificationUnitTests`, `BlockedCheckpointDiscoveryUnitTests`와 새 checkpoint 정책·저장 case 4개만 실행했다. 현재 후보의 37개 unit test가 0.815초에 실패 없이 통과했으며 이전 후보의 성공을 재사용하지 않았다. R-046 archive는 base 원본과 byte 단위로 같고 AGENTS·PLANS·ARCHITECTURE·R-048은 base와 동일하다. 마지막 확인에서 `origin/main`만 별도 runtime hotfix `f26cb14`로 전진했으나 HEAD와 공통 base는 그대로이고 G-107 구현·근거 입력은 변경되지 않았다. 기본 도구 실행은 정상 동작하며 최신 base 반영과 최종 verify·Review는 Manager에 맡긴다.
- 2026-10-01: 새 관리 rebase 후보 HEAD `1e1e7c0dc2e5aee8ad6dc722994efe878968edf0`과 base `f26cb14fb9b4790a5167d9ea5dc09a172780bb60`을 확인했다. 현재 AGENTS·ExecPlan·PRODUCT·R-048·관련 ARCHITECTURE와 base의 활성 R-046 원본을 다시 읽고 16개 파일의 최종 diff 전체를 재확인했다. 이번 HEAD에서 복구·게시·알림·fixture 수집 unit class 4개와 새 checkpoint 정책·저장 case 4개만 새로 실행해 37개가 0.780초에 실패 없이 통과했다. 과거 후보 결과는 현재 검증 증거로 재사용하지 않는다. R-046 이동은 base 원본과 byte 단위로 같고 AGENTS·PLANS·ARCHITECTURE·R-048 및 새 runtime hotfix 파일은 base와 동일하다. 추가 코드 변경은 필요하지 않으며 최종 verify·Review는 Manager에 맡긴다.
- 2026-10-01: Worker 결과 거부의 원인은 `TARGETED_TESTS`의 `실패 없음`을 `_targeted_test_failure_matches`가 `실패` marker로 인식한 보고 형식이다. 실제 테스트 실행은 `OK`였다. 코드와 결과 검사 규칙을 변경하지 않고 HEAD `1e1e7c0`·base `f26cb14`의 현재 입력과 전체 변경을 다시 확인한 뒤 같은 최소 범위 unit test 37개를 새로 실행해 `OK`(0.703초)를 얻었다. 최종 증거를 `PASS`·실행 명령·개수·시간으로 보고하며 `parse_worker_result`가 그 응답을 수용함을 확인했다. R-046의 내용 보존 이동과 보호 문서의 base 일치도 재확인했으며 전체 verify·Review는 Manager에 맡긴다.
- 2026-10-01: `review:project_blocked_recovery.py::projectblockedreconciler._adopt::project-only-remote-adoption`을 수정했다. G-106 채택 helper에 선택된 project 복구 context를 명시적으로 전달해, 기존 PLAN_BLOCKED 원인 일치 대신 저장된 프로젝트 원인·계획 identity·독립 계획 차단·실행 자리를 대조한다. synthetic 계획 차단을 만들거나 기존 독립 원인을 바꾸지 않으며 일반 PLAN_BLOCKED의 원인 검사는 유지한다. 원격 OID·게시 계보·로컬 HEAD·index·ignored 경로·origin·허용 변경·metadata 검사를 그대로 적용하고, fast-forward 전에 현재 계획의 기존 실행 base(`active_sha`, 없으면 `plan_sha`)에서 전체 작업 범위를 독립적으로 검사한다. 이 기준은 Requirement archive 원본을 유지하며 최신 base의 fetch·범위 검사는 후속 반영 단계에서 수행한다. 채택 거부는 원래 project 투영·오류를 덮지 않는다. 새 unit test는 실제 채택 helper와 독립 범위 경계를 사용하고 Git·관측 효과만 mock으로 대체해 project-only의 채택→base 반영→원인 검사, 반복 처리 억제, 별도 사용자 결정 보존, 범위·원인·자리 거부 및 일반 계획 차단 gate를 검증한다.
- 2026-10-01: 관리 rebase 후보 HEAD `166a3228b21892b294c47a84f27aaf53db9932c7`과 base `f26cb14fb9b4790a5167d9ea5dc09a172780bb60`의 현재 AGENTS·ExecPlan·PRODUCT·R-048·관련 ARCHITECTURE 및 base의 활성 R-046 원본을 다시 읽고 이번 수정을 포함한 17개 파일의 최종 diff 전체를 확인했다. 첫 targeted 실행은 테스트 fixture에서 두 계획을 동시에 활성화하려다 SQLite 단일 실행 제약에 걸렸다. 다른 계획이 실행 자리에 투영된 fixture로 수정하고 동일 명령을 재실행했다. 최종 코드에서 관련 unit class 4개·새 checkpoint 정책/저장 case 4개·일반 채택 case 3개만 실행해 45개가 `OK`(0.904초)였다. R-046 이동은 base 원본과 byte 단위로 같고 AGENTS·PLANS·ARCHITECTURE·R-048은 base와 동일하다. 실제 Git integration과 전체 verify는 실행하지 않았으며 최종 Manager verify·Review를 기다린다.

- 2026-10-01: Reviewer의 `project-recovery-fetch-refmap`을 수정했다. 프로젝트 복구의 후속 commit fetch에만 `--refmap=`을 전달해 `remote.origin.fetch`가 대상 밖 공유 ref를 갱신하지 않게 한다. 잘못된 ref mapping을 모사한 unit test와 실제 Git에서 소유 branch 외 모든 ref의 보존을 비교하는 Manager용 회귀 case를 추가했다. 현재 HEAD `598984e`와 base `f26cb14`의 근거 입력·최종 변경을 재확인하고 관련 unit class 4개와 개별 case 7개를 새로 실행해 46개 `OK`(0.924초, exit 0)를 확인했다. R-046 archive는 base 원본과 byte 단위로 같으며 실제 Git integration·전체 verify·Review는 Manager 대기다. AGENTS.md와 docs/PLANS.md는 수정하지 않았다.
