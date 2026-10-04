---
id: G-150
title: 코드 기반 부하 적응형 verify 병렬 실행
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-058] Test Timing and Agent-Free Test Execution.md
  - docs/requirements/[R-065] 실제 부하 기반 verify 병렬 실행.md
depends_on:
  - G-149
complexity: high
risk: medium
priority: normal
---

## Purpose

준비 비용을 줄인 unittest를 독립 프로세스에 배분하고 실제 CPU·메모리 부하에 따라 실행량을 코드로 조절한다. 등록 프로젝트 수로 고정 분할하지 않으며 여러 verify가 같은 여유 자원을 중복 소비하는 시작 경합을 조율한다.

## Acceptance Criteria

- `auto`·양의 정수 상한·직렬 `1`을 한 설정 경계에서 검증하고 기존 설정 파일의 생략은 `auto`로 처리한다. 실제 실행량은 자원 제한·사용자 상한·대기 모듈 범위 안에서 결정한다.
- 환경 snapshot과 실행·예약 상태를 입력받는 계산 함수는 모델 호출 없이 결정과 이유를 반환한다. 등록 프로젝트 수 또는 활성 verify 개수에 비례한 고정 분할을 사용하지 않는다.
- 실행 중 신규 모듈 배정에서 부하를 재평가한다. 여유 자원 활용, 부하 증가 시 신규 배정 제한, 회복 후 확대, 낡은 관측·미반영 예약의 보호와 대기 작업의 진행을 확인할 수 있다.
- Manager 실행 인스턴스가 공유하는 조율은 확인·예약·시작 실패 회수를 결합하고 자식 종료 또는 미시작을 확인한 예약만 해제한다. 종료 불명은 여유 자원으로 계산하지 않으며 다른 verify의 예약·프로세스와 기존 프로젝트 스케줄링은 보존된다.
- 직렬·병렬 모드의 테스트 ID·수집 범위·fixture 계약과 결과·종료 코드가 일치한다. 수집 오류·누락·중복·비정상 종료는 실패하고 module/class fixture 비용·중단 기록은 보존된다.
- 전체 timeout 안에 자원 대기를 포함하고 timeout·취소·통신 실패 후 해당 자식 종료를 확인한다. 관측 실패는 사유와 함께 직렬로 축소하며 설정·테스트·runner 오류를 성공 또는 환경 fallback으로 숨기지 않는다.
- 같은 환경·검증 범위의 Manager 전후 기록으로 실행량 변화, 시간과 메모리 비용을 확인한다. PRODUCT·ARCHITECTURE에는 구현 후 유지할 검증·책임 경계만 반영한다.

## Implementation

