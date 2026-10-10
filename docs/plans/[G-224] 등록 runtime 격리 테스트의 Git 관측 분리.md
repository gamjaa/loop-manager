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
- [ ] 정책 전환·공통 Git 환경 격리·실제 효과 예외/대응 확인과 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- complexity medium: 기존 scope/command 경계와 실제 연결 예외를 명확한 책임에서 구분한다. risk medium: 격리/소유권 회귀를 mock 성공으로 가릴 수 있어 실제 판단·보존·필요한 OS/Git 통합을 유지한다. 테스트 rollback과 기존 통합 회귀로 복구할 수 있으며 priority normal이다.
- 2026-10-08 초기 범위에서는 이미 전환된 직접 작업을 유지하고 남은 관측 정책과 근거 없는 반복 실행을 다루며 G-222/G-223과 독립적으로 검증·Merge한다고 정했다. Requirement 전체 충족은 Manager의 최신 근거 재판단에 맡긴다.
- 2026-10-10: R-080에 추가된 공통 실제 Git 환경 격리 구현·설정 효과 회귀를 이 작업의 책임으로 통합한다. G-222·G-223은 이 작업의 Merge 뒤 공통 경계를 소비하며 이 작업은 두 작업의 선행 구현을 요구하지 않는다. ID·경로·기존 수행 기록과 complexity/risk medium·priority normal을 보존한다. 변경은 테스트 환경에 한정되고 비용 비교 조건은 R-080이 참조하는 R-065 기준을 따른다. R-065의 배정/import 분산과 R-067의 가드/임시 경로 개선은 범위에 추가하지 않는다.
