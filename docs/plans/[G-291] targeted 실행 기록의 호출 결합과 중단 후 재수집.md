---
id: G-291
title: targeted 실행 기록의 호출 결합과 중단 후 재수집
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
depends_on: []
complexity: high
risk: high
priority: high
---

## Purpose

물리 Worker 호출의 targeted 기록을 해당 repository·ExecPlan·invocation에 결합하고 Manager 중단 후에도 미반영 결과를 재수집한다. 현재 `invocation_service.run_agent()`는 메모리의 `prepared_launch.attempt`를 `finally`에서 한 번 읽는다. `reconcile_interrupted()`는 미종료 invocation을 정리하지만 그 폴더를 다시 수집하지 않는다. 테스트도 finally가 실행되는 KeyboardInterrupt와 이미 SQLite에 들어간 결과의 재개만 확인한다. 기록 생성 후 finally 이전 종료나 수집/저장 실패에서는 실제 결과 파일이 남아도 다음 호출이 다른 폴더를 사용해 누락될 수 있다.

## Acceptance Criteria

- R-055/PRODUCT: 같은 Worker 세션 내부 재실행·CLI retry·fallback과 Manager 재시작에서 확인된 개별 실행 결과를 잃지 않고 한 번만 반영한다. 기록 폴더·실행 ID를 물리 invocation, repository·ExecPlan·역할·현재 소유 작업에 지속적으로 연결하고 다른 작업/호출의 자료를 가져오지 않는다.
- 완료한 개별 결과가 파일에 남고 SQLite 반영 전 Manager가 중단돼도 재개 경계에서 재관측한다. 일부 줄·실행 미완료·파일 부재를 테스트 실패나 성공으로 합성하지 않는다. 기록 경로·소유권·종료 근거가 불확실하면 그 자료와 해당 작업만 보존·대기하며 다른 작업을 막지 않는다. 살아 있는 writer나 종료 불명 자원을 임의 정리하지 않는다.
- 수집·해석·SQLite 저장 실패의 미소비 원문과 재수집 근거를 보존한다. 재시도에서 기존 중복/상충 검사와 원자 관측을 유지한다. 불확실한 수집을 테스트 세 번 실패나 완료 보고 교정 소진으로 바꾸지 않으며 실제 provider 정책 거부·문서 오류·독립 안전 차단은 기존대로 구분한다.
- 재수집으로 세 번째 실패가 확인되면 새 Worker·CLI retry·verify 진입 전에 기존 `targeted_test_exhausted` 경계에서 처리한다. 차단 확정과 도달 표식 소비는 원자적이고 실행·실패 감사는 남는다. 이미 처리한 기록을 재생해 중복 차단하거나 실패 횟수를 초기화하지 않는다.
- 현재 Codex의 호출별 쓰기 허용 임시 폴더와 프로젝트 Python/runtime runner 분리를 유지한다. 공통 홈·다른 호출·사용자 원본의 쓰기 권한을 넓히지 않고 runtime/모델 교체로 과거 기록 귀속을 잃지 않는다. 기존 파일·DB를 삭제하는 migration이나 비호환 상태 변경은 포함하지 않는다.

## Implementation

- [x] 확인한 `loop_manager_internal/agents/invocation_service.py`의 `observe_launch`·finally 수집과 `agents/agent_invocation.py`의 물리 retry identity를 재사용한다. `agents/process_receipt.py`와 `storage/agent_invocation_state.py`에 직접 반영된 실행 전 receipt 경로 결합·종료 복구를 소비하며 종료 ledger를 새로 만들지 않는다. 이 증거에 targeted 기록 위치·미소비 수집을 결합해 정상/예외 종료·중단 복구에서 같은 자료를 재관측한다. receipt 부재·불확실·live writer를 폴더 존재나 result_code로 대신 확인하지 않는다. Codex 전용 launch 객체가 없는 공급자도 소비할 수 있는 최소 기록 문맥을 두되 실제 Claude 연결은 G-292에 남긴다.
- [x] 확인한 `loop_manager_internal/storage/agent_invocation_state.py`의 ledger·`reconcile_interrupted()`와 `storage/targeted_test_state.py`의 중복 없는 저장을 연결한다. 기존 scope·호출/프로세스 종료·수동 작업 보호를 재사용하고 필요한 비밀값 없는 결합 정보만 호환되게 보존한다. 광범위한 상태 폴더 탐색으로 경로의 repository·작업 귀속을 추측하지 않는다.
- [x] 확인한 `loop_manager_internal/verification/targeted.py:collect()`의 경로/크기·identity·완료 줄 검사와 `execution/execution_worker_handler.py`의 호출 전/후 차단·증거 부재 판정을 재수집 경계와 연결한다. 수집 오류를 provider 설정 실패나 rebase 완료 보고 횟수로 오인하지 않도록 `execution_rebase_handler.py`·`recovery/recovery_runtime.py`의 기존 targeted correction/pause를 사용한다. 실제 독립 오류는 숨기지 않는다.
- [x] 확인한 `tests/test_agent_invocation.py`·`tests/test_targeted_test_streak.py`의 임시 파일·실제 SQLite fixture와 `tests/test_execution_worker_phase.py`의 일반/rebase 경계를 확장한다. 중단 위치를 주입해 durable 재관측을 검사하고 실제 유료 Agent·Git 실행은 추가하지 않는다. PRODUCT·ARCHITECTURE에는 반영한 복구 경계만 통합한다.

