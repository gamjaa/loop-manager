---
id: G-299
title: 일괄 fetch 추적 ref의 심볼릭 대상 보호
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-092] 프로젝트 원격 브랜치 일괄 동기화.md
depends_on: []
complexity: high
risk: critical
priority: high
---

## Purpose

R-092의 일괄 수신 뒤 추적 ref 반영이 심볼릭 ref를 따라 사용자 branch나 다른 공유 ref를 변경하지 않게 한다. 이미 반영된 목록 조회·일괄 fetch·branch별 수신 검증·공유 잠금과 ref별 journal은 재사용하고, 대상 밖 쓰기를 막는 경계와 그 회귀만 보완한다.

현재 `_record_fetch_ref()`는 OID만 관측하고 일반 `update-ref`를 실행한다. 예를 들어 `refs/remotes/origin/topic`이 `refs/heads/user-work`를 가리키면, 예상 이전 OID 검사가 통과해도 실제 쓰기는 사용자 branch에 도달할 수 있다. ref 이름으로 잡은 잠금·journal과 실제 쓰기 대상이 달라지는 문제이며 실제 발생 사건이나 손실을 확인했다는 뜻은 아니다.

## Acceptance Criteria

- 정상 직접 추적 ref의 갱신·신규 생성은 branch별 검증과 기존 ref별 CAS/journal로 수행한다. 목록 조회 1회·필요한 일괄 수신 최대 1회와 변경 없는 실행의 수신 생략을 유지한다.
- 대상 추적 ref가 사용자 branch·다른 추적 ref·공유 ref를 가리키는 심볼릭 ref이거나 dangling symref이면 그 대상을 따라 쓰지 않는다. 소유권을 확정할 수 없는 심볼릭 형상과 기존 파생 `origin/HEAD`는 보존하고 해당 쓰기를 기존 안전한 보류·실패 경계로 돌린다. 사용자 alias를 정상 직접 ref로 조용히 치환하지 않는다.
- 관측 뒤 쓰기 전에 직접 ref가 같은 OID의 심볼릭 ref로 바뀌어도 대상 밖 ref를 변경하거나 그 변경을 Manager의 정상 전이로 채택하지 않는다. OID CAS와 사전 검사만으로 raw ref 종류의 경쟁을 해결했다고 간주하지 않는다.
- 잠금과 journal의 대상은 실제로 변경하는 정확한 ref와 일치한다. 원래 intent·소유자·실패 감사와 이미 완료한 다른 ref 전이를 보존하며 경쟁 값을 강제로 덮거나 과거 성공으로 새 형상을 승인하지 않는다.
- 이전 OID 전용 journal은 유효한 직접 ref와 소유 reflog 근거로 재개할 수 있다. 현재 심볼릭 ref의 referent OID가 같다는 사실만으로 미완료 쓰기의 완료·소유권을 확정하지 않는다. 기존 기록을 일괄 수정하거나 미실행 성공을 만들지 않는다.
- 같은 ref 반영 helper를 쓰는 단일 branch 경로의 대상·삭제 판정·완료 조건과 정상 복구를 유지한다. 보완한 실제 보존 경계는 PRODUCT·ARCHITECTURE의 관련 절에 통합한다.

## Implementation

