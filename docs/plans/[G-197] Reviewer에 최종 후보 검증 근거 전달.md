---
id: G-197
title: Reviewer에 최종 후보 검증 근거 전달
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-003] Targeted Worker Unit Tests.md
  - docs/requirements/[R-077] Manual Plan Work Start and Completion.md
  - docs/requirements/[R-072] Reuse Successful Candidate Results with Proven Inputs.md
depends_on:
  - G-208
  - G-209
  - G-210
  - G-211
  - G-212
  - G-213
  - G-215
  - G-216
  - G-217
complexity: medium
risk: medium
priority: high
---

## Purpose

Reviewer가 현재 후보의 Worker targeted unit test 결과와 Manager 전체 verify 성공 근거를 넘겨받도록 R-003의 누락된 전달 경계를 완성한다. 현재 진입 gate는 두 결과를 검사하지만 prompt에는 targeted test 미실행 사유만 전달하며, 회귀 테스트도 성공 결과의 누락을 요구한다.

## Acceptance Criteria

- 실제 Reviewer 호출에는 검증된 최종 Worker 결과의 targeted test 실행 범위·결과 또는 구체적인 미실행 사유와, 같은 후보의 Manager 전체 verify 성공 근거가 포함된다. Worker가 보고한 command·결과와 Manager가 확인한 사실을 구분하고, 보고되지 않은 command·검사 수·성공을 합성하지 않는다.
- 전달 근거는 기존 후보 HEAD·base·계획/입력 검증 gate를 통과한 결과만 사용한다. 실패·누락·다른 후보의 결과로 Reviewer를 시작하지 않으며, rebase·후보/관련 입력 변경·중단 재개에서 낡은 결과를 전달하지 않는다. stdout이 비어 있는 정상 verify 성공도 Manager가 확인한 성공으로 전달한다. G-182의 유효한 verify 재사용은 원래 실제 run·같은 작업 주기·현재 후보 입력 동일성 근거로 전달하고 새 verify 실행으로 표시하지 않는다. 새 실패·중단·변경 입력·receipt/후보 연결 불일치에는 과거 성공을 전달하지 않는다.
- Reviewer는 전달된 결과와 코드·테스트·verify 변경을 읽기 전용으로 검토해 테스트 누락·회귀 위험·검증 약화를 판단한다. 테스트나 verify를 재실행하지 않는다. Worker의 최소 targeted 범위, 같은 세션의 실패 수정·재실행, 미실행 사유와 Manager 전체 verify의 유효한 성공 필수·실패 복귀 계약 및 R-072의 증명된 재사용 예외를 유지한다.
- 전달은 필요한 결과와 후보 식별 근거로 제한하고 기존 비밀값 제거를 적용한다. commit 제목·PR Summary와 무관한 Worker 출력 또는 전체 실행 로그를 Reviewer prompt에 복제하지 않는다. 전달 실패를 성공 근거로 대체하지 않고 기존 재개 가능한 상태·session·작업물을 보존한다.
- R-077의 사용자 제출 후보에는 실제 Worker targeted 결과가 없음을 명시하고 외부 테스트를 PASS로 합성하지 않는다. 실제 verify PASS와 유효한 사용자 verify 생략을 구분해 전달하며, 수동 묶음의 모든 G·후보 R과 후보 근거를 유지한다. 수락 뒤 자동 Worker 교정이 실제 수행됐으면 그 최종 targeted 결과를 전달한다. 위의 필수 Worker·verify 진입 조건은 일반 자동 경로에 적용하며 수동 예외를 그 경로로 확대하지 않는다.

## Implementation

- agents/agent_prompts.py, execution/result_validation.py와 execution Review/verify handler의 기존 결과 파싱·후보 token·checkpoint·feedback 경계를 재사용한다. 검증된 TARGETED_TESTS와 Manager verify 근거를 작업별 compact한 사용자 prompt에 연결하고, 미실행 사유만 남기는 현재 helper의 책임을 이 전달 계약에 맞춘다. 읽기 전용·검증 재실행 금지 규칙은 고정된 docs/AGENTS.common.md+docs/AGENTS.reviewer.md instruction을 재사용하며 결과 전달로 권한을 확대하지 않는다. 별도 Worker 반환 schema나 범용 증거 서비스를 도입하지 않는다.
- 실제 호출 직전 기존 진입 gate와 전달 근거의 후보를 함께 확인한다. 정상 실행·verify/Review 교정·저장 단계 재개에 같은 경계를 적용하고, 긴 출력은 판정에 필요한 command·결과·미실행 사유를 잃지 않도록 처리한다. 성공 시 Worker 재호출이나 추가 검증 실행을 결과 전달 수단으로 사용하지 않는다.
- Merge된 `storage/verify_receipts.py`와 verify handler·revision gate를 재사용해 실제 run/현재 후보에 연결된 reuse/수동 waiver를 구분한다. 원래 성공 출력만으로 현재 입력을 증명하거나 새 receipt·사용량·성공을 합성하지 않는다. receipt 저장/재사용 정책과 G-183의 Review 재사용은 다시 구현하지 않는다.
- Merge된 `execution/manual_candidate.py`의 수동 prompt·unconfirmed targeted 표식과 실제/생략 단계 결과를 같은 전달 경계에서 유지한다. 수동 외부 입력의 성공으로 채우지 않으며 생략 identity·운영 설정 자체의 교정은 G-200에 남긴다.
- 자동 문서·역할·검증 권한은 docs/AGENTS.common.md와 해당 역할의 docs/AGENTS.*.md를, 프로젝트 규칙·계획 작성과 검증·재시도 설계는 AGENTS.md·docs/ARCHITECTURE.md·docs/PLANS.md를 따른다. 관련 helper·fixture를 우선 재사용하고 지정 루트 경로를 보존한다.

