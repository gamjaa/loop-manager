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

원격 base의 `tests/support/bootstrap_saved_scope.py`는 정상 경로 마지막에서만 비용 기록을 전달한다. 보존 후보 `8d63b8f5c7a3b7f8b24166d9b330eba7644705c5`에는 `_collect_bootstrap_costs()`·`_track_bootstrap_costs()`의 정리 전 회수와 최소 unit이 이미 있다. 이를 보존해 최신 base에 통합하고, 정상 경로의 수집 불완전이 기존 phase·Git 비용 assertion을 건너뛰게 하는 분기만 교정한다. 이번 base의 미확인 경과 시간 진단은 그대로 재사용한다. G-187의 실제 OS·부하 비교와 G-295의 Windows 예산은 별도 목표다.

## Acceptance Criteria

- R-065/R-067: 성공·timeout·자식 실행 오류·후속 assertion 실패 모두에서 현재 소유한 fixture의 수집 가능한 부모/자식 비용 기록이 임시 폴더 정리 전에 기존 상위 verify 기록으로 전달된다. 원래 PID·시각·clock domain·phase/test identity와 완료 여부를 유지하고, 부모가 포함하는 자식 시간을 별도 전체 경과 시간으로 더하지 않는다.
- 실패 직전까지 flush된 완전한 이벤트는 보존한다. 강제 종료로 마지막 줄이 불완전하거나 파일이 일부 없을 때도 앞선 수집 가능한 근거를 잃지 않고 수집 한계를 남긴다. 미완료 phase의 종료·duration·성공이나 없는 기록을 합성하지 않는다. clock domain 비교가 불가능한 미완료 이벤트는 `elapsed: null`과 현재 base의 `경과 시간 미확인` 진단을 유지한다. 중복 회수 경로가 같은 이벤트를 두 번 전달하지 않는다. 정상 경로는 수집 완결성을 확인한 뒤 기존 phase 개수·Git 비용 assertion을 모두 수행하며, 수집 실패를 성공 경로의 검사 생략으로 바꾸지 않는다.
- timeout·실행 오류·기존 assertion의 실패 종류와 원래 출력은 유지된다. 진단 회수의 읽기/형식 오류가 원래 실패를 덮거나 성공으로 바꾸지 않는다. 상위 writer가 없는 단독 unit 환경에서도 원래 테스트 결과를 유지하고, 비용 기록을 필수 성공 receipt나 조율/종료 근거로 사용하지 않는다.
- R-020/R-080: 현재 두 bootstrap 회귀의 역할별 실제 자식 dispatch, main Worker/Reviewer·develop Planner, A의 저장 상태에서 B runtime 재개, B 선택 뒤 C 전진과 다음 실행의 C 선택, 같은 SHA 재사용·등록 직후 중단의 기존 대응을 유지한다. session·worktree·Worker 파일·상태·실패 이력·project block 부재와 exact-SHA runtime assertion을 삭제·skip하거나 성공 mock으로 바꾸지 않는다.
- 기존 Linux 90초/Windows 180초의 내부 제한과 전체 verify 예산·수집 범위·Agent 가드·Git 격리·종료 확인을 유지한다. 계측 보존만으로 timeout이 해결됐다고 보고하지 않는다. 추가 실제 CLI 반복이나 별도 benchmark·runner·전역 cache·새 진단 저장소를 만들지 않는다.

## Implementation

- [ ] 보존 후보의 `tests/support/bootstrap_saved_scope.py:_collect_bootstrap_costs()`·`_track_bootstrap_costs()`와 `_run_child()`·`_assert_saved_scope_resume()`의 정리 순서를 최신 base에 통합한다. 이미 구현한 회수·중복 방지는 유지하고 정상 경로의 `if collect_costs():` 아래에서만 기존 assertion을 실행하는 분기는 수집 완결성 검사와 필수 assertion으로 정합화한다. 실패 경로에서는 회수 오류가 원래 timeout·실행/proof 오류를 덮지 않도록 구분한다. 파일/프로세스 소유 범위와 기존 `run_verify_process()`의 종료 결과를 바꾸지 않는다.
- [ ] 회수는 이 fixture가 생성한 파일만 읽고 기존 이벤트 identity를 보존한다. 중단된 JSONL의 완전한 줄·마지막 미완성 줄, 읽기 오류와 원래 테스트 예외를 구분한다. 필요한 수집 한계는 기존 테스트 진단 방식에 남기며 조율 인증값·전체 환경이나 사용자 문서를 출력하지 않는다. 공통 runner의 결과 판정·시간 합산·reservation schema를 이 fixture 문제로 변경하지 않는다.
- [ ] 확인한 `tests/test_bootstrap_runtime.py:BootstrapSavedScopeFixtureUnitTests`의 고정 지침·Git 격리·실제 저장 invocation 근거 fixture에 최소 unit 회귀를 통합한다. 임시 JSONL·기존 EventWriter와 명령 결과 double을 사용하고 부모/자식 CLI를 새로 실행하지 않는다. `tests/support/git.py:copy_manager_import_fixture()`·`isolated_git_test`와 `tests/support/runtime.py:record_saved_scope_invocation()`을 그대로 재사용한다.
- [ ] 확인한 `tests/test_bootstrap_saved_scope.py`·`tests/test_bootstrap_runtime_selection.py`의 실제 연결 회귀와 `docs/ARCHITECTURE.md`의 자체 테스트 Git 경계/부모·자식 계측 계약을 대조한다. 필요한 설명만 기존 소유 절에 통합하고 실제 연결을 unit으로 대체하거나 R-020의 지원 조건을 줄이지 않는다. 실패 원인 조사에서 이 작업 밖의 제품 결함이 확인되면 관측 근거를 보존해 기존 공통 결함 경계로 보고한다.

