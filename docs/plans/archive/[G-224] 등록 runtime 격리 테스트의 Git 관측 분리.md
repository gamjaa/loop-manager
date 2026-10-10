---
id: G-224
title: 등록 runtime 격리 테스트의 Git 관측 분리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-080] Git Mock Defaults for Tests.md
depends_on: []
complexity: medium
risk: medium
priority: normal
---

## Purpose

등록·runtime·Agent 격리·작업 공간/정리의 남은 관측 정책 테스트에 Git mock 기본 경계를 적용한다. 현재 `test_scope_role_boundaries.py`는 role/path 허용·거부를 검사하면서 매 조건에 repository와 실제 전후 snapshot을 만든다. 이 판정은 명시적 GitSnapshot으로 검사하고 snapshot 생성·OS/실제 Git 효과는 필요한 별도 회귀에 유지할 수 있다.

유지하는 실제 Git 테스트의 공통 환경 격리도 이 작업에서 제공한다. fixture 최초 seed 생성부터 후속 명령·필요한 자식까지 사용자 설정·초기화 template의 우연한 영향을 제외하며 G-222·G-223은 이 공통 계약을 각 소비 경계에 적용한다.

## Acceptance Criteria

- 등록/runtime·role scope·작업 공간/정리·source 동기화와 provider/verify process 연결 테스트에서 정책 조건 조합은 Git/CLI 관측 경계를 mock으로 제공하고 실제 허용/거부·저장·보존을 검사한다. 예상 밖 명령·실패/오류·연속 관측 변화가 성공 mock에 숨겨지지 않는다.
- role/path별 scope 검사는 tracked/staged/untracked·선택 문서/계획·read-only 관측값을 제공하며 실제 validate_agent_scope를 실행한다. 실제 snapshot 파싱·파일 bytes/index·Git metadata·OS 격리·lease·worktree 효과는 필요한 통합 회귀에서 assertion으로 확인한다.
- 현재 bootstrap의 역할별 저장 상태/부모·자식 dispatch/exact-SHA runtime·B/C 전환과 등록 중단/동일 SHA 재사용의 연결 회귀를 유지한다. 공통 오류를 모든 역할·branch 전체 CLI로 반복하지 않으며 실제 연결에 영향을 주는 상호작용은 최소 통합 case로 남긴다.
- 실제 Git/CLI 유지·추가 이유와 전환/축소한 조건의 계속 검사 위치를 테스트 ID·assertion으로 대조할 수 있다. 실패 시 파일/index/session/감사·소유권 보존과 중단 뒤 같은 작업 재개를 검증하고 목적 달성 뒤 무관한 교정·차단·게시를 반복하지 않는다.
- 전체 verify의 수집과 기존 상한·skip 조건을 유지한다. 최초 import의 의존성/부작용처럼 새 interpreter가 계약인 검사는 유지하며 실제 Agent나 GitHub/Telegram을 호출하지 않는다.
- 공통 실제 Git fixture는 최초 seed 생성·독립 복사·후속 명령·필요한 자식에서 사용자 global/system 설정과 template의 우연한 영향을 받지 않는다. hooks·commit signing·filters·fsmonitor와 설정 상속 검사는 필요한 설정을 격리된 테스트 입력으로 명시한다. 사용자 설정 파일·호출자 환경·운영 checkout의 설정은 보존하고 실패·정리 후에도 테스트 간 입력이 섞이지 않는다.
- PRODUCT에 이미 적용된 mock 기본/실제 효과 예외와 필수 전체 검증 유지 규칙을 간결하게 반영하고 ARCHITECTURE의 자체 테스트 경계와 맞춘다. 다른 분할 범위의 완료를 문서 갱신만으로 선언하지 않는다.

## Implementation