- [x] 확인한 `loop_manager_internal/git/base_fetch_service.py`의 `_recorded_project_fetch()`·`_record_fetch_ref()`에서 raw ref 종류·정확한 대상과 OID를 쓰기·완료 관측에 연결한다. `runtime/bootstrap_adapter.py:_tracking_ref_oid()`는 commit OID로 역참조하므로 이것만으로 직접 ref임을 증명하지 않는다. 다른 소비자가 쓰는 OID helper의 의미를 일괄 변경하지 않고 필요한 관측을 fetch 책임 안에 둔다.
- [x] Git이 실제 쓰기 시점에도 대상 밖으로 역참조하지 않는 방법과 심볼릭 형상 보존을 함께 적용한다. 단순 사전 `symbolic-ref` 검사나 `--no-deref` 추가만으로 경쟁 중 사용자 alias 보존까지 보장한다고 가정하지 않는다. 기존 Git adapter·명령 경계를 재사용하고 실제 지원 Git 계약으로 구현 방법을 확인한다.
- [x] 확인한 `loop_manager_internal/git/manager_git_journal.py`의 `execute()`·`recover_active()`·`verified_transition()`과 `runtime/resource_lock.py`의 ref별 lease를 재사용한다. fetch에 필요한 관측·marker 경계를 보완하되 다른 journal kind의 의미와 기존 version 1 기록을 바꾸지 않는다. 복구 시 심볼릭 형상·외부 전환을 만난 경우 기존 intent와 사용자 ref를 보존한다.
- [x] 확인한 `tests/test_project_fetch_branch_deletion.py`의 fake Git 관측과 실제 journal, `tests/test_manager_git_journal.py`의 중단 fixture를 확장한다. 직접 ref·symbolic/dangling·동일 OID 전환 경쟁·기존 intent 재개 판단은 mock으로 검사하고 성공 판단 자체는 mock하지 않는다.
- [x] 확인한 `tests/test_origin_isolation.py:OriginIsolationScopeTest`와 `tests/support/origin.py`·`tests/support/git.py`를 재사용해 실제 역참조 쓰기 차단의 최소 Git 회귀를 둔다. 기존 수신·설정 보존 회귀와 assertion을 중복하지 않고 새 실행이 증명하는 효과를 테스트에 설명한다.
- [x] 확인한 PRODUCT의 `ExecPlan 생성과 자동 처리`·`원격 기준과 기본 worktree 보존`, ARCHITECTURE의 `역할 결과와 Manager Git 전이`에 실제로 보완한 정확한 ref 대상·경합 보존 조건만 반영한다. Requirement와 다른 ExecPlan은 수정하지 않는다.

## Validation

- Worker targeted 검사는 `tests.test_project_fetch_branch_deletion.ProjectFetchBranchDeletionTest`와 `tests.test_manager_git_journal.ManagerGitTransitionTests`의 변경한 최소 case를 실행한다. 직접 ref의 생성/갱신·동일 OID no-op과 과거 단일 fetch의 pending receipt 재개가 유지돼야 한다.
- symbolic alias가 사용자 branch 또는 다른 공유 ref를 가리키는 관측, dangling alias, 관측 뒤 동일 OID alias 전환을 주입한다. 대상 밖 쓰기 0회·alias 보존·기존 intent/실패 감사 보존을 확인한다. ref 잠금 소유자와 실제 쓰기 대상이 달라지는 실행은 허용하지 않는다.
- 최소 실제 Git 회귀에서 alias·referent·파생 `origin/HEAD`·사용자 HEAD/index/파일의 전후 값을 직접 비교한다. 정상 직접 ref가 함께 있으면 그 완료 결과를 보존하며, 이 검사를 같은 정책 조합마다 실제 저장소로 반복하지 않는다.
- 기존 `test_old_single_branch_pending_receipt_recovers_when_remote_is_equal`와 `test_project_fetch_recovers_owned_ref_update_after_result_write_crash`의 ref 쓰기 중복 방지 근거를 유지한다. 기존 journal이나 reflog가 부족한 경우 성공 기록을 합성하지 않아야 한다.

## Progress

- [x] revision `3e4b78eec608ae04c00caecaa864f1f6a5555839`의 R-092·관련 설계·ref 반영/journal·테스트를 정적으로 대조했다. 직접 구현된 일괄 fetch는 재사용하며 테스트·verify를 실행하지 않았다.
- [x] 정확한 ref 쓰기·심볼릭 대상 보존과 legacy 복구 보완
- [x] 최소 정책/실제 Git 회귀와 제품·설계 반영

## Decision Log

