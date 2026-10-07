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
  - G-197
  - G-208
  - G-209
  - G-210
  - G-211
  - G-212
  - G-213
  - G-215
  - G-216
  - G-217
complexity: high
risk: high
priority: high
---

## Purpose

마지막 실제 Review 승인 당시의 verify 입력·전체 diff·명세·검토 기준과 현재 후보를 비교해 Review만의 성공 재사용을 결정한다.

## Acceptance Criteria

- verify 성공 재사용 가능성과 마지막 승인 후보 대비 전체 PR diff·선택 ExecPlan/sources·PRODUCT/ARCHITECTURE·사용자 결정·공통/역할별 원본 묶음·실제 Reviewer instruction·프로젝트 지침·Review provider/model/effort가 모두 유효하게 동일할 때만 Review를 생략한다.
- 목록 부재/오류/누락 의심·입력 비교 불가, 문서나 승인 입력 변화, 부분 성공 근거는 Review를 실행한다. verify와 무관한 명세 변화에서는 verify 재사용과 새 Review를 구분한다.
- 승인 receipt는 실제 PASS·검증된 후보/입력·작업 주기에 결합하고 최종 게시 직전 다시 확인한다. rebase·경쟁·중단/재시작에서 오래된 Review로 새 diff를 승인하거나 새 실패를 이전 PASS로 덮지 않는다.
- 다른 계획/작업의 PASS·교정 이력·승인을 복제하지 않는다. 생략 이유와 원래 근거를 남기며 실제 Reviewer가 실행되면 기존 읽기 전용·검증 결과 전달·issue 교정 계약을 유지한다.

## Implementation

- execution Review handler·publication 후보 확인과 storage의 기존 validation/checkpoint 경계를 확장한다. verify receipt와 Review 입력/receipt를 분리하되 필수 선행 검증 관계를 유지한다.
- 기존 공통·역할별 원본 묶음 버전과 실제 Reviewer 합성 instruction의 hash, 문서 source fallback/예약어 규칙 버전, G-171 최종 문서 diff 보고를 재사용한다. 최신 runtime 원본을 진행 중 작업의 고정 버전에 섞거나 common 본문만 비교해 역할 규칙 변경을 놓치지 않는다. 실제 외부 검토 기준을 확인할 수 없으면 새 Review를 선택한다.
- 실제 Reviewer 호출에는 G-197의 최종 후보 검증 근거 전달 경계를 재사용한다. verify 성공을 재사용했으면 원래 성공 근거와 현재 입력의 동일성 확인을 전달하고, 새 verify를 실행한 것으로 보고하지 않는다.
- 자동 역할·권한·검증은 docs/AGENTS.common.md와 해당 역할의 docs/AGENTS.*.md를, 프로젝트 규칙·설계·계획 작성은 AGENTS.md·docs/ARCHITECTURE.md·docs/PLANS.md를 참조한다. 기존 helper·관련 테스트를 재사용하고 지정 루트 경로를 보존한다.

## Validation

- Review·publication revision 테스트에서 완전 동일/문서만 변경/검증 입력 변경·profile/공통 또는 역할 지침/결정 변경·예약어 source·receipt 부재·게시 경쟁의 최소 unit case를 실행한다. 고정 원본 묶음과 실제 Reviewer instruction의 동일성은 기존 guidance fixture로 확인한다.
- Manager 전체 verify는 verify/Review 독립 선택, 실제 no-call/필수 call, 마지막 승인 기준·scope·최종 PR gate와 중단 재개를 검증한다.
- 실제 Review가 필요한 경우 실행한 verify 또는 재사용한 verify의 유효한 근거가 같은 후보에 결합돼 전달되는지 확인한다. Review 생략은 새 Reviewer 호출·새 테스트 실행 근거를 만들지 않는다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [x] 2026-10-06 f4e82b89646bebf33bb4812d393af98c045ae2aa와 mutable READY snapshot의 원본 hash를 대조해 G-197 선행 연결 및 실제 호출의 근거 전달 조건만 보완했다. 구현·테스트 실행은 하지 않음.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 검증·전체 diff·검토 기준·게시를 결합해 complexity high, 승인 없는 후보 게시 영향으로 risk high다.
- 2026-10-06: mutable READY token a382b8f255fc01e7ddfd58be4210b3263692ebb1e781025a65291808f7b619f3의 G-183만 개정한다. R-003 감사에서 기존 성공 결과 전달이 없음을 확인해 G-197의 실제 Merge를 선행 조건으로 추가한다. ID·경로·title·sources·Purpose와 기존 재사용 목표 및 dependency를 보존하고, 재사용한 성공을 새 실행으로 오인하지 않도록 전달 조건을 명확히 한다.
