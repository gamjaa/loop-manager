---
id: G-356
title: 공통 Telegram 큐와 독립 큐 순회의 연결
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-053] 이벤트 시점에 맞춘 진행 상황 알림.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
  - docs/requirements/[R-076] 운영 명령어와 상태 조회 및 Skill 지원.md
depends_on:
  - G-354
  - G-355
complexity: high
risk: high
priority: high
---

## Purpose

기존 durable outbox를 Manager 공통 전송 큐에서 소비해 프로젝트 단계의 진행·종료와 독립적으로 claim·전송·재시도한다. G-353~G-355의 실행·프로젝트별 Git·공통 DB 요청에 Telegram 완료 수집을 연결하고 각 큐의 진행 기회를 보장한다.

현재 `scheduler.run_registered_projects()`는 프로젝트 실행 전/후 pending을 전송하고, 단계 callback의 `flush_project_events()`와 `delivery._deliver_rows()`는 송신을 동기로 수행한다. 사건 생산/현재성·일반/무음 정책은 유지하며 남은 전송 조율만 바꾼다.

## Acceptance Criteria

- R-053/R-091: A의 Planner·PR 복구·Worker·verify가 진행/대기 중이어도 A/B의 확정된 발송 가능 사건을 공통 큐에서 처리한다. 프로젝트 Future 완료·다음 외부 실행·다른 프로젝트의 단계 종료가 전송/재시도의 전제가 되지 않는다.
- claim/현재성 확인은 짧은 DB 요청, Telegram 응답은 DB·프로젝트 Git lease 밖, 성공/실패 적용은 별도 DB 요청으로 처리한다. 각 순회는 큐별 재확인 가능한 요청의 유한 처리 뒤 다음 큐로 넘어가며 실제 네트워크는 제한된 외부 실행으로 분리한다. 한 전송의 지연/오류·순번 대기가 다른 사건/실행/DB/Git 진행을 막지 않는다.
- 기존 사건 identity·고정 본문·조각 cursor·attempt/error·전송/취소 감사를 유지한다. 동일 사건/조각 중복 claim과 확정 반복을 막고 전송 직전/각 조각의 현재성을 확인한다. 확인 저장 전 중단의 기존 at-least-once 한계를 보존하며 보장하지 않는 exactly-once를 선언하지 않는다.
- 부분 실패는 원래 요청/순서와 다음 유효 재시도 시점을 보존한다. 재시작·비활성/재활성에서도 오래된 사건의 미전송 잔여분만 취소하고 이미 보낸 조각·감사는 유지한다. 알림 실패는 호출/검증/게시 결과·실패 횟수·관리/실행 자리를 바꾸지 않는다.
- 전체 pause의 `run`은 알림·원격 효과를 시작하지 않는 R-076 계약을 유지하고 프로젝트 pause는 해당 프로젝트의 자동 처리를 제외한다. 비활성화는 새로운 전송을 시작하지 않으며 사건의 현재성·감사 보존과 다른 프로젝트 진행을 유지한다.
- G-351의 복구 불가 PR 사건과 기존 신규 게시·시작/재개·소진/완료·draft/PR 사건은 같은 큐를 사용한다. 새 사건 정책·추정 사용자 행동·추가 Agent/Git 조회를 만들지 않는다. G-341/G-342/G-326의 미충족 사건 생산/의미 판단은 각 소유 계획에 남긴다.

## Implementation

- [ ] 확인한 `notifications/delivery.py:_claim_piece()`·`_deliver_rows()`·`dispatch_pending()`·`flush_project_events()`를 유한 claim→송신→확정으로 연결한다. `repository.py`·`policy.py`·`transport.py`의 기존 outbox·현재성·송신 adapter를 재사용하고 필요한 호환 claim/재시도 근거만 보완한다. 별도 알림 채널·서비스·설정은 추가하지 않는다.
- [ ] `application/scheduler.py`의 실행 전후 dispatch와 프로젝트/작업의 inline flush를 공통 큐 등록/깨우기로 통합한다. G-353의 중앙 완료 수집·G-354/G-355의 순번/결과 확정에 연결하고, 오래 걸리는 프로젝트나 발송 하나가 다음 큐 순회를 독점하지 않게 한다. 실제 외부 실행을 위한 executor와 대기 요청의 보관을 구분한다.
- [ ] source별 사건 확정 transaction·현재성/generation·late confirmation을 소비한다. 송신 후 DB 확정 실패에는 이미 확보한 결과부터 이어가며 단순 DB 재시도로 무조건 재송신하지 않는다. 중단 후 실제 수신 여부를 확인할 근거가 없으면 기존 미확인 전송의 한계를 감사에 보존한다.
- [ ] 확인한 `tests/test_notification_delivery.py`, `test_notification_events.py`, `test_registered_project_scheduler.py`, `test_task_scheduling.py`의 실제 outbox·mock 송신·제어된 외부 실행을 연결한다. PRODUCT·ARCHITECTURE의 알림/조율 절을 통합하고 사건별 설명을 반복하지 않는다.

## Validation

- Worker targeted unit은 위 변경 case다. 프로젝트 Future를 미완료로 둔 채 A/B 사건의 전송·부분 재시도를 확인하고, 송신 adapter를 지연/실패시켜도 다른 실행/큐가 진행하며 네트워크 중 독립 DB 쓰기가 가능한지 검사한다. 실제 Telegram 메시지는 보내지 않는다.
- 기존 현재성·부분 cursor·늦은 확인·중복/철회·일반/무음 회귀를 유지한다. 송신 전/후와 DB 확정 중단·재시작·비활성화/재활성화·전체/프로젝트 pause에서 소유 claim·감사·미전송 잔여분과 기존 실행 결과를 확인한다.
- 선행 큐의 연결 회귀에 Git 순번·DB 잠금·사용 한도·retry·호출 종료·Telegram 응답 보류를 함께 둔다. 각 큐 뒤의 적격 요청이 진행하고 해소 시 같은 요청을 한 번 배정하며, 무변화 busy loop·대기 thread 증가·미종료 예약 조기 회수가 없어야 한다. Git/DB의 실제 효과 회귀는 선행 테스트를 재사용하고 여기서는 중앙 연결의 최소 조합만 검사한다.

## Progress

- [x] 검토 revision의 실행 전후 dispatch·단계 inline flush·outbox claim/송신/확정과 변경 명세를 정적으로 대조했다. 구현·테스트·전송은 수행하지 않았다.
- [ ] 공통 전송 큐·유한 조각 처리·독립 순회 연결
- [ ] 지연/부분 실패·재시작·현재성·네 큐의 진행 회귀 및 설계 반영

## Decision Log

- G-350/G-351의 단계/최종 차단과 G-353~G-355의 큐 전환을 소비한다. 기존 사건 생산 작업의 Merge를 전송 계층 도입의 전제로 만들지 않고 현재 지원 사건을 모두 수용하며, 후속 G-326은 이 공통 전송 경계를 소비한다. 기존 outbox를 다른 저장소로 옮기거나 성공·사용자 판단을 만들지 않는다.
- complexity high·최초 level 5: 네트워크 효과·현재성·조각 claim·DB 확정을 독립 조율/재시작에 연결해야 한다. risk high: 중복/오래된 사용자 조치 통지와 전송 확정 혼선이 생길 수 있다. 고유 claim·현재성 재검사·cursor/감사 보존·단계 중단 회귀로 제한한다. priority high는 장시간 작업 중에도 확정된 사용자 대응 사건을 전달하기 위한 것이다. Model·Effort는 Manager 설정을 따른다.