## Validation

- Worker는 위 세 모듈에서 변경한 최소 case/class만 실행한다. 파일 생성과 SQLite를 실제로 사용하되 provider·프로세스 종료·Git 관측은 기존 double로 제공한다. finally를 생략한 중단 상태를 저장 fixture로 구성하고 새로운 repository/Manager 실행 전체를 반복하지 않는다.
- 기록 위치 결합 전/후, 완료 JSONL 생성 후 finally 이전, 수집 후 DB commit 이전, DB 반영 후 차단 이전 및 차단 transaction 중단을 각각 대조한다. 재시작 후 확인된 결과는 정확히 한 번 반영되고 세 번째 실패는 다음 Worker 없이 기존 차단으로 이어져야 한다.
- `tests/test_agent_process_receipt.py`의 종료 proof·실패 결과 불변 회귀를 재사용한다. 이 작업의 최소 파일/SQLite fixture에서는 receipt 복구 전후에 targeted 원문을 한 번만 소비하고 live·종료 불명에서는 원문·미소비 상태를 보존하는 연결만 검증한다. 이미 있는 native 프로세스 그룹 종료 테스트를 조건별로 반복하거나 복구 관측 시각을 실제 종료 시각/실행 시간으로 합성하지 않는다.
- CLI transient retry·session fallback·새 session/후보·같은 호출의 재관측에서 모든 확인된 실행을 동일 작업에 연결한다. 서로 다른 repository·ExecPlan·invocation, 과거/현재 runtime과 수동 실행 중인 writer의 기록이 섞이거나 삭제되지 않아야 한다.
- 부분 마지막 줄·손상 JSON·중복/상충 ID·경로 교체/symlink·기록 부재·읽기 실패와 SQLite rollback을 검사한다. 검증 불가 자료를 실패로 합성하지 않고 기존 결과·원문·미소비 복구 근거를 유지해야 한다. 오류 뒤 정상 재관측도 같은 실행을 중복 집계하지 않아야 한다.
- 일반 Worker와 rebase 교정의 새 근거 부재는 일시 중단이며 기존 문서 보고 한도를 소비하지 않아야 한다. 실제 provider 정책 실패·완료 보고 오류·verify/Review 실패와 독립 사용자 판단은 유지한다. invocation의 물리 종료·사용량 감사가 수집 오류 때문에 미완료로 남지 않는 기존 회귀도 유지한다.

## Progress

- [x] delta의 invocation·runner·저장·Worker/rebase 변경 및 `agent_invocation_state.py`의 재시작 조정을 정적으로 대조했다. DB 반영 후의 재개와 Python finally 종료는 직접 구현된 것으로 구분했다.
- [x] 물리 호출의 기록 위치 결합·미소비 수집 복구
- [x] 중단·경합·중복·증거 부재의 최소 unit 회귀

## Decision Log

