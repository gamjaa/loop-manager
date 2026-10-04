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
- Windows audit targeted: `py -3 -B -m unittest tests.test_verify_resources tests.test_verify_runtime.GuardAndConfigurationTests` (17개 통과).
- 신규 회귀가 수정 전 실패하고 수정 후 통과하는지 확인한다.
- 사용자가 명시적으로 요청한 동일 manifest 직렬/auto 실측은 기존 runner의 전체 timeout·자식 종료 확인 경계를 사용한다. 실측 결과와 미검증 사항을 아래 및 PR에 기록한다.
- 전체 verify가 새 회귀를 포함하는 동일 필수 테스트·fixture 및 compile을 실행하고 누락·중복·실패를 성공으로 처리하지 않는지 확인한다.

## Progress

- [x] 관련 제품·설계·R-065와 coordinator/sampler/decide 호출 경계 확인.
- [x] 회귀 재현과 최소 수정 및 targeted unit test: 신규 테스트 2개가 수정 전 실패, 수정 후 자원·예약 unit test 14개 통과.
- [x] 동일 manifest 직렬/auto 실측 및 결과 검토. 아래와 같이 전체 verify는 미통과이며 성능 절감률을 확정하지 않는다.
- [x] 코드·회귀·계획으로 변경 범위를 확인하고 초안 PR 준비. 전체 verify 통과·공식 Review·사용자 Merge는 아직 완료되지 않았다.

실측 후보는 `e21f60fc6b1929a0c074aecb669aadc3fc7d91e3`, 환경은 Windows `DESKTOP-jkw`, Python 3.13.14, Git 2.54.0.windows.1, 허용 CPU/affinity 12개다. 두 비교 모두 후보·manifest·관측 환경이 같으며 실행은 순차 수행했다. 동시 부하는 통제하지 않고 실제 CPU·메모리 관측으로 기록했다.

| 범위 | 직렬 | auto |
| --- | --- | --- |
| 전체 185모듈·2,263테스트 | 600초 timeout, 263개 완료 (통과 218·skip 4·오류 31·실패 10) | 317.327초에 오류 종료, 898개 완료 (통과 624·skip 21·오류 196·실패 57) |
| 관련 3모듈·49테스트 | 1.385초, 통과 38·skip 9·실패 2 | 2.567초, 통과 38·skip 9·실패 2 |
| 전체 범위의 최대 동시 모듈/worker 수 | 1/1 | 12/12 |
| 전체 범위의 관측된 worker별 최대 peak 메모리 | 73.6 MiB | 115.3 MiB |

- 두 전체 실행에서 CPU·메모리 값이 있는 snapshot의 직렬 축소는 0건이다. 최초 CPU 차이 관측이 없는 snapshot만 각각 한 번 직렬 축소했다. auto는 실제 CPU busy 1.44~12.0 범위에서 CPU·미반영 예약 대기도 기록했다. 관측된 가용 메모리는 직렬 약 3.36~3.94 GiB, auto 약 1.53~4.05 GiB다. worker peak는 프로세스별 관측 최댓값이며 전체 동시 RSS 합계가 아니다.
- 전체 실패에는 Codex 설정의 격리 검사, 심볼릭 링크 권한, Git config·경로 가정과 SQLite 정리 문제가 포함된다. auto는 결과 파일 `module-76.json` 접근 거부 후 worker 종료 시간 초과로 종료했다. 전체 verify·compile 성공이나 공식 Review 통과로 처리하지 않는다.
- 좁은 manifest는 `test_verify_resources`, `test_verify_runtime`, `test_verify_process`의 전체 테스트를 기존 discovery에서 선택하고 실제 runner/sampler/coordinator/worker 및 600초 timeout·Job 경계를 사용했다. 자원 관측과 배정은 mock하지 않았다. 동일 실패 2개는 Windows CRLF 출력 기대와 중첩 fixture 시간 계측의 중복이며 테스트를 제외하거나 약화하지 않았다. 작은 실행의 auto 추가 worker 시작 비용도 실측에 포함된다.
- 모든 비교의 `scope_matches`와 `observed_environment_matches`는 true, 실패·미완료를 고려한 `comparable`은 false다. 성공한 전체 검증의 성능 개선으로 주장하지 않으며 G-135 운영 시간과 직접 비교하지 않는다.
- 로컬 원본은 `.cache/verify/observation-time-20261004-{serial-2,auto,focused-serial,focused-auto}/`에 보존했다. 일회성 좁은 manifest harness는 `.cache/verify/observation-benchmark.py`이며 제품 runner나 verify 스크립트의 수집 범위를 변경하지 않는다.

## Decision Log

- 2026-10-04: 관측을 미래로 허용하거나 snapshot 시각을 보정하지 않고 관측 이후에 clock을 읽는다. 실제 낡은 관측과 시간 역행의 안전한 축소는 유지한다.
- 2026-10-04: G-135 실행 시간은 운영 발견 근거이며 이번 로컬 실측의 baseline 또는 결함 단독 효과로 취급하지 않는다.
- 2026-10-04: 첫 직렬 실측은 테스트 수집 전에 Windows Python 3.13 subprocess audit의 `executable=None`을 `os.fsdecode()`에 전달해 실패했다. 실제 command 검사는 유지하면서 생략된 executable 검사만 건너뛰는 최소 호환 수정을 실측 선행 조건으로 추가한다.
