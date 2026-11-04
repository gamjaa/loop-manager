---
id: G-347
title: bootstrap 회귀 실패의 비용과 진행 근거 보존
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-065] 실제 부하 기반 verify 병렬 실행.md
  - docs/requirements/[R-067] 프로젝트 공통 verify 실행 규약.md
  - docs/requirements/[R-080] Git Mock Defaults for Tests.md
  - docs/requirements/[R-020] Manager-Owned Runtime Worktree Scope Reconciliation.md
depends_on: []
complexity: medium
risk: medium
priority: high
---

## Purpose

bootstrap 저장 상태 회귀가 timeout이나 assertion 실패로 끝나도 이미 기록된 부모·자식 CLI 비용과 진행 근거를 임시 폴더 정리 전에 기존 verify 진단에 보존한다. 실패한 실행의 병목을 다시 조사할 수 있게 하는 국소 변경이며, 보고 409의 timeout 원인이나 성능 개선을 확정하는 작업은 아니다.

원격 base의 `tests/support/bootstrap_saved_scope.py`는 정상 경로 마지막에서만 비용 기록을 전달한다. 보존 worktree의 현재 HEAD `52aa404ded19ca5f684717184074b5f9e7bd59e1`에는 정리 전 회수·중복 방지·정상 수집 완결성 검사와 최소 unit이 이미 있다. 이 구현·기록을 보존해 최신 base에 통합하며 이미 수행한 보완은 반복하지 않는다. 최신 `timing.summarize()`는 상위 run ID와 다른 이벤트뿐 아니라 ID가 없는 중첩 기록도 집계에서 제외한다. 후보의 미확인 시간 연결 unit은 상위 run/manifest가 없는 디렉터리만 검사하므로 실제 전체 verify의 원문 보존·진단 접근과 상위 집계 분리를 추가 확인해야 한다. 기존 미확인 경과 시간 처리와 targeted 수집기의 비용 진단 제외는 재사용한다. G-187의 실제 OS·부하 비교와 G-295의 Windows 예산은 별도 목표다.

## Acceptance Criteria

- R-065/R-067: 성공·timeout·자식 실행 오류·후속 assertion 실패 모두에서 현재 소유한 fixture의 수집 가능한 부모/자식 비용 기록이 임시 폴더 정리 전에 기존 상위 verify 기록으로 전달된다. 원래 PID·시각·clock domain·phase/test identity와 완료 여부를 유지하고, 부모가 포함하는 자식 시간을 별도 전체 경과 시간으로 더하지 않는다.
- 실패 직전까지 flush된 완전한 이벤트는 보존한다. 강제 종료로 마지막 줄이 불완전하거나 파일이 일부 없을 때도 앞선 수집 가능한 근거를 잃지 않고 수집 한계를 남긴다. 미완료 phase의 종료·duration·성공이나 없는 기록을 합성하지 않는다. 소유 중첩 기록의 진단에서 clock domain 비교가 불가능한 미완료 이벤트는 `elapsed: null`과 `경과 시간 미확인` 표시를 유지한다. 중복 회수 경로가 같은 이벤트를 두 번 전달하지 않는다. 정상 경로는 수집 완결성을 확인한 뒤 기존 phase 개수·Git 비용 assertion을 모두 수행하며, 수집 실패를 성공 경로의 검사 생략으로 바꾸지 않는다.
- R-067: 실제 상위 `runner.jsonl`·manifest가 있는 경우에도 정리된 fixture의 원문·수집 한계와 미완료 위치를 기존 verify 진단에서 찾을 수 있다. 상위 요약은 정확히 일치하는 run ID의 이벤트만 집계하며, ID 부재·다른 run의 bootstrap 기록을 상위 phase·Git 비용·결과·경과 시간에 합산하지 않는다. 원문의 run ID를 상위 값으로 소급 채우거나 필터를 완화하지 않고 소유 중첩 진단으로 연결한다. 비용 기록은 결과 receipt·조율·종료 증거가 아니다.
- timeout·실행 오류·기존 assertion의 실패 종류와 원래 출력은 유지된다. 진단 회수의 읽기/형식 오류가 원래 실패를 덮거나 성공으로 바꾸지 않는다. 상위 writer가 없는 단독 unit 환경에서도 원래 테스트 결과를 유지하고, 비용 기록을 필수 성공 receipt나 조율/종료 근거로 사용하지 않는다.
- R-020/R-080: 현재 두 bootstrap 회귀의 역할별 실제 자식 dispatch, main Worker/Reviewer·develop Planner, A의 저장 상태에서 B runtime 재개, B 선택 뒤 C 전진과 다음 실행의 C 선택, 같은 SHA 재사용·등록 직후 중단의 기존 대응을 유지한다. session·worktree·Worker 파일·상태·실패 이력·project block 부재와 exact-SHA runtime assertion을 삭제·skip하거나 성공 mock으로 바꾸지 않는다.
- 기존 Linux 90초/Windows 180초의 내부 제한과 전체 verify 예산·수집 범위·Agent 가드·Git 격리·종료 확인을 유지한다. 계측 보존만으로 timeout이 해결됐다고 보고하지 않는다. 추가 실제 CLI 반복이나 별도 benchmark·runner·전역 cache·새 진단 저장소를 만들지 않는다.

