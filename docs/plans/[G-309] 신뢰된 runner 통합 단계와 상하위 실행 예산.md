---
id: G-309
title: 신뢰된 runner 통합 단계와 상하위 실행 예산
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-067] 프로젝트 공통 verify 실행 규약.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
  - docs/requirements/[R-086] 영향 기반 부분 검증과 일반 선행 Review.md
depends_on: []
complexity: high
risk: high
priority: high
---

## Purpose

Python·Perl 실제 runner 예제를 일반 unittest 자식에서 신뢰된 전체 verify의 별도 필수 통합 단계로 옮기고, 해당 단계의 상위 처리와 하위 runner가 하나의 실제 실행 시간 예산을 사용하게 한다. 두 언어의 기존 정상·native 실패 assertion과 Agent/API 차단을 보존한다. 실행 경계와 대기 관측은 함께 구현해, 분리한 통합 단계가 자식 생존을 실제 처리로 오인하거나 대기 누계를 중복 공제하는 중간 계약을 게시하지 않는다.

현재 `tests/test_verify_language_examples.py`와 `tests/test_partial_verify_runner.py:test_actual_partial_and_full_runs_preserve_manifest_and_separate_results`는 unittest 본문에서 실제 runner를 중첩 실행한다. 두 파일은 `tests/support/verify_fixture.py:nested_runner_source()`의 고정 자원 관측 bootstrap을 공유하며, 언어 예제의 Manager coordinator는 `_example_test_coordinator()`의 합성 8 CPU·고정 메모리 sampler를 사용한다. 이 fixture는 실제 수집·프로세스·manifest/native 결과 assertion을 유지하지만 신뢰된 실행 경계·실제 부하 조율·상하위 대기 예산의 충족 근거는 아니다. 내부 90초·바깥 120초의 fixture 상한과 합성 용량 확인 unit도 대기 관측 연결을 대신하지 않는다.

`runner.run()`은 가드를 설치하고 `core.execute_command()`도 `worker_environment()`를 사용하므로 일반 command로 같은 테스트를 감싸는 방식은 조율 접속·예산 관측의 소유권을 분리하지 못한다. `ExecutionClock.waiting()`은 session 파일의 누계를 합산하고 `VerifyCoordinator._allocation()`은 해당 session의 active/uncertain 예약을 검사한다. 상위에 살아 있는 하위 runner가 있을 때 전체 트리의 순수 대기를 확인하는 연결과 겹친 session 구간의 중복 공제 방지가 남아 있다. 기존 coordinator·프로세스 종료·clock·두 adapter를 재사용한다.

## Acceptance Criteria

