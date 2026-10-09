---
id: G-236
title: 전체와 프로젝트 pause resume의 보존 경계
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-076] Read-Only Operational State CLI.md
  - docs/requirements/[R-077] Manual Plan Work Start and Completion.md
depends_on: []
complexity: high
risk: high
priority: high
---

## Purpose

자동 실행 중에도 전체/프로젝트 pause를 접수해 새 작업을 막고 대상 프로세스 종료·작업 보존을 확인한다. 범위별 resume는 pause만 해제하며 자동 실행이나 독립 차단 해제를 수행하지 않는다.

## Acceptance Criteria

- pause [repo]는 먼저 요청을 저장하고 대상 Agent·verify·자식 종료를 유한 시간 안에 확인한다. 단계·session·작업물·진단·게시 intent를 보존하며 다른 프로젝트나 다른 기기의 수동 구현 종료를 주장하지 않는다.
- Git·게시 중단 결과는 현재 소유권·실제 원격·기존 복구 증거로 확인한다. 종료·저장·게시 결과 미확인에서는 pause와 미완료 대상을 보존하고 완료·새 자원 사용을 허용하지 않는다. 재시작도 같은 요청을 이어간다.
- 전체 pause의 run은 bootstrap/runtime·원격·Agent·verify·알림 전에 안내·코드 0으로 종료한다. 프로젝트 pause는 해당 대상을 제외하며 명시 repo로 우회하지 않는다. 수락된 수동 제출·복구·유지보수의 자동 후속 처리도 pause를 준수한다.
- 전체/프로젝트 pause는 독립 저장하고 resume [repo]는 해당 범위만 해제한다. 기존 사용자 결정·차단·usage 제한·dependency·PR 대기는 유지하고 resume 자체는 작업을 시작하지 않는다.
- pause 중 조회·설정·제어·기존 수동 준비와 요청 접수는 가능하다. 정상 무변경/완료 0, 안전 거부·종료 미확인 1, 인자 오류 2이며 요청 저장과 실제 중단을 구분한다.

## Implementation

기존 process/resource lock·subprocess 종료/verify 자식 조율·실행 checkpoint·수동 요청 저장을 재사용한다. bootstrap 이전의 pause 조회와 실행 중 요청 접수는 실행 잠금 소유자의 작업을 탈취하지 않는다. 필요한 호환 저장·요청/확인 전이는 기존 runtime/storage 경계에 두고 PRODUCT·ARCHITECTURE와 운영 안내에 반영한다. 새 daemon·실행 엔진·자동 병렬 Worker를 만들지 않는다.

## Validation

- 실제 저장·정책을 실행하는 targeted unit test로 중첩 범위·resume 격리·접수/선택 경합·종료 확인 실패·재시작과 수동/복구 후속 대기를 확인한다. 전체 pause 진입에서 외부 효과는 호출 시 실패 fixture로 차단한다.
- 전체 verify는 대상 자식만 종료·예약은 종료 확인 뒤 회수, Git/게시 미확정·session/실패 보존 및 다음 안전한 경계 재개를 확인한다. 실제 프로세스 종료 효과는 필요한 기존 회귀/최소 연결만 유지한다.

## Progress

- [x] eb797d3a에는 pause/resume 명령과 저장 gate가 없다. 기존 잠금·자식 종료·checkpoint를 정적으로 확인했다. 실행 없음.
- [ ] 요청·중단 확인·재개 구현과 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- complexity high: 실행 잠금 밖의 요청과 bootstrap 전 gate, 프로세스 종료 및 비원자적인 Git/저장 결과의 수명을 함께 설계해야 한다. risk high: 잘못된 종료 확인·예약 해제는 미완료 작업과 새 실행을 겹치게 하며 작업/intent에 따른 수동 복구가 필요하다. 미확인 보존·범위 격리·중단/경쟁을 검증한다.
- R-076에 명시된 중단 요청만 구현하며 reset이나 제출 취소는 후속 계획으로 분리한다.
