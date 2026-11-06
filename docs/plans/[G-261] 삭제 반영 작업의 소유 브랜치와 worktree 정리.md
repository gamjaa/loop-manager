---
id: G-261
title: 삭제 반영 작업의 소유 브랜치와 worktree 정리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
depends_on:
  - G-258
  - G-260
  - G-265
  - G-266
  - G-267
  - G-268
  - G-327
  - G-329
  - G-331
  - G-332
  - G-333
  - G-334
  - G-335
complexity: high
risk: critical
priority: high
---

## Purpose

확인된 문서 삭제를 반영한 취소·완료 작업의 살아 있는 정확한 Manager 소유 로컬·원격 branch와 worktree를, 작업물 보존과 기대 OID 검증 뒤 정리한다. 원격 ref 삭제 경계는 아래 기존 User Decision의 선택지 A를 유지한다. 현재 원격 ref 부재의 소유 worktree/local ref 정리와 G-260의 취소 감사·퇴역 자료/문서 사본·기록/파일 최종 제거는 직접 구현으로 소비한다.

남은 범위는 실제 자원이 남은 취소 감사도 원래 승인된 작업/자원 소유권과 보존 근거로 확인해 안전한 물리 정리에 연결하고, 정확한 살아 있는 원격 ref의 lease 삭제·부분 정리와 중단 재개를 완성하는 것이다. G-260의 이미 사라진 자원 확인과 최종 기록 제거를 새로 구현하거나, 취소 감사의 native 형식이 맞는다는 이유만으로 살아 있는 자원 삭제를 허용하지 않는다. 일반 취소·실제 MERGED·독립 작업의 기존 보존은 유지한다.

## Draft Decision

Question: R-055의 확인된 삭제를 반영한 취소·완료 작업에 한해, 아래 소유권·작업물 보존·정확한 ref/기대 OID·PR head 사용 확인과 중단 복구 조건으로 Manager의 로컬·원격 작업 브랜치 및 worktree 자동 정리 경계를 도입하도록 승인하는가?
Facts: R-055 D09/D10은 이 자동 정리를 요구하지만 ARCHITECTURE의 현행 Repository Safety and Security는 Merge된 로컬 관리 branch만 비강제 삭제하고 원격 branch는 자동 삭제하지 않는다. Architecture Change Policy는 “worktree, branch, commit, push와 PR 생성 경계 변경”에 사용자 승인을 요구한다. 현재 cleanup은 PR head에 포함된 clean worktree를 확인하며 local-only commit·dirty·미확인은 pending으로 보존한다. R-055는 원하는 변경의 근거이고 이 계획의 사용자 승인 기록을 대신 작성하지 않는다.
Options: (A) 아래 조건을 모두 지키는 R-055의 소유 로컬·원격 자원 정리 경계를 승인한다. (B) 현재 로컬 Merge 정리를 유지하고 이 확장은 defer한다. 다른 범위나 보존 조건을 원하는 경우 사용자 명세를 먼저 조정한다.
Impact: A는 기존 cleanup/Manager Git 효과를 확장하며 보호 실패·경쟁·PR head 사용 미확인은 해당 자원 pending으로 남긴다. B에서는 G-258–G-260의 확인된 문서·DB·취소 반영은 진행할 수 있고 이 자원 정리 목표는 미충족으로 남는다. 승인 전 아래 구현과 그 의존 실행을 시작하지 않는다.

## User Decision

Decision: approve
Revision: 5553ed3c62389b0e1ba07bcdcaf4a56efd067c17d9cf20bbb62ee073c2f8fff4
Answer: 선택지 A를 승인한다. 앞으로 작업 문서의 확인된 삭제를 반영한 취소·완료 작업에 대해 Manager가 관련 Manager 소유 원격·로컬 작업 브랜치와 worktree를 자동 정리하도록 한다. 이 계획의 작업물 보존, 소유권 확인, 정확한 ref·기대 OID 검증, PR head 사용 확인 및 중단 복구 조건을 모두 적용한다. base·사용자·다른 작업의 자원은 삭제하지 않으며, 보존 실패·사용 중·불확실한 자원은 pending으로 유지한다. 이 승인으로 Architecture Change Policy의 해당 경계 변경 판단을 해소하고 G-261의 draft를 해제한다.

## Acceptance Criteria

