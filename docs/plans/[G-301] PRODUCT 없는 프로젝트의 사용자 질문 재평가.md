---
id: G-301
title: PRODUCT 없는 프로젝트의 사용자 질문 재평가
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
  - docs/requirements/[R-061] Agent 역할·문서 권한과 draft ExecPlan 사용자 판단.md
depends_on: []
complexity: medium
risk: high
priority: high
---

## Purpose

PRODUCT 없이 활성 Requirement와 ARCHITECTURE로 등록한 프로젝트도 R-091의 사용자 질문 재평가를 수행하게 한다. 현재 의미 판단·인용 검사·checkpoint CAS·동일 입력 재사용은 유지하고, 선택 문서의 확인된 부재와 필수 source 조회 실패를 구분하는 입력 수집만 보완한다.

`runtime/registration_service.py:inspect_registration_files()`와 PRODUCT의 등록 계약은 PRODUCT 또는 활성 Requirement를 허용한다. 그러나 `recovery/checkpoint_recovery.py:_assess_user_decision_cause()`는 sources에 없는 PRODUCT도 이전·현재 revision에서 무조건 `git show`로 읽고, 부재 시 Reviewer 호출 전에 실패한다. `tests/test_task_scheduling.py`의 현재 의미 판단 회귀는 모든 경로에 같은 본문을 반환해 이 등록 가능한 형상을 확인하지 못한다. ARCHITECTURE·PLANS의 기존 필수 등록 계약은 바꾸지 않는다.

## Acceptance Criteria

- PRODUCT를 sources에 선언하지 않았고 이전·현재의 확인된 원격 tree에 모두 없으며 유효한 활성 Requirement와 ARCHITECTURE가 있는 프로젝트에서, 현재·이전 작업 본문과 실제 상위 명세로 기존 질문을 재평가할 수 있다. PRODUCT 부재 자체로 새 질문·승인·문서를 만들거나 기존 판단을 무조건 해소하지 않는다.
- 확인된 선택 문서의 부재, 새로 추가/삭제된 문서, 필수/선언 source의 부재·모호한 R-ID 및 Git 조회 실패를 구분한다. 존재·내용 변화는 판단 identity와 변경 문맥에 보존한다. 필수/선언 source를 읽지 못하면 원인·질문·checkpoint·기존 이력을 유지하며 선택 문서 부재로 조용히 누락하지 않는다.
- 질문 해소는 변경된 현재 source의 정확한 인용과 ExecPlan 이외의 현재 상위 명세 인용·이유를 계속 요구한다. 존재하지 않는 PRODUCT의 인용, 삭제 사실만의 승인 추론, 링크/frontmatter 교정만의 해소, source/원격 revision·차단 원인·checkpoint 경쟁은 수용하지 않는다. 질문이 남거나 판단/인용이 불확실하면 차단을 유지한다.
- 같은 질문·동일한 source 내용/존재 입력은 기존 평가를 재사용하고, 존재나 내용이 바뀌면 낡은 결과를 재사용하지 않는다. 평가 저장과 실제 차단 해제를 구분하며 다른 작업의 투영·session·예약·실패와 독립 진행을 보존한다. 저장된 draft 결정은 R-061의 기존 사용자 출처 검증 경로로 남긴다.

## Implementation

- [ ] 확인한 `loop_manager_internal/recovery/checkpoint_recovery.py:_assess_user_decision_cause()`의 이전/현재 source 읽기를 검증된 revision의 파일 존재와 연결한다. `catalog/execplan_drafts.py:input_digests()`의 선택 PRODUCT 부재 표현과 `catalog/draft_decisions.py:user_changes()`의 실제 원격 파일 구분을 참고하되 draft 승인 처리 자체는 변경하지 않는다. 일반 `git show` 실패를 빈 본문으로 대체하지 않는다.
- [ ] 확인한 `catalog/requirement_sources.py`·`git/command_adapter.py:revision_requirement_documents()`의 R-ID resolver와 기존 Git 관측 port를 재사용한다. 필수 ARCHITECTURE·선언 sources·선택 PRODUCT의 경계를 유지하고, 새로 생기거나 사라진 선택 문서는 존재 변화까지 기존 의미 비교·prompt·identity·인용 검사에 연결한다. 공통 문서 loader나 새 상태 저장소는 필요하지 않으면 추가하지 않는다.
- [ ] 기존 `user_decision_assessment`·이력·generation CAS와 `scope_reconciliation.py`·`blocked_plan_reconciliation.py`·`blocked_plan_service.py`의 호출을 대조해 같은 수집 계약을 적용한다. 의미 판단 Reviewer·원인별 재개·대상 lease는 직접 구현을 소비하며 G-297의 배정 재검사·G-298의 차단/rebase 귀속을 중복 구현하지 않는다.
- [ ] 확인한 `tests/test_task_scheduling.py:TaskSchedulingTests.test_user_question_recheck_requires_current_cause_evidence_and_preserves_failed_assessments`의 실제 SQLite/checkpoint와 모의 Git·Agent를 재사용한다. path/revision별 서로 다른 원문·명시적 부재/오류를 제공하고, 상위 명세 인용과 실제 차단 보존을 검사한다. 필요한 상위 호출 연결은 `tests/test_plan_block_revalidation.py`·`tests/test_worker_outcome.py`의 기존 fixture에 최소 사례로 추가한다.
- [ ] PRODUCT·ARCHITECTURE의 사용자 질문 재평가 설명에 실제 보완한 선택 문서 수집 경계만 필요한 경우 통합한다. 등록 정책·Requirement·다른 ExecPlan과 사용자 결정은 수정하지 않는다.

