---
id: G-166
title: 사용자 작업 branch 분기의 보존형 통합
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-060] Collaborative Git Recovery and Remote Blocked Snapshots.md
  - docs/requirements/[R-055] Blocking Path Audit and Minimal Interruption.md
depends_on: []
complexity: high
risk: critical
priority: critical
---

## Purpose

로컬·원격 작업 branch의 정상 분기를 후속 commit ancestry만으로 거부하는 checkpoint 채택을 사용자 작업 보존형 통합으로 바꾼다.

## Acceptance Criteria

- 확인된 사용자 변경은 Agent scope 위반으로 오인하지 않는다. 원격 work branch와 base가 함께 전진해도 로컬 commit·staged·unstaged·untracked 작업을 보존해 work branch를 먼저 반영하고 최신 base를 통합한다.
- 기본 checkout의 코드·index·HEAD·base ref 변화와 Manager 비소유 branch 생성·수정·삭제는 선택 자원이 유효하면 자동 작업을 막지 않는다. 이름 유사성으로 branch 소유권을 채택하지 않는다.
- 첫 충돌·일시 실패·중단은 기존 세션·marker·작업물 보존 복구를 먼저 시도한다. 동일 원인의 무진전 반복은 제한하고 사용자 원격 push·수동 해결은 정상 실행/재시작에서 재관측해 완료한 반영을 중복하지 않는다.
- 실제 origin·공유 자원·Agent 위반·불확실한 사용자 변경은 계속 거부한다. 바뀐 후보는 기존 검증·Review·게시 gate를 충족하고 해결된 원인만 해제한다.

## Implementation

- recovery/checkpoint_recovery.py·checkpoint_service.py와 현재 managed rebase·scope provenance helper를 재사용한다. 사용자 branch 입력, local 보존 자료, 원격 lease와 반영 완료 상태를 결합한다.
- 단순 HEAD ancestry 대신 안전하게 확인한 입력·소유권·반영 증거로 재개 여부를 판단한다. 로컬 base 동기화는 R-056/G-161과 분리하고 미완료 rebase를 새 성공으로 채택하지 않는다.
- 문서·권한·게시와 검증의 공통 경계는 AGENTS.md, docs/ARCHITECTURE.md, docs/PLANS.md를 따른다. 필요한 기존 helper와 관련 테스트를 우선 재사용하고 새 루트 항목을 만들지 않는다.

## Validation