- R-055의 목표 철회·관련 참조 개정·ExecPlan 삭제가 확인된 미Merge 작업은 PR close/원격 branch 삭제 대기보다 삭제 처리를 우선한다. 원격 ref가 살아 있거나 활성 row 없이 취소/hold·cleanup 근거만 남아도 정확한 소유/PR·기대 OID·종료 근거로 정리한다. 이름/경로 부재만으로 채택하지 않으며 정리 미완료의 필수 근거는 완료까지 한시 보존한다. 삭제 확정의 전용 보존 사본/정리 기록은 최종적으로 G-260의 제거 경계로 넘기며 ID 표식 이외에 영구 보존하지 않는다.

- G-258/G-260에서 삭제·현재 수명을 확인한 취소·완료 작업의 자원만 대상이다. repository·작업·정확한 소유 branch/worktree·writer 종료·다른 작업과 수동 소유자의 사용 여부를 정리 직전에 확인한다. base·사용자 branch·다른 작업 자원을 이름이나 문서 부재만으로 삭제하지 않는다.
- 미게시 commit·권위 있는 사용자 변경·원문·session·실행/실패 근거와 필요한 PR head를 복구 가능한 위치에 보존하고 보존 결과를 검증한 뒤 정리한다. 보존 실패·원문/소유권 불확실·미종료 writer·사용자 Git operation은 자원을 남기며 정리 완료를 만들지 않는다. 현재 dirty/local-only 보존 규칙을 단순히 제거하지 않는다.
- 원격의 실제 ref와 기대 OID·PR Open/Closed/Merged 상태·현재 head 사용을 재확인해 정확한 대상만 삭제한다. 새 commit·재생성·소유권 변경을 낡은 요청으로 제거하지 않는다. PR head 보존과 정리가 양립함을 확인하지 못하면 해당 정리만 보류하며 PR을 자동 종료·Merge하지 않는다.
- 문서 게시·DB 취소/완료·자원별 정리는 하나의 원자 작업으로 가정하지 않는다. 소유 intent·삭제 revision·보존 위치·정확한 ref/기대 OID 및 자원별 실제 결과를 유지하고 각 확정 지점부터 멱등 재개한다. 이미 사라진 동일 자원은 결과와 대조하고 같은 이름으로 재생성된 자원은 별도로 검증한다.
- 원격 ref 삭제 뒤 결과 미기록, local ref와 worktree 일부 정리, 권한·보호 정책·원격 조회 실패에서도 확인된 문서·DB 반영을 되돌리지 않는다. 미정리 대상·보존 위치·실제 이유를 pending으로 남기고 독립 작업을 계속한다. 같은 확인 결과로 삭제·게시·알림을 반복하지 않는다.
- 승인된 경계는 확인된 문서 삭제 처리에 한정한다. 기존 Merge cleanup·수동 reset/manual stop·base 보호·일반 force push 금지와 PR Merge의 사용자 소유를 유지하고 승인되지 않은 범위로 자원 정리를 확대하지 않는다.

## Implementation

- [x] 확인한 `catalog/deleted_plan_cleanup.py:cleanup()`의 원격 ref 부재 시 worktree/정확한 local ref 정리와 `storage/deleted_plan_state.py`의 파일 queue를 재사용한다. 현재 ref가 있으면 보류하는 경계를 기존 Manager journal·정확한 기대 OID의 삭제 lease로 연결한다. 확인한 `_rowless_cancellations()`의 native 감사와 `_rowless_ownership()`의 실제 부재 판정은 최종 제거 근거로 재사용한다. 실제 자원이 있는 후보는 별도 소유/보존 확인 뒤 물리 정리하고, 부재가 확인된 뒤에만 기존 최종 제거로 넘긴다. 형식 검사나 과거 cleanup_pending을 살아 있는 자원 소유의 증명으로 대신 쓰지 않는다. 일반 Merge cleanup·원격 사용자/base/다른 작업·수동 소유 보호를 확대하지 않는다.
- [x] 물리 정리 완료를 G-260의 기존 `storage/deleted_plan_state.py`·`catalog/deleted_document_cleanup.py`·파일 queue의 최종 제거/재시도에 연결한다. 문서 사본/native journal 복구·퇴역 acceptance/retired 사본의 실제 부재 확인·공유 usage/outbox/공통 결함/완료 Merge 보호는 이미 구현된 경계를 소비한다. 원래 자원 정리와 후속 기록 제거의 확정 지점을 구분하며 같은 엔진을 복제하지 않는다. 작업물/PR head 보존·소유/종료 확인에 필요한 자료를 먼저 지우지 않고, 일반 취소·실제 MERGED의 원문/실행 이력과 타 작업의 공유 자료는 유지한다. 별도 무차별 보존 폴더 삭제·새 정리 엔진은 만들지 않는다.

