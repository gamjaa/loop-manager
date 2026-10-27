---
id: G-332
title: verify adapter의 공통 worker 전송 분리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-067] 프로젝트 공통 verify 실행 규약.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
depends_on:
  - G-339
complexity: medium
risk: high
priority: normal
---

## Purpose

unittest adapter에 놓인 도구 독립 worker 생성·IPC와 두 adapter가 복제한 직렬 예약 제한을 공통 실행 계층으로 옮긴다. Python/Perl의 수집·fixture·reporter/native 결과 의미는 각 adapter에 남기고, 기존 core의 spawn/validate_report 경계와 모든 실행 계약을 보존한다.

revision `f5a9d4ea510c62cea769eacd32c604f32bf25425`의 저장된 발견 12는 `loop_manager_internal/verification/perl_adapter.py`가 `unittest_adapter.start_worker/Worker`를 전송 용도로 소비하고, 두 execute가 같은 `SerialClient` 상태·acquire 제한을 별도로 가짐을 확인했다. 현재 core callback·child 환경·clock 인터페이스를 유지한 채 전송 책임만 옮긴다. G-309의 신뢰된 통합 단계·상하위 예산 변경은 해당 기존 계획의 미충족 목표로 보존한다.

## Acceptance Criteria

- 도구 독립 프로세스 생성·IPC와 직렬 예약 제한은 공통 실행 책임을 가지며 Perl adapter가 unittest 수집/실행 구현을 전송 수단으로 import하지 않는다.
- adapter별 argv/cwd/환경·로그·worker/report identity와 core callback 계약이 유지된다. 직렬은 worker 한 개 재사용·동시 예약 한 개, independent는 기존 단위별 배정과 자원 상한을 유지한다.
- acquire → process start/PID bind → report/종료 확인 → release 순서, 미시작 반환과 종료 불명 예약·진단 보존을 유지한다. report 수신만으로 살아 있는 프로세스를 종료/회수한 것으로 처리하지 않는다.
- 사용자 동작과 공개 계약인 runner/adapter 호출·version 1 선언·IPC 전송·환경 가드·시간 예산, manifest/result/receipt·IPC의 저장 형식을 보존한다. native TAP/skip/TODO와 unittest fixture/NOT_SELECTED 의미를 통합하거나 새 adapter를 추가하지 않는다.

## Implementation

- [x] 확인한 `loop_manager_internal/verification/unittest_adapter.py`·`loop_manager_internal/verification/perl_adapter.py`의 `Worker`·`start_worker`와 직렬 client 제한을 기존 `verification/core.py`의 공통 실행 경계에 모은다. core 책임과 순환 import를 피하려면 예정 파일 `verification/worker_transport.py`에 전송만 두고 기존 adapter의 필요한 내부 호환 진입점은 얇게 유지한다. 새로운 범용 작업 프레임워크는 만들지 않는다.
- [x] 각 adapter의 spawn이 기존 callback을 통해 언어별 명령·환경·진단 identity를 넘기게 한다. 공통 helper는 테스트 discovery·reporter·TAP 판정을 알지 않으며 reservation client의 acquire/bind/release와 종료 확인을 그대로 전달한다.
- [x] 현재 child 환경·clock·core callback 경계를 그대로 사용한다. `guard.py`·프로세스 종료/예약 보존·예산 흐름을 재설계하거나 일반 테스트 자식에 조율 인증값을 돌려주지 않는다. G-309의 상위 통합 단계·상하위 예산 구현과 테스트 이전은 이 작업에 포함하지 않는다.
- [x] 확인한 `tests/test_verify_perl_adapter.py`·`tests/test_verify_contract.py`·`tests/test_verify_runtime.py`의 transport/serial fixture를 재사용해 공통 전송을 검증한다. 각 adapter 연결·serial budget 테스트는 서로 다른 연결 계약이므로 중복으로 제거하지 않는다. 실제 구조 설명만 ARCHITECTURE/VERIFY의 실행 책임 절에 반영한다.

## Validation