- 2026-10-15 delta: 호출별 process receipt와 종료 재관측은 직접 구현됐다. targeted JSONL의 지속 결합·미소비 재수집은 여전히 finally 밖에 없으므로 기존 목표를 유지하되 새 종료 증거를 재사용한다. G-290의 분류·G-292의 Claude 전달과 dependency는 보존하며 현재 Planner는 구현·테스트·verify를 실행하지 않았다.
- G-290의 native 분류와 독립적으로 구현·검증할 수 있다. 그 결과 taxonomy와 기존 결과 스키마를 호환되게 소비하며 source·의존성 변경으로 실제 테스트 실패 한도를 우회하지 않는다. G-189는 두 구현과 Claude 연결 뒤 후속 소비자를 감사한다.
- complexity high: 파일 기록·물리 호출 종료·DB 반영·차단 소비 사이 중단과 살아 있는 writer의 소유권을 일관되게 연결할 판단이 남아 있다. risk high: 누락·오귀속이 필수 차단을 우회하거나 다른 작업을 잘못 차단하며 사후 수동 복구에는 원문과 호출 결합이 필요하다. 미소비 자료·기존 결과를 보존하고 정확한 identity·transaction·중단 회귀로 완화한다. priority high는 실제 실행 근거 보존이라는 필수 무인 계약 때문이다.
- 쓰기 허용 경로·인증 저장·Git 전이·같은 프로젝트 병렬 실행을 넓히지 않는다. 비호환 DB/상태 변경이 필요하면 해당 구현을 멈추고 Architecture Change Policy의 판단을 남긴다. additive 결합의 기술 선택을 사용자 정책의 새 승인으로 만들지 않는다.
- 선택된 unittest version 1·launcher Python 3.11–3.14·기존 필수 compile과 Agent guard를 유지한다. Planner는 구현·테스트·verify를 실행하지 않았으며 기존 KeyboardInterrupt/DB 재개 테스트의 성공을 새 중단 구간의 검증으로 주장하지 않는다.

## Implementation Record

- 2026-10-16 Worker: invocation ledger에 결합한 `targeted_test_collections`와 실행 ID 귀속을 additive schema로 추가했다. 실행 전에 기존 private call 디렉터리의 정규 경로·device/inode와 기존 process receipt를 저장하며, Codex launch callback과 launch 독립적인 `on_targeted_prepared` port를 제공한다. 실제 Claude 연결은 추가하지 않았다.
- finally·`reconcile_interrupted()`·Worker/CLI retry 전후 경계가 같은 종료 receipt를 사용해 미소비 결과를 재관측한다. 결과·실행 ID 귀속·소비 완료를 같은 transaction에 저장한다. 앞선 호출이 불확실하면 같은 작업의 후속 결과를 기다리며 다른 repository·ExecPlan은 수집한다. 종료 불명·수동 writer·경로 교체·symlink·부분 줄·해석/읽기/저장 오류에서 원문과 귀속을 보존한다.
- 수집 대기는 provider의 원래 결과·사용량·종료 감사를 유지하는 별도 표식으로 전달한다. 정상 완료가 검증돼도 미소비 기록이 있으면 targeted correction pause로 처리하며 독립 provider 거부·보고 오류·사용자 결정은 기존 경계를 따른다. 재수집된 세 번째 실패는 다음 Worker·CLI retry·verify 전에 기존 원자 차단/도달 표식 소비로 처리한다. rebase targeted correction은 일반 완료 보고 occurrence를 올리지 않는다.
- PRODUCT의 실패/복구 보장과 ARCHITECTURE의 호출 결합·저장 형식·재수집/대기 계약을 갱신했다. source 경로는 실제 R-055 파일과 일치하며 frontmatter·Requirement·다른 ExecPlan·PLANS는 수정하지 않았다. verify 입력은 기존 `loop_manager_internal/**`·`tests/**` glob이 변경 코드를 포함하므로 목록 변경이 필요하지 않았다.

## Targeted Validation Record