- 대상은 `tests/test_origin*.py`, `test_bootstrap*.py`, `test_scope*.py`, `test_workspace*.py`, `test_worktree*.py`, `test_cleanup*.py`, `test_source_sync.py`, `test_self_merge_handoff.py`, `test_registered_project_scheduler.py`, `test_managed_configuration.py`와 platform/provider/Agent/verify/Git fixture·공통 진입 테스트의 실제 Git/CLI 사용 case다. G-222의 계획/catalog/유지보수와 G-223의 실행/수동/게시 복구 case는 제외한다.
- 기존 `tests/support/scope.py`의 snapshot 생성, origin/workspace/runtime fixture와 command adapter를 사용한다. 실제 SQLite·파일/OS effect는 assertion에 필요할 때 유지한다. 공동 fixture를 선택적으로 확장하되 별도 runner·전역 cache·운영 설정은 도입하지 않는다.
- 기존 `tests/support/git.py`와 테스트 명령 경계에 공통 격리 환경을 제공한다. `_initialize_remote_repository`의 직접 bare init과 `git_command`, seed 생성·복사 뒤 명령과 운영 Git adapter·Python 자식이 사용하는 환경까지 대조한다. 설정은 테스트 subprocess에만 명시적으로 전달하고 운영 설정이나 사용자 파일을 수정하지 않는다. G-222·G-223이 같은 경계를 재사용할 수 있게 하며 범용 fixture·runner를 추가하지 않는다.
- 기존 `tests/test_git_fixture_seed.py`의 seed 단일 생성·파일/ref/index/config 독립성·실패 정리 검증을 유지한다. 설정 상속 자체를 검사할 때는 통제된 global/system 입력과 명시적 테스트 설정을 제공하고 테스트별 설정·실패가 기본 seed나 다음 테스트에 남지 않게 한다.
- 직접 반영된 bootstrap 역할별 child·PID/시각 계측·공통 crash/reuse 축소와 verify input의 platform API mock은 유지한다. 실제 연결이 필요한 이유를 테스트에 남기며 정책 변형은 실제 판정·저장을 쓰는 mock으로 옮긴다.
- 각 전환의 assertion 대응과 실제 실행의 계약/효과를 기록한다. AGENTS.md·ARCHITECTURE의 Git 경계와 Worker 최소 targeted 범위를 따르며 실행 격리·등록·복구 정책을 변경하지 않는다.

## Validation

- Worker는 변경한 role/path scope·등록 문서/선택 revision·소유권/정리 거부·관측 오류의 최소 unit case를 실행한다. process guard로 의도하지 않은 실행을 거부하고 실제 판단·SQLite 보존·같은/변경 입력 처리를 확인한다.
- 전환한 정책 case는 예상 밖 process를 거부하는 경계와 기존 Git 계측으로 Git 실행 0회를 확인한다. 공통 격리는 최초 seed의 직접 init·후속 명령·운영 Git adapter·필요한 자식에 실제 전달되는 환경을 최소 unit case로 검사한다. 격리된 입력의 hook·서명·template·filter/fsmonitor 영향 차단과 명시적 설정/상속 적용, 호출자 보존·실패 정리·다음 테스트 독립성을 확인한다. 실제 설정 효과는 mock으로 확인할 수 없는 최소 Git 연결 회귀에 한정한다.
- Manager 전체 verify는 실제 부모/자식 exact-SHA 연결, snapshot/index/worktree·OS 격리·source 보존·정리/중단 재개·필수 최초 import와 process 종료 회귀를 전환 case와 함께 통과해야 한다. 실제 Git 예외의 effect assertion과 수집 범위 보존을 Review한다.
- 변경 전후 영향 모듈과 필수 연관 회귀를 같은 환경·대상 집합·Python/Git 버전·계측/캐시 조건·병렬도에서 반복 측정해 중앙값을 비교한다. revision·실제 worker 수·CPU/메모리 제한·동시 부하·임시 경로와 Git 호출 수·누적 시간·seed/복사/준비·모듈 경과 시간을 남긴다. 기존 전체 기준 기록과 Manager 최종 전체 verify 근거를 연결하며 국소 비교마다 전체 suite를 반복하지 않는다. bootstrap 부모 시간에 포함된 child 시간을 합산하거나 누적 비용을 전체 경과 시간과 더하지 않으며 모듈 개선을 전체 verify 절감으로 환산하지 않는다.

## Progress