## Implementation

- [x] 보존 후보의 `tests/support/bootstrap_saved_scope.py:_collect_bootstrap_costs()`·`_track_bootstrap_costs()`와 `_run_child()`·`_assert_bootstrap_costs()`의 정리 순서를 최신 base에 통합한다. HEAD `52aa404`에 구현된 회수·중복 방지·수집 완결성 검사·필수 assertion을 보존하고 실제 통합 차이만 교정한다. 실패 경로에서는 회수 오류가 원래 timeout·실행/proof 오류를 덮지 않도록 구분한다. 파일/프로세스 소유 범위와 기존 `run_verify_process()`의 종료 결과를 바꾸지 않는다.
- [x] 회수는 이 fixture가 생성한 파일만 읽고 기존 이벤트 identity를 보존한다. 중단된 JSONL의 완전한 줄·마지막 미완성 줄, 읽기 오류와 원래 테스트 예외를 구분한다. 필요한 수집 한계는 기존 테스트 진단 방식에 남기며 조율 인증값·전체 환경이나 사용자 문서를 출력하지 않는다. 공통 runner의 결과 판정·시간 합산·reservation schema를 이 fixture 문제로 변경하지 않는다.
- [x] 상위 writer에 회수한 raw JSONL과 기존 테스트 진단을 재사용해 소유 중첩 기록의 위치·수집 상태를 연결한다. `verification/timing.py:summarize()`의 엄격한 run 필터를 유지하면서, 요약에서 제외된 원문도 fixture 정리 뒤 조회할 수 있게 한다. 필요한 별도 보존 경로는 같은 verify 진단 디렉터리 안에 한정하며 새 진단 저장소·전역 이벤트 schema를 만들지 않는다. `tests/test_verify_runtime.py:TimingAndManifestTests.test_summary_does_not_adopt_unowned_nested_diagnostics`의 직접 교정은 다시 구현하지 않는다.
- [x] 확인한 `tests/test_bootstrap_runtime.py:BootstrapSavedScopeFixtureUnitTests`의 고정 지침·Git 격리·실제 저장 invocation 근거 fixture에 최소 unit 회귀를 통합한다. 임시 JSONL·기존 EventWriter와 명령 결과 double을 사용하고 부모/자식 CLI를 새로 실행하지 않는다. `tests/support/git.py:copy_manager_import_fixture()`·`isolated_git_test`와 `tests/support/runtime.py:record_saved_scope_invocation()`을 그대로 재사용한다.
- [x] 확인한 `tests/test_bootstrap_saved_scope.py`·`tests/test_bootstrap_runtime_selection.py`의 실제 연결 회귀와 `docs/ARCHITECTURE.md`의 자체 테스트 Git 경계/부모·자식 계측 계약을 대조한다. 필요한 설명만 기존 소유 절에 통합하고 실제 연결을 unit으로 대체하거나 R-020의 지원 조건을 줄이지 않는다. 실패 원인 조사에서 이 작업 밖의 제품 결함이 확인되면 관측 근거를 보존해 기존 공통 결함 경계로 보고한다.

## Validation