- checkpoint 복구·managed rebase·scope 회귀에서 양쪽 원격 전진+로컬 각 변경 형태, 사용자 수동 해결, 다른 branch, 중단·재시작 관련 최소 unit case를 실행한다.
- Manager 전체 verify는 실제 Git rename/delete/binary 보존, Agent 위반 거부, 이중 관측·lease·SQLite 확정과 독립 차단 보존을 검증한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [x] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 통합·출처·재시작 판단의 결합 때문에 complexity high, 사용자 작업 손실 가능성 때문에 risk critical다. PR에 대한 checkpoint 게시와 충돌 진단은 G-167·G-168로 분리한다.
- 원격 작업 branch의 검증된 사용자 후속 입력과 로컬 전용 작업은 `recovery/work_branch_integration.py`에서 기존 baseline tree·managed rebase·SQLite checkpoint를 사용해 통합한다. staged·unstaged·untracked 원형은 baseline ref와 intent에 남기고, 작업 파일을 reset·clean하지 않고 보존 commit으로 고정한 뒤 작업 branch와 최신 base를 순서대로 반영한다. 새 루트 항목·설정·스키마는 추가하지 않는다.
- 실제 저장된 Agent 결과는 먼저 검증한다. 이후 사용자 입력의 정확한 blob과 선택 자원의 전이만 비교 기준에 반영하며, legacy 결과에서도 무관한 기본 checkout·비소유 branch의 사후 변화는 범위 밖으로 둔다. Agent 출처가 불확실한 로컬 파일·index 변경과 실제 origin·공유 자원·Agent 위반은 계속 거부한다. 변경 후보의 검증·게시 token을 무효화하고 독립 차단·session·기존 결과 감사는 보존한다.
- 새 사용자 push로 소유 rebase를 재시작할 때 충돌 파일의 tree와 모든 index stage를 ref에 보존한 뒤 해당 operation만 abort한다. 충돌 자료를 정상 구현에 무조건 적용하지 않으며, 수동 완료·중단된 확정은 현재 상태와 원격 lease를 재관측해 중복 반영을 피한다. 원격 진단 게시와 PR 확장은 이 계획에 포함하지 않는다.
- targeted 검증: `python3 -m unittest tests.test_work_branch_integration tests.test_checkpoint_adoption.BlockedCheckpointRefreshUnitTests` 32개 통과. 테스트 입력·대기 intent 기대값·Remote fixture 형식의 세 실패를 수정하고 동일 명령으로 재검증했다. `scripts/verify.sh`는 기존 전체 검증 runner의 모듈 탐색으로 새 테스트를 포함하므로 변경하지 않았다. 실제 Git 양쪽 원격 전진·rename/delete/binary·사용자 push 회귀는 추가·갱신했으며 Worker는 실행하지 않았다.
- Manager 검증 피드백 대응: work branch 채택 뒤 base rebase 시작·재시작에는 재관측한 lease를 직접 전달한다. tracking ref가 이전 OID여도 그 값을 새 원격 기대값으로 사용하지 않는다. project의 독립 scope 검사는 로컬 반영 전에 수행해 거부 시 HEAD를 보존한다.
- 사용자 입력 정규화는 scope ID·시작 digest와 완료된 격리 ledger가 있는 실행에 한정한다. 기존 Agent scope 차단은 저장된 결과를 지우지 않고 현재 상태 교정 경로로 재검사하며, 동일 HEAD에서는 ancestry Git 호출을 추가하지 않는다. 명시적인 통합 전 검사에서는 실제 저장된 Agent 결과를 계속 검증한다. rebase scope predicate는 기존 순수 경로 helper를 사용한다.
- 보존 tree를 index에 적용한 뒤 stat 정보만 refresh하고 작업 파일이 달라지면 rebase를 시작하지 않는다. 원래 index·working tree와 보존 intent는 유지한다. 관련 unit 회귀에 stale binary stat, refresh 실패, 같은 HEAD의 무명령 재평가와 scope 교정을 추가했다.
- 피드백 수정 후 targeted unit 58개(통합·checkpoint 채택 모듈, scope baseline class, legacy/Agent 위반/project 거부/소유권 관측/rebase predicate 개별 case)와 pending rebase 시작·기존 상태 재사용 개별 2개가 각각 통과했다. 이번 targeted 실행에는 실패가 없었다. 전체 verify와 실제 Git 통합 테스트는 실행하지 않았다. 전달된 Planner FUTURE_IDEAS 경계 실패는 이번 변경과 관계없는 planning 경계이므로 수정 범위에 추가하지 않았다.
- 추가 검증 피드백 대응: 원인 미상·legacy 차단은 사용자 입력 정규화에 넣지 않는다. 진단된 Agent 결과가 아직 위반이면 정상 차단 재평가에서 기존 commit 격리·교정 경로를 유지하고, 통합 전 `require_safe_result` 검사에서는 즉시 거부한다. 원래 감사 기록과 사용자 작업을 정상 입력으로 덮어쓰지 않는다.
- Planner FUTURE_IDEAS 실패의 이전 범위 외 판단을 정정한다. 공유 `changed_files`의 unstaged name-only 조회가 stat만 바뀐 파일을 변경으로 보고했으므로 기존 내용 기반 numstat helper를 재사용했다. index를 refresh하지 않으며 staged·untracked·commit 변경과 특수 문자를 포함한 경로는 보존한다. Planner 정책이나 기능은 변경하지 않는다.
- 해제 직후 중단 회귀는 현재 reconciler의 transaction 완료 경계를 사용하고 올바른 Manager checkpoint fixture를 전달한다. 보존된 Agent 감사와 채택된 후보 비교 기준을 함께 검사한다. targeted unit은 사용자 입력·legacy 교정·파일 관측·해제 중단 13개와 기존 scope baseline·채택 거부·Worker 재개·baseline 복구 19개가 각각 통과했다. 실제 Git 회귀와 전체 verify는 Manager가 재검증한다.
- 사용자 문서 충돌의 보존·재시작은 최종 변경 승인과 분리한다. 저장된 사용자 입력 경로의 nonzero index stage와 현재 소유 rebase의 branch·원래 HEAD·target metadata가 일치하면 충돌 파일을 보존 대상으로 허용한다. 전체 관측의 identity·lease·mode·index 검사는 유지하며, stage가 사라지면 정확한 사용자 blob 또는 기존 Worker 문서 권한·승인 계약을 다시 요구한다.
- 보존 ref 갱신 뒤 또는 rebase 시작 직전 중단된 intent에는 원래 baseline·index·작업 파일·보존 commit을 그대로 재사용한다. 재관측·fetch·후속 ancestry·선택 branch/worktree 소유권을 확인한 새 push로 원격 lease를 바꾸고 보존 적용 검사를 재실행한다. 이전 입력은 history에 남기며 새 로컬 변경·다른 operation·재작성은 계속 거부한다.
- 현재 base와 재생 변경의 파일 관측 helper 연결 충돌은 base의 `_unstaged_git_paths` 이름과 양쪽 회귀를 보존해 해결했다. 통합·현재 rebase adapter·중단 원격 관측 class 및 파일 관측 개별 case를 실행해 28개 unit test가 통과했다. 새 거부 테스트가 기존 Worker 허용 PRODUCT 경로를 선택한 오류를 Requirement 경로로 수정하고 동일 명령으로 재검증했다. 문서 충돌 후 해결 push와 두 중단 지점의 실제 Git 회귀를 추가했으며 Worker는 해당 테스트·전체 verify·commit·rebase continuation을 실행하지 않았다.
- 남은 중단 회귀의 Manager 진단에서 원격 관측이 없는 fixture가 legacy 해제 경로를 실행했음을 확인했다. 실제 프로젝트 진입 순서처럼 원격 관측을 먼저 등록해 현재 reconciler의 해제 경계에 중단을 주입한다. 대응 unit case도 공개 `resume_plan_blocks` 경로에서 실행하며 legacy fallback 호출을 거부하고 Manager checkpoint·baseline 감사와 hydration 뒤 채택 HEAD를 검사한다. 해당 개별 unit test 1개가 통과했으며 실제 Git 회귀와 전체 verify는 Worker가 실행하지 않았다.
- 관측 등록 이후의 Manager 진단에서는 수동 게시 fixture에 tree·commit identity가 없어 채택 뒤 소유권 검사가 정상 거부했음을 확인했다. 중단 회귀는 기존 dispatcher 기반 fixture로 게시 증거를 갖추며, unit도 실제 `owned_remote_oid` 판정을 사용한다. 유효한 게시 증거의 해제·중단·감사 보존과 증거가 없는 후보의 차단·감사 보존 개별 unit 2개가 통과했다. 소유권 조건이나 제품 복구 동작은 변경하지 않았으며 실제 Git 회귀와 전체 verify는 Manager 재검증 대상으로 유지한다.
- Review 대응: `adopted` 단계의 base rebase가 진행 중이면 원격을 재관측하고, fetch·후속 ancestry가 증명된 새 push는 채택 단계로 돌린다. 충돌 stage·작업 원형과 중단 intent를 보존하고 소유 rebase만 abort한 뒤 새 작업 branch와 최신 base를 반영한다. 작업 branch 충돌과 base 충돌의 보존·abort 경계는 같은 helper를 사용하며 독립 차단·Agent 감사·원격 재작성 거부는 유지한다.
- 정상 반영 tree는 기존 임시 index helper와 literal pathspec의 Git `add`로 정규화한다. 원형 baseline tree·원래 index는 별도로 보존하고 commit은 기존 `_commit_tree`의 명시적 Manager identity를 사용한다. 변환 실패는 선택 HEAD·index를 이동하기 전에 반환한다. 새 루트 항목·설정·verify 경로 변경은 없다.
- 해당 Review 수정의 targeted unit은 통합 class와 base 충돌 후 재시작·기존 pending rebase 개별 case 17개, checkpoint 채택·재작성 거부·base 보존 ref 개별 case 5개가 통과했다. CRLF·UTF-16 원형/정규화, 사용자 identity 없는 보존 commit과 base 충돌 뒤 원격 해결 push의 실제 Git 회귀를 추가했으며 Worker는 실행하지 않았다. 전체 verify와 실제 Git 회귀는 Manager 검증으로 남긴다.
- 원격 재관측의 rebase 병합에서는 실제 base rebase와 소유권이 일치하는 미착수 통합 intent의 후속 push 처리 조건을 함께 보존했다. 통합 intent가 없는 base 충돌에서 branch·base 동시 전진과, branch lease가 같은 경우 저장된 base target 유지의 unit 회귀를 추가했다. 통합·원격 재관측 class와 base 충돌 재채택·재시작·보존 ref 개별 unit 23개가 통과했으며 실제 Git 회귀·전체 verify·commit·rebase continuation은 실행하지 않았다.
- 문서 충돌 회귀의 첫 사용자 push 뒤 원격 관측을 갱신하고 저장된 OID를 확인하도록 fixture를 수정했다. 낡은 관측에서는 HEAD·checkpoint·Agent 감사를 보존하고 갱신 후 정확한 새 lease로 채택하는 unit 회귀를 추가했으며, 현재 후보의 관련 unit 70개가 통과했다. 원격 경쟁 검사는 유지하고 실제 Git 회귀·전체 verify는 Manager 검증 대상으로 남긴다.
- 충돌 재시작 저장·abort 직후 중단에 추가 후속 push가 결합돼도, 정확한 fetch가 진행 중인 채택·재시작 입력 모두의 후속임을 확인한 뒤 같은 branch/worktree/request의 intent를 갱신한다. work branch와 base 재시작은 기존 baseline·충돌 snapshot·원래 HEAD를 재사용하고 이전 대상을 history에 보존하며, 끝난 abort는 반복하지 않는다. 재작성·현재 소유권·Agent 위반 거부와 완료된 과거 base 기록의 비차단 회귀를 포함한 targeted unit 59개가 통과했다. 두 중단 지점의 실제 Git 회귀를 추가했으며 실행은 Manager 전체 verify에 맡긴다.
- 수동 `rebase --continue` 완료와 새 후속 push가 함께 있으면, 충돌 재시작 전에 현재 소유권·정확한 새 lease의 COMPLETE 판정을 transaction 안에서 다시 관측하고 checkpoint generation을 대조한다. 완료 HEAD와 원래 통합 target을 먼저 저장하고 이전 intent·원형 baseline을 history에 보존한 뒤 그 HEAD에서 새 입력을 통합한다. 완료된 Git 작업을 abort하거나 재생하지 않으며, rebase 시작·재시작 뒤에는 최신 generation으로 완료 service를 다시 구성한다.
- 현재 base 대비 최종 diff와 근거 문서를 재확인하고 통합 unit class 31개, checkpoint 채택 unit class 22개, 보존 stage·blob·사용자 scope·문서 계약·원격 재관측·감사·파일 관측·소유권 관련 좁은 unit 26개가 통과했다. 완료 확정 전후 중단과 추가 push, 관측·generation 변경 및 소유권 거부 회귀를 포함한다. 수동 완료 후 새 push의 실제 Git 회귀를 추가했으며 Worker는 실제 Git 통합 테스트·전체 verify·commit·push·PR 생성을 실행하지 않았다.