- [x] 2026-10-08 f3ae228의 R-080·설계 경계·bootstrap 전환 diff, role scope 원본·snapshot helper와 실제 등록 revision/작업 공간 회귀를 정적으로 확인했다. 구현·테스트 실행은 하지 않았다.
- [x] 2026-10-10 사용자 요청으로 갱신된 R-080과 공통 Git fixture의 seed·직접 init·후속 명령 경계를 대조하고 환경 격리 책임·Git 실행 0회·비용 측정 조건을 문서에 반영했다. 구현·테스트·성능 측정은 하지 않았다.
- [x] 2026-10-10 역할·경로 snapshot, 설정 차단 재진입, 등록 지침 관측, cleanup dirty/재관측·소유권 거부와 기존 경로 충돌을 process guard로 전환했다. 공통 seed/명령 환경과 실제 adapter·자식의 테스트별 환경 격리를 구현하고 targeted 회귀를 실행했다.
- [x] 2026-10-10 전역 ignore·attributes 보완 전 WSL `/home/` 전체 verify: 210개 모듈, 3,398 pass / Windows 전용 5 skip, compile 성공. 기존 3,400개 ID 누락 없이 환경 격리 3개 ID만 추가됐다. 변경 코드·fixture 31개 파일의 Windows/WSL SHA-256 일치를 확인했다.
- [x] 2026-10-10 문서 재검증에서 발견한 전역 ignore·attributes 누락을 공통 XDG 경로와 system attributes 차단으로 보완했다. 같은 기존 테스트가 이전 격리 방식에서는 실패함을 확인하고 관련 59개 회귀와 보완 후 전체 verify를 통과했다. 최종 수정본의 비용을 다시 3회 비교하고 문서 AC를 재대조했다.
- [ ] 별도 Reviewer의 읽기 전용 Review·사용자 Merge

## Decision Log

- complexity medium: 기존 scope/command 경계와 실제 연결 예외를 명확한 책임에서 구분한다. risk medium: 격리/소유권 회귀를 mock 성공으로 가릴 수 있어 실제 판단·보존·필요한 OS/Git 통합을 유지한다. 테스트 rollback과 기존 통합 회귀로 복구할 수 있으며 priority normal이다.
- 2026-10-08 초기 범위에서는 이미 전환된 직접 작업을 유지하고 남은 관측 정책과 근거 없는 반복 실행을 다루며 G-222/G-223과 독립적으로 검증·Merge한다고 정했다. Requirement 전체 충족은 Manager의 최신 근거 재판단에 맡긴다.
- 2026-10-10: R-080에 추가된 공통 실제 Git 환경 격리 구현·설정 효과 회귀를 이 작업의 책임으로 통합한다. G-222·G-223은 이 작업의 Merge 뒤 공통 경계를 소비하며 이 작업은 두 작업의 선행 구현을 요구하지 않는다. ID·경로·기존 수행 기록과 complexity/risk medium·priority normal을 보존한다. 변경은 테스트 환경에 한정되고 비용 비교 조건은 R-080이 참조하는 R-065 기준을 따른다. R-065의 배정/import 분산과 R-067의 가드/임시 경로 개선은 범위에 추가하지 않는다.

## 구현 검증 대응

| 전환한 기존 테스트 | 계속 검사하는 판단·보존 assertion |
|---|---|
| `ScopeRoleBoundariesTest`의 기존 5개 ID | tracked/staged/untracked snapshot을 제공하고 실제 `validate_agent_scope`의 역할별 거부, 새/기존 계획·선택 계획 허용과 read-only 거부를 검사한다. process guard로 Git/CLI 0회를 보장한다. |
| `ScopeConfigurationTest.test_blocked_config_change_stops_before_git_reentry` | 현재 config 관측만 제공한다. 실제 프로젝트 차단 저장, 오류 값 비노출, 원래 결과 snapshot 보존과 migration/Git/Agent no-call을 유지한다. 실제 config 파싱·digest 비노출은 같은 클래스의 `test_git_config_changes_are_rejected_without_leaking_values`에 남긴다. |
| `OriginRegistrationTests.test_registration_never_reads_root_cli_guidance` | 선택 revision별 tree/blob 관측으로 실제 등록 검증을 실행한다. AGENTS blob을 읽지 않는 assertion과 현재 파일 bytes 보존을 추가했다. 실제 revision 읽기는 `test_managed_configuration`의 등록 회귀가 유지한다. |
| `WorktreeCleanupTest.test_dirty_reasons_cover_tracked_staged_and_untracked_changes` / `test_unverifiable_pr_head_after_assessment_preserves_worktree` | status 결과·후속 assessment 관측만 제공하고 실제 dirty 분류와 삭제 거부를 검사한다. 예상 명령/관측 횟수와 파일 bytes 보존을 확인한다. |
| `CleanupPendingStateTest.test_unowned_pending_is_not_inspected_and_clears_only_when_missing` | 실제 SQLite의 pending 소유권, 파일 bytes와 경로 소실 뒤 record 제거를 유지한다. Git 없는 경로에서 validate/remove/process no-call을 확인한다. |
| `WorkspaceCollisionsTest.test_new_workspace_rejects_preexisting_path` | 실제 기존 디렉터리와 marker를 제공하고 실제 생성 거부와 bytes 보존을 검사한다. setup 저장소·계획·DB를 제거하고 process no-call을 확인한다. |
| `SourceSynchronizationTests.test_safe_fast_forward_skips_local_state_matrix_without_gating_remote_sha` | tracked/staged/untracked·ahead/diverged·detached/다른 branch·operation 8조건에 명시적 snapshot과 ancestry를 제공한다. 실제 동기화 판단의 skip·선택 remote SHA 보존, 정확한 관측 명령 목록과 파일 bytes/process no-call을 검사한다. 실제 HEAD/index/checkout 보존은 인접 source 통합 회귀를 유지한다. |