- Worker는 `tests.test_bootstrap_runtime.BootstrapSavedScopeFixtureUnitTests`의 변경 관련 최소 case만 실행한다. 성공, 자식 timeout 결과, 자식 실행 예외, proof assertion 실패를 주입하고 회수 뒤 fixture 임시 폴더가 없어져도 상위 기록에 원래 이벤트가 정확히 한 번 남는지 검사한다. 원래 실패가 유지되고 실제 Git/CLI 호출은 0회여야 한다.
- 같은 unit fixture에서 writer 없음, 기록 없음, 완전한 이벤트 뒤 미완성 마지막 줄, 읽기/JSON 오류를 구분한다. 앞선 완전한 근거 보존·수집 한계 표시와 미완료 이벤트의 성공/종료 비합성을 확인하며 원래 예외가 바뀌지 않아야 한다. 정상 경로에 수집 불완전을 주입하면 검사 생략으로 통과하지 않아야 하며, 완전한 수집에서는 부모/자식 phase 개수와 Git cost 기록 assertion을 모두 유지한다. 실제 CLI를 더 실행하지 않고 기존 관측 double·임시 파일로 확인한다.
- 후보의 `tests/test_bootstrap_runtime.py:BootstrapSavedScopeFixtureUnitTests.test_collected_incomplete_phase_keeps_unknown_elapsed_in_diagnostic`를 실제 상위 `run_start`·`run_end`와 manifest가 있는 fixture로 확장한다. ID 부재·다른 run의 완전/미완료 기록을 회수한 뒤 원본 임시 경로를 지워도 상위 raw 진단에서 정확히 한 번 찾을 수 있고, 소유 중첩 진단의 미확인 시간은 유지돼야 한다. 상위 결과·phase·Git 비용·경과 시간은 오염되지 않아야 하며 필터가 정상적으로 제외한 기록을 상위 active로 요구하지 않는다. 기존 identity 없는 단독 진단 조건도 보존한다. 실제 Git/CLI는 실행하지 않는다.
- `tests/test_verify_runtime.py:TimingAndManifestTests.test_summary_does_not_adopt_unowned_nested_diagnostics`와 `test_diagnostic_preserves_active_events_without_comparable_clocks`를 대응 회귀로 재사용한다. 기존 필터·null 포맷 처리를 다시 구현하거나 중첩 기록에 상위 ID를 합성하지 않는다. fixture 연결을 바꾼 최소 unit만 실행하며 원문 보존을 필수 결과/종료 증거로 승격하지 않는다.
- 현재 `verification/targeted.py:collect()`는 `phase_start`·`phase_end`·`git_cost`·`git_cost_incomplete`를 개별 결과에서 제외한다. 회수 진단이 상위 targeted JSONL에 함께 남아도 개별 결과의 run/version 검증과 원본 진단 보존은 유지한다. 직접 수정된 `tests/test_targeted_test_streak.py:TargetedRunnerTests.test_nested_phase_diagnostics_do_not_poison_test_outcomes`를 대응 회귀로 재사용하며 같은 collector 수정·unit을 복제하지 않는다. 실제 연결을 변경할 때만 필요한 최소 case에 포함한다.
- 기존 두 실제 bootstrap 회귀는 Manager의 기존 전체 검증에서 유지한다. 결과가 실패하면 동일 실행의 원래 출력과 회수된 근거를 연결해 원인 미확정을 보존한다. 자식 PID별 누적 비용·부모가 포함한 자식 시간·전체 경과를 구분하며 다른 실행의 성공이나 자원 정책 변경을 이 후보의 성공으로 쓰지 않는다.

## Progress

- [x] revision `97df4cae88d54e9fcb469f5366d7ee360e5469e5`에서 보고 409와 현재 bootstrap fixture·두 실제 회귀·기존 unit·EventWriter, 관련 활성 명세 및 catalog를 정적으로 대조했다. 정상 끝에서만 비용 기록을 전달하는 경로를 확인했다. 구현·테스트·전체 verify는 실행하지 않았다.
- [x] 보존 후보 `8d63b8f5c7a3b7f8b24166d9b330eba7644705c5`의 회수 구현·unit과 후보 본문의 완료 표시를 읽기 전용으로 확인했다. 기존 구현·검증 기록은 해당 작업 공간에 보존하며 이번 확인을 최신 후보의 verify·Review 성공으로 승계하지 않는다.
- [x] 이번 revision에서 보존 worktree의 HEAD `dec1a217e789807c2818bd190035b247d635f105`와 미커밋 diff를 읽기 전용으로 확인했다. 정상 수집 완결성·필수 assertion 및 정상/미확인 시간 연결 unit의 구현과 후보 본문의 완료 기록은 보존한다. 작업 공간·Git·실행 이력을 수정하거나 후보의 검증 성공을 새로 확정하지 않았다.
- [x] revision `79d56d1df767cbbd7c3a9fb0274ca0b53a0294ff`에서 보존 worktree의 clean HEAD `52aa404ded19ca5f684717184074b5f9e7bd59e1`와 회수·필수 assertion·기존 unit 및 본문 완료 기록을 읽기 전용으로 대조했다. 최신 base의 ID 없는 중첩 기록 제외와 후보 unit의 상위 run/manifest 부재를 확인했다. 기존 수행 기록을 보존하며 이번 확인으로 실행 검증 성공을 새로 기록하지 않았다.
- [x] 보존 구현·완료 기록의 최신 base 통합 및 실제로 남은 차이 확인
- [x] 기존 실패 회수·정상/미확인 시간 unit 보존과 변경 관련 최소 검증
- [x] 실제 상위 run/manifest 아래 원문 접근·중첩 진단과 엄격한 상위 집계 분리 확인
- [x] 기존 실제 bootstrap 연결 회귀 정적 대조; 실제 실행과 실패 진단 대조는 Manager 전체 verify에서 수행한다.

