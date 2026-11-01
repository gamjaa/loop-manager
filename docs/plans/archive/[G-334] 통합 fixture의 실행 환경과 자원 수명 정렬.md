---
id: G-334
title: 통합 fixture의 실행 환경과 자원 수명 정렬
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-080] Git Mock Defaults for Tests.md
  - docs/requirements/[R-067] 프로젝트 공통 verify 실행 규약.md
depends_on:
  - G-328
complexity: medium
risk: medium
priority: normal
---

## Purpose

실제 외부 실행이 필요한 통합 fixture의 준비·환경·자원 정리 책임과 수명을 기존 helper 경계에 맞춘다. Git 격리는 최초 seed부터 운영 adapter/자식까지 적용하고, SQLite 연결의 실패 시 소유권과 Windows launcher fixture의 실제 진입 파일을 바로잡아 기존 assertion이 의도한 경로에 도달하게 한다.

revision `f5a9d4ea510c62cea769eacd32c604f32bf25425`의 저장된 발견 16·19·30은 명령별 Git 환경만으로 실제 adapter의 상속 환경까지 격리하지 못하는 사례를, 31–33은 연결 미등록/초기화 실패의 정리 누락을 확인했다. 발견 20은 Windows fixture가 현재 `verify.ps1`의 `verify_windows.py` 진입을 제공하지 않음을 확인했다. 실환경 실행 실패나 파일 잠금을 측정한 결과는 아니다.

## Acceptance Criteria

- 아래 세 실제 Git 통합 fixture는 setUp의 첫 init부터 후속 helper·실제 adapter·필요한 자식까지 격리 환경을 상속한다. 사용자 global/system 설정·template·hook·attributes의 우연한 영향을 제외하고 테스트가 명시한 local/상속 설정은 유지한다.
- 파일 DB를 생성하는 `_persist_interrupted_worker()`는 정상 처리·초기화 예외 모두에서 자신이 소유한 연결을 닫는다. 테스트 직접 생성 연결은 즉시 cleanup을 등록하며 정상·assertion 실패·초기화 예외에서 닫힌다. 기존 transaction·commit 의미와 caller가 소유한 다른 연결은 보존한다.
- Windows/POSIX launcher fixture가 각각 실제 진입 파일에서 venv·cwd·UTF-8·종료 코드 37 검증에 도달한다. 운영 launcher·WSL/native 검증 정책·timeout은 변경하지 않는다.
- 사용자 동작과 공개 계약인 코드/fixture 반환·환경 복원, DB/receipt의 저장 형식과 실제 Git 효과 assertion을 보존한다. 운영 checkout/사용자 설정·다른 테스트의 자원·공유 임시 루트를 수정하거나 정리하지 않는다.

## Implementation

- [ ] 확인한 `tests/test_maintenance_assessment.py`·`tests/support/maintenance_planning_case.py`에 기존 `tests/support/git.py:isolated_git_test` 또는 같은 수명의 격리를 적용한다. 이미 격리하는 `MaintenanceAuthorityTest`와 중첩·복원 충돌을 만들지 않고 원격 게시·source drift·DB rollback assertion을 유지한다.
- [ ] 확인한 `tests/test_notification_events.py`의 실제 rebase/diff/blame 사례와 `tests/test_worker_checkpoint_publication.py`의 실제 `capture_git_snapshot`까지 같은 Git 격리를 적용한다. 명령별 env만 주는 준비와 전체 환경 상속을 구분하고 rebase·공백·비UTF-8·mode/gitlink·중복 알림·checkpoint 게시/재개 assertion을 유지한다. helper의 환경 복원 검증은 `tests/test_git_fixture_seed.py:GitEnvironmentTests`를 재사용한다.
- [ ] 확인한 `tests/test_workspace_missing_recovery.py:test_missing_worktree_and_branch_blocks_without_recreation`의 직접 연결은 생성 직후 `tests/support/lifecycle.py:register_connection_cleanup`에 등록한다. 연결 생성과 cleanup 등록 사이에 schema·등록·assertion 처리를 두지 않는다.
- [ ] 확인한 `tests/support/scope.py:_persist_interrupted_worker`의 파일 DB 연결은 schema·Git 관측·파싱·등록·UPDATE 어느 단계의 예외에서도 닫도록 한다. `with connection`의 transaction 종료와 `close()`의 연결 수명을 구분하고 existing transaction/commit 의미를 유지한다.
- [ ] 확인한 `tests/test_platform_compatibility.py`의 launcher fixture는 Windows에서 실제 `scripts/verify_windows.py` 진입을 테스트 double로 제공하고 POSIX에서는 기존 runner 진입을 유지한다. `tests/test_verify_windows_launcher.py`의 WSL→native·공유 timeout·정리 검증을 우회하거나 복제하지 않는다. 필요 수명 unit은 기존 `tests/test_support_fixtures.py`에 한정한다.