실제 실행을 유지하는 경계는 다음과 같다. 역할별 공통 오류나 복구 뒤 교정·게시 조합을 새로 늘리지 않았고 기존 수집 ID를 제거하지 않았다.

- `test_scope_git_metadata`·`test_scope_ignored_artifacts`: 실제 commit/ref/index flags·ignore 파일·metadata의 전후 snapshot과 거부를 확인한다. malformed config/index·중복 ref/worktree의 오류 판정은 기존 `test_scope_services`의 fake adapter 회귀도 유지한다.
- `test_origin_isolation`·`test_scope_interrupted_agents`·`test_scope_*recovery`: 실제 worktree·runtime·index와 invocation/session·SQLite 보존 및 중단 재개를 확인한다.
- `BootstrapSavedScopeResumeTests`의 두 기존 ID: 실제 부모/자식 PID와 역할별 저장 상태, exact-SHA, 등록 중단·동일 SHA 재사용 및 B/C 전환 assertion을 유지한다.
- `test_source_sync`·`test_self_merge_handoff`·workspace/cleanup 통합 회귀: 실제 rebase/index·journal·lease·경로·소유권·파일 보존과 중단 재개를 확인한다. scheduler의 실제 자식 종료·pipe 정리, provider의 fake process 판정과 verify input의 platform mock, 새 interpreter import 계약은 그대로 유지한다.
- `GitEnvironmentTests`: 직접 bare init/후속 명령의 환경은 unit으로 대조한다. 최소 실제 Git 회귀 하나에서 seed·독립 복사·운영 adapter·Python 자식의 격리, 통제된 global/system 상속과 template·filter·서명·hook의 명시적 효과, 실패 뒤 다음 복사 독립성을 확인한다. 전역 ignore·attributes를 통제된 XDG 입력으로 제공해 seed의 `src/app.py` 추적, untracked 파일 관측과 eol 속성을 검사하고 명시적 `env`에서는 입력이 적용됨을 확인한다. system attributes 차단 변수의 전달도 unit으로 확인한다. setup 실패/다음 테스트의 호출자 환경 복원은 별도 unit이다. 설정·ignore·attributes 파일의 bytes는 보존된다.
- 공통 helper의 `env` 인자 계약을 소비하는 `MaintenanceGitAliasTest.test_git_alias_strips_output_and_preserves_subprocess_failure`의 예상 호출만 함께 갱신했다. 출력 strip과 원래 subprocess 예외 identity assertion을 유지하며 G-222의 정책 전환을 추가하지 않았다.

## 비용 비교

기준은 작업 시작 시 fetch한 최신 `origin/main`의 `e4c69f7411084621e8fbb575874e06e59ae7debf`다. 전역 ignore·attributes 보완 후 최종 수정본을 WSL Ubuntu의 `/home/jeong/codex-g224-20261010/{baseline,candidate}`와 공통 `/home/jeong/codex-g224-20261010/tmp`에서 Python 3.12.3·Git 2.43.0, worker 1개로 직렬 비교했다. 기존 scope_role_boundaries·scope_configuration·origin_registration·worktree_cleanup·cleanup_pending_state 및 GitFixtureSeedTests의 동일 38개 ID를 실행했다. 새 환경 격리 테스트 3개는 비용 표본에서 제외하고 targeted/전체 verify에서 검사한다. 각 표본은 새 interpreter·동일 계측, 프로세스별 seed cache 최초 생성과 bytecode warm 조건으로 실행했으며 verify와 겹치지 않았다.

| 지표 | 기준 3회 | 후보 3회 | 중앙값 |
|---|---|---|---|
| 경과 초 | 14.005 / 13.948 / 16.478 | 11.237 / 11.446 / 9.195 | 14.005 → 11.237 |
| Git 호출 | 2052 / 2052 / 2052 | 1549 / 1549 / 1549 | 2052 → 1549 |
| Git 누적 초 | 5.223 / 5.249 / 5.529 | 3.760 / 3.797 / 3.772 | 5.249 → 3.772 |
| seed 초기화 phase 누적 초 | 3회 중앙값 0.099 | 3회 중앙값 0.104 | 0.099 → 0.104 |
| 독립 복사 phase 누적 초 | 3회 중앙값 1.619 | 3회 중앙값 0.724 | 1.619 → 0.724 |

