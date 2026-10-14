---
id: G-286
title: 사용자 Requirement의 PR 문서 보고 계약 정합화
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-061] Agent 역할·문서 권한과 draft ExecPlan 사용자 판단.md
  - docs/requirements/[R-064] 모델 호출을 통한 공통 Agent 지침 전달.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
  - docs/requirements/[R-087] 문서 보고 교정 반복 제한.md
depends_on: []
complexity: low
risk: low
priority: normal
draft: true
---

## Purpose

G-272에서 확인한 R-061·R-064의 PR 문서 보고 상충을 사용자 소유 Requirement의 정합화 판단으로 보존한다. PRODUCT·ARCHITECTURE의 허용된 교정은 기존 작업에서 독립적으로 수행하고, 자동 Planner·Worker가 Requirement를 편집해 최신 R-082의 권한 경계를 우회하지 않는다. 이미 구현된 R-087의 보고 검사·PR 렌더링을 다시 구현하지 않는다.

## Draft Decision

Question: R-061·R-064에 남아 있는 PR 본문의 diff 링크·실제 변경 내용 보고 요구를 사용자가 R-087의 파일명·변경 요약·이유·영향만 표시하는 계약에 맞게 정합화할 것인가, 아니면 이 문서 정합화 목표를 보류할 것인가?
Facts: 현재 R-061의 문서 보고·Reviewer 절과 Acceptance Criteria 및 R-064의 지침 변경 보고·Acceptance Criteria는 PR diff 링크 또는 실제 변경 내용 표시를 요구한다. R-087·현재 PR 렌더링은 파일명·변경 요약·이유·영향만 표시하고 실제 내용·patch·diff 링크·hash를 제외한다. 최신 R-082·Worker 지침·결과 검사는 모든 Requirement 변경을 금지하며 자동 Planner도 사용자 명세를 수정할 수 없다. 기존 G-272의 해당 정합화 목표를 이 draft로 보존하고 허용된 제품·설계 설명 교정은 독립적으로 남긴다. G-281의 질문은 Worker 문서 수정 권한 표현에 한정되어 이 PR 보고 질문의 답변으로 확대할 수 없다.
Options: (A) 사용자가 R-061·R-064의 해당 보고 표현을 R-087과 연결해 원격에 반영한다. (B) 남은 사용자 문서 정합화 목표를 보류하고 상충 표현과 판단 대기를 보존한다. 다른 PR 표시 정책을 원하면 사용자 Requirement에서 먼저 확정한다.
Impact: A의 원문·출처·질문별 의미를 확인한 뒤 같은 ID의 실제 미충족 범위를 재평가한다. 이미 사용자가 직접 정합화를 마쳤다면 중복 Worker 구현이나 소급 검증 성공을 만들지 않는다. B에서는 이 목표와 관련 Requirement 완료 대기를 유지하며 독립 문서 교정·다른 작업은 진행한다. 결정 전 Worker·workspace·branch·PR을 시작하지 않으며 이 질문은 Worker의 Requirement 편집 예외나 기존 PR 표시 변경을 승인받는 질문이 아니다.

## Acceptance Criteria

- 사용자 변경의 검증된 원문·출처를 현재 질문에 연결해 보고 정책의 의미·충분성과 남은 상충을 확인한다. 답변 존재·승인 표시·draft 제거·무관한 문서 변경만으로 질문 해소나 정책 전환을 만들지 않는다.
- 사용자 정합화가 반영된 R-061의 문서 보고/Reviewer/Acceptance Criteria와 R-064의 공통·역할 지침 변경 보고/Acceptance Criteria를 R-087 및 실제 렌더링과 읽기 전용으로 대조한다. 추가·삭제·rename·rebase·PR head 갱신의 최종 diff 결합, 문서별 이유·영향, 사용자 PR 본문·immutable 링크 보호는 보존한다.
- Worker 결과의 DOCUMENT_CHANGE에 필요한 patch SHA-256과 PR 본문에서 제외하는 검증용 hash를 구분한다. Summary 검증·조기 전체 문서 보고 검사·반복 한도·기존 verify/Review/게시 gate를 바꾸지 않는다.
- Planner·Worker는 활성 및 archive Requirement를 편집하지 않으며 draft·User Decision 정리는 Manager에 남긴다. 사용자 변경 뒤 실제 미충족 작업만 유지하고 직접 반영된 정합화에 소급 ExecPlan 실행·테스트·PR Merge나 자동 성공을 요구하지 않는다.