## Validation

- Worker targeted unit은 기존 `tests.test_git_fixture_seed.GitEnvironmentTests`와 `tests.test_support_fixtures`의 변경 관련 수명/초기화 실패 case다. `_persist_interrupted_worker()`의 schema·Git 관측·파싱·등록·UPDATE에 실패를 주입해 helper 소유 연결이 닫히는지 확인하고, 직접 연결의 즉시 cleanup 등록과 정상·assertion 실패 뒤 종료를 검사한다. 운영 SQLite 형식이나 transaction 결과를 바꾸지 않아야 한다. 삭제된 guidance factory를 재생성하거나 그 반환·초기화 실패 테스트를 추가하지 않는다.
- 실제 Git 효과가 필요한 세 통합 모듈은 전체 verify에 남긴다. 첫 init·운영 snapshot·자식의 환경 상속과 예외 후 부모 환경 복원을 확인하며, assertion 대상인 실제 rebase/commit/push·중단 재개/중복 방지·source drift 검증을 mock 성공으로 바꾸지 않는다.
- Windows에서는 `tests/test_platform_compatibility.py`의 변경한 launcher case를 최소 native 회귀로 확인해 실제 진입·interpreter·cwd·UTF-8·종료 코드 37에 도달해야 한다. Linux 결과로 Windows 성공을 주장하지 않는다. 전체 verify는 기존 WSL 전체 및 native 핵심 회귀와 임시 경로/정리 계약을 유지한다.
- 읽기 전용 Review에서 테스트 준비의 격리·연결 수명만 바뀌고 지원 플랫폼·WSL 요구·저장 형식·파일/HEAD 보존 assertion이 줄지 않았는지 확인한다. 없는 플랫폼 실행 결과를 합성하지 않는다.

## Progress

- [x] 저장된 7건의 fixture 발견과 기존 Git/cleanup helper·플랫폼 진입 계약을 대조했다. 구현·테스트·verify는 실행하지 않았다.
- [x] revision `8d19ea8aed39dd70b4a49546d0bde312c8a0aac0`의 G-328 변경과 현재 소비자를 정적으로 대조했다. `tests/support/guidance.py:active_connection`과 전용 repository 준비는 삭제됐고 코드·테스트의 호출도 없어 해당 factory 수명 교정만 잔여 범위에서 제외했다. 나머지 Git 격리·직접 연결 cleanup·파일 DB helper의 예외 정리·launcher 목표는 유지하며 실행 검증 성공을 추가하지 않는다.
- [x] 준비·격리·연결 수명과 launcher fixture를 정렬하고 직접 연결의 즉시 cleanup 등록 및 정상·예외 종료를 확인했다. Git 환경 복원, 파일 DB 단계별 예외 정리, POSIX launcher 단위도 통과했다. Windows native 회귀는 실행하지 않았으며 전체 verify는 Manager가 수행한다.

## Decision Log

- complexity medium: 기존 helper를 재사용하면서 setUp→adapter/자식→cleanup과 파일 DB helper·테스트 직접 연결의 소유 수명을 함께 맞춘다. risk medium: 잘못된 cleanup은 테스트 환경/연결을 누출하거나 파일 보존 회귀를 가릴 수 있어 예외/부모 환경/정확한 소유 자원 검증을 유지한다. 최초 level은 3이다.
- G-328의 삭제 결과를 소비하며 남은 `guidance_plan`·`git_result`는 이 작업의 연결 수명 수정 대상이 아니다. `depends_on: G-328`과 기존 ID·경로·수행 기록은 유지한다. fixture 진입 파일 수정과 수명/격리는 통합 테스트가 기존 계약을 실행할 수 있게 하는 범위이며 운영 기능·새 전역 fixture 체계를 추가하지 않는다.
- snapshot token `ce36ea1bf2b3c3353b79ef866f68f9b35134e1d560d9e10003b80001541946c9`의 revision·원문 hash를 확인해 같은 ID로 개정했다. 폐기 helper의 재구현만 제외하며 남은 Acceptance Criteria·관련 파일·검증·sources와 선행 Merge gate를 보존한다. 문서 개정은 구현·verify·Review 성공이나 기존 실행 상태 변경이 아니다.