아래 User Decision의 승인 범위에서 `git/cleanup_service.py`·`merge_cleanup_service.py`의 소유권·재검사·pending 처리, 기존 Manager Git journal·자원 claim·PR 관측·게시 lease를 재사용한다. 취소/완료의 현재 수명과 자원별 보존·삭제 intent를 연결하고 실제 ref 삭제 효과를 의미 판단·DB 취소와 분리한다. 복구 가능한 보존·정확한 기대 OID의 거부를 기존 관리 저장 경계 안에서 구현하며 새로운 외부 서비스·비호환 migration·독립 운영 상태 원본을 만들지 않는다.

G-238의 명시적 reset/manual stop·READY 복귀는 이 작업에 포함하지 않는다. 해당 경로와 공통 보존·Git journal 책임이 있으면 현재 Merge된 helper를 재사용하되 미완료 CLI 구현을 복제하거나 수동 소유 권한을 확대하지 않는다. PRODUCT·ARCHITECTURE에는 승인 후 실제 구현한 삭제 대상·보존·PR/lease·pending/재개 계약을 현행 정리 절에 통합한다.

## Validation

- 현재 `tests/test_deleted_plan_cleanup.py`의 원격 ref 부재 정리·writer/PR/원격 revision 경쟁·파일 queue 재시도 회귀는 소비한다. 새 검증은 살아 있는 정확한 소유 ref의 기대 OID 삭제와 거부, row 없는 취소 근거의 실제 자원 정리와 기존 최종 제거로의 인계에 둔다. G-260의 native 문서/retired 사본·기록/파일 제거 행렬은 재실행하는 새 lifecycle로 복제하지 않는다. 공유 로그/사본의 타 작업 bytes·실제 MERGED 근거는 불변이어야 한다.
- 원격 삭제 뒤 DB 미확정·일부 local/worktree 정리·전용 사본 제거 실패/재시작에서 필수 증거가 남고 완료 보고가 없어야 한다. 완료 뒤 ID 표식 외 대상별 기록/전용 사본이 없고 같은 이름 새 ref/새 소유자는 제거되지 않아야 한다. 기존 실제 임시 원격 lease/ref 효과 회귀를 최소 확장하며 정책 조합은 Git 관측 double·실제 SQLite/파일로 검사한다.

- `tests/test_worktree_cleanup.py`, `tests/test_cleanup_pending_state.py`, `tests/test_workspace_cleanup_ownership.py`의 최소 targeted unit으로 취소·완료 자격, base/사용자/다른 작업·수동 소유 거부, 미종료·보존 실패·dirty/local-only·PR head 사용 불확실·권한/보호 정책 실패를 실제 정책·SQLite·파일 보존으로 확인한다. Git·PR 관측은 mock하며 정리 판정을 성공값으로 대체하지 않는다.
- 기존 Git 통합 fixture의 임시 원격에서 정확한 소유 ref 삭제, 삭제 직전 새 commit과 같은 이름 재생성의 lease 거부, local-only commit·작업 파일의 복구 가능성을 실제로 확인한다. assertion은 ref/OID·복구된 bytes·사용자/base 불변 및 worktree 등록에 연결하며 실제 외부 repository에 게시하지 않는다.
- 기존 journal·pending fixture에 문서 게시 뒤 DB 미확정, 취소 확정 뒤 정리 전, 원격 삭제 뒤 결과 저장 전과 자원별 부분 정리의 재시작을 연결한다. 반복 삭제·중복 결과·취소 rollback·독립 실행 억제가 없어야 한다.
- Manager 전체 verify는 R-055 D09/D10·기존 Merge cleanup·lease 거부·수동 소유 보호 및 감사 보존을 유지한다. Reviewer는 소유권 밖 삭제, 보존 미확인 삭제, 낡은 OID 삭제와 PR 상태/정리 성공 합성을 blocking issue로 확인한다. 이 계획은 승인과 실제 검증·Merge 없이는 완료로 기록하지 않는다.

## Progress

- [x] 살아 있는 원격 ref·row 없는 취소 감사의 실제 소유 자원 정리와 기존 최종 제거 인계. 이미 부재한 자원/문서 사본·기록/파일 제거는 G-260의 직접 구현을 소비한다.

