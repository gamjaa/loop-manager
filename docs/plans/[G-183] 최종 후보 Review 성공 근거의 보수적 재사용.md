---
id: G-183
title: 최종 후보 Review 성공 근거의 보수적 재사용
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-072] 입력 동일성이 증명된 구현 후보 성공 결과 재사용.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
  - docs/requirements/[R-064] 모델 호출을 통한 공통 Agent 지침 전달.md
  - docs/requirements/[R-066] Agent CLI 지침의 독립성.md
  - docs/requirements/[R-085] Codex·Claude 인증 전용 자동 실행 환경.md
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
  - G-265
  - G-266
  - G-267
  - G-268
complexity: high
risk: high
priority: high
---

## Purpose

마지막 실제 Review 승인 당시의 verify 입력·전체 diff·명세·검토 기준과 현재 후보를 비교해 Review만의 성공 재사용을 결정한다. verify 재사용은 R-072의 저장소 코드 입력 기준을 소비하고, 실제 Reviewer의 지침·profile·환경·정책 동일성은 별도로 확인한다.

## Acceptance Criteria

- verify 성공 재사용 가능성과 마지막 승인 후보 대비 전체 PR diff·선택 ExecPlan/sources·PRODUCT/ARCHITECTURE·사용자 결정·공통/역할별 원본 묶음·실제 Reviewer instruction·Review provider/model/effort가 모두 유효하게 동일할 때만 Review를 생략한다. R-066에 따라 프로젝트 루트 지침의 존재·내용·해시와 과거 파일 승인을 별도 수집·대조하거나 재사용 gate로 사용하지 않는다. 실제 PR diff·확인된 일반 verify 입력의 변경은 기존 조건으로 판단한다.
- 이 작업의 마지막 최종 승인 재사용에서 목록 부재/오류/누락 의심·입력 비교 불가, 문서나 승인 입력 변화, 부분 성공 또는 미최종 선행 결과만 있으면 필요한 Review를 실행한다. R-086 일반 선행 PASS 뒤의 실제 전체 verify와 최종 승격은 해당 별도 계약을 유지하며 이 재사용 판정으로 추가 Reviewer를 강제하거나 전체 verify를 생략하지 않는다. verify와 무관한 명세 변화에서는 verify 재사용과 새 Review를 구분한다.
- 승인 receipt는 실제 PASS·검증된 후보/입력·작업 주기에 결합하고 최종 게시 직전 다시 확인한다. rebase·경쟁·중단/재시작에서 오래된 Review로 새 diff를 승인하거나 새 실패를 이전 PASS로 덮지 않는다.
- 다른 계획/작업의 PASS·교정 이력·승인을 복제하지 않는다. 생략 이유와 원래 근거를 남기며 실제 Reviewer가 실행되면 기존 읽기 전용·검증 결과 전달·issue 교정 계약을 유지한다.
- G-241의 일반 verify 재사용은 최신 R-072의 목록·저장소 코드/테스트/fixture·의존성 선언·검증 스크립트/선언과 선택 진입점의 bytes·경로·kind·mode 및 누락 확인으로 판단한다. 설치 도구·환경변수·사용자/시스템 설정·외부 동적 입력·Git revision 값의 동일성이나 `runtime_identity` 증명을 verify의 선행 조건으로 요구하지 않는다. 이 제외를 실제 Reviewer의 환경·정책 비교로 확대하지 않는다. R-082의 선행 Review PASS·VERIFY_REQUIRED와 과거 verify 참고 출력은 최종 승인 receipt가 아니며, 검증 보완 이후에도 문서 선행 결과 자체를 재사용·게시 근거로 승격하지 않는다. R-086의 일반 선행 결과는 별도 증거이며 해당 전체 검증·최종 승인 gate를 통과한 기록만 마지막 최종 승인으로 읽을 수 있다.
- R-085의 실제 Reviewer 실행 환경과 소비하는 설정·정책 입력이 같다는 근거를 확인한다. 인증 원본의 사용하지 않는 개인 config 변경은 재사용 입력이 아니며, 관리 home/source 선택·유효 정책 변경 또는 과거 환경의 입력 부재는 새 Review로 연결한다. 인증값을 지문·receipt·로그에 넣거나 현재 환경을 과거 승인 당시의 환경으로 소급 채택하지 않는다.

## Implementation

