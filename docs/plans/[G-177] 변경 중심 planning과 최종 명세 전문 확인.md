---
id: G-177
title: 변경 중심 planning과 최종 명세 전문 확인
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-057] Change-Focused Planning and Final Full Verification.md
  - docs/requirements/[R-061] Agent Roles and Draft ExecPlan Decisions.md
depends_on:
  - G-170
  - G-176
complexity: high
risk: high
priority: critical
---

## Purpose

일반 planning의 변경 검토와 프로젝트 완료 전 최종 전체 문서 확인을 별도 목적·근거로 기록한다. 현재 prompt는 매번 전체 읽기를 요구하고 최종 확인을 별도로 증명하지 않는다.

## Acceptance Criteria

- 최초/유효 기준선 없음은 전체 입력을 읽는다. 이후 일반 planning은 마지막 성공 원격 입력과의 변경·삭제/rename 및 영향받는 문서/코드·catalog를 우선 검토하고 필요한 문맥을 읽는다. 무관한 변경으로 같은 판단을 반복하지 않는다.
- 빠른 점검·변경 planning·추가 계획 없음은 최종 제품 충족을 대신하지 않는다. 기존 실행/PR/READY/draft·차단 gate가 모두 해소된 완료 후보에서 일반 Planner가 활성 명세 전문과 최신 구현·검증을 최종 확인한다.
- 최종 확인의 입력 identity·결과를 보존해 같은 입력/재시작에서 재사용한다. 미충족이면 source에 연결된 후속 계획, 사용자 결정 필요면 draft로 남기고 완료·archive 성공을 합성하지 않는다.
- 유지보수는 확정 범위의 실제 Merge와 필요한 게시·catalog 반영이 확인되면 종료한다. 프로젝트 최종 전문 확인은 별도의 기존 완료 후보 조건에서만 실행하며 유지보수 종료의 추가 조건으로 사용하지 않는다. 종료만을 이유로 일반 Planner를 호출하지 않으며 기존 변경 입력·프로젝트 완료 조건에서의 정상 선택은 유지한다. 선택된 기존 작업을 선점하지 않고 입력 변화·불완전한 근거·게시 실패로 성공을 확정하지 않는다.

## Implementation

- planning 입력 지문·성공 cycle·역할 선택과 agents/agent_prompts.py의 일반 planning 문맥을 확장한다. R-068의 Requirement별 judgment를 재사용하되 프로젝트 전체 최종 전문 확인과 혼동하지 않는다.
- 변경 기준선·최종 receipt를 실제 원격 revision과 명세 내용에 결합하고 delta에서 불확실한 영향은 필요한 원문 확인으로 보완한다. 성공 receipt와 활성 작업 소유권을 분리한다.
- 공통 경계는 AGENTS.md, docs/ARCHITECTURE.md와 docs/PLANS.md를 참조한다. 기존 helper·관련 테스트를 재사용하고 지정 루트 경로를 보존한다.

## Validation

- tests/test_planning_selection.py와 planning publication/requirement retirement 테스트에서 최초·delta·삭제/rename·최종 확인·후속/draft·동일 입력 no-call의 최소 unit case를 실행한다.
- Manager 전체 verify는 변경 입력 처리, READY/PR 순서, 완료 통지·retirement·게시 실패와 유지보수 종료 후 선택의 회귀를 검증한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [x] 2026-10-06 b8ac37e의 G-176 Merge와 PRODUCT 변경을 확인하고 유지보수 종료와 프로젝트 최종 확인의 서로 다른 조건을 명확히 함.
- [x] 2026-10-06 변경 비교·최종 전문 확인의 입력/결과 저장, 완료 gate와 게시 transaction 연결 및 관련 targeted unit 검증을 구현함.
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 성공 기준선·역할 선택·완료 gate가 연결되어 complexity high, 누락된 목표의 조기 완료 영향으로 risk high다.
- G-176의 단일 평가·확정 범위 종료를 유지한다. R-057의 최종 전문 확인은 프로젝트 완료 판단에만 결합하며 유지보수 주기를 다시 열거나 사후 재검증을 복원하지 않는다.
- 검토 문맥·결과 검증은 `planning/specification_review.py`, 원격 적용 전 attempt와 성공 기준선·최종 receipt는 `storage/planning_review_state.py`로 분리했다. 기존 입력 지문·역할 선택·mutable snapshot·Requirement judgment·게시 복구 helper를 재사용하며 루트 항목을 추가·이동하지 않는다.
- 최초/비교 실패는 전문 검토, 비교 가능한 일반 호출은 delta 검토를 한다. 변경 검토·Requirement 판단 게시 후 입력과 catalog가 안정적인 완료 후보에서만 별도 최종 확인을 선택한다. 최종 확인에는 원격 입력 identity, 모든 활성 명세와 구현·검증 파일 근거 및 fulfilled/followup/needs_decision 결과를 요구한다. 후속 계획·draft의 source·기존 권한·사용자 결정 경계를 유지한다.
- 성공 근거는 기존 원격 확인·SQLite 원자 적용 transaction에서만 확정한다. 최종 확인 도중 base 전진·불완전한 결과·게시/적용 실패는 성공을 전진시키지 않는다. 새 일반 검토 성공과 실제 Merge 수·유지보수 revision·저장된 후보/verify/review identity 변경은 오래된 최종 완료 근거를 무효화하며 이력은 보존한다. 최종 Planner에 검증 identity를 전달하고 누락·오래된 근거를 성공으로 합성하지 않도록 명시한다.
- `docs/PRODUCT.md`의 일반 planning·프로젝트 완료 장기 규칙을 구현과 맞췄다. Requirement·ARCHITECTURE·PLANS·AGENTS와 사용자 결정은 수정하지 않았다. 기존 Linux `scripts/verify.sh`는 전체 검증 runner를 실행하고 실패 코드를 반환하므로 변경 없이 유지했다.
- 검증: `SpecificationReviewSelectionTest` 9개와 완료 지문·archive·원격 게시 원자 적용 unit case 3개, 실제 state port 완료 후보 및 기존 DB의 additive schema 적용 unit case 각 1개를 통과했다. 첫 실행의 fixture 비교 지문·transaction 설정 오류를 교정한 뒤 같은 명령으로 재실행했다. 전체 verify·Review·사용자 Merge는 Manager의 후속 단계다.