- [x] 2026-10-10 delta·R-055 D09/D10·ARCHITECTURE의 현행 정리/변경 정책·cleanup 구현과 기존 회귀를 정적으로 대조했다. 구현·테스트·전체 verify를 실행하지 않았다.
- [x] 2026-10-11 사용자 Architecture Change Policy 판단: 선택지 A 승인 및 draft 해제 요청을 위 User Decision에 기록했다.
- [x] 승인 범위의 자원 보존·정리·재개 구현과 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

### 2026-11-06 구현 후보와 완료 조건 대조

구현과 관련 교정을 마쳤으며 미해결 targeted 실패는 없다. Manager 전체 verify·Reviewer·사용자 Merge는 남아 있으며 이 기록은 공식 완료나 dependency 충족 선언이 아니다. 시작 HEAD는 `49c5f60e34b1b70103fd1565ebbe2099bae99700`이다. sources의 실제 R-055 D09/D10과 PRODUCT/ARCHITECTURE를 대조했고 모든 depends_on ID의 archive 경로를 확인했다. 원격 게시·삭제 판단·취소 원문 복구와 기존 문서/퇴역 사본 정리는 현재 G-258/G-260 계열 구현을 소비하며 새 게시·취소 엔진을 만들지 않았다.

| 완료 조건 | 현재 구현·확인 근거 |
|---|---|
| 확인된 철회만 정리하고 일반 취소·MERGED 보존 | 기존 원문 hash·revision ancestry·현재 참조 판단·historical Merge·공통 결함 보호를 그대로 통과한 후보만 자원 helper로 전달한다. `test_deleted_plan_cleanup` 89개 회귀로 원문/관계/공유 자료·실제 MERGED 보호를 확인했다. |
| 살아 있는 소유 원격·로컬 ref/worktree와 row 없는 취소 감사 | `deleted_plan_resources.cleanup`이 결정적 작업 경로·등록 origin·Git 등록/공통 repository·원래 소유 row/감사와 현재 독립/수동 참조를 다시 검사한다. row 없는 경로도 실제 호출 종료를 검사하고 같은 자원 정리와 `forget`/파일 queue에 연결했다. `test_live_rowless_cancellation_hands_partial_cleanup_to_existing_erasure_queue`가 부분 실패부터 최종 ID 표식까지 확인한다. |
| 작업물·원문·session·실패·PR head 보존 | `assess_cleanup`의 dirty/index flag/Git operation/local-only 거부를 유지하며 수동 잠금·다른 branch 관측도 보존한다. 게시 head의 전체 bundle을 fsync·verify/head 검사하고 정리 중 감사·실패 자료를 유지한다. 복구 원본이 없거나 미게시 commit이면 삭제하지 않는다. 새 정책 행렬과 기존 최소 worktree 회귀가 보존을 확인한다. |
| 정확한 PR·원격 기대 OID·새 소유자 보호 | 등록 origin과 고정 URL을 검사하며 PR 전체 branch 이력의 Open/Merged/fork·다른 URL/base/head를 거부한다. 저장된 OID의 삭제 lease와 원래 로컬 OID의 no-deref 삭제만 사용한다. lease 경쟁·심볼릭 ref·정리 완료 후 같은 이름/OID의 ref 재생성과 제거 증거 뒤 worktree 재생성을 새 자원으로 보류한다. PR을 close/Merge하지 않는다. |
| 비원자 삭제·중단 복구와 독립 진행 | 기존 journal에 원격/로컬 삭제 전이·보존본 지문·revision을 저장하고 worktree 제거 journal을 재사용한다. 원격/로컬 효과 뒤 결과 미기록, 원격 삭제 뒤 worktree 실패, 권한/정책 실패는 원래 intent로 재개한다. 자원 충돌 범위의 pending만 유지하며 문서·취소 상태를 rollback하지 않는다. 새 중단 회귀와 기존 journal/cleanup pending 최소 회귀로 확인했다. |
| 최종 전용 자료 제거·동일 효과 반복 금지 | 자원 부재 뒤 bundle·정확한 삭제 slot·원래 worktree 생성/제거 slot과 receipt를 기존 `forget` 파일 queue로 넘긴다. queue는 소유·형식·봉인된 결과를 재검사하고 파일 제거 직후 결과 저장 중단도 부재로 확정한다. native 회귀에서 원래 작업 bytes 복구, base 불변, ref/OID와 worktree 등록 제거, 전용 자료/queue 제거 및 ID 표식만 남는 결과를 확인했다. |
| 승인 경계·문서/검증 입력 | PRODUCT와 ARCHITECTURE의 관련 절을 실제 동작·저장 계약에 맞춰 갱신했다. 일반 Merge 정리·reset/manual stop·사용자 Merge 권한은 확장하지 않았다. 새 코드/테스트는 기존 `loop_manager_internal/**`, `tests/**` verify 입력에 포함되므로 선언 목록 추가가 필요하지 않다. 목록 밖 파일의 비소비는 전수 확인하지 않았으므로 제외 identity를 선언하지 않는다. |

