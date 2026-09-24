# [R-025] Reviewer Specification Deadlock Blocking

## Context

Reviewer는 현재 구현 결함과 테스트 누락을 `FAIL`로 보고하고, Loop Manager는 이를 Worker에게 전달해 수정한다. 그러나 근거 PRODUCT·Requirement·ARCHITECTURE 또는 ExecPlan에 필요한 결정이 빠졌거나 서로 모순되어 어느 구현이 옳은지 판단할 수 없는 경우에도 같은 경로를 반복하면 Worker가 제품 정책을 임의로 정하거나 수정 불가능한 Review가 되풀이된다. 기존 `PLAN_BLOCKED`는 계획 내부의 부족하거나 모순된 요구사항을 수용하지만 Reviewer가 이 원인을 명시적으로 반환하는 계약은 없다.

## Requirement

Reviewer는 검증이 성공한 구현을 읽기 전용으로 검토할 때, 구체적인 명세 결손 때문에 Acceptance Criteria의 충족 여부를 결정할 수 없다면 일반적인 코드 결함과 구분해 보고해야 한다. 보고에는 문제를 일으킨 문서·절·상충 내용 또는 빠진 결정, 판단할 수 없는 구현 선택, 사용자에게 필요한 보강 내용을 포함한다. 여러 해석 중 하나를 임의로 선택하거나 문서를 직접 수정하지 않는다.

Loop Manager는 유효한 명세 교착 보고를 현재 ExecPlan의 `PLAN_BLOCKED` 원인으로 기록하고 사용자에게 해당 경로와 질문을 알린다. 같은 문제를 Worker에게 반복 수정하도록 전달하지 않으며, 독립적인 READY 계획은 기존 직렬·dependency 규칙대로 계속 처리한다. 사용자가 근거 명세나 ExecPlan을 수정해 입력이 바뀐 경우에만 해당 계획을 재평가한다. 재개 후에도 결손이 남아 있으면 새 증거를 남기고 다시 차단한다.

Reviewer가 현재 문서로 판단 가능한 코드 결함·테스트 누락·verify 약화를 발견했을 때는 기존 `FAIL` 경로를 사용한다. Reviewer 결과 형식 오류는 명세 교착으로 간주하지 않고 기존 bounded protocol 교정을 따른다.

## Acceptance Criteria

- 서로 충돌하는 두 명세 조항 때문에 구현 선택을 확정할 수 없으면 Reviewer가 두 위치와 필요한 사용자 결정을 제시하고 해당 계획만 `PLAN_BLOCKED`로 전환한다.
- 필요한 Acceptance Criteria가 빠져 현재 구현의 정답을 판정할 수 없으면 Reviewer가 누락된 기준과 영향을 받는 동작을 제시하고 해당 계획만 차단한다.
- 차단 원인·근거 파일 경로·질문과 입력 지문을 보존해 재실행 뒤에도 동일한 상태와 알림 내용을 복구한다.
- 명세 교착 보고 뒤에는 동일한 불확실성을 해결하려고 Worker, verify와 Reviewer를 자동 반복하지 않는다.
- 사용자에게는 보강할 문서와 결정할 내용을 포함한 기존 BLOCKED 알림을 중복 없이 보낸다. Telegram이 비활성화돼 있으면 상태와 로그에서 같은 내용을 확인할 수 있다.
- 근거 명세 또는 ExecPlan 변경이 확인되면 기존 차단 해제 절차로 재평가하며, 변경이 없으면 반복 호출하지 않는다.
- 명세가 충분한 경우의 코드 결함과 테스트 누락은 기존 Review `FAIL`·Worker 수정 경로를 유지한다.
- 결과 프로토콜 오류나 막연한 품질 우려만으로 명세 교착을 판정하지 않는다.
- Reviewer는 파일을 수정하거나 테스트를 실행하지 않는다.

## Product Impact

- 문서 보강이 필요한 Review 교착은 수정 불가능한 Worker 반복 대신 계획 단위 사용자 결정으로 전환된다.

## Non-Goals

- AI가 빠진 제품 정책을 대신 결정하거나 근거 문서를 자동 수정하지 않는다.
- 현재 문서로 해결 가능한 일반 Review issue를 명세 교착으로 승격하지 않는다.
- 프로젝트 전체를 차단하거나 독립 계획의 실행을 멈추지 않는다.

## Open Questions

- 없음