- R-067: Manager와 단독 전체 verify가 같은 revision·지원 플랫폼/도구 조건에서 unittest·필수 compile과 별도 runner 통합 단계를 모두 수행한다. 통합 단계의 선언·예상 실행 항목·실제 완료 결과를 전체 manifest/진단에 결합하며, 누락·중복·미완료·실패·timeout은 전체 PASS가 아니다. 부분 verify는 이 단계를 성공 처리하지 않고 R-086의 기존 부분 결과·후속 실제 전체 실행을 유지한다.
- Python과 Linux 지원 Perl 예제 각각에서 standalone·coordinated의 정상 및 의도된 native 실패를 확인한다. 현재 Python compile → tests와 Perl compile → tests 순서·fixture/skip/TODO·수집/결과 identity·필수 범위를 보존한다. native 실패로 기대한 테스트가 실제 실패했는지 확인하고 인프라 오류·시간 초과·도구 부재를 기대 실패의 성공으로 바꾸지 않는다. Windows의 미지원 Perl만 명시적으로 구별하며 지원 환경의 필수 도구 부재는 실패다.
- 신뢰된 상위 단계가 runner 시작·조율 접속·예산 관측을 소유한다. 일반 테스트와 예제 테스트/command 자식에는 기존 Agent/API 거부·중첩 guard·조율 인증값 및 예산 제어값 제거를 유지한다. 일반 프로젝트의 임의 command·환경값이나 경로 이름만으로 신뢰 권한을 얻지 못한다. 실제 coordinated 결과는 session·예약·PID/시작 identity·해제의 관측으로 확인하며 요청 인수나 `coordinated=true`만으로 확정하지 않는다.
- 실제 통합 단계는 위 bootstrap의 고정 CPU·idle·메모리 값과 합성 coordinator sampler로 배정을 강제하지 않는다. 실제 자원 관측·대기와 공유 예산을 사용하며, 결정적인 sampler 입력은 별도 정책/clock unit test에 한정한다. R-086의 실제 partial/full roundtrip도 같은 신뢰 경계에서 선정·NOT_SELECTED·필수 command 미호출과 전체 결과 분리를 확인하고, 상위 전체 검증의 필수 단계 성공을 부분 PASS로 대신하지 않는다.
- R-091: 상위와 하위가 모두 배정만 기다리는 확인된 구간만 전체 예산에서 한 번 제외한다. 하위 runner 생존만으로 순수 대기를 거부하지 않으며, 상위 준비/수집/결과 처리·다른 자식의 실제 실행·종료 불명·heartbeat 단절·관측 불일치 구간은 제외하지 않는다. 병렬 자식 시간을 합산하거나 session별 겹친 대기를 중복 공제하지 않는다.
- 예산 10초에서 확인된 대기 30초·실제 처리 2초는 통과하고 실제 처리 12초는 timeout이다. 중간 대기·단계 전환·재개·재시작에 이미 소비한 시간을 유지한다. 하위 자체 기록과 상위 판정이 불일치하면 전체 성공을 거부한다. WSL·native 간 다른 monotonic 원점의 수치를 직접 빼지 않고 기존 공유 누계·lifecycle 경계를 유지한다.
- 실패·취소·pause·timeout·시작 실패에서는 하위 manifest·원래 결과·실행/대기/종료 진단을 전용 임시 경로 정리 전에 보존한다. 해당 실행의 종료 또는 미시작이 확인된 예약만 회수하고 종료 불명은 자료·자리와 함께 보존한다. 다른 실행의 자식·예약·파일은 유지하며 비밀값은 진단에 남기지 않는다.

## Implementation

