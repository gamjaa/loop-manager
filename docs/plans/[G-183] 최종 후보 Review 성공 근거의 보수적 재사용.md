---
id: G-183
title: 최종 후보 Review 성공 근거의 보수적 재사용
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-072] Reuse Successful Candidate Results with Proven Inputs.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
  - docs/requirements/[R-064] 모델 호출을 통한 공통 Agent 지침 전달.md
  - docs/requirements/[R-066] AGENTS 변경 사전 승인.md
  - docs/requirements/[R-085] Codex Claude 인증 전용 자동 실행 환경.md
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
  - G-241
  - G-242
  - G-263
  - G-264
complexity: high
risk: high
priority: high
---

## Purpose

마지막 실제 Review 승인 당시의 verify 입력·전체 diff·명세·검토 기준과 현재 후보를 비교해 Review만의 성공 재사용을 결정한다.

## Acceptance Criteria

- verify 성공 재사용 가능성과 마지막 승인 후보 대비 전체 PR diff·선택 ExecPlan/sources·PRODUCT/ARCHITECTURE·사용자 결정·공통/역할별 원본 묶음·실제 Reviewer instruction·Review provider/model/effort가 모두 유효하게 동일할 때만 Review를 생략한다. R-066에 따라 프로젝트 루트 지침의 존재·내용·해시와 과거 파일 승인을 별도 수집·대조하거나 재사용 gate로 사용하지 않는다. 실제 PR diff·확인된 일반 verify 입력의 변경은 기존 조건으로 판단한다.
- 목록 부재/오류/누락 의심·입력 비교 불가, 문서나 승인 입력 변화, 부분 성공 근거는 Review를 실행한다. verify와 무관한 명세 변화에서는 verify 재사용과 새 Review를 구분한다.
- 승인 receipt는 실제 PASS·검증된 후보/입력·작업 주기에 결합하고 최종 게시 직전 다시 확인한다. rebase·경쟁·중단/재시작에서 오래된 Review로 새 diff를 승인하거나 새 실패를 이전 PASS로 덮지 않는다.
- 다른 계획/작업의 PASS·교정 이력·승인을 복제하지 않는다. 생략 이유와 원래 근거를 남기며 실제 Reviewer가 실행되면 기존 읽기 전용·검증 결과 전달·issue 교정 계약을 유지한다.
- G-241의 일반 verify 입력 증명과 최신 완료 조건을 확인한다. R-082의 선행 Review PASS·VERIFY_REQUIRED와 과거 verify 참고 출력은 최종 승인 receipt가 아니며, 검증 보완 전에는 재사용·게시 근거로 승격하지 않는다.
- R-085의 실제 Reviewer 실행 환경과 소비하는 설정·정책 입력이 같다는 근거를 확인한다. 인증 원본의 사용하지 않는 개인 config 변경은 재사용 입력이 아니며, 관리 home/source 선택·유효 정책 변경 또는 과거 환경의 입력 부재는 새 Review로 연결한다. 인증값을 지문·receipt·로그에 넣거나 현재 환경을 과거 승인 당시의 환경으로 소급 채택하지 않는다.

## Implementation

- execution Review handler·publication 후보 확인과 storage의 기존 validation/checkpoint 경계를 확장한다. verify receipt와 Review 입력/receipt를 분리하되 필수 선행 검증 관계를 유지한다.
- 기존 공통·역할별 원본 묶음 버전과 실제 Reviewer 합성 instruction의 hash, 문서 source fallback/예약어 규칙 버전, G-171 최종 문서 diff 보고를 재사용한다. 최신 runtime 원본을 진행 중 작업의 고정 버전에 섞거나 common 본문만 비교해 역할 규칙 변경을 놓치지 않는다. 실제 외부 검토 기준을 확인할 수 없으면 새 Review를 선택한다.
- 실제 Reviewer 호출에는 G-197의 최종 후보 검증 근거 전달 경계를 재사용한다. verify 성공을 재사용했으면 원래 성공 근거와 현재 입력의 동일성 확인을 전달하고, 새 verify를 실행한 것으로 보고하지 않는다.
- G-262의 준비된 실행 환경·실제 설정 관측과 G-263/G-264의 provider별 source 선택을 소비한다. 재사용 판정과 실제 Reviewer 실행이 같은 비밀값 없는 환경/정책 identity를 사용하게 하며 별도 home 탐색·개인 config 조회·인증 cache를 추가하지 않는다. 기존 격리/설정 증거의 확인 불가를 동일성으로 대용하지 않는다.
- 자동 역할·권한·검증은 고정된 docs/AGENTS.common.md와 해당 역할의 docs/AGENTS.*.md instruction을 사용하고, 설계·계획 작성은 docs/ARCHITECTURE.md·docs/PLANS.md를 참조한다. 프로젝트 지침 해석은 Agent CLI에 맡기며 Manager가 루트 지침을 읽거나 과거 승인·patch를 Review receipt에 추가하지 않는다. 기존 helper·관련 테스트와 R-064의 예약 경로를 유지한다.

## Validation

