---
id: G-183
title: 최종 후보 Review 성공 근거의 보수적 재사용
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-072] Reuse Successful Candidate Results with Proven Inputs.md
depends_on:
  - G-182
  - G-147
  - G-171
complexity: high
risk: high
priority: high
---

## Purpose

마지막 실제 Review 승인 당시의 verify 입력·전체 diff·명세·검토 기준과 현재 후보를 비교해 Review만의 성공 재사용을 결정한다.

## Acceptance Criteria

- verify 성공 재사용 가능성과 마지막 승인 후보 대비 전체 PR diff·선택 ExecPlan/sources·PRODUCT/ARCHITECTURE·사용자 결정·공통/프로젝트 지침·Review provider/model/effort가 모두 유효하게 동일할 때만 Review를 생략한다.
- 목록 부재/오류/누락 의심·입력 비교 불가, 문서나 승인 입력 변화, 부분 성공 근거는 Review를 실행한다. verify와 무관한 명세 변화에서는 verify 재사용과 새 Review를 구분한다.
- 승인 receipt는 실제 PASS·검증된 후보/입력·작업 주기에 결합하고 최종 게시 직전 다시 확인한다. rebase·경쟁·중단/재시작에서 오래된 Review로 새 diff를 승인하거나 새 실패를 이전 PASS로 덮지 않는다.
- 다른 계획/작업의 PASS·교정 이력·승인을 복제하지 않는다. 생략 이유와 원래 근거를 남기며 실제 Reviewer가 실행되면 기존 읽기 전용·검증 결과 전달·issue 교정 계약을 유지한다.

## Implementation

- execution Review handler·publication 후보 확인과 storage의 기존 validation/checkpoint 경계를 확장한다. verify receipt와 Review 입력/receipt를 분리하되 필수 선행 검증 관계를 유지한다.
- G-147의 고정 공통 버전, 문서 source fallback/예약어 규칙 버전, G-171 최종 문서 diff 보고를 재사용한다. 실제 외부 검토 기준을 확인할 수 없으면 새 Review를 선택한다.
- 공통 경계는 AGENTS.md, docs/ARCHITECTURE.md와 docs/PLANS.md를 참조한다. 기존 helper·관련 테스트를 재사용하고 지정 루트 경로를 보존한다.

## Validation

- Review·publication revision 테스트에서 완전 동일/문서만 변경/검증 입력 변경·profile/지침/결정 변경·예약어 source·receipt 부재·게시 경쟁의 최소 unit case를 실행한다.
- Manager 전체 verify는 verify/Review 독립 선택, 실제 no-call/필수 call, 마지막 승인 기준·scope·최종 PR gate와 중단 재개를 검증한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 검증·전체 diff·검토 기준·게시를 결합해 complexity high, 승인 없는 후보 게시 영향으로 risk high다.