## Decision Log

- snapshot token `22f83effbf72610e55ef3eacb8b4134f17d50e530e98825c854dfb36bf245f60`의 원문 hash와 started ID·경로·metadata·dependency·기존 기록을 보존했다. 최신 run 필터는 직접 구현으로 소비하고 추가 미충족 범위만 같은 ID에 반영한다. 후보의 회수 구현은 유지하되 상위 집계에서 ID 없는 이벤트가 제외되는 사실과 원문 진단의 접근 가능성을 구분한다. 보고 409의 timeout 원인이나 현재 후보의 verify·Review 성공은 확정하지 않으며 다른 공통 결함과 연결하지 않는다.

- snapshot token `12070d83acc62d1ccdb6c5bea8d0cb7b976900ea497676f09b94f7f27da00df1`의 ID·경로·원문 hash와 기존 기록을 확인했다. 이번 delta의 targeted 비용 진단 제외는 직접 구현으로 소비하며 정상/실패 회수 목표를 확장하지 않는다. 후보 확인과 재개 조건 정리는 구현·targeted test·전체 verify·Review 성공이 아니다. 보고 409의 원래 timeout 원인은 여전히 확정하지 않았고 별도 공통 결함 연결도 만들지 않는다.
- revision `8fe2686a4fb360883602faf3951b6ac82759fc99`의 `timing.diagnostic()` null 처리와 기존 회귀는 직접 구현으로 소비한다. snapshot token `7b52c07e28a762a9132691c8f24dc300fe40dadae8afc732ae607d67de6b7387`의 started ID·경로·metadata·dependency와 기존 기록을 보존한다. 후보의 정상 수집 실패 시 assertion 생략은 R-080과 이 작업의 기존 완료 조건에 맞춰 같은 ID에서 교정한다. 별도 교정 계획·timeout 확대·검증 성공은 만들지 않으며 보존 후보·Git·실행 이력을 수정하지 않았다.
- 보고 409는 전체 verify 1800초 종료와 두 회귀의 내부 timeout을 관측했으나 원인은 확정하지 않았다. 현재 코드에서 확인한 것은 실패 시 비용 기록 회수 누락이다. 새 계획은 이 누락만 교정하며 원래 timeout과 동일 원인이라는 공통 결함 연결을 제안하지 않는다. 계측을 확보한 뒤에도 별도 원인 판단과 실제 후보 검증이 필요하다.
- R-080과 ARCHITECTURE는 역할별 저장 상태와 실제 자식 dispatch를 실제 프로세스 회귀로 유지하도록 한다. 느리다는 이유로 이 조건을 삭제하거나 이미 최소화한 공통 중단/재사용 회귀를 다시 전환하지 않는다. G-187의 실제 OS·성능 자료 수집, G-295의 Windows 예산과 G-236의 pause 구현도 재구현하지 않는다. 다른 작업의 실패 원인을 밝히기 위한 선행 Merge나 역방향 dependency를 요구하지 않아 독립적으로 검증할 수 있다.
- complexity medium·최초 level 3: 자식 실행·assertion·JSONL 회수·임시 경로 정리의 예외 순서와 원래 결과 보존을 맞추는 명확한 경계다. risk medium: 회수 오류가 원래 실패를 가리거나 중복 이벤트로 비용을 왜곡할 수 있어 실제 파일과 예외 주입 unit으로 제한한다. 제품 데이터·외부 게시 효과는 없고 fixture 변경을 되돌릴 수 있다. priority high는 여러 작업의 전체 verify 실패를 조사할 근거가 사라지는 현재 경로를 막기 위한 것이다. Model·Effort는 Manager의 명시 설정을 따른다.