- Review·publication revision 테스트에서 완전 동일/문서만 변경/검증 입력 변경·profile/공통 또는 역할 지침/결정 변경·예약어 source·receipt 부재·게시 경쟁의 최소 unit case를 실행한다. 고정 원본 묶음과 실제 Reviewer instruction의 동일성은 기존 guidance fixture로 확인한다.
- 프로젝트 루트 지침을 별도 조회하지 않고 과거 파일 승인이나 전용 지침 지문 부재만으로 Review를 요구하지 않는지 확인한다. 실제 승인 근거 부재·PR diff·일반 verify 입력·고정 instruction 변경에는 필요한 Review를 수행한다.
- Manager 전체 verify는 verify/Review 독립 선택, 실제 no-call/필수 call, 마지막 승인 기준·scope·최종 PR gate와 중단 재개를 검증한다.
- 실제 Review가 필요한 경우 실행한 verify 또는 재사용한 verify의 유효한 근거가 같은 후보에 결합돼 전달되는지 확인한다. Review 생략은 새 Reviewer 호출·새 테스트 실행 근거를 만들지 않는다.
- 단계별 문서 변경 뒤 선행 PASS·VERIFY_REQUIRED·최종 PASS를 구분하고, 일반 verify 재사용 및 새 완료 조건의 증거 부재에서 최종 Review를 잘못 생략하지 않는 최소 unit case를 확인한다.
- R-085 fixture에서 실제 미소비 개인 설정만 변경한 경우와 관리 home/source·소비 설정/정책 변경·과거 환경 identity 부재를 분리한다. 실제 no-call/필수 Reviewer call·기존 receipt 보존·비밀값 비노출을 확인하며 모의 인증 성공을 실계정 실행 근거로 사용하지 않는다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [x] 2026-10-06 f4e82b89646bebf33bb4812d393af98c045ae2aa와 mutable READY snapshot의 원본 hash를 대조해 G-197 선행 연결 및 실제 호출의 근거 전달 조건만 보완했다. 구현·테스트 실행은 하지 않음.
- [ ] 구현 및 관련 targeted unit 검증
- [x] 2026-10-09 eb797d3a의 R-072·R-082와 현재 verify adapter·문서 선행 Review를 정적으로 대조했다. 일반 verify 재사용은 미충족이며 선행 Review는 최종 승인이 아니다. 구현·테스트 실행 없음.
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge
- [x] 2026-10-11 delta 검토: PR 전 snapshot token `4e4794ea5a72e4acbf130a00b86c65166feb4676b19e417a33a4af84e631cb28`, revision `6fc245469a6bd3cced81dbf64fc049130982bbc8`와 원본 hash `94dc14021dce370d8033c0c91bf1ab0dcea013699e1240603e397a78571a4314`를 대조했다. R-085의 미구현 실행 환경 전환을 기존 최종 Review 동일성 목표에 연결했다. 기존 기록을 보존하며 구현·테스트·전체 verify는 실행하지 않았다.

## Decision Log

- 2026-10-11: G-263/G-264의 실제 provider 환경 분리를 선행으로 연결한다. 새 Reviewer 환경을 확인하지 않고 개인 설정 지문이나 과거 환경의 PASS를 재사용하는 경로를 막으며, 인증 환경 구현은 해당 계획에 남긴다. 기존 high complexity의 승인/입력/게시 수명 판단과 high risk의 미검증 후보 게시·receipt 보존/재검증 복구를 유지한다. ID·경로·기존 목표·dependency·수행 기록을 보존한 delta 개정이며 프로젝트 최종 전문 확인이 아니다.

- 검증·전체 diff·검토 기준·게시를 결합해 complexity high, 승인 없는 후보 게시 영향으로 risk high다.
- 2026-10-06: mutable READY token a382b8f255fc01e7ddfd58be4210b3263692ebb1e781025a65291808f7b619f3의 G-183만 개정한다. R-003 감사에서 기존 성공 결과 전달이 없음을 확인해 G-197의 실제 Merge를 선행 조건으로 추가한다. ID·경로·title·sources·Purpose와 기존 재사용 목표 및 dependency를 보존하고, 재사용한 성공을 새 실행으로 오인하지 않도록 전달 조건을 명확히 한다.
- 2026-10-09: snapshot token 324c039278646b522a5cbad59f1ba7ad5c4d543c36002cf4bdc2efc2bd0e05ed와 원본 hash를 확인했다. 기존 dependency와 목표를 보존하며 일반 verify 증명 및 단계별 문서 판단 보완을 선행 연결한다. high complexity의 남은 판단은 마지막 승인·현재 후보·verify 증명·문서 선행 Review의 수명과 게시 경쟁을 일관되게 결합하는 것이다. risk high는 잘못된 승인 재사용으로 미검증 후보가 게시되는 영향이며 receipt 보존·재검증과 PR 수동 복구가 필요하다.
- 2026-10-09 사용자 요청에 따른 지침 경계 정합화: R-072의 검토 입력을 R-064·R-066에 맞춰 명확히 했다. 프로젝트 루트 지침의 별도 수집·지문·파일 승인 gate를 제거하며 실제 diff·검증 입력·고정 원본 묶음·Reviewer instruction 비교와 기존 재사용 목표·dependency·실행 이력은 보존한다. 구현·검증 성공을 기록하지 않는다.
