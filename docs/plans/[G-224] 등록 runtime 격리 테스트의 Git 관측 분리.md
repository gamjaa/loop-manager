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

## Acceptance Criteria

- 등록/runtime·role scope·작업 공간/정리·source 동기화와 provider/verify process 연결 테스트에서 정책 조건 조합은 Git/CLI 관측 경계를 mock으로 제공하고 실제 허용/거부·저장·보존을 검사한다. 예상 밖 명령·실패/오류·연속 관측 변화가 성공 mock에 숨겨지지 않는다.
- role/path별 scope 검사는 tracked/staged/untracked·선택 문서/계획·read-only 관측값을 제공하며 실제 validate_agent_scope를 실행한다. 실제 snapshot 파싱·파일 bytes/index·Git metadata·OS 격리·lease·worktree 효과는 필요한 통합 회귀에서 assertion으로 확인한다.
- 현재 bootstrap의 역할별 저장 상태/부모·자식 dispatch/exact-SHA runtime·B/C 전환과 등록 중단/동일 SHA 재사용의 연결 회귀를 유지한다. 공통 오류를 모든 역할·branch 전체 CLI로 반복하지 않으며 실제 연결에 영향을 주는 상호작용은 최소 통합 case로 남긴다.
- 실제 Git/CLI 유지·추가 이유와 전환/축소한 조건의 계속 검사 위치를 테스트 ID·assertion으로 대조할 수 있다. 실패 시 파일/index/session/감사·소유권 보존과 중단 뒤 같은 작업 재개를 검증하고 목적 달성 뒤 무관한 교정·차단·게시를 반복하지 않는다.
- 전체 verify의 수집과 기존 상한·skip 조건을 유지한다. 최초 import의 의존성/부작용처럼 새 interpreter가 계약인 검사는 유지하며 실제 Agent나 GitHub/Telegram을 호출하지 않는다.
- PRODUCT에 이미 적용된 mock 기본/실제 효과 예외와 필수 전체 검증 유지 규칙을 간결하게 반영하고 ARCHITECTURE의 자체 테스트 경계와 맞춘다. 다른 분할 범위의 완료를 문서 갱신만으로 선언하지 않는다.

## Implementation

- 대상은 `tests/test_origin*.py`, `test_bootstrap*.py`, `test_scope*.py`, `test_workspace*.py`, `test_worktree*.py`, `test_cleanup*.py`, `test_source_sync.py`, `test_self_merge_handoff.py`, `test_registered_project_scheduler.py`, `test_managed_configuration.py`와 platform/provider/Agent/verify/Git fixture·공통 진입 테스트의 실제 Git/CLI 사용 case다. G-222의 계획/catalog/유지보수와 G-223의 실행/수동/게시 복구 case는 제외한다.
- 기존 `tests/support/scope.py`의 snapshot 생성, origin/workspace/runtime fixture와 command adapter를 사용한다. 실제 SQLite·파일/OS effect는 assertion에 필요할 때 유지한다. 공동 fixture를 선택적으로 확장하되 별도 runner·전역 cache·운영 설정은 도입하지 않는다.
- 직접 반영된 bootstrap 역할별 child·PID/시각 계측·공통 crash/reuse 축소와 verify input의 platform API mock은 유지한다. 실제 연결이 필요한 이유를 테스트에 남기며 정책 변형은 실제 판정·저장을 쓰는 mock으로 옮긴다.
- 각 전환의 assertion 대응과 실제 실행의 계약/효과를 기록한다. AGENTS.md·ARCHITECTURE의 Git 경계와 Worker 최소 targeted 범위를 따르며 실행 격리·등록·복구 정책을 변경하지 않는다.

## Validation

- Worker는 변경한 role/path scope·등록 문서/선택 revision·소유권/정리 거부·관측 오류의 최소 unit case를 실행한다. process guard로 의도하지 않은 실행을 거부하고 실제 판단·SQLite 보존·같은/변경 입력 처리를 확인한다.
- Manager 전체 verify는 실제 부모/자식 exact-SHA 연결, snapshot/index/worktree·OS 격리·source 보존·정리/중단 재개·필수 최초 import와 process 종료 회귀를 전환 case와 함께 통과해야 한다. 실제 Git 예외의 effect assertion과 수집 범위 보존을 Review한다.
- 비용 비교는 기존 계측·병렬도·수집 조건의 영향 모듈/필수 연관 회귀만 비교한다. bootstrap 부모 시간에 포함된 child 시간을 합산하지 않으며 모듈 개선을 전체 verify 절감으로 보고하지 않는다.

## Progress

- [x] 2026-10-08 f3ae228의 R-080·설계 경계·bootstrap 전환 diff, role scope 원본·snapshot helper와 실제 등록 revision/작업 공간 회귀를 정적으로 확인했다. 구현·테스트 실행은 하지 않았다.
- [ ] 정책 전환·실제 효과 예외/대응 확인과 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- complexity medium: 기존 scope/command 경계와 실제 연결 예외를 명확한 책임에서 구분한다. risk medium: 격리/소유권 회귀를 mock 성공으로 가릴 수 있어 실제 판단·보존·필요한 OS/Git 통합을 유지한다. 테스트 rollback과 기존 통합 회귀로 복구할 수 있으며 priority normal이다.
- 이미 전환된 직접 작업은 유지하며 남은 관측 정책과 근거 없는 반복 실행만 다룬다. G-222/G-223과 독립적으로 검증·Merge하고 Requirement 전체 충족은 Manager의 최신 근거 재판단에 맡긴다.
