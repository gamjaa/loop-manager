---
id: G-340
title: 신규 ExecPlan과 draft의 게시 확정 알림
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-053] 이벤트 시점에 맞춘 진행 상황 알림.md
  - docs/requirements/[R-061] Agent 역할·문서 권한과 draft ExecPlan 사용자 판단.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
depends_on: []
complexity: medium
risk: medium
priority: normal
---

## Purpose

일반·유지보수 planning에서 원격 게시·catalog·DB 등록이 확정되면 새 일반 ExecPlan은 무음, 새 draft는 일반 알림으로 전달한다. draft 생성과 현재 질문 목록을 함께 표시해 중복을 막고 질문 없는 신규 draft도 전달한다. 기존 `record_planning_publication()`은 질문 알림만 생성하므로 게시 확정 경계와 durable outbox를 그대로 확장한다.

## Acceptance Criteria

- R-053: 실제 새 일반 ExecPlan의 게시·등록이 확정되면 새 ID·제목·확인한 문서 링크와 실행 또는 dependency 대기를 batch별 한 번 무음으로 묶어 알린다. 기존 문서 개정·삭제·rename, 미게시 초안·게시 실패·새 계획 없는 결과는 생성 사건이 아니다. 같은 batch의 일반 작업과 draft가 섞여도 유형별 수준을 유지하고 항목별로 누락·중복 발송하지 않는다.
- 새 draft는 질문 유무와 관계없이 생성 사실을 일반 알림으로 전달한다. 현재 미해결 질문이 있으면 R-061의 판단 목록과 통합하고 같은 생성/질문을 두 사건으로 보내지 않는다. 일반 작업의 무음 게시 묶음과 draft/질문의 일반 알림을 구분해 한쪽 수준으로 합치지 않는다. 이후 새 질문은 기존 질문 identity·revision 기준을 따르며 링크·제목 변화만으로 생성 또는 승인을 재요청하지 않는다. 알림은 draft의 실행 gate를 열지 않는다.
- 사건은 확인된 게시 결과와 등록 transaction에 결합하고 commit 후 다음 장시간 작업 전에 전송을 시도한다. push 후 DB 미적용·적용 후 해제/정리 실패·재시작에서 같은 publication을 다시 게시하거나 이미 등록한 새 ID를 새 생성으로 세지 않는다. 부분 batch는 실제 게시된 새 문서만 표현하며 전체 planning 성공을 합성하지 않는다.
- 생성과 전송 직전·각 조각 claim에서 게시 문서·현재 catalog와 질문의 유효성을 확인한다. 철회·사라진 문서·해소된 질문의 무효 조각만 취소하며 생성과 질문을 서로 다른 근거로 판단한다. 고정 본문·확인 cursor·전송/실패/취소 감사와 이미 전송한 조각을 보존하고 과거 취소 사건을 정책 변경만으로 부활시키지 않는다.
- 기존 PR 검토·사용자 행동·복구 불가 사건과 Telegram 설정·분할/재시도를 유지한다. 비활성화·송신 실패가 게시·catalog·작업 실행·검증 결과를 바꾸지 않으며 알림용 Agent·원격 조회·Git 쓰기를 추가하지 않는다.

## Implementation