- [ ] 확인한 `verification/runner.py`·`entrypoint.py`·`contract.py`·`manifest.py`, `scripts/verify.json`·`verify.sh`·`verify.ps1`·`verify_windows.py`에서 전체 검증을 소유하는 지점을 정해 자체 필수 통합 단계를 연결한다. 공통 runner의 준비된 상위 실행 경계를 사용하고 일반 guard 적용 command에서 runner를 중첩 호출하는 우회는 만들지 않는다. 필요하면 `loop_manager_internal/verification/integration.py`를 새로 두어 자체 예제 실행/결과 검증만 담당시킨다. 선언과 manifest의 호환·필수 단계 완결성을 함께 검증하고 무관한 등록 프로젝트를 자동 전환하지 않는다.
- [ ] 확인한 `tests/test_verify_language_examples.py`의 실행 본문과 `tests/support/verify_examples/python/`·`perl/`을 새 통합 단계에서 재사용한다. 정상/native 실패·도구/manifest/report·예약 회수 assertion을 이전하고, 실제 예제 경로의 `nested_runner_source()`·`_example_test_coordinator()` 합성 sampler 의존을 제거한다. 후자의 실행 소비자가 없어지면 전용 helper와 `LanguageExampleCoordinatorTests.test_nested_example_coordinator_uses_available_fixture_capacity`도 함께 정리한다. 이 unit은 폐기할 fixture 용량만 확인하므로 실제 예약·native 결과 검증의 대체 근거로 남기지 않는다. 일반 unittest의 같은 실제 runner 재실행만 제거하고 독립 계약/결과 validator unit은 보존하며 skip·빈 suite로 필수 검증을 대신하지 않는다.
- [ ] R-086의 위 partial/full roundtrip을 같은 상위 통합 경계로 이전한다. 실제 Git 후보·선정 모듈/테스트 ID·문서 변경 identity·남은 불확실성·NOT_SELECTED·compile 미호출·partial/full manifest payload 대응 assertion을 보존한다. `tests/support/verify_fixture.py`의 두 소비자와 `test_nested_runner_fixes_resource_observation_and_restores_sampler`를 함께 대조해, 고정 관측 bootstrap이 더 이상 필요 없으면 전용 helper와 그 복원 테스트를 정리한다. 제거한 검증과 새 단계·기존 정책 unit의 대응을 남기며 timeout·종료 불명·선정/결과 validator unit은 유지한다. 실제 sampler의 정책 조합을 중복 subprocess로 늘리지 않는다.
- [ ] 확인한 `verification/execution_clock.py`·`coordination.py`·`manager_adapter.py`와 `execution/verify_process.py`의 실행별 clock·heartbeat·session/예약·PID guard를 사용해 상하위 실행 관계와 순수 배정 대기를 연결한다. 부모의 준비·처리와 다른 활성 자식까지 확인해 공제 가능한 구간을 결정하고, 겹치는 대기·중단/재시작의 누계 identity와 단조성을 보존한다. 일반 자식의 누계 파일이나 자체 선언을 권위 있는 시간 제어로 사용하지 않으며 종료 불명·기록 손상/불일치는 성공으로 확정하지 않는다. 새 daemon·CPU 강제 배정·timeout 증가는 도입하지 않는다.
- [ ] 확인한 `verification/worker_transport.py`의 `Worker`·`start_worker()`·`SerialClient`와 `guard.py:worker_environment()`를 두 adapter의 공통 전송·환경 경계로 재사용한다. `unittest_adapter.py`의 discovery/fixture·프로젝트 interpreter와 `perl_adapter.py`의 native 명령/TAP 판정은 각 adapter에 남기며 전송·직렬 예약 제한을 다시 구현하지 않는다. 신뢰된 상위의 한정된 실행 port와 일반/예제 자식을 구별하고 가드 해제나 인증값 전달 예외로 해결하지 않는다. 기존 `VerifyTerminationUncertain`, Windows Job·Linux 트리 종료, WSL receipt·실행별 정리 및 입력 revision 연결을 재사용한다.
- [ ] 확인한 `tests/test_verify_contract.py`·`test_verify_runtime.py`·`test_task_scheduling.py:ExecutionClockTests`·`test_verify_windows_launcher.py`의 clock/process/환경 fixture를 확장한다. 필요하면 `tests/test_verify_integration.py`를 순수 orchestration·결과 validator unit용으로 추가한다. 실제 runner 통합 실행은 새 필수 단계의 책임이며 Worker targeted command로 옮기지 않는다.
- [ ] `scripts/verify-inputs.json`의 현재 코드/fixture/선언 범위가 실제 통합 단계의 모든 소비 입력을 포함하는지 확인해 필요한 누락만 보완한다. `docs/VERIFY.md`·ARCHITECTURE의 실행 경계·결과·시간 예산을 실제 구현에 맞춰 통합한다. G-295는 Windows 준비·정리의 남은 deadline 연결을, G-187은 실제 OS 격리·동시 부하·동일 조건 실측을 담당하며 이 작업에서 중복 구현하거나 완료로 표시하지 않는다.

## Validation