- Worker targeted unit은 위 모듈의 변경한 worker 생성/transport·serial 예약·종료 case다. process/IPC 관측을 double로 제공하되 실제 argv/환경 구성·예약 제한·보고/종료 처리를 검사한다.
- 양쪽 adapter에서 serial 재사용·independent 배정·시작 실패·미시작 반환·report 전/후 종료·통신/JSON 오류·timeout/cancel·종료 불명을 확인한다. 로그/report identity 혼합, 조기 release, 상한 중복 적용과 예약 유실을 거부해야 한다.
- 전체 verify는 현재 선언된 필수 검증과 Python/지원 Linux Perl standalone·coordinated 정상/native 실패, session·PID·예약 회수·현재 시간 예산·child guard 회귀를 유지한다. G-309가 소유한 신뢰된 통합 단계로의 이전·상하위 예산 보완은 별도 미충족 목표이며, 이 리팩터링의 통과나 Worker unit mock으로 해당 목표·실제 OS 격리의 성공을 기록하지 않는다.
- 읽기 전용 Review에서 adapter 간 의존 제거와 언어별 결과 의미·공개/저장/IPC 계약 보존을 확인한다. Perl AST 부분 선정·새 OS 지원·성능 실측은 이 리팩터링에 추가하지 않는다.

## Progress

- [x] 저장된 전송 결합/직렬 중복과 G-309의 실행 경계를 대조해 현재 인터페이스로 독립 검증할 공통화 범위를 확정했다. 구현·테스트·verify는 실행하지 않았다.
- [ ] 보고 369의 범위 밖 공통 결함 교정 G-339 반영 뒤 보존된 후보·실패 근거로 남은 검증 재개
- [ ] 공통 전송·직렬 제한 분리와 양쪽 adapter 회귀 검증
- [x] 공통 전송·직렬 제한을 분리하고 양쪽 adapter 연결과 공통 pool의 targeted unit을 검증했다. 전체 verify·실제 OS 격리는 Manager 검증으로 남긴다.

## Decision Log

- 공통 결함 snapshot `48f1af6159481ff29edbdcc6abd90b12fcc5246e1f069c1a7e424751d43e785b`의 보고 369는 이 작업의 전체 verify에서 정책 fixture와 targeted 기록 테스트가 실패했다고 관측한다. 현재 원격에서 원인을 각각 Git 관측 누락·3회 실패의 잘못된 도달 기대임을 정적으로 확인해 G-339에서 원인별로 구분한다. 어느 하나만 고치면 같은 필수 전체 verify가 나머지 실패로 막혀 하나의 Merge 단위로 두며, dependency는 남은 필수 검증의 확인된 실패를 선행 교정하는 관계다. 이 관계로 전송 구현 범위를 넓히지 않는다. started ID·경로·기존 기록·분류와 후보/실패를 보존하고 계획 게시만으로 교정·전체 verify 성공이나 차단 해제를 확정하지 않는다.
- 직접 sources 밖 R-061의 해시 호환·작업 base 기준은 이 작업에서 관측된 문서 보고 오류를 재평가하는 입력이다. 현재 결합 구현과 G-189의 후속 감사를 소비하며 adapter 리팩터링 안에서 보고 엔진을 재구현하거나 전수 감사를 역방향 선행으로 추가하지 않는다.

- complexity medium: 공통화할 책임과 adapter callback이 이미 구분돼 있다. risk high: 프로세스/예약 수명을 잘못 옮기면 종료 불명 실행의 자리 회수·가드 누락이 생길 수 있다. 기존 receipt·PID identity·환경 정제·다른 run 보존을 유지하며 시작/종료 실패 회귀로 확인한다. 최초 level은 3이다.
- G-309는 통합 실행 위치·시간 예산, 이 계획은 adapter 간 전송 의존만 소유한다. child 환경·clock·core callback 계약을 보존하므로 G-309의 구현은 공통 전송 분리의 필수 선행이 아니다. 파일 수정 충돌 방지만으로 선행을 추가하지 않으며, 현재 인터페이스에서 독립 검증·Merge하고 추가 유지보수 평가를 요구하지 않는다.