## Validation

- Worker는 `tests.test_bootstrap_runtime.BootstrapSavedScopeFixtureUnitTests`의 변경 관련 최소 case만 실행한다. 성공, 자식 timeout 결과, 자식 실행 예외, proof assertion 실패를 주입하고 회수 뒤 fixture 임시 폴더가 없어져도 상위 기록에 원래 이벤트가 정확히 한 번 남는지 검사한다. 원래 실패가 유지되고 실제 Git/CLI 호출은 0회여야 한다.
- 같은 unit fixture에서 writer 없음, 기록 없음, 완전한 이벤트 뒤 미완성 마지막 줄, 읽기/JSON 오류를 구분한다. 앞선 완전한 근거 보존·수집 한계 표시와 미완료 이벤트의 성공/종료 비합성을 확인하며 원래 예외가 바뀌지 않아야 한다. 정상 경로에 수집 불완전을 주입하면 검사 생략으로 통과하지 않아야 하며, 완전한 수집에서는 부모/자식 phase 개수와 Git cost 기록 assertion을 모두 유지한다. 실제 CLI를 더 실행하지 않고 기존 관측 double·임시 파일로 확인한다.
- 현재 `tests/test_verify_runtime.py:TimingAndManifestTests.test_diagnostic_preserves_active_events_without_comparable_clocks`를 재사용한다. fixture에서 회수한 미완료 foreign/legacy 이벤트가 기존 `summarize()`·`diagnostic()`에도 남는 연결만 필요한 경우 최소 unit에 통합한다. 시간값을 0이나 현재 플랫폼 시각으로 대체하거나 이미 직접 교정된 포맷 처리를 다시 구현하지 않는다.
- 기존 두 실제 bootstrap 회귀는 Manager의 기존 전체 검증에서 유지한다. 결과가 실패하면 동일 실행의 원래 출력과 회수된 근거를 연결해 원인 미확정을 보존한다. 자식 PID별 누적 비용·부모가 포함한 자식 시간·전체 경과를 구분하며 다른 실행의 성공이나 자원 정책 변경을 이 후보의 성공으로 쓰지 않는다.

## Progress

- [x] revision `97df4cae88d54e9fcb469f5366d7ee360e5469e5`에서 보고 409와 현재 bootstrap fixture·두 실제 회귀·기존 unit·EventWriter, 관련 활성 명세 및 catalog를 정적으로 대조했다. 정상 끝에서만 비용 기록을 전달하는 경로를 확인했다. 구현·테스트·전체 verify는 실행하지 않았다.
- [x] 보존 후보 `8d63b8f5c7a3b7f8b24166d9b330eba7644705c5`의 회수 구현·unit과 후보 본문의 완료 표시를 읽기 전용으로 확인했다. 기존 구현·검증 기록은 해당 작업 공간에 보존하며 이번 확인을 최신 후보의 verify·Review 성공으로 승계하지 않는다.
- [ ] 보존 구현의 최신 base 통합과 정상 경로의 수집 완결성·필수 assertion 교정
- [ ] 기존 실패 회수 unit 보존과 새 정상/미확인 시간 연결의 최소 회귀
- [ ] 기존 실제 bootstrap 연결 회귀와 실패 진단 보존 확인

## Decision Log

- revision `8fe2686a4fb360883602faf3951b6ac82759fc99`의 `timing.diagnostic()` null 처리와 기존 회귀는 직접 구현으로 소비한다. snapshot token `7b52c07e28a762a9132691c8f24dc300fe40dadae8afc732ae607d67de6b7387`의 started ID·경로·metadata·dependency와 기존 기록을 보존한다. 후보의 정상 수집 실패 시 assertion 생략은 R-080과 이 작업의 기존 완료 조건에 맞춰 같은 ID에서 교정한다. 별도 교정 계획·timeout 확대·검증 성공은 만들지 않으며 보존 후보·Git·실행 이력을 수정하지 않았다.
- 보고 409는 전체 verify 1800초 종료와 두 회귀의 내부 timeout을 관측했으나 원인은 확정하지 않았다. 현재 코드에서 확인한 것은 실패 시 비용 기록 회수 누락이다. 새 계획은 이 누락만 교정하며 원래 timeout과 동일 원인이라는 공통 결함 연결을 제안하지 않는다. 계측을 확보한 뒤에도 별도 원인 판단과 실제 후보 검증이 필요하다.
- R-080과 ARCHITECTURE는 역할별 저장 상태와 실제 자식 dispatch를 실제 프로세스 회귀로 유지하도록 한다. 느리다는 이유로 이 조건을 삭제하거나 이미 최소화한 공통 중단/재사용 회귀를 다시 전환하지 않는다. G-187의 실제 OS·성능 자료 수집, G-295의 Windows 예산과 G-236의 pause 구현도 재구현하지 않는다. 다른 작업의 실패 원인을 밝히기 위한 선행 Merge나 역방향 dependency를 요구하지 않아 독립적으로 검증할 수 있다.
- complexity medium·최초 level 3: 자식 실행·assertion·JSONL 회수·임시 경로 정리의 예외 순서와 원래 결과 보존을 맞추는 명확한 경계다. risk medium: 회수 오류가 원래 실패를 가리거나 중복 이벤트로 비용을 왜곡할 수 있어 실제 파일과 예외 주입 unit으로 제한한다. 제품 데이터·외부 게시 효과는 없고 fixture 변경을 되돌릴 수 있다. priority high는 여러 작업의 전체 verify 실패를 조사할 근거가 사라지는 현재 경로를 막기 위한 것이다. Model·Effort는 Manager의 명시 설정을 따른다.