- Worker는 위 기존 모듈의 변경 관련 최소 unit만 실행한다. fake monotonic clock·자원 sampler·프로세스 관측을 사용하되 실제 clock·조율·결과 validator를 통과시킨다. 선언/단계 누락·결과 중복·범위/revision/session 불일치·부분 결과의 전체 승격 거부와 Manager/단독의 필수 항목 일치를 검사한다.
- 부모가 살아 있는 하위 runner를 관리하며 순수 대기하는 경우, 부모 처리나 형제 실행이 겹치는 경우, 두 하위의 대기가 겹치는 경우를 구별한다. 10/30/2초·12초 초과, heartbeat 단절·종료 불명·누계 역행/손상·대기 근거 불일치·중간 재개/재시작을 확인한다. 예산 인수 전달만 검사하지 말고 실제 남은 시간·timeout 판정·후속 호출 차단을 assertion으로 둔다.
- 기존 `tests/test_verify_contract.py:CandidateEnvironmentTests`·`CoordinatorIdentityTests`, `tests/test_verify_runtime.py:GuardInstallationTests`·`ManagerAdapterTests`의 인증값 제거·중첩 Python/Agent 거부·다른 run 보호를 유지한다. 공통 전송의 환경 정제/assignment identity와 serial 거절 뒤 재시도·bind/idle/release 전달은 같은 파일의 `VerifyContractTests` 두 직접 회귀를, pipe·JSON·비정상 종료·취소의 종료 확인 뒤 release는 `test_verify_runtime.py:RunnerPoolTests.test_pool_failure_paths_stop_before_release_and_preserve_original_error`를 재사용한다. 두 adapter의 spawn/serial 연결 회귀를 남기고 같은 공통 전송 assertion을 복제하지 않는다. 신뢰되지 않은 일반 command가 조율/예산 권한을 얻지 못하고 부모 환경이 보존되는지 검사한다.
- 새 필수 통합 단계에서 두 언어 × standalone/coordinated × 정상/native 실패를 실제 실행한다. session 접속·예약/bind/release와 native assertion·수집 범위·필수 compile·실제 종료를 대조하고, 기대 실패의 assertion 변경이 timeout·통신 실패로 통과하지 않아야 한다. 실패/취소 진단을 정리 전에 남기며 종료 불명/다른 run은 보존한다. 지원 도구 부재를 skip으로 숨기지 않는다.
- 이전한 partial/full 실제 회귀는 동일 discovery의 선택 모듈 1개·필수 테스트/fixture skip, 미선택 5개 모듈의 NOT_SELECTED, 문서의 변경 전후 identity와 불확실성, 부분 단계의 compile 미호출 및 후속 전체 manifest/command를 계속 검사한다. 일반 unittest의 실제 중첩 실행과 고정 관측 bootstrap·언어 예제의 전용 합성 coordinator/helper unit을 정리한 뒤에도 이 대응이 빠지지 않아야 한다. 실제 두 언어 통합 단계의 acquire/bind/release·필수 결과·종료 assertion을 유지하고 제거한 fixture 전용 검증과의 대응을 결과에 남긴다. 상위 통합 단계의 실행은 실제 sampler·session 경로를 사용하고, fake sampler의 대기/예산 조합은 앞의 독립 unit 회귀로 구분한다.
- Windows 전체는 기존 WSL 전체와 native 핵심 회귀의 결합을 유지한다. Linux unit 결과나 테스트 이동만으로 Windows 실행·OS 격리·성능 성공을 기록하지 않는다. G-187의 실제 자료 수집과 G-295의 준비/정리 보완은 별도 완료 조건이다.

## Progress

- [x] revision `9f877ad110999e0f198f7977d23d31d6b8eb5d89`의 선택 adapter(version 1 unittest, launcher Python 3.11–3.14)·지원 계약·예제 unittest 중첩 호출·guard·session 대기 누계와 관련 회귀를 정적으로 대조했다. 구현·테스트·verify는 실행하지 않았다.
- [ ] 신뢰된 필수 통합 단계·두 언어 및 partial/full 실제 회귀 이전과 상하위 실행 예산 연결
- [ ] 최소 unit·실제 필수 단계의 결과/종료 근거 및 문서 정합성 확인

- [x] 관리 rebase 완료 뒤 HEAD `56364d86ee1739d7e89dc242f7fb44ded56adff1`의 현재 계획·sources·base 대비 전체 변경을 다시 대조했다. verify feedback `verify:171739809e3963eccd204dde3617e29d64b49847762e42a76cdfa8337dc54b05` occurrence 1/revision 1의 실패를 개별 targeted unit으로 재현했다. 공정 배정 fixture는 PID 100의 시작 identity를 살아 있는 상태로 둔 채 stopped=True를 선언해, 새 종료 확인 계약과 모순됐다. 같은 파일의 release 회귀처럼 identity 제거로 종료 관측을 먼저 반영한 뒤 finish를 호출하도록 fixture만 수정했다. 배정 순서·pending CPU/메모리·다른 session 예약 보존 assertion과 실제 종료 확인 구현은 유지했다.

