---
id: G-159
title: 테스트 경계 준비와 Git 관측 재사용
sources:
  - docs/requirements/[R-070] 테스트 초기 환경 재사용과 Git 관측 비용 절감.md
depends_on: []
complexity: high
risk: high
priority: normal
---

## Purpose

R-070의 반복 준비와 동일 검사 구간의 중복 관측을 줄인다. 사용자가 직접 요청한 일반 PR 작업이며 자동 Planner 입력과 분리한 manual 계획으로 기록한다.

## Acceptance Criteria

- 활성 journal이 없으면 기록 전용 snapshot을 수집하지 않는다.
- 게시 검사에서 이미 얻은 rebase 관측을 소유권 소비자에 전달하되 현재 expectation과 checkpoint generation이 같을 때만 사용한다. 두 차례 안정성 관측, push 전후와 transaction 재검증은 보존한다.
- 선택 및 다른 등록 worktree의 Git 조회를 통합하고 index·파일·ignore·operation·소유권 감시 범위를 보존한다.
- 복구·게시·Review 변형의 공통 준비를 재사용하며 실제 전체 흐름과 기존 assertion의 대응 근거를 남긴다. 실제 파일과 Git·DB·journal을 독립시켜 이전 성공 결과를 검증으로 사용하지 않는다.
- 단계·호출 원인·준비 비용과 Git 명령 횟수를 계측하고 변경 전후 비교 및 회귀 검증을 남긴다.

## Implementation

1. journal 관측을 지연 평가하고 동일 게시 inspect 구간의 rebase 결과를 전달한다.
2. snapshot의 중복 Git 조회를 묶고 불완전한 결과를 거부한다.
3. 기존 fixture에 검사 경계 준비를 적용하고 소유 임시 자원과 SQLite만 복원한다.
4. 비용 진단과 집중 회귀 검증, 보호 계약 대응표를 기록한다.

## Validation

- Worker는 변경된 관측·journal·소유권·fixture의 targeted unit test만 실행한다.
- 실제 lifecycle, 게시 경쟁·중단·재시작 및 전체 비교 verify는 기존 Manager 실행 범위를 유지한다. Worker가 직접 전체 또는 integration 검증을 실행하지 않는다.
- Windows/Linux verify entrypoint는 기존 전체 manifest를 유지한다.

## Progress

- [x] R-070, 제품·설계·ExecPlan 규칙 및 기존 fixture와 관측 경로 확인
- [x] 구현과 targeted unit test 29개 통과
- [x] 일반 PR 게시: https://github.com/gamjaa/loop-manager/pull/150
- [ ] Manager 전체 비교 verify, Review 및 사용자 Merge

## Decision Log

- 2026-10-05: 기존 G-152/G-154 초기 복사·조회 통합을 재사용한다. 범용 cache 서비스와 실행 단계 사이 관측 cache는 추가하지 않는다.
- 2026-10-05: 전체 verify 실행 금지 규칙에 따라 전체 경과 시간 절감률은 Manager 측 측정 전 확정하지 않는다.
- 2026-10-05: 복원 fixture는 테스트가 직접 소유한 임시 경로와 SQLite만 재사용한다. 테스트 간 전역 준비 cache와 경로 치환 없이 같은 테스트의 직렬 변형만 복원한다. 다른 테스트·worker는 별도 경로를 쓴다. 복사본과 DB digest가 다르면 현재 자원을 지우기 전에 실패하며, 자료를 저장하지 않은 첫 실행은 기존 독립 준비를 수행한다.
- 2026-10-05: Git diff가 복원 후 index의 stat cache를 실제로 갱신하는 것을 파일 hash 비교로 확인했다. 읽기 전용 `git_bytes`의 diff에 `diff.autoRefreshIndex=false`를 적용하고 submodule 상태 조회의 optional lock도 끈다. 관측 자체의 index 변경을 막는 R-070 범위의 추가 수정이다.
- 2026-10-05: 최초 checkout에는 미게시 commit `53f7322`의 R-064/R-069 문서 변경도 있었다. PR은 원격 main `8531877`을 기준으로 R-070 원문과 이 구현만 포함한다. 기존 로컬 main commit은 보존한다.