### Targeted 검증과 교정

- 프로젝트 Python `python3`와 지정된 targeted runner로 실행 결과를 호출 기록 폴더에 남겼다. 최종 삭제 정리 모듈 89개, 새 소유 자원 모듈 7개, 기존 경계 최소 회귀 7개가 각각 통과했다. 전체 suite/verify·lint/build/type check는 실행하지 않았다.
- 실제 Git은 기존 격리 fixture의 임시 원격 하나에서 생산 자원 정리 함수의 lease 경쟁·거부/재시도·bundle 복구 bytes·worktree/로컬·원격 ref 제거와 최종 queue 중단을 확인했다. 실제 외부 repository·CLI/API는 호출하지 않았다. Windows native는 이 Linux 호출에서 실행하지 않았으며 Manager의 환경별 검증에 남긴다.
- 첫 기존 모듈 실행의 4개 실패 중 자원 없는 취소/hold의 canonical PR/ref 관측 누락 3개를 복원했다. 남은 기존 검증은 새 명세가 살아 있는 자원의 조회를 허용하므로 “관측 없음” 대신 읽기 전용 관측만 허용하고 실제 작업 bytes·DB 불변을 모두 검사하도록 교정했다. 테스트 삭제·skip은 없다. 새 모듈의 초기 오류는 base branch 입력을 관측 전에 거부하도록 교정하고, 취소 fixture의 게시 OID를 execution snapshot 생성 전에 지정해 실제 CAS를 통과하도록 해결했다. 최종 실패는 없다.

FAILED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_deleted_plan_cleanup
Initial result: FAIL (이전 호출 첫 실행 4개 assertion, 다음 실행 1개 assertion).
RERUN: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_deleted_plan_cleanup
결과: 89 tests, OK.

FAILED_COMMAND: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_deleted_plan_resources
Initial result: ERROR (이전 호출 초기 정책 입력·취소 fixture 오류).
RERUN: python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_deleted_plan_resources
결과: 7 tests, OK.

기존 경계 최소 실행은 `test_worktree_cleanup.WorktreeCleanupTest`의 `test_last_removal_guard_preserves_late_operations_and_index_flags`, `test_interrupted_cleanup_with_changed_head_recovers_for_recheck`, `test_local_only_and_unverifiable_commits_are_preserved`, `test_cleanup_pending_state.CleanupPendingStateTest.test_unowned_pending_is_not_inspected_and_clears_only_when_missing`, `test_workspace_cleanup_ownership.WorkspaceCleanupOwnershipTest.test_merge_cleanup_preserves_unowned_deterministic_worktree`, `test_manager_git_journal.ManagerGitTransitionTests`의 `test_pending_mixed_state_blocks_and_preserves_intent`, `test_candidates_require_an_unbroken_chain_to_live_ref`를 `tests.` prefix의 정확한 7개 ID로 한 번 실행했다. 결과는 7 tests, OK다. `git diff --check`도 통과했다.

이번 교정 호출(`call-5w367pre`)에서는 결과 검증 거부의 원인을 먼저 대조했다. 이전 호출의 19개 targeted JSONL을 읽기 전용으로 확인한 결과, 과거 실패/오류가 있던 모든 test ID의 최신 결과는 success였다. 폐기한 중간 native 테스트 ID까지 포함하면 최신 성공 ID는 104개이며, 최종 구현의 실행 단위는 앞서 보고한 103개다. 결과 검증기는 `FAILED_COMMAND`와 같은 명령의 `RERUN` 사이에 최초 FAIL/ERROR 결과가 있어야 해결된 이력으로 처리한다. 이전 최종 응답과 본문의 command pair에 그 결과가 빠져 보고가 거부됐으므로, 기존 검증기를 변경하지 않고 최초 결과를 명시했다.