FAILED_COMMAND: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_verify_resources.ReservationTests.test_waiting_verify_gets_turn_and_other_reservations_survive_finish`
Initial result: ERROR (1 test), VerifyTerminationUncertain으로 전달된 전체 verify 오류를 재현했다.
RERUN: `python3 "$LOOP_MANAGER_TARGETED_TEST_RUNNER" --unittest tests.test_verify_resources.ReservationTests.test_waiting_verify_gets_turn_and_other_reservations_survive_finish`
Final result: PASS (1 test).

- [x] 수정 뒤 같은 runner로 `tests.test_verify_resources.ReservationTests` 9개 통과. 추가로 `tests.test_verify_integration.ChildCoordinatorTests.test_ancestor_exit_cannot_reclaim_live_descendant_runner_or_worker`와 `test_active_and_uncertain_sibling_block_wait_and_other_run_is_preserved`를 개별 ID로 실행해 2개 통과했다. 실제 생존한 하위 runner/worker 회수 거부와 독립 예약 보존을 현재 후보에서 확인했다. 이전 후보의 성공은 이번 호출의 실행 결과로 사용하지 않았다. 기존 tests/** 입력 glob이 fixture 수정을 포함한다. 전체 verify·실제 통합/native·Review는 실행하지 않았으며 외부 검증은 별도로 남는다. 호환 코드·self-runtime 정책과 사용자 판단 기록은 변경하지 않았다.

## Decision Log

- revision `5395d93192ede14c777bb26aebca4b2f4a98b769`의 공통 worker 전송 분리는 현재 코드와 두 adapter·pool 회귀로 소비한다. 남은 신뢰된 통합 단계·상하위 대기 예산·실제 예제 이전은 그대로이며 전송 추출의 재구현이나 새 선행 작업을 만들지 않는다. snapshot token `4a5912aa2b0768f4afe120f9f6c10856d16b732ca30d00f65aa092e6cf12a9cf`의 ID·경로·metadata·dependency와 기존 기록을 보존한다. 이번 정적 대조는 테스트·verify·Review 성공이나 차단 해제가 아니다.

- 이번 delta의 `_example_test_coordinator()`와 전용 용량 unit은 중첩 예제 fixture의 안정화다. 실제 부하/신뢰 경계의 충족으로 채택하지 않고 기존 이관 작업에 helper·전용 unit의 참조 정리만 반영한다. native 성공/실패·manifest·예약·종료 검증은 새 필수 통합 단계와 기존 정책 unit에서 계속 보장하며 테스트가 느리거나 실패한다는 이유로 범위를 줄이지 않는다. snapshot의 ID·경로·metadata·dependency·기존 기록을 보존하고 구현·테스트는 실행하지 않았다.

- `2464194a` → `f7909364` delta의 공유 고정 관측 helper·두 소비자와 상한 조정을 정적으로 대조했다. 기존 R-067/R-091 실행 경계와 R-086 결과 분리 목표 안에서 이관·정리 대상을 구체화하며 새 계획이나 부하 정책을 추가하지 않는다. snapshot token `08819886363176af579bd36518ba720176da782bb013723c45dfe8dc359608a5`의 원본 hash와 일치함을 확인했고 ID·경로·metadata·dependency·기존 기록을 보존한다. fixture 안정화나 과거 unit 기록을 실제 조율·통합 성공으로 채택하지 않았으며 구현·테스트·verify는 실행하지 않았다.

- R-067의 실행 경계와 R-091의 상하위 대기는 같은 통합 실행 계약으로 묶는다. 기존 비종료 G-187은 실제 자료 수집, G-295는 Windows launcher의 준비/정리 누락이므로 별도 구현을 맡기고 해당 계획은 이 결과를 소비한다. 두 adapter·예제·종료/clock의 기존 구현과 회귀를 재사용한다.
- complexity high: 상위/하위 처리·겹친 대기·재시작 예산과 신뢰된 실행/일반 자식의 경계를 함께 결정해야 한다. risk high: 잘못된 대기 공제·단계 누락은 필수 검증을 성공으로 오인시키고 잘못된 종료 확인은 독립 실행을 손상할 수 있다. 소유 identity·guard·확인된 종료·원본 진단을 보존하고 불일치의 성공 거부와 중단/경쟁 회귀로 완화한다. 외부 종료 효과는 코드 rollback만으로 복구되지 않는다.
- priority high는 반복되는 검증 기반 오류와 잘못된 전체 성공을 막기 위한 것이다. 최초 level은 5이며 Model·Effort는 Manager 설정을 따른다. 새로운 adapter·권한 완화·비호환 상태 변경·외부 infrastructure는 요구하지 않는다. 이 delta 검토는 프로젝트 최종 전문 확인이나 실행 성공이 아니다.