- [ ] 확인한 `loop_manager_internal/planning/planner_publication.py`와 `maintenance/maintenance_publication.py`의 `record_planning_publication(connection, repo, result.plans, plan_commit)`을 재사용한다. 신규 `result.plans`와 개정/삭제 집합을 구분하고 publication intent·확정 commit·등록 문서 identity로 사건을 결합한다. 기존 준비/원자 적용·CAS와 R-082의 게시 후 잔여 정리 분리를 재설계하지 않는다.
- [ ] 확인한 `notifications/events.py:record_planning_publication()`·`enqueue_draft_decisions()`, `policy.py`의 질문 현재성/확인 identity와 `repository.py`의 고유 사건 키·고정 payload를 확장한다. batch와 일반 작업/draft 유형을 결합해 같은 유형을 묶고 일반 작업은 `silent: true`, draft/질문은 일반 알림으로 기록한다. 게시 근거가 없는 legacy 문서나 단순 catalog 재관측을 신규 생성으로 승격하지 않는다. 필요한 저장 정보는 기존 outbox/게시 기록을 우선 사용하고 불가피한 추가 필드는 기존 행을 보존하는 호환 방식으로 제한한다.
- [ ] 확인한 `notifications/policy.py:is_current()`·`delivery.py`의 공통 claim/즉시 flush와 `rendering.py`의 문서 URL·entity·분할을 연결한다. 질문 없는 draft와 기존 질문이 함께 있는 경우도 생성 항목과 미통지 질문을 구별하고 통지 확인의 출처를 유지한다. 기존 `silent`→`disable_notification` 전달을 재사용해 유형별 수준을 모든 조각/재시도에 유지하며 공통 transport나 전체 알림 allowlist를 무제한으로 열지 않는다.
- [ ] 확인한 `tests/support/notification.py`, `tests/test_notification_events.py`, `test_notification_delivery.py`와 `test_planning_publication.py`·`test_maintenance_publication.py`의 SQLite/게시 fixture에 최소 연결을 추가한다. PRODUCT·ARCHITECTURE의 게시 알림/질문 payload·identity 절에는 실제 구현된 계약만 통합한다.

## Validation

- Worker는 위 모듈에서 변경한 게시 확정·질문 통합·조각 claim의 최소 unit만 실행한다. 실제 SQLite·원문/고정 payload와 mock 원격/송신으로 일반 작업만 있는 batch의 무음, draft만 있는 batch의 일반 알림과 혼합 batch의 유형별 묶음/중복 방지를 확인한다. 질문 유무·후속 새 질문과 기존 개정만의 0건도 검사하며 실제 Git·Telegram·Agent는 호출하지 않는다.
- 일반·유지보수의 게시 성공/실패, DB 적용 전후 중단과 재진입에서 유형별 확정 묶음 1건·미확정 0건, 새 ID·문서 링크·dependency 표시를 검사한다. transport의 HTTP 경계 double로 일반 작업의 모든 조각·재시도에 `disable_notification=true`, draft/질문에는 일반 수준이 전달되는지 확인한다. 현재 custom adapter의 위치 인자 mock만으로 무음을 검증했다고 기록하지 않는다. 기존 publication CAS·fast-forward·외부 효과 1회 회귀를 재사용하며 같은 Git lifecycle을 반복하지 않는다.
- 부분 전송 중 질문 일부 해소·문서 철회·링크만 변경, 재시작·늦은 확인·송신 실패를 주입한다. 유효한 새 draft 생성은 누락되지 않고 동일 질문 중복·무효 잔여 조각 전송은 없어야 한다. 기존 PR 후보/사용자 행동의 확인·감사 assertion은 유지한다.
- 구정책의 일괄 게시 무통지 기대는 새 확정 사건과 내부 진행/실패 무통지로 구분한다. 테스트 제거는 폐기된 조건 또는 동일 assertion의 중복에만 한정하고 남긴 회귀 대응을 기록한다. 비활성·실패 송신 전후 실제 게시·실행 상태가 같아야 한다.

## Progress