## Validation

- Worker는 위 의미 판단 selector와 새 선택 문서 case, 실제 수정한 상위 복구 호출의 최소 unit case만 실행한다. Git·모델 관측만 mock하고 source 구분·인용 검증·실제 checkpoint/차단 상태는 구현을 통과시킨다. 실제 CLI·원격·계정 실행은 필요하지 않다.
- PRODUCT 없는 유효 프로젝트에서 Requirement의 실제 정책 변경이 질문을 해소한 경우와 선택이 남은 경우를 구분한다. 해소/미해소 결과의 근거·동일 입력 1회 호출·재관측 재사용을 확인하고, 해당 평가 자체로 PLAN_BLOCKED나 다른 작업 상태가 변하지 않아야 한다.
- 이전에 없던 PRODUCT 추가·현재 삭제와 반복 부재를 서로 다른 입력으로 검사한다. 존재하지 않는 파일의 인용과 이전 source 인용은 거부하고 현재 Requirement/ARCHITECTURE의 실제 근거를 확인한다. R-ID rename은 기존 resolver로 정상 연결하며 필수 ARCHITECTURE 또는 선언 source의 부재, 모호한 R-ID와 확인된 파일 읽기 실패는 승인이나 빈 본문으로 채택하지 않는다.
- Agent 미완료·잘못된 인용·원격/source 변화·차단 원인 또는 checkpoint CAS 경쟁에서는 이전 기준·질문·미완료 근거와 경쟁 변경을 보존한다. 기존 링크/frontmatter-only·draft 보류·독립 작업 회귀는 유지하며 동일 조건/assertion의 테스트를 복제하지 않는다.
- 기존 verify version 1·unittest·launcher Python 3.11–3.14·모듈 독립 수집·필수 compile을 유지한다. 새 adapter·실환경 검증 의무를 도입하지 않으며 unit 결과를 전체/OS verify 성공으로 표시하지 않는다.

## Progress

- [x] revision `41f8c16d78f203701a1cdd5cba1cebd6c04be465`의 등록 계약·새 재평가 reader·기존 선택 문서 수집/resolver·관련 테스트를 정적으로 대조했다. PRODUCT 없는 등록 가능 형상이 새 reader의 필수 조회와 충돌함을 확인했으며 구현·테스트·verify는 실행하지 않았다.
- [ ] 선택 문서의 확인된 부재와 필수 source/조회 실패 구분
- [ ] 의미 판단·재사용·경쟁 및 상위 복구 호출의 최소 회귀

## Decision Log

- R-091의 원인별 의미 재평가를 PRODUCT의 기존 등록 가능 범위에 연결하는 잔여 수정이다. PRODUCT 생성을 새 선행 조건으로 요구하거나 ARCHITECTURE를 선택 문서로 바꾸지 않는다. 선택 문서 부재를 사용자 승인으로 해석하지 않으며 R-061의 draft 출처 검증과 실제 재개 gate를 유지한다.
- G-189의 전수 감사는 이 국소 교정 Merge 뒤 후속 소비자를 확인한다. G-297/G-298과는 서로 다른 구현 경계이며 선행 기능을 새로 만들 필요가 없어 dependency는 비워 둔다. 직접 반영된 의미 판단 기능에 소급 계획·검증 성공을 만들지 않는다.
- complexity medium: 기존 등록·파일 존재·source resolver와 의미 판단 입력의 정해진 경계를 연결한다. risk high: 필수 source를 선택 부재로 오인하면 사용자 질문의 불충분한 해소를 수용할 수 있다. 확인된 부재만 구분하고 인용·원격/원인/CAS 경쟁 거부와 원문·질문·checkpoint 보존으로 방지한다. priority high는 정상 등록 프로젝트의 복구 중단과 잘못된 해제를 함께 막기 위한 것이다. 최초 level은 3이며 Model·Effort는 Manager의 명시 설정을 따른다.