## Implementation

- [ ] 사용자 원격 변경이 제공되면 확인한 `docs/requirements/[R-061] Agent 역할·문서 권한과 draft ExecPlan 사용자 판단.md`의 문서 보고·Reviewer·AC와 `docs/requirements/[R-064] 모델 호출을 통한 공통 Agent 지침 전달.md`의 지침 변경 보고·AC를 읽기 전용으로 대조한다. 남은 표현과 근거를 같은 계획의 판단에 보존하며 사용자 문서를 대신 고치지 않는다.
- [ ] 확인한 `loop_manager_internal/documents/pr_description.py:common_guidance_report()`·`refresh_common_guidance_pr()`, `execution/execution_verify_handler.py`·`execution_recovery_handler.py`와 `tests/test_execution_documents.py`의 렌더링·조기 검사·기존 PR 보존 회귀를 재사용해 정책 의미를 확인한다. 코드·테스트·지침을 오래된 문구에 맞춰 변경하지 않는다.
- [ ] 판단 결과는 질문별 사용자 원문·경로·남은 범위로 반환한다. G-272의 PRODUCT·ARCHITECTURE 교정과 G-281의 Worker 권한 질문을 중복 구현하거나 서로의 답변으로 간주하지 않는다.

## Validation

- 문서 판단만 수행하므로 실행 가능한 관련 targeted unit test가 없다. 위 구현·기존 회귀 원본과 변경된 사용자 문서를 읽기 전용으로 대조하고 테스트 존재·과거 PASS를 현재 문서 품질 또는 새로운 실행 성공으로 기록하지 않는다.
- 사용자 변경 전체·부분·보류·상충·무관한 수정에서 해소된 질문과 남은 목표를 구분한다. 실제 최종 diff/hash 검증은 유지하되 PR 본문에 diff 링크·patch·hash를 다시 요구하지 않는지 확인한다. 자동 권한 확대·사용자 결정 대필·draft 직접 해제를 하지 않는다.

## Progress

- [x] 2026-10-14 기준 `2a4c4db3c02ed9d92abee4bcf7928117cefd89bc`의 R-061·R-064·R-082·R-087, 현재 보고·scope 코드와 관련 회귀 원본을 정적으로 대조했다. G-272의 사용자 Requirement 교정 지시는 최신 쓰기 권한에서 실행할 수 없으며 해당 표현은 현재 원격에 남아 있다. 사용자 답변·구현·테스트·verify 성공은 만들지 않았다.
- [ ] 사용자 판단과 검증된 원격 문서 반영 확인
- [ ] 실제 남은 정합화 범위와 구현·문서 의미 대조

## Decision Log

- 기존 G-272의 목표를 삭제하지 않고 자동으로 고칠 수 있는 제품·설계 절과 사용자 소유 Requirement의 정합화를 나눈다. 같은 ID로 유지하는 G-272의 과거 수행 기록은 보존한다. 이번 draft의 질문은 기존 G-281의 Worker 권한 질문 및 G-277/G-284의 자동 쓰기 경계 질문과 다르다.
- complexity low: 확정된 R-087 구현과 사용자 문서의 해당 표현만 대조하며 새 실행·저장 설계가 없다. risk low: 읽기 전용 판단과 승인 근거 보존에 한정되고 사용자 명세·코드·실행 상태를 변경하지 않는다. priority normal은 필수 명세 정합화다. Model·Effort는 Manager의 명시 설정을 따른다.
- Requirement 쓰기 금지를 피해 사용자 목표·필수 검증을 없애거나 draft를 해제하지 않는다. PRODUCT·ARCHITECTURE 교정은 이 사용자 판단에 의존시키지 않고 독립 작업으로 남긴다.