이번 호출에서 동일 두 모듈을 지정 runner로 재실행해 삭제 정리 89개와 자원 정리 7개가 각각 OK였다. 현재 코드·테스트와 위 완료 조건 표, R-055 D09/D10 및 현행 설계를 다시 대조했으며 제품 코드의 추가 교정은 필요하지 않았다. 이전 경계 회귀 7개의 성공 기록은 유지한다. 이전 최종 응답은 실제 `parse_worker_result`에서 같은 TargetedTestFailure로 거부됐고, 교정한 최종 응답은 수용됨을 확인했다. 이 확인은 전체 verify·Review나 Manager의 공식 완료를 대신하지 않는다.

남은 작업은 Manager 전체 verify·Review와 사용자 Merge다. 구현 중 보존/PR/소유권이 미확인인 실제 입력은 명세대로 pending이며, 새 사용자 정책 선택이나 checkpoint는 필요하지 않았다.

## Decision Log

- revision `249477cd835923fa7923e92e55482efdc4ddce11`과 snapshot token `22ad530edef08485a2e956e3db5a8749e250ce80bf27d9743f680c72df20a993`에서 원본 hash·ID·경로·기존 수행 기록과 User Decision 원문을 확인했다. G-260의 실제 Merge·내용 보존 archive와 현재 `deleted_plan_cleanup.py`/`deleted_document_cleanup.py`·native 자원 inventory·기록/파일 보호 회귀를 소비한다. 이 개정은 이미 없는 자원의 후속 제거를 다시 계획하지 않고 남은 살아 있는 자원의 소유·보존·정확한 ref 삭제·중단 인계만 구체화한다. 새 사용자 승인을 만들거나 기존 결정/판단 양식을 정리하지 않는다. 구현·테스트·전체 verify는 실행하지 않았다.

- 최신 R-055는 정리에 필요한 검증된 보존과 완료 후 대상 전용 사본 제거를 구분한다. 아래 사용자 승인·거절 문구와 수행 기록은 변경하지 않는다. 일반 취소·MERGED의 기존 보존/승인 범위를 유지하고 확인된 미Merge 목표 철회에만 최종 제거 예외를 적용한다. 원격 ref 부재의 직접 구현은 재계획하지 않으며 G-260의 새 기록 수명을 선행 소비한다.
- 새 분류 risk critical: 삭제 확정의 최종 사본 제거는 복구 불가능할 수 있어 아래 과거 high risk의 백업 복구 설명을 그대로 적용할 수 없다. 필수 보존/소유/PR head·정확한 lease와 타 작업 귀속을 먼저 확인하고 미완료 중단에는 근거를 유지하며 최종 제거/타 작업 불변을 검증한다. complexity high는 비원자 자원 삭제·한시 증거·DB/파일 확정의 재시작 연결에 따른다.

- 2026-10-11 사용자가 “앞으로 작업 문서가 삭제됐을 때 매니저가 관련 원격/로컬 브랜치도 삭제하도록 해줘. 유저 결정 남기고 draft 상태 풀어서 프리해줘”라고 명시적으로 요청했다. 해당 사용자 결정을 대신 기록하고 draft 표시를 제거했다. 기존 Draft Decision과 구현·검증 조건은 승인 범위와 판단 지문 보존을 위해 유지한다. 선행 작업·정상 실행 gate는 계속 적용한다.

- R-055는 새 자원 정리 목표를 명확히 지정하지만 현재 Manager 효과에는 소유 원격 ref 자동 삭제 연결이 없다. 정책 적용 대상의 구현을 임의 승인하지 않고 이 ID의 draft 판단으로 남긴다. 사용자만 draft 해제와 User Decision을 작성하며 Agent가 결정이나 승인을 합성하지 않는다.
- G-189의 전체 차단 감사, G-257의 참조 복구 게시, G-258의 관측·감사 구분, G-259의 의미 판단·문서 게시와 G-260의 현재 취소를 중복하지 않는다. 정리 보류가 확인된 취소나 독립 작업을 완료 대기로 되돌리지 않아야 한다. 이번 변경 검토는 프로젝트 최종 전문 확인이 아니다.
- complexity high: 비원자 원격 ref·로컬 branch·worktree·DB 결과를 보존 증명·기대 OID·PR head와 묶어 중단/재생성에도 안전하게 재개하는 설계 판단이 남아 있다. risk high: 잘못된 정리는 미게시 작업이나 사용 중인 원격 head를 잃게 할 수 있다. 검증된 보존본·정확한 ref/OID·Git/정리 intent로 수동 복원할 수 있어야 하고 외부 ref 삭제를 코드 rollback으로 복구한다고 가정하지 않는다. 보존 실패·경쟁·부분 정리·복구 가능성을 AC·Validation·Review에서 확인한다.