## 보호 계약 대응

| 변경한 시나리오 | 공통 준비와 남은 검사 |
|---|---|
| `ManagedRebaseLifecycleTests`의 checkpoint/PR 정상·legacy journal 전후 4쌍 | 정상은 실제 첫 충돌 교정 뒤 다음 충돌 대기 상태, legacy는 실제 첫 충돌 대기 상태를 파일·Git·DB째 보존한다. 각 쌍을 기존 대표 메서드 안의 `sealed_restart=False/True` 변형으로 합쳤다. 두 변형 모두 마지막 충돌 해소, journal seal 전/후 중단, SQLite 미반영, 재시작, journal 불변·session·audit·중복 rebase/push 방지, Worker→verify→Review assertion을 유지한다. 기존 수동 완료·journal 부재·새 base·누적 rebase 흐름은 그대로 실행한다. 별도의 after-seal 테스트 ID 4개는 이 subtest로 대응한다. |
| publication `test_rebase_recovery_marker_survives_agent_pause` | 실제 충돌 준비·Manager 최초 실행과 Agent 미호출을 한 번 검사하고 복원한다. transient/usage-limit 각각 marker·session·호출 횟수·재개·최종 PLAN_BLOCKED assertion을 그대로 검사한다. |
| PR `test_transient_and_usage_limit_pauses_resume_same_recovery` | PR·dirty/ahead 기본 checkout·원격 충돌 준비를 공유한다. 각 오류별 실제 복구·Agent pause·재개·원래 PR·기본 checkout 보존을 그대로 검사한다. |
| PR `test_external_command_interruptions_preserve_recovery_and_resume` | 실제 PR reconciliation 직후를 복원한다. GitHub/fetch/rebase/push 중단별 실패 주입·lease·단일 rebase/push·기존 PR·기본 checkout assertion을 유지한다. |
| Review `test_review_protocol_errors_do_not_retry_worker_or_consume_history` | 첫 변형은 실제 Worker·verify를 통과하고 Reviewer handler 입구를 저장한다. 7개 출력 변형은 같은 입구에서 Reviewer 교정·session·prompt·실패/승격 이력을 각각 검사한다. Worker와 verify 호출은 첫 변형에서만 1회, 나머지는 0회로 확인한다. `test_review_protocol_recovery`의 정상 교정 후 게시와 유효 FAIL→Worker 교정 전체 흐름은 그대로 둔다. |

복원한 옛 snapshot을 새 경로의 관측 증거로 만들지 않는다. checkpoint/journal의 경로와 내용은 같은 소유 임시 경로에서 그대로 복원하고, 실행 재개 시 기존 제품의 현재 상태·digest·generation 검사를 다시 거친다. unit test는 실제 Git ref·index·원격·파일·SQLite 복원, 메모리/파일 DB, 자료 손상 거부, 한 변형의 중단과 병렬 테스트 격리를 확인한다.

## 비용과 검증 기록