source 상태 matrix의 기존 단일 ID·8조건도 같은 환경에서 따로 3회 비교했다. 경과 초는 기준 1.550 / 1.549 / 1.544, 후보 0.011 / 0.011 / 0.010이며 중앙값은 1.549 → 0.011초다. Git 호출은 매회 333 → 0, Git 누적 시간 중앙값은 1.458 → 0초다. 원래 origin fixture는 seed/복사 phase를 제공하지 않으므로 해당 준비 비용을 별도 phase 값으로 만들지 않고 실제 Git 비용과 테스트 경과에 포함한다.

최종 표본별 Git 비용·test/module별 누적 경과·seed/복사 phase는 WSL 작업 디렉터리의 `xdg-fix-{policy,source}-{baseline,candidate}-{1,2,3}.jsonl`에 남긴다. `performance-xdg-fix.json`에 원본 표본·자원·fixture phase·모듈별 test 시간을 집계한다. CPU affinity는 12개이고 별도 CPU quota는 없으며 메모리는 8,092,880 KiB, cgroup memory limit은 `max`다. 자체 verify·다른 표본과 겹치지 않았다. 38개 ID 표본의 종료 관측 CPU busy는 기준 0.544–0.898, 후보 0.514–0.639 CPU다. 최초 짧은 관측은 `busy=None` 그대로 보존한다. source 표본의 시작 관측은 0.21초 간격으로 확보했고 짧은 후보의 종료 관측은 sampler의 0.2초 cache 값을 유지하므로 새 부하 측정값으로 해석하지 않는다. 보완 전 비용 표본은 기존 `performance.json`과 원본 로그에 보존한다.

이 국소 비교는 기존 R-080의 Linux 기준 기록과 환경·대상이 다르며 전체 verify의 절감량으로 환산하지 않는다. 기존 전체 기준 기록을 새로 실행했다고 주장하지 않는다.

## 최종 전체 검증 근거

2026-10-10 전역 ignore·attributes 보완 후 WSL Ubuntu에서 `/home/jeong/codex-g224-20261010/candidate`를 실행 위치로, `/home/jeong/codex-g224-20261010/tmp`를 TMPDIR로 삼아 `bash scripts/verify.sh`를 실행했다. 수집·skip 조건과 기존 600초 상한을 유지했다. `auto` 배정의 실제 최대 worker는 12개, 경과 시간은 305.335초이며 210개 모듈과 compile이 성공했다. 전체 3,403개 ID 중 3,398 pass / 기존 Windows 전용 5 skip으로 기존 ID 누락은 없다. run ID는 `95e7b7ef131243e5848f687b8e955445`다. 중간 source 전환을 위해 중단한 실행과 alias의 예상 `env` 인자 누락으로 실패한 실행은 성공 근거에 포함하지 않았다.

WSL의 `full-verify-xdg-fix/`에 manifest·worker/runner 원본·summary가 있고 `evidence-xdg-fix.json`에 전체 ID 대조와 환경을 기록했다. 운영 Agent·GitHub·Telegram을 호출하지 않았으며 Windows 네이티브 verify는 실행하지 않았다. 검증·비용·입력 대조의 요약은 현재 checkout의 `.cache/verify/g224/`에도 보존한다. 코드·fixture 31개 파일의 Windows/WSL SHA-256 일치와 검증 중 입력 보존을 확인했다. 보완 전 전체 verify 근거는 기존 `full-verify-confirmation/`과 `evidence.json`에 보존한다.

문서 기준 재검증에서는 R-080·이 작업의 AC·PRODUCT·ARCHITECTURE를 구현 검증 대응의 실제 판단/process guard 및 통합 회귀와 대조했다. 전역 ignore·attributes의 seed 개입·untracked 은폐·eol 적용은 기존 격리 방식에서 회귀가 실패하고 수정본에서 통과함을 확인했다. 명시적 XDG 입력의 적용, 설정 파일 bytes와 호출자 환경 복원도 유지했다. `git diff --check`와 수집·상한·skip·실제 효과 예외 대조에서 추가 미충족 사항은 발견하지 않았다. 별도 Reviewer·Merge 또는 G-222·G-223 완료를 선언하지 않는다.