## Validation

- Worker는 tests/test_loop_manager.py의 prompt 경계와 tests/test_execution_verify_phase.py·tests/test_worker_result_contract.py의 관련 최소 unit case만 수정·실행한다. 최종 targeted 결과와 verify 성공 전달, 구체적 미실행 사유, 빈 verify stdout, 알려진 실패 후 동일 command 재실행, 오래된 후보 거부, 비밀값 제거와 무관한 출력 제외를 확인한다. 실제 Agent는 test double로 대체한다.
- tests/test_worker_validation.py·tests/test_worker_publication_validation.py의 성공 결과 누락 기대값도 수정하되 연결된 실행 흐름의 검증은 Manager 전체 verify에 맡긴다. Worker 반환→필수 전체 verify→읽기 전용 Reviewer 순서와 실패 시 같은 Worker session 재개, targeted 내부 재실행의 SQLite retry 비소비, rebase/입력 변경·중단 재개 후 최종 후보 근거 전달 및 게시 gate를 검증한다. Reviewer 결과 전달을 전체 검증 생략 또는 테스트 재실행 허용으로 바꾸지 않는다.
- `tests/test_manual_work.py`의 기본 Reviewer 호출·verify 생략·자동 교정 사례를 사용해 외부 targeted 미확인, 실제 verify/사용자 생략의 구분과 실제 Worker 교정 뒤 결과 전달을 회귀로 확인한다. 수동 묶음의 G별 Review 결과·현재 후보·비밀값 제거를 유지한다.
- `tests/test_verify_receipts.py`의 실제 성공→현재 후보 reuse·입력 변화·새 실패/중단·다른 주기·CAS와 waiver fixture를 재사용해 Reviewer 전달의 원래 run/현재 연결, 새 실행 비합성과 잘못된 근거 거부를 최소 unit으로 확인한다. R-080 및 ARCHITECTURE의 Git 경계에 따라 전달/프로토콜 조건은 관측 mock과 실제 handler·SQLite로 검사한다. G-223에서 관련 테스트가 전환되면 그 경계를 재사용하되 전환 완료를 선행으로 요구하거나 실제 publication/lease 회귀를 삭제하지 않는다.

## Progress

- [x] 2026-10-06 제공된 planning revision f4e82b89646bebf33bb4812d393af98c045ae2aa에서 R-003 전체 기준과 실제 prompt·진입 gate·결과 파싱·관련 테스트를 대조했다. 성공 결과 누락은 기존 G-171의 문서 수정/PR 설명과 별개이며 G-183은 전달 계약을 기존 동작으로 전제한다. 원격 tip 재조회는 DNS 실패로 확인하지 못했다. 구현·테스트 실행은 하지 않았다.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- Reviewer 호출 여부 검사만으로 결과 전달 기준을 충족한 것으로 판단하지 않는다. R-003의 원래 목표와 ARCHITECTURE의 명시적 전달 계약을 복구하며 새로운 제품 동작이나 검증 권한을 추가하지 않는다.
- 기존 검증 결과와 후보 경계의 제한된 연결로 complexity medium이다. 잘못된 근거 전달은 검토 누락을 만들지만 기존 후보/게시 gate를 유지해 risk medium이며, 필수 검토 근거의 누락을 보완하므로 priority high다.
- G-183의 실제 Review 입력·재사용 판단이 이 전달 계약을 먼저 사용하도록 해당 READY 계획만 dependency를 추가한다. G-171의 문서 보고와 G-181/G-182의 verify 재사용은 이 계획에 구현하지 않는다.
- 2026-10-07 f2ae0811a0230045852743a5fae84c6a91579bb0과 mutable READY snapshot token 4def92eecf35e12389410c9449070f8fe254e00c1a34f9fbefbc30a69824fc47의 원본 hash를 대조했다. R-077이 추가한 수동 사용자 제출/실제 Worker 교정의 차이를 전달 회귀에 명시해, 원래 결과 전달 목표가 수동 테스트 성공 합성이나 강제 Worker 호출을 만들지 않도록 했다. ID·Purpose·기존 dependency·complexity medium·risk medium·priority high를 보존하며 구현·테스트 실행은 하지 않았다.
- 2026-10-08: planning revision f3ae228ff5f497d9c3ff0e8fe3c775cb6ea8df0d, mutable READY token 29463c16a1139038f51b09c0f15ebadd5815c0deb0ae21505089e7c7a6be46bb와 G-197 원문 hash 1ba5aa3a972dd85620e3baebf057fe50aeae42b4b06f623d73e9e97e70771417을 대조했다. G-182의 실제 Merge로 추가된 verify run/reuse receipt를 기존 결과 전달 목표에 연결하고 활성 R-072를 source에 추가했다. ID·경로·title·Purpose·기존 dependency·complexity medium·risk medium·priority high와 수동 제출 차이를 보존한다. 전달 검증의 Git 조건은 R-080의 기존 관측 경계를 따르며 G-223의 테스트 전환과 독립적으로 수행할 수 있다. 구현·테스트 실행은 하지 않았다.