- 환경: Windows, Python 3.13.14, Git 2.54.0.windows.1. 코드 기준은 `53f7322`에 이 PR의 미커밋 후보를 적용한 상태다. targeted unit은 직렬 실행이며 별도 부하를 주입하지 않았다. 다른 호스트 프로세스의 동시 부하는 통제하지 않았다.
- 대표 준비 unit 비교는 두 경우 모두 기존 초기 repository seed가 준비된 상태다. 독립 준비 2회와 준비 1회+저장+복원+identity 검증을 비교한다. 현재 후보에서 구 경로와 새 경로를 같은 assertion으로 실행한다. 최초 seed 생성의 효과를 이번 절감으로 합산하지 않는다.
- 실제 Git 명령: **40 → 26회**, snapshot: **3 → 2회**. 한 실행의 전체 준비 구간은 **2.0595 → 2.0204초**였다. 복사·저장·digest 검사·복원·정리를 포함한다. 절대 시간 차이는 작고 단일 실행이므로 절감률을 보장하지 않는다. 이 값은 unit 준비 사례의 경과 시간이며 모듈 누적 또는 전체 verify 경과 시간이 아니다.
- 원격 main으로 기준을 정리한 `1ee9a90`에서 같은 준비 unit을 재실행해 통과했다. 명령·snapshot 수는 같고 **1.9657 → 1.8163초**였다. 재사용의 저장 0.2583초·identity 확인 0.0461초·복원 0.2158초·정리 0.0808초를 포함한다. 이 재실행도 전체 verify 비교는 아니다.
- HEAD/branch 묶음 조회는 attached 기본 checkout+detached 추가 worktree의 구 조회와 동일 snapshot을 반환하며 Git 명령 2회를 줄인다. 두 index의 bytes도 관측 전후 동일하다.
- 동일 inspect의 소유권 소비자는 전체 expectation·checkpoint generation이 같은 결과만 사용한다. repository·worktree·branch·target·remote·입력·범위·generation이 다르면 새 관측으로 돌아가는 unit case를 남겼다. 독립 원격 소유권 거절도 유지한다. 전체 결과를 실행 단계나 재시작 사이에 보존하지 않는다.
- journal 없음/owner 불일치/이미 완료에는 기록 관측 0회, 유효 활성 journal에는 지연 수집한다. 관측 오류가 나면 intent를 보존한다.
- verify worker의 실제 Git subprocess 비용은 snapshot 밖의 준비·관측도 포함해 test/서수형 subtest/phase/호출 경로별로 기록한다. Git 인자·출력·config 값은 기록하지 않는다. snapshot 이벤트도 상위 소비자 경로와 현재 test/phase를 보존한다. 포함 관계인 비용은 합산하지 않는다.
- 통과 command: `py -3 -B -m unittest tests.test_recovery_boundary tests.test_git_snapshot_cost tests.test_manager_git_journal.CurrentRebaseAuditTests tests.test_execution_current_rebase_adoption_phase.CurrentRebaseAdoptionTests.test_ownership_reuses_only_matching_inspect_evidence tests.test_verify_runtime.TimingAndManifestTests tests.test_scope_git_metadata.ScopeGitMetadataTest.test_changes_in_another_registered_worktree_are_rejected tests.test_scope_git_metadata.ScopeGitMetadataTest.test_index_flags_are_rejected_for_worker_allowed_path tests.test_scope_git_metadata.ScopeGitMetadataTest.test_preexisting_index_flags_do_not_hide_agent_file_changes` — **29개 통과**, 39.702초. 변경 파일 구문과 diff 공백 검사도 통과했다.
- 최초 sandbox 실행은 Windows 임시 디렉터리 접근 오류가 있어 정상 임시 환경의 승인된 targeted 실행으로 다시 검증했다. 별도로 실행한 기존 CurrentRebaseAdoptionTests 클래스의 일부 case는 `phase context가 최신 snapshot과 다릅니다`로 실패했다. 대표 `test_new_activation_preserves_adopted_stage_and_revalidates_worker`를 변경 전 `53f7322`에서도 같은 지점·오류로 재현했다. 이 클래스를 전체 통과로 보고하지 않는다.
- **미실행**: 변경한 실제 lifecycle/PR/Review 시나리오와 Manager 전체 verify·전후 모듈 비교·전체 경과 시간 비교. Windows에서 Linux 전용 lifecycle의 skip 조건도 변경하지 않았다. 기존 verify 스크립트와 전체 manifest discovery 범위를 유지하며, Manager 검증·Review·사용자 Merge 전에는 R-070 완료로 판정하지 않는다.