- [ ] 확인한 `loop_manager_internal/execution/execution_review_handler.py`의 `_review_entry_evidence()`·Review 처리와 publication 후보 확인, storage의 기존 validation/checkpoint 경계를 확장한다. `verification/inputs.py`·`manager_adapter.py`와 `execution/result_validation.py`에 남은 환경/runtime 완전성 gate의 전환은 G-241이 담당하며 이 작업에서 중복 구현하지 않는다. 그 결과의 코드 입력 receipt와 실제 Reviewer 호출 identity를 분리하되 필수 선행 검증 관계를 유지한다. R-086의 preliminary_reviews·실제 전체 run ID·최종 승격 및 게시 재확인 경계를 보존하고, 부분 token/receipt를 전체 검증 증거로 읽거나 일반 선행 단계를 다시 구현하지 않는다.
- 기존 공통·역할별 원본 묶음 버전과 실제 Reviewer 합성 instruction의 hash, 문서 source fallback/예약어 규칙 버전, G-171 최종 문서 diff 보고를 재사용한다. 최신 runtime 원본을 진행 중 작업의 고정 버전에 섞거나 common 본문만 비교해 역할 규칙 변경을 놓치지 않는다. 실제 외부 검토 기준을 확인할 수 없으면 새 Review를 선택한다.
- 실제 Reviewer 호출에는 G-197의 최종 후보 검증 근거 전달 경계를 재사용한다. verify 성공을 재사용했으면 원래 성공 근거와 현재 입력의 동일성 확인을 전달하고, 새 verify를 실행한 것으로 보고하지 않는다.
- G-262의 준비된 실행 환경·실제 설정 관측과 G-263/G-264의 provider별 공통 홈/소비 source를 사용한다. 역할별 임시 경로와 공통 홈을 구분하고 실제 현재 설정 생성·교체 및 최종 검사 뒤 변경을 같은 비밀값 없는 환경/정책 identity에 반영한다. 사용하지 않는 과거 runtime config·개인 config를 조회·복원하거나 인증 cache를 추가하지 않는다. 과거 환경의 증거 부재나 확인 불가를 동일성으로 대용하지 않는다.
- 자동 역할·권한·검증은 고정된 docs/AGENTS.common.md와 해당 역할의 docs/AGENTS.*.md instruction을 사용하고, 설계·계획 작성은 docs/ARCHITECTURE.md·docs/PLANS.md를 참조한다. 프로젝트 지침 해석은 Agent CLI에 맡기며 Manager가 루트 지침을 읽거나 과거 승인·patch를 Review receipt에 추가하지 않는다. 기존 helper·관련 테스트와 R-064의 예약 경로를 유지한다.

## Validation

- Review·publication revision 테스트에서 완전 동일/문서만 변경/검증 입력 변경·profile/공통 또는 역할 지침/결정 변경·예약어 source·receipt 부재·게시 경쟁의 최소 unit case를 실행한다. 고정 원본 묶음과 실제 Reviewer instruction의 동일성은 기존 guidance fixture로 확인한다.
- 확인한 `tests/test_verify_receipts.py`·`tests/test_preliminary_review.py`의 실제 receipt/SQLite fixture로 verify 코드 입력 불변과 Reviewer 입력 변화를 분리한다. verify의 환경/runtime 미확인만으로 전체 실행·Review를 강제하지 않고, 실제 Reviewer 환경·정책 변경 또는 과거 호출 증거 부재에는 새 Review가 필요해야 한다. 코드 입력 누락·변경·새 실패와 R-086 일반 선행 PASS 뒤 실제 전체 run 조건은 계속 유지한다. 기존 환경 완전성 테스트를 새 코드 기준의 성공 근거로 채택하지 않는다.
- 프로젝트 루트 지침을 별도 조회하지 않고 과거 파일 승인이나 전용 지침 지문 부재만으로 Review를 요구하지 않는지 확인한다. 실제 승인 근거 부재·PR diff·일반 verify 입력·고정 instruction 변경에는 필요한 Review를 수행한다.
- Manager 전체 verify는 verify/Review 독립 선택, 실제 no-call/필수 call, 마지막 승인 기준·scope·최종 PR gate와 중단 재개를 검증한다.
- 실제 Review가 필요한 경우 실행한 verify 또는 재사용한 verify의 유효한 근거가 같은 후보에 결합돼 전달되는지 확인한다. Review 생략은 새 Reviewer 호출·새 테스트 실행 근거를 만들지 않는다.
- 단계별 문서 변경 뒤 선행 PASS·VERIFY_REQUIRED·최종 PASS를 구분하고, 일반 verify 재사용 및 새 완료 조건의 증거 부재에서 최종 Review를 잘못 생략하지 않는 최소 unit case를 확인한다.
- R-085 fixture에서 실제 미소비 개인 설정만 변경한 경우와 관리 home/source·소비 설정/정책 변경·과거 환경 identity 부재를 분리한다. 실제 no-call/필수 Reviewer call·기존 receipt 보존·비밀값 비노출을 확인하며 모의 인증 성공을 실계정 실행 근거로 사용하지 않는다.
- 공통 홈의 정상 설정 생성/교체 뒤에는 새 현재 설정 관측을 사용하고 낡은 설정의 Review를 재사용하지 않는다. 호출별 소유 임시 경로 정규화가 다른 worktree/role/정책 변경을 숨기지 않는지 기존 provider execution fixture로 확인한다. R-085 Non-Goals의 session/CLI·실계정/OS 호환성 미수행을 별도 선행 gate로 추가하지 않으며 현재 호출의 실제 격리/완료 증거는 계속 요구한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [x] 2026-10-06 f4e82b89646bebf33bb4812d393af98c045ae2aa와 mutable READY snapshot의 원본 hash를 대조해 G-197 선행 연결 및 실제 호출의 근거 전달 조건만 보완했다. 구현·테스트 실행은 하지 않음.
- [ ] 구현 및 관련 targeted unit 검증
- [x] 2026-10-09 eb797d3a의 R-072·R-082와 현재 verify adapter·문서 선행 Review를 정적으로 대조했다. 일반 verify 재사용은 미충족이며 선행 Review는 최종 승인이 아니다. 구현·테스트 실행 없음.
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge
- [x] 2026-10-11 delta 검토: PR 전 snapshot token `4e4794ea5a72e4acbf130a00b86c65166feb4676b19e417a33a4af84e631cb28`, revision `6fc245469a6bd3cced81dbf64fc049130982bbc8`와 원본 hash `94dc14021dce370d8033c0c91bf1ab0dcea013699e1240603e397a78571a4314`를 대조했다. R-085의 미구현 실행 환경 전환을 기존 최종 Review 동일성 목표에 연결했다. 기존 기록을 보존하며 구현·테스트·전체 verify는 실행하지 않았다.

