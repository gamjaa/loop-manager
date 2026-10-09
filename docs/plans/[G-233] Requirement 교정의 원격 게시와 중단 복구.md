---
id: G-233
title: Requirement 교정의 원격 게시와 중단 복구
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-071] Remaining Manager-Owned Deterministic Work.md
depends_on:
  - G-232
complexity: high
risk: high
priority: high
---

## Purpose

Manager가 안전한 원격 처리 경계에서 Requirement 중복·제목 교정을 실행하고 같은 문서 교정 commit으로 base에 게시한다. 경쟁·실패·재시작에도 기존 작업과 참조를 보존하며 같은 번호 배정·commit·push를 반복하지 않는다.

## Acceptance Criteria

- 최신 원격 snapshot·작업 관계·자원 소유권에서 G-232 결과를 계산하고 별도 Manager 소유 worktree에서 적용한다. 기본 checkout·실행 중 Agent/verify worktree를 수정하지 않으며 모델을 호출하지 않는다.
- 게시 직전 원격 base·원문·새 작업 참조·목적지·소유권과 허용 diff를 재확인한다. 새 참조·원격 전진에서 보호 대상과 번호를 다시 계산하고 오래된 결과를 push하지 않는다. base 게시는 기존 fast-forward·권한/보호 gate를 유지한다.
- 편집 전 원문·배정·결과·소유 intent를 보존한다. 편집·commit·push·원격 확인·상태 적용 사이의 중단은 실제 결과를 대조해 같은 후보를 재사용하며 번호·commit·push를 중복하지 않는다. 결과가 변조되거나 이미 게시된 후보가 제거됐으면 덮어쓰지 않는다.
- push 실패·순서/참조 조회 실패·동시 변경은 원인과 작업물을 보존한다. 기계적 처리의 실패를 Planner 호출로 바꾸거나 성공으로 기록하지 않는다. 기존 실행·session·실패·사용자 결정·독립 차단과 최신 게시 gate를 유지한다.
- 자동 diff가 기존 변경 검증·관련성 및 후속 verify/Review 입력에 반영된다. rename된 Requirement를 참조하는 비종료 작업·완료 판단은 G-231로 같은 문서를 사용하며 archived 문서·ExecPlan은 수정되지 않는다.

## Implementation

기존 Manager Git journal·자원 예약·planning publication intent의 책임을 재사용한다. 교정 게시를 의미 판단과 분리하고 복구 결과는 실제 원격 확인 뒤에만 확정한다. 필요한 저장은 기존 호환 경계 안에서 추가하며 새로운 서비스·Agent·비호환 migration을 도입하지 않는다. PRODUCT·ARCHITECTURE에 안전한 처리 시점·소유권·게시·복구 계약을 반영한다.

## Validation

- 실제 선택·상태 저장을 실행하는 targeted unit case에서 새 참조·목적지 경합·원격 전진·push 실패·각 저장 경계 중단·원격 게시 후 DB 미확정을 검사한다. Git·PR·모델은 관측/명령 경계만 mock한다.
- 전체 verify는 원문·branch·session·독립 차단 보존, 같은 요청 재시작의 무중복과 자동 diff의 검증/Review 포함을 확인한다. 필요한 실제 fast-forward·lease·journal 효과는 기존 통합 회귀를 재사용하고 부족한 연결만 이유를 남겨 검증한다.

## Progress

- [x] eb797d3a의 catalog·게시·retirement 경계를 읽었다. Requirement 교정용 원격 게시·재검사·재개 연결은 없다. 구현·테스트 실행 없음.
- [ ] 게시·복구 통합과 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- complexity high: Git 편집·게시와 저장 사이의 중단, 새 참조의 보호 전환 및 번호 재배정의 멱등성을 함께 설계해야 한다. risk high: 오래된 교정 게시가 사용자 문서·실행 source 관계를 바꿀 수 있어 보존 intent와 Git 원문에 따른 수동 복구가 필요하다. 경쟁·중단·원문 보존을 완료 조건으로 둔다.
- 이미 구현된 ExecPlan ID 정규화·Manager barrier·Requirement retirement를 다시 계획하지 않는다. 로컬 조회의 후보 번호는 원격 예약이 아니다.