1. `loop_manager_internal/config/managed_config.py`와 설정 예제에 `[loop] verify_parallelism = auto`를 추가한다. 양의 정수는 각 verify의 상한으로 해석하고 `1`은 직렬이다. Manager가 검증한 설정과 실행 identity를 runner에 명시적으로 전달하며 설정 파일·자격 증명을 자식에 복사하지 않는다. 단독 runner는 동일 계산 경계를 사용하고 Manager 조율 부재와 사용자 상한을 기록한다.
2. 자원 관측과 판단을 작은 독립 경계로 분리한다. Windows·Linux의 실제 허용 CPU와 부하·메모리 여유를 수집하며 Linux에서는 affinity와 적용되는 cgroup 제한을 구분한다. 관측 시각·출처·유효성을 함께 전달하고 허용 CPU 수만으로 현재 CPU 사용량을 알 수 있다고 취급하지 않는다.
3. 순수 계산 함수는 최근 관측값, 현재 실행량과 미반영 예약, 관측된 비용·사용자 상한·대기량을 사용한다. 확장과 제한의 기준을 구분해 일시적인 사용률로 요동하지 않게 하고, 한번에 모든 대기 작업을 시작하지 않는다. 기준·유효기간·보호 여유는 한 정책 경계에 모으고 G-148·G-149의 실제 자료로 보수적 초기값과 선택 이유를 기록한다. 매 verify마다 여러 병렬도를 시험하지 않는다.
4. 현재 `loop_manager_internal/application/scheduler.py`의 실행 인스턴스가 작은 공유 조율 객체를 소유하도록 한다. 확인·예약은 lock 안에서 수행하고 테스트 실행·대기는 lock 밖에서 수행한다. 예약 identity와 종료를 연결하고 대기 작업을 순환 배정해 한 verify의 반복 요청이 다른 작업을 계속 밀어내지 않게 한다. mutable 모듈 전역 상태·새 상주 서비스·분산 조율은 추가하지 않는다.
5. 기존 verify subprocess 경계를 통해 runner의 추가 실행 요청과 완료·실패를 Manager 조율에 연결한다. Manager와 별도 프로세스인 runner에 threading lock만 전달하는 방식은 사용하지 않는다. 표준 라이브러리의 최소 로컬 통신 수단을 선택하고 호출 identity·생명주기·통신 실패 정리를 명시한다. 통신은 선택된 verify의 자식 실행량만 제어하며 임의 명령·다른 worktree 조작 권한을 제공하지 않는다. 다른 등록 프로젝트의 기존 verify는 변경하지 않고 그 실제 부하를 관측한다.
6. 기존 unittest discovery·load_tests 결과에서 단일 manifest를 만들고 모듈 단위로 독립 프로세스에 배분한다. 같은 모듈의 순서와 fixture 수명을 유지하고 직렬도 같은 runner를 사용한다. 테스트별 결과를 manifest와 대조하고 skip·expected failure·unexpected success·subtest 오류·fixture 오류를 기존 unittest 의미대로 집계한다. compile 단계도 전체 성공 판정에 포함한다.
7. 모듈 배정 때 부하를 재확인하고 과부하에서는 추가 배정을 대기시킨다. 실행 중인 테스트를 부하 조절 목적으로 중단하지 않으며 idle 프로세스의 메모리도 관측·예산에 포함한다. 관측 실패 시 추가 확대를 중단하고 이미 시작한 테스트를 정리한 뒤 직렬로 축소한다.
8. G-145의 자식 종료 확인과 G-148의 이벤트·진단을 연결한다. 종료·취소·시작 실패·통신 중단에서는 자식 종료 또는 미시작을 확인한 해당 예약만 회수하고 결과 없는 작업을 성공으로 만들지 않는다. 종료 불명은 기존 안전 오류와 예약으로 보존한다. 전체 timeout은 시작 후 자원 대기·테스트·compile을 포함한다.
9. Manager가 같은 환경에서 직렬·auto 모드, 단일 verify와 서로 다른 부하의 동시 verify를 비교한다. 실행량과 예약 변화, 시간·메모리, 실패·복구 결과를 기록하고 구현 후 장기 책임 경계만 PRODUCT·ARCHITECTURE에 반영한다. 같은 프로젝트의 여러 ExecPlan·Worker 실행 정책은 변경하지 않는다.

## Validation

- Worker는 새 계산·예약·manifest·집계 unit test와 변경된 설정·verify·scheduler 경계의 최소 unit case를 실행한다. 관측값·시계·프로세스·통신은 fake로 주입한다.
- 계산 case는 많은 등록 프로젝트와 단일 verify, 같은 활성 개수의 서로 다른 부하, CPU quota·메모리 부족, 낡은 snapshot, 미반영 예약, 상한·직렬 설정, 관측 실패와 회복을 포함한다.
- 예약 case는 동시 요청·시작 실패·완료·취소·통신 끊김, 종료 불명 예약 보존, 중복 해제, 다른 verify 보존과 대기 작업 진행을 확인한다. manifest case는 custom load_tests, module/class fixture, 수집 오류·누락·중복과 모든 결과 유형의 종료 코드 의미를 확인한다.
- Manager 전체 verify는 실제 Windows·Linux 프로세스 격리, 직렬·병렬 범위 일치, compile 실패, 실제 Git·SQLite 비간섭, 자원 대기 포함 timeout과 해당 자식만 종료되는 회귀를 확인한다. 부하 변화를 포함한 성능 비교도 Agent 밖에서 수행한다.

## Progress

- [x] 설정·환경 관측과 순수 실행량 계산 구현
- [x] 실행 인스턴스의 예약 조율·runner 통신과 병렬 모듈 실행 구현
- [x] 결과·진단·timeout 보존과 targeted unit test
- [ ] Manager 환경별 비교·전체 verify, 읽기 전용 Review와 사용자 Merge

## Decision Log

