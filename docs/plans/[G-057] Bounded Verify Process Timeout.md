---
id: G-057
title: Bounded Verify Process Timeout
sources:
  - docs/requirements/[R-026] Verify Hang Timeout Recovery.md
depends_on:
  - G-051
complexity: medium
risk: high
priority: critical
---

## Purpose

현재 `verify(worktree)`는 종료 상한 없이 `command`를 호출한다. 멈춘 테스트가 CPU를 점유하면 실패 결과조차 생성되지 않아 자동 실행이 차단된다.

## Acceptance Criteria

- 공통 `verify_timeout_seconds`는 기본 1800초의 양의 정수다. 정상 verify의 성공·실패 출력과 상태 전이는 유지한다.
- 시간 초과 시 verify와 자식 프로세스를 운영체제에 맞게 종료하고 제한된 출력·진단을 실패 결과로 보존한다. 남은 자식 CPU 작업이 없음을 확인한다.
- Worker는 현재 profile·session과 피드백 계약으로 원인을 교정하며 필요한 테스트를 삭제하지 않는다. 같은 상한으로 재검증하고, 진전 없는 같은 시간 초과의 반복 한도는 기존 verify 실패 정책을 따른다.
- 필요한 검증이 정상 진행 중이고 상한 부족이 확인되면 즉시 해당 계획만 차단해 설정 조정을 안내하며, 상한 변경 뒤 기존 workspace에서 재개한다. 다른 계획의 실행과 SQLite/검증 출력 저장 형식은 보존한다.

## Implementation

- `managed_config.py`의 설정 검증과 `loop_manager.py`의 verify subprocess 경계에 시간 상한을 전달한다. Linux process group 및 Windows process tree를 안전하게 종료하고 종료 실패도 진단한다.
- `execution_handlers.py`의 verify 실패 분류에서 시간 초과와 상한 부족 증거를 별도 처리하고 기존 `validation_feedback`의 출력 제한·지문을 재사용한다.
- Worker prompt에 무한 반복·교착·중복 검증을 먼저 점검하도록 필요한 진단만 전달한다.

## Validation

- Worker는 verify subprocess helper, `tests/test_execution_handlers.py`의 verify phase, `tests/test_managed_configuration.py`의 설정 관련 targeted unit test만 실행한다.
- 종료되지 않는 자식 프로세스, 정상 성공·실패, 교정 후 재실행, 세 번 동일 시간 초과, 상한 부족 판단과 설정 변경 재개를 검증한다.
- 전체 verify에서 기존 검증 범위, 실패 지문·피드백, Review gate와 비밀값 가림이 유지돼야 한다.

## Progress

- [ ] verify 설정 및 프로세스 종료 경계 추가
- [ ] 시간 초과 분류·재개 targeted test 추가

## Decision Log

- 시간 상한은 정상 검증을 줄이는 도구로 사용하지 않는다. 실제 프로세스 종료를 테스트한다.
