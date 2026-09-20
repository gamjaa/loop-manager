---
id: G-018
title: Idempotent Telegram and Completion Notifications
sources:
  - docs/PRODUCT.md
depends_on:
  - G-017
complexity: medium
risk: high
priority: high
---

## Purpose

PR 생성, 사용자 조치가 필요한 차단과 프로젝트 작업 주기 완료를 Telegram으로 알린다. 정상 실행과 재시도 경로에서는 동일 알림의 반복 전송을 방지하고, 전송 실패를 실행 상태와 분리해 자동 재시도한다. 외부 전송 결과를 확정할 수 없는 오류 구간까지 exactly-once 전달을 보장하지는 않는다.

## Acceptance Criteria

- Telegram token과 chat ID가 모두 비어 있으면 알림 event를 만들거나 외부 전송을 시도하지 않는다.
- 두 값이 모두 설정된 경우 PR 생성, ExecPlan 또는 planning 단계의 BLOCKED, 프로젝트 완료 event를 durable 상태 변경 뒤 SQLite에 기록한다.
- PR 알림에는 프로젝트명, ExecPlan ID·제목과 PR 링크를 포함한다.
- BLOCKED 알림에는 프로젝트명, ExecPlan 또는 planning 단계, 원인과 사용자가 수정할 ExecPlan 및 근거 문서 경로를 포함한다.
- 프로젝트 완료 알림에는 프로젝트명과 마지막 완료 작업을 포함하고, 비종료 ExecPlan과 해결되지 않은 `planning_error`가 없으며 Planner가 추가 작업이 없음을 확인한 뒤에만 생성한다.
- 동일 PR, 동일 BLOCKED 원인과 동일 완료 주기는 하나의 outbox event로 수렴하고, 전송 성공이 기록된 event는 정상적인 프로세스 재실행이나 재시도에서 다시 전송되지 않는다.
- Telegram이 요청을 수락한 뒤 로컬 전송 성공 기록 전에 프로세스가 중단되거나 응답을 확인할 수 없는 오류가 발생하면, 미전송 event 재시도로 같은 알림이 일부 중복될 수 있다. 이 오류 구간의 exactly-once 전달은 보장하지 않는다.
- 전송 실패는 ExecPlan 상태, PR 생성과 다른 프로젝트 처리에 영향을 주지 않고 미전송 event로 남아 다음 Loop Manager 실행에서 재시도된다.
- 새 PRODUCT, Requirement 또는 구현 입력으로 새 작업 주기가 시작되면 완료 알림을 다시 보낼 수 있지만 Requirement archive 이동만으로 새 완료 주기를 만들지 않는다.
- dirty/ahead/diverged 기본 worktree에서도 이미 기록된 알림 재전송과 PR/Merge 기반 event 동기화는 계속된다.
- Telegram token과 chat ID는 Agent prompt, subprocess 환경, 명령 인자, 운영 로그와 오류 메시지에 노출되지 않는다.
- 구조화 로그, 임의 상태 알림 또는 자동 Merge는 추가하지 않는다.

## Implementation

- notification outbox와 프로젝트 완료 주기를 호환 가능한 SQLite schema로 추가하고 event 고유 key와 전송 성공 상태로 정상 경로의 중복을 막는다.
- 상태 transaction 안에서 event를 기록하고 외부 Telegram 호출은 commit 뒤 별도 dispatcher가 수행한다.
- dispatcher는 Telegram 성공 응답 뒤 event를 전송 완료로 기록한다. 성공 여부가 모호한 중단·통신 오류에서는 전달 누락을 피하기 위해 미전송 상태를 유지하며, 이후 재시도가 중복 알림을 만들 수 있음을 허용한다.
- Python 표준 라이브러리의 HTTPS 경계로 Telegram 요청을 보내며 테스트에서는 network adapter를 대체할 수 있게 한다.
- scheduler 시작과 종료에서 pending outbox를 재시도하되 전송 오류를 프로젝트 실행 결과와 분리한다.

## Validation

- Worker는 notification outbox와 completion cycle 테스트 class만 targeted unit test로 실행한다.
- 알림 비활성, 부분 설정 거부, 세 event payload, event 생성의 멱등성과 전송 성공이 기록된 event의 재전송 방지를 검증한다.
- 명시적인 전송 실패와 성공 여부가 모호한 오류 뒤에는 event가 pending으로 남아 다음 실행에서 재시도되는지 검증하되, 모호한 오류 뒤의 exactly-once 전송은 테스트 조건으로 요구하지 않는다.
- 새 입력 완료 주기와 archive-only 이동, dirty 프로젝트의 pending 전송 및 비밀값 비노출을 검증한다.
- 전체 verify에서 프로젝트 상태 전이, PR 복구, planning fingerprint와 다중 프로젝트 실패 격리가 유지돼야 한다.

## Progress

- [ ] notification outbox와 완료 주기 schema 구현
- [ ] 상태 전이별 event 생성과 고유 key 구현
- [ ] Telegram dispatcher와 실패 재시도 구현
- [ ] payload·중복·보안·완료 주기 테스트 추가

## Decision Log

- 알림 전달 여부는 작업 성공 여부와 분리하고 transactional outbox로 중복과 중단 복구를 처리한다.
- 사용자 결정(2026-09-20): 중복 알림 방지는 정상 동작의 기본 요구사항이지만 분산 전송 경계의 exactly-once 보장은 요구하지 않는다. Telegram 수락 뒤 로컬 성공 기록 전 중단처럼 결과가 모호한 오류에서는 eventual delivery를 위한 재시도로 일부 중복이 발생할 수 있다.
- 제품에 정의된 세 event 외 실시간 상태 알림은 만들지 않는다.