- 사용자는 병렬도 결정을 모델이 아닌 코드가 수행하고, 등록 프로젝트 수로 자원을 정확히 분할하지 않으며 실제 부하에 따라 여유 자원을 적극 사용하도록 요청했다.
- G-149 뒤 실행해 공통 fixture 구조·비용의 변경과 병렬 runner 변경을 분리한다. G-145의 종료 보존과 G-148의 시간·자원 기록은 선행 DAG를 통해 확보한다.
- Manager 안의 작은 조율과 순수 계산 경계를 사용한다. 같은 프로젝트의 여러 ExecPlan 동시 실행과 여러 호스트 간 자원 관리로 확대하지 않는다.
- 사용자 상한은 실행량의 최대값이며 자원 제한을 무시하는 강제 병렬도 설정이 아니다. 과거 자료는 비용 추정에만 사용하고 현재 후보 전체 검증을 대체하지 않는다.
- 2026-10-04: 사용자가 선행 계획의 Merge 전 로컬 직접 구현을 위임했다. G-148·G-149와 같은 수동 작업 branch에서 준비하며 원래 dependency, 서버의 G-135 작업과 같은 프로젝트의 ExecPlan 실행 정책을 변경하지 않는다. 자동 Worker가 이 기록을 active 계획으로 실행하지 않도록 `manual/`에 둔다. 전체 검증·측정·Review·관련 선행 Merge·사용자 Merge 전에는 완료로 간주하지 않는다.
- 새 `verification/` 경계가 OS 관측, 순수 계산, 인증된 loopback 예약 통신, unittest manifest와 실행·진단을 담당한다. scheduler 실행 인스턴스가 coordinator를 소유하고 thread ContextVar로 cap·상태 경로를 전달한다. 통신은 해당 실행의 예약 연산만 허용하며 자식 PID의 소유권과 시작 identity를 확인한다. 통신 오류는 실행 실패이고 관측 오류만 직렬 fallback이다. 다른 프로젝트의 기존 verify는 바꾸지 않는다.
- 초기 worker 예산 128 MiB와 이후 관측 peak의 1.25배는 실측 전 보호값이다. 메모리 여유는 64~256 MiB(관측 여유의 10%)를 남긴다. 관측은 0.2초 동안 재사용하고 2초를 넘으면 낡은 값으로 취급한다. 시작 예약의 CPU·메모리는 최소 0.75초 보호하며 미시작·종료 불명 예약은 시간 경과로 해제하지 않는다. FIFO 대기 요청은 살아 있는 요청의 순서를 보호하고 1초간 재요청이 없는 순번만 제거한다. 이 값의 서버 성능 근거는 아직 없다.
- 허용 CPU/affinity와 적용된 Linux cgroup quota·조상 메모리 제한을 기록한다. 관측 CPU 차이가 없거나 제한 범위의 사용량을 확인할 수 없으면 직렬이다. 정상 관측에서는 등록 프로젝트 수·활성 verify 개수로 고정 분할하지 않고 여유 CPU를 사용한다. 모듈마다 재평가하며 idle worker를 재사용해 Git seed와 이미 점유한 메모리를 유지한다.
- 직렬과 auto는 같은 원래 discovery/load_tests manifest와 모듈 내부 순서·suite를 사용한다. 결과를 ID·fixture 소유자와 대조하고 정당한 module/class skip은 별도 근거로 처리한다. Windows는 시작 handshake 전에 Job Object를 배정하고 Linux는 입력 pipe를 보존하는 subreaper로 자식을 추적한다. idle은 예약 해제가 아니며 예약 해제는 확인된 종료 뒤다. 실제 OS 자식·Git·SQLite의 격리는 Manager 검증 대상이다.
- G-148 기록의 관련 unit case 50개가 통과했다. compile은 mock으로 전체 경로와 실패 전파를 확인했으며 두 스크립트의 기존 215개 대상을 보존한다. PRODUCT와 README에는 구현한 검증 규칙과 사용법을 반영했다. ARCHITECTURE는 기본 사용자 편집 문서이므로 이번 직접 작업에서 수정하지 않았으며 구현 책임·경로는 이 기록과 README에 남긴다. 장기 설계 문서 반영과 환경별 직렬/auto·동시 verify 성능 비교는 최종 확인 때 남아 있다.
- 최신 main `99f7971`을 합친 뒤 직접 영향이 있는 unit case 5개와 diff를 재확인했다. compile은 추가된 base 모듈까지 총 226개를 포함한다. 기존 Draft PR #138과 `codex/verify-fixture-fast-path` branch를 갱신하며 새 PR·서버 SQLite 상태·기본 작업 공간은 변경하지 않는다.