- [Git update-ref 공식 계약](https://git-scm.com/docs/git-update-ref#_description)은 기본 쓰기가 심볼릭 ref를 역참조하고 `--no-deref`는 ref 자체를 덮는다고 설명한다. 현재 코드와 이 계약의 대조로 대상 밖 쓰기 가능성을 식별했다. alias 보존과 쓰기 시점의 경쟁도 함께 검증하며 실제 손실 발생이나 수정 성공으로 기록하지 않는다.
- R-092의 정확한 ref·사용자 작업 보존을 구현하는 국소 교정이다. 새 원격 동기화 기능·사용자 ref 정리·branch 정책·비호환 journal migration을 도입하지 않는다. G-300은 남은 R-092 회귀/비용 근거를, G-189는 Merge 뒤 후속 차단·복구 소비자를 담당한다.
- complexity high: OID가 같아도 다른 raw ref 종류·쓰기 대상이 되는 경쟁을 기존 잠금·미완료 journal과 결합해 막아야 한다. risk critical: 잘못된 역참조는 소유권 밖 사용자 branch를 갱신해 미게시 commit의 연결을 잃게 하며 코드 rollback이 이를 복구하지 않는다. 쓰기 전 보존·정확한 대상 강제와 최소 실제 Git 회귀로 방지하고 불확실한 상태는 원문·intent를 남긴다. 최초 level은 6이며 Model·Effort는 Manager의 명시 설정을 따른다.

## Implementation Results

- fetch 책임 안의 raw 형상 관측을 쓰기·완료·복구에 적용했다. 일괄·단일 수신과 같은 project_fetch journal을 소비하는 안내용 경로도 동일한 보호를 사용한다. journal의 execute·recover_active·verified_transition과 ref lease는 변경 없이 재사용하며 다른 kind와 version 1 기록의 형식은 유지했다.
- `--no-deref` transaction을 prepare한 뒤 Git 잠금 안에서 심볼릭 여부를 검사한다. 검사 실패에는 commit을 보내지 않고 EOF로 중단한다. 별도 파일 잠금·hook 설치·최신 symref 명령 없이 Git 2.43의 기존 transaction 계약으로 구현했다.
- mock Git과 실제 journal로 alias 대상·dangling·동일 OID 경쟁·legacy pending 복구·완료 전이 보존을 확인했다. 실제 Git 한 회귀는 prepare의 정확한 ref 잠금, alias·referent·파생 HEAD 형상·사용자 HEAD/index/파일과 intent 보존을 직접 비교한다. 기존 중단 복구 회귀의 중복 쓰기 방지 assertion은 새 stdin 명령 형식에 맞춰 유지했다. 테스트 삭제·skip·assertion 약화는 없다.
- PRODUCT의 제품 보존 조건과 ARCHITECTURE의 정확한 ref 쓰기·복구 계약을 통합했다. Requirement·다른 ExecPlan·선택된 frontmatter·docs/PLANS.md는 수정하지 않았다.

## Targeted Validation Results

- 선택 adapter는 `unittest` version 1, Python은 launcher 선택이며 지원 범위는 Python 3.11–3.14다. `scripts/verify-inputs.json`의 기존 `loop_manager_internal/**`·`tests/**`가 변경 실행 입력을 포함해 추가 선언은 필요하지 않다.
- 프로젝트 Python `python3`으로 `$LOOP_MANAGER_TARGETED_TEST_RUNNER --unittest`를 사용해 `ProjectFetchBranchDeletionTest`의 기존 17개·신규 3개(총 20개)를 실행해 통과했다. 이후 추가한 transaction 응답/실패/EOF·raw 조회 실패 2개도 개별 실행해 통과했다.
- `ManagerGitTransitionTests.test_fetch_raw_shape_failure_preserves_pending_and_sealed_evidence`와 `OriginIsolationScopeTest`의 신규 exact-lock alias 회귀·기존 `test_project_fetch_recovers_owned_ref_update_after_result_write_crash` 3개를 개별 실행해 통과했다.
- `ManagerBaselineReconciliationUnitTests`의 수정한 fake 경계를 쓰는 mixed-token 복구·SQLite identity·archive 후 단일 fetch 3개를 개별 실행해 통과했다. 총 28개 targeted case가 모두 성공했고 실패 실행은 없다.
- 전체 suite·integration suite·verify·lint·build·type check는 Worker가 실행하지 않았다. Windows native 실행은 하지 않았으며 binary LF 입력과 CRLF 응답은 mock으로 확인했다. 전체 verify와 최종 완료 판단은 Manager에 인계한다.

## Worker Report Correction

- 결과 검증 재호출의 원인은 실제 targeted 실행 결과가 아니라 최종 보고의 `실패 실행 없음` 문구였다. 현재 파서는 해당 문구의 `실패`를 미해결 결과로 분류한다. 같은 보고에서 이를 명시적 `28개 targeted case: PASS`로 교체하면 기존 `parse_worker_result`가 수용함을 직접 확인했다. 파서 정책 변경은 이 작업 범위에 포함하지 않는다.
- 이전 호출의 targeted JSONL 3개(`ea1aa9b182e340c7a50bdcaf6b36484d`: 20개, `6b3ca08d99a446d8b29229ad86fa0c5f`: 3개, `64e8df31a0544acd8d5b4503524989fa`: 5개)를 읽기 전용으로 대조했다. 각 case의 시작·종료 ID가 일치하고 총 28개 모두 `success`다. 기존 기록을 변경하거나 새 실행 결과로 합성하지 않았다.
- 이번 호출은 보고·문서만 교정하므로 unit test를 추가하거나 재실행하지 않았다. 이미 성공한 구현 검사를 반복하지 않았으며, 전체 verify는 Manager에 남긴다. 이전 checkpoint 생성은 Git 메타데이터의 읽기 전용 제한으로 수행되지 않았고 이번 호출에서는 재시도하지 않았다.

## Rebase Conflict Recovery — 2026-11-05

- Manager가 지정한 `c27286e307e56f53e67b33375c7b172f00e42663`의 ExecPlan·PRODUCT·R-092를 `git show`로 확인하고 현재 작업 형상과 대조했다. 최신 R-092의 다른 작업 branch 전진 허용과 현재 FIFO 보호를 유지하며 심볼릭 대상 보호만 결합했다. sources 경로는 실제 파일과 일치하며 frontmatter·Requirement·다른 ExecPlan은 수정하지 않았다.
- 유일한 충돌은 `git/command_adapter.py`의 명령 실행 경계였다. 최신 `git_command_turn` 블록 안에 `before_commit` 전달을 통합해 프로젝트 순번과 정확한 ref 잠금 안의 raw 형상 검사를 함께 보존했다. ARCHITECTURE의 같은 책임 절에 prepare부터 commit/중단·자식 회수까지의 FIFO 범위를 명시했다.
- 이번 호출의 지정 targeted runner로 7개 case가 PASS다. 신규 `ProjectFetchBranchDeletionTest.test_prepared_ref_check_stays_inside_project_git_turn`은 정상·거부 양쪽의 순번 진입/종료와 콜백 전달을 검사한다. 같은 class의 transaction protocol·동일 OID alias 경쟁·legacy pending 복구 3개, `ManagerGitTransitionTests`의 raw 형상 복구 보존 1개, `OriginIsolationScopeTest`의 실제 Git exact-lock alias 보존 1개, `TaskSchedulingTests.test_direct_git_writes_join_turn_but_read_only_queries_do_not` 1개를 각각 최소 case로 실행했다.
- 과거 verify `e28f00056175f2d30779fd5e017bcb7c`는 감사 이력이다. 현재 유지보수 선택은 READY 유무와 독립적으로 명세 변경을 검사하고, runner 통합은 `scripts/verify.json`의 필수 `runner-integration` 단계와 `TrustedRunnerPort`의 예산·대기 heartbeat로 연결돼 있음을 현재 원본에서 확인했다. Manager의 원인 해소 평가를 현재 후보의 전체 검증 성공으로 기록하지 않으며 과거 로그만으로 같은 결함을 새 실패로 보고하지 않는다.
- 전체 verify·통합 suite·Windows native 검증은 실행하지 않았다. 코드의 충돌 마커를 제거했으며 index의 충돌 확정·rebase 계속 진행·완료·최종 전체 diff 및 공식 선언 검사는 Manager에 인계한다. 현재 Git rebase가 진행 중이므로 이번 결과는 현재 충돌 교정 형상의 targeted 근거이며 rebase 완료 후보의 전체 성공이 아니다.