## Decision Log

- revision `1db8c31aea93a2490f571fa1bc4dac6a03406f46`의 전체 검토에서 R-072의 코드 입력 재사용과 R-085의 실제 Reviewer 환경 증명을 분리했다. G-241의 과거 외부 환경 완전성 요구는 최신 명세의 구현 조건이 아니며, 해당 계획은 이번 수정 snapshot 밖이므로 개정 필요성을 Manager에 반환한다. 필요한 G-241 dependency와 이 작업의 기존 기록·metadata는 보존하고 별도 중복 작업이나 검증 성공을 만들지 않는다.

- 2026-10-12 사용자 요청의 문서 정합화: R-086 일반 선행 최종 승격과 이 작업의 마지막 최종 승인 재사용을 구분했다. R-082 문서 선행 결과의 승격 금지를 명확히 하고 실제 활성 source 경로를 교정했다. ID·목표·기존 dependency·metadata·수행 기록은 보존하며 G-183 구현 완료나 자동 Planner snapshot 적용으로 기록하지 않는다.

- 2026-10-11: G-263/G-264의 실제 provider 환경 분리를 선행으로 연결한다. 새 Reviewer 환경을 확인하지 않고 개인 설정 지문이나 과거 환경의 PASS를 재사용하는 경로를 막으며, 인증 환경 구현은 해당 계획에 남긴다. 기존 high complexity의 승인/입력/게시 수명 판단과 high risk의 미검증 후보 게시·receipt 보존/재검증 복구를 유지한다. ID·경로·기존 목표·dependency·수행 기록을 보존한 delta 개정이며 프로젝트 최종 전문 확인이 아니다.

- 검증·전체 diff·검토 기준·게시를 결합해 complexity high, 승인 없는 후보 게시 영향으로 risk high다.
- 2026-10-06: mutable READY token a382b8f255fc01e7ddfd58be4210b3263692ebb1e781025a65291808f7b619f3의 G-183만 개정한다. R-003 감사에서 기존 성공 결과 전달이 없음을 확인해 G-197의 실제 Merge를 선행 조건으로 추가한다. ID·경로·title·sources·Purpose와 기존 재사용 목표 및 dependency를 보존하고, 재사용한 성공을 새 실행으로 오인하지 않도록 전달 조건을 명확히 한다.
- 2026-10-09: snapshot token 324c039278646b522a5cbad59f1ba7ad5c4d543c36002cf4bdc2efc2bd0e05ed와 원본 hash를 확인했다. 기존 dependency와 목표를 보존하며 일반 verify 증명 및 단계별 문서 판단 보완을 선행 연결한다. high complexity의 남은 판단은 마지막 승인·현재 후보·verify 증명·문서 선행 Review의 수명과 게시 경쟁을 일관되게 결합하는 것이다. risk high는 잘못된 승인 재사용으로 미검증 후보가 게시되는 영향이며 receipt 보존·재검증과 PR 수동 복구가 필요하다.
- 2026-10-09 사용자 요청에 따른 지침 경계 정합화: R-072의 검토 입력을 R-064·R-066에 맞춰 명확히 했다. 프로젝트 루트 지침의 별도 수집·지문·파일 승인 gate를 제거하며 실제 diff·검증 입력·고정 원본 묶음·Reviewer instruction 비교와 기존 재사용 목표·dependency·실행 이력은 보존한다. 구현·검증 성공을 기록하지 않는다.