- Linux / Python 3.12.3, `scripts/verify.json`의 unittest version 1·launcher Python과 `docs/VERIFY.md`의 Python 3.11–3.14 계약을 확인했다. 프로젝트 Python으로 `LOOP_MANAGER_TARGETED_TEST_RUNNER`를 실행해 지정된 기록 폴더에 결과를 남겼다. 실제 Agent·Git·native 프로세스 그룹 테스트는 추가 실행하지 않았고 전체 suite·verify·lint·build·type check는 실행하지 않았다.
- `test_targeted_test_streak`의 TargetedRecoveryTests(8), TargetedRunnerTests(4), WorkerTargetedFailureTests(최초 최종 묶음 9)와 `test_agent_invocation.RunnerTests`의 수집 오류/KeyboardInterrupt/독립 provider 실패 3 case, `test_execution_worker_phase`의 rebase 수집 대기 1 case: 최종 동일 명령 재실행 25개 PASS. 최초 24개 묶음의 1개 실패는 차단 소비용 SQLite trigger가 결과 수집의 반복 표식 UPDATE에도 작동한 fixture 오류였다. trigger를 `OLD.repeated=1 AND NEW.repeated=0`으로 좁혀 동일 명령 24개 PASS 후, 반환 결과 보존 변경과 새 정상 보고 대기 회귀를 포함해 같은 명령 25개 PASS를 확인했다.
- TargetedHistoryTests 5개, 명시적 사용자 결정/rebase 개별 이력 2 case, 기존 ProcessReceiptTest의 복구/불확실/실패 결과 불변/종료 관측 유실 4 case: 11개 PASS.
- 물리 retry·session fallback 기록 귀속 1 case PASS. CLI retry 직전 재수집 차단 1 case는 fixture의 실행 중 state_dir 교체로 최초 실패했으며 상태 루트를 호출 전에 고정한 뒤 동일 명령 재실행 PASS. live writer의 실패 보고에서 scope·미소비 자료를 유지하는 1 case PASS. 최종 확인한 서로 다른 targeted unit test는 39개다. rebase의 소비된 turn 정리와 원문 audit를 유지하면서 occurrence만 제외하도록 저장소 경계를 보완한 뒤, 관련 rebase 2 case와 일반 수집 대기/scope 보존 2 case를 재검증해 4개 PASS를 확인했다.
- 파일 생성·실제 SQLite 재열기와 transaction trigger로 finally 생략, collect 뒤 DB 저장 전 중단, DB 저장 실패/rollback, 저장 뒤 차단 전 재개, 차단 transaction rollback을 확인했다. 수동/live writer는 종료 관측 double을 사용하고 기존 native receipt 회귀의 의미를 유지했다. Windows 및 전체 verify 결과는 Manager 검증 대상으로 남는다.
- 2026-10-16 결과 보고 교정: 이전 최종 응답은 두 명령 쌍에서 `FAILED_COMMAND`와 `RERUN` 사이의 최초 실행 결과를 생략하고 그 설명을 앞에 두어 기존 Worker 결과 검증에서 거부됐다. 구현·검증 정책은 유지하고 각 명령 쌍 사이에 최초 결과, 재실행 뒤에 최종 결과를 명시하도록 보고를 교정했다. 기존 `tests.test_worker_result_contract.WorkerResultContractTest.test_two_corrected_runs_keep_initial_outcomes_and_clean_final_results` 1 case를 지정된 targeted runner로 실행해 PASS했고, 실제 교정 보고문도 `parse_worker_result()`로 수용됨을 확인했다. 이번 교정은 선택 ExecPlan의 기록과 최종 보고에 한정하며 이미 통과한 구현 테스트와 전체 verify는 재실행하지 않았다.
- 2026-10-16 Review 교정(`directory-scan-error-consumed`): 관리 rebase 후 HEAD `919a868`과 base `191c154`의 전체 diff, 최신 ExecPlan·PRODUCT·R-055 및 unittest version 1/launcher Python 3.11–3.14 계약을 재확인했다. `Path.glob()`의 디렉터리 열거 오류 억제로 미소비 기록이 빈 결과로 확정되는 경로를 `os.scandir()`와 기존 OS별 파일명 규칙을 유지하는 `fnmatch`로 교체했다. 새 `TargetedRecoveryTests.test_directory_scan_error_preserves_unconsumed_results_until_retry`는 PermissionError/OSError의 직접 전파, 원문·error 상태·미소비 보존, DB 재열기 후 세 결과/도달 표식 복구와 중복 없는 재관측을 확인한다. 현재 후보에서 이 case와 기존 파일 읽기/경로 교체, 빈 수집/부분 줄, Worker 차단/CLI retry, KeyboardInterrupt 수집, rebase 대기의 관련 8 case를 지정된 targeted runner로 실행해 9개 PASS했다. 파일명 매칭 보완 후 같은 9개도 PASS했다. 이전 후보의 성공을 현재 후보의 근거로 재사용하지 않았고 전체 verify·통합/native 프로세스 검증은 실행하지 않았다. PRODUCT·ARCHITECTURE의 기존 읽기 오류 보존 계약과 verify 입력 glob은 그대로 적용하며 이번 교정에서 추가 수정하지 않았다.

- 2026-10-16 추가 관리 rebase 수용 후 재검증: HEAD `199c652`, base `7ff893a`의 전체 최종 diff와 최신 ExecPlan·PRODUCT·R-055·adapter 계약을 다시 대조했다. `os.scandir()`의 오류 전파와 신규 재수집 회귀가 그대로 유지돼 코드 추가 수정은 하지 않았다. 현재 후보에서 지정된 targeted runner로 신규 열거 재수집, 기존 파일 읽기/경로 보호, 빈 기록/부분 줄, Worker 차단/CLI retry, KeyboardInterrupt 수집, rebase 대기의 동일 최소 9 case를 새로 실행해 모두 PASS했다. 이전 후보의 결과는 이번 근거로 재사용하지 않았으며 전체 verify·Review는 Manager에 인계한다.