- [x] revision `75365e14b334d4944331b44913595f7d350e915c`의 R-053/R-061/R-082와 일반·유지보수 등록 hook, 질문 정책·outbox를 정적으로 대조했다. 구현·테스트·전송은 실행하지 않았다.
- [x] 최신 revision `02282ab44776e0bc9a62bfa24befa96caae13ffe`의 R-053 변경을 재검토했다. 신규 일반 작업의 무음·draft의 일반 알림을 본문과 혼합 batch/실제 전달 인자 검증에 반영했다. 이전 문서 변경 오류를 구현 실패로 취급하거나 수행하지 않은 테스트·전송 성공을 기록하지 않았다.
- [x] 기존 알림 전송 경로의 분할·실패 재시도에서 무음 설정이 HTTP 요청까지 유지되는 단위 회귀를 추가했다. 신규 게시 사건의 구현과 검증은 아직 남아 있다.
- [ ] 일반 게시 묶음의 등록 근거·표시와 무음 전송을 연결했다. 일반·유지보수 등록 transaction 완료 직후 기존 즉시 전송을 호출하며 rollback 시에는 호출하지 않는다. 일부 철회 시 확인된 조각을 보존하고 미확인 유효 항목을 이어 보내며 추가 복구·전달 검증은 남았다.
- [ ] 신규 draft와 현재 미통지 질문을 같은 일반 알림으로 기록하고 질문 없는 신규 draft도 표시한다. 질문 해소 또는 목록 변경 시 이전 사건의 확인 cursor를 보존하며 미전송 생성 항목을 다시 구성한다. 나머지 현재성·복구 검증은 아직 남았다.
- [ ] 게시 시점 문서 경로와 현재 질문 경로를 구분해 이름·링크 변경이 생성 identity를 잃지 않도록 했다. 나머지 검증은 남았다.
- [ ] 취소와 경합한 늦은 전송 확인은 게시 ID·revision별 기존 확인 조각으로 재검사해 후속 목록에서 중복 항목을 제외한다. 전체 게시 복구 시나리오는 계속 확인해야 한다.
- [ ] 유지보수 게시 DB rollback·재적용에서 사건이 rollback되고 확정 묶음이 한 번 저장됨을 검증했다. PRODUCT·ARCHITECTURE의 알림 조건과 payload·현재성 계약을 구현에 맞췄다.
- [ ] 일반 게시 intent 복구·동일 등록 재관측의 단일 사건, 혼합 batch의 HTTP 알림 수준, 전송 실패 전후 catalog 보존을 검증했다. 기존 draft 목록 조각 경계 테스트에서 표시 호환 문제를 발견해 수정하고 같은 테스트 class를 재실행했다.
- [ ] 질문 없는 draft 게시 후 새 질문은 별도 판단으로 통지하며 생성은 반복하지 않음을 확인했다. 비활성 알림의 유지보수 등록과 미병합 dependency 표시도 SQLite unit으로 확인했다.
- [ ] 신규 게시 identity와 draft/질문 통합·현재성 연결
- [ ] 게시 중단·중복·부분 전송 최소 회귀와 제품/설계 반영

## Decision Log

- R-053이 명시적으로 바꾼 게시 알림만 담당한다. 실제 시작은 G-341, 프로젝트 소진/완료는 G-342, 지속 장애는 기존 G-326에 남긴다. 같은 공통 claim을 사용하되 사건별 현재성은 분리하고 파일 충돌만으로 선행을 만들지 않는다. G-282/G-337의 향후 부분 게시/정리도 실제 적용 batch라는 기존 계약으로 소비한다.
- 최신 R-053은 새 일반 ExecPlan의 주의 수준만 무음으로 바꿨다. 같은 ID·경로·목표와 기존 정적 조사 기록을 보존하고 유형별 batch/알림 수준을 구분한다. 별도 ExecPlan이나 새 채널·설정을 추가하지 않으며 draft 생성과 질문 통합·중복/감사 보존 조건은 유지한다.
- complexity medium·최초 level 3: 게시/등록과 질문·조각 확인의 경계는 정해져 있으나 여러 상태 전이를 연결해야 한다. risk medium: 잘못된 identity는 허위 생성·중복 판단 요청·누락을 만들 수 있어 등록 근거·현재성·확인 cursor를 검증하고 감사를 보존한다. priority normal은 R-053의 필수 정보 알림이며 새 실행 제어를 추가하지 않는다. Model·Effort는 Manager 설정을 따른다.
