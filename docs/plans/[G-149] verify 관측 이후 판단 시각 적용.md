---
id: G-149
title: verify 관측 이후 판단 시각 적용
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-065] 실제 부하 기반 verify 병렬 실행.md
depends_on: []
complexity: low
risk: low
priority: high
---

## Purpose

신규 자원 관측의 시각이 관측 전에 읽은 판단 시각보다 뒤라는 이유로 정상 verify가 직렬 축소되는 결함을 수정한다. 2026-10-04 사용자 요청에 따른 로컬 작업이며 G-135의 Git cleanup·checkpoint 범위와 분리한다. 기존 R-065의 관측 유효성·자원 보호 규칙을 복원하며 새 제품 정책을 추가하지 않는다.

## Acceptance Criteria

- acquire의 자원 관측 이후에 판단 시각을 읽고 정상 신규 관측에서는 CPU·메모리 및 예약이 허용하는 추가 병렬 배정이 가능하다.
- 실제 stale 또는 미래 시각 관측과 관측 실패의 직렬 축소, CPU·메모리 보호, 사용자 상한, 미시작·종료 불명 예약 및 세션 간 배정 정책을 유지한다.
- fake clock 회귀 테스트가 신규 관측과 관측 중 실제로 오래된 snapshot을 구분한다.
- 같은 후보·manifest의 직렬/auto 실측에서 검증 범위·결과, 실행량, 관측 이유와 소요 시간을 비교하고 실제 부하의 영향을 구분한다.

## Implementation

1. `VerifyCoordinator.request()`의 acquire에서 sampler 호출 뒤 판단 시각을 읽고 대기열·예약 계산 및 생성에 일관되게 사용한다. `resources.decide()`의 보호 조건은 유지한다.
2. `tests/test_verify_resources.py`에 시간 전진·stale·복구 회귀를 추가하고 기존 예약·자원 unit test로 보호 정책을 확인한다.
3. 기존 verify runner와 스크립트의 동일 manifest·compile·timeout·Agent 차단 경로로 직렬/auto를 비교한다. 제품·Requirement·G-135 문서는 변경하지 않는다.
4. Windows 실측 시작에서 확인된 subprocess audit의 `executable=None` 입력만 수용한다. 명시된 executable과 실제 command의 Agent 차단 및 자식 차단 환경 상속은 보존한다.

## Validation

- targeted: `py -3 -B -m unittest tests.test_verify_resources`.
- 신규 회귀가 수정 전 실패하고 수정 후 통과하는지 확인한다.
- 사용자가 명시적으로 요청한 동일 manifest 직렬/auto 실측은 기존 runner의 전체 timeout·자식 종료 확인 경계를 사용한다. 실측 결과와 미검증 사항을 아래 및 PR에 기록한다.
- 전체 verify가 새 회귀를 포함하는 동일 필수 테스트·fixture 및 compile을 실행하고 누락·중복·실패를 성공으로 처리하지 않는지 확인한다.

## Progress

- [x] 관련 제품·설계·R-065와 coordinator/sampler/decide 호출 경계 확인.
- [x] 회귀 재현과 최소 수정 및 targeted unit test: 신규 테스트 2개가 수정 전 실패, 수정 후 자원·예약 unit test 14개 통과.
- [ ] 동일 manifest 직렬/auto 실측 및 결과 검토.
- [ ] 변경 범위 확인 및 PR 생성. Merge 여부와 실행 상태는 문서에서 관리하지 않는다.

## Decision Log

- 2026-10-04: 관측을 미래로 허용하거나 snapshot 시각을 보정하지 않고 관측 이후에 clock을 읽는다. 실제 낡은 관측과 시간 역행의 안전한 축소는 유지한다.
- 2026-10-04: G-135 실행 시간은 운영 발견 근거이며 이번 로컬 실측의 baseline 또는 결함 단독 효과로 취급하지 않는다.
- 2026-10-04: 첫 직렬 실측은 테스트 수집 전에 Windows Python 3.13 subprocess audit의 `executable=None`을 `os.fsdecode()`에 전달해 실패했다. 실제 command 검사는 유지하면서 생략된 executable 검사만 건너뛰는 최소 호환 수정을 실측 선행 조건으로 추가한다.
