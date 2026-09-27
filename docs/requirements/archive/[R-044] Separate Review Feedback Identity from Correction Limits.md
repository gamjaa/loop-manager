# [R-044] Separate Review Feedback Identity from Correction Limits

## Context

G-088의 Review에서는 격리 미구현, 불충분한 검증, 모든 Agent 호출 거부와 writable mount 별칭 누락이 같은 `missing-write-isolation` ISSUE_ID로 기록됐다. 이전 결함을 수정한 뒤 다른 결함이 발견돼도 같은 지문의 실패 횟수가 누적돼 `review_retry_exhausted`로 차단됐다. ISSUE_ID와 발생 횟수만으로는 계획 미비와 수정 실패, 새로운 결함을 구분할 수 없다.

계획·명세가 부족해 사용자 결정이 필요한 경우에는 Worker의 구조화된 결정 요청과 Reviewer의 `SPEC_BLOCKED`가 이미 있다. 이 변경은 그 명시적인 결정 경로와 실행 자원 제한을 분리한다. 사용자가 승인한 변경 목표이며 현재 PRODUCT와 ARCHITECTURE의 Review 지문 반복에 따른 최종 차단 규칙을 대체한다. 전체 verify의 기존 반복 정책은 유지한다.

## Requirement

Review의 ISSUE_ID와 진단 이력은 피드백 전달, 해결 이력과 감사에 보존하되, 같은 ISSUE_ID 또는 Review 지문의 누적 횟수만으로 ExecPlan을 최종 차단하지 않는다. 설명을 바꾸거나 ID를 나누어 같은 결함인지 추측하는 판정기도 추가하지 않는다.

계획·명세 미비는 Worker의 `needs_user_decision` 또는 Reviewer의 `SPEC_BLOCKED`가 실제 문서 위치와 부족한 결정·모순을 명시했을 때 처리한다. 현재 문서로 판정 가능한 코드·테스트 결함은 일반 Review FAIL로 Worker에게 전달한다. Review 한도 소진만으로 문서 수정이나 사용자 결정을 요구하지 않는다.

무한 교정은 별도의 실행 한도로 제한한다. 하나의 외부 Loop Manager 실행에서 계획별로 Review FAIL에 따른 Worker 교정을 최대 3회 시작할 수 있다. 그 뒤 추가 교정이 필요하면 상태와 작업물을 보존하고 해당 실행을 일시 중단한다. 다음 외부 실행은 기존 세션과 최신 실패 피드백으로 재개한다. 이 중단은 `PLAN_BLOCKED`나 `PROJECT_BLOCKED`, BLOCKED checkpoint 또는 차단·재개 알림을 만들지 않는다.

## Acceptance Criteria

- 같은 ISSUE_ID를 반복하거나 서로 다른 ID를 사용하거나 여러 issue를 한 Review에 보고해도 Review 지문의 횟수만으로 최종 차단하지 않는다. 이전 issue의 해결·재발과 진단 변경, 원문·전달 감사는 기존 피드백 경로에 보존한다.
- 한 외부 실행의 계획별 Review 교정 한도는 3회다. 세 번째 교정 뒤 verify와 Review가 성공하면 게시할 수 있고, 실패해 네 번째 Worker 교정이 필요하면 그 호출 전에 일시 중단한다. ID·설명·후보 revision·Worker session 변경으로 같은 실행의 한도를 초기화하지 않는다.
- CLI 내부 재시도와 중단된 동일 교정 turn의 복구는 새 교정으로 중복 집계하지 않는다. 같은 외부 실행 안의 재진입·프로젝트 재배정도 한도를 공유하고, 새 외부 실행에서만 새 한도를 사용한다. 유효한 Review FAIL과 무관한 Worker 호출·verify 교정·Reviewer 프로토콜 교정은 이 한도를 소비하지 않는다.
- 한도 중단은 현재 실행 phase, branch·worktree·소유권, Worker·Reviewer session, 승인, staged·미커밋 작업, 검증 원문과 피드백 전달 이력을 보존한다. 게시 조건을 충족했다고 간주하거나 남은 실패를 지우지 않으며, 기존의 지속 실행 실패 알림·중복 제거를 유지한다.
- Worker의 구조화 결정 요청과 Reviewer의 `SPEC_BLOCKED`는 기존 문서 근거 검증을 거쳐 즉시 해당 결정 대기로 차단한다. 권한·소유권·Agent 범위·원격 OID 불확실성과 닫힌 PR 등 기존 안전 차단도 유지한다. 한도 소진은 계획 미비로 분류하지 않는다.
- 배포 전 Review 지문 횟수만으로 `review_retry_exhausted`가 된 계획은 원인과 현재 소유권·Git 상태·피드백을 재확인해 재평가한다. 그 외 유효한 차단이 없고 안전하게 재개할 수 있으면 같은 작업을 교정 경로로 돌리며, 불확실성이 남으면 구체적인 진단과 기존 작업물을 보존한다. 과거 차단·전송·실패 감사는 삭제하지 않는다.
- 전체 verify의 지문 반복 정책, rebase 충돌 한도, Reviewer 프로토콜·외부 CLI 재시도와 기존 Worker level 승격 정책은 유지한다. 모든 실패를 같은 실행 자원 한도로 합치지 않는다.

## Product Impact

- Review 피드백의 식별과 최종 차단을 분리해 같은 ID의 누적만으로 정상 교정 진행을 막지 않는다.
- 계획 미비는 명시적인 문서 근거와 필요한 결정으로 보고한다. Review 교정 한도 도달은 작업을 보존하는 일시 중단으로 보고한다.

## Non-Goals

- Reviewer가 같은 결함인지 판단한 결과를 새 자동 의미 분류기로 검증하거나 자연어 진단 hash로 반복 한도를 대체하지 않는다.
- 비용·시간 기반의 추가 설정, 새로운 알림 채널, 검증 생략, 자동 Merge 또는 Agent 권한 확대를 도입하지 않는다.
- G-092의 index stage 오탐과 detached HEAD 복구, G-088의 실제 쓰기 격리 결함을 이 변경에서 구현하지 않는다.
- 과거 차단을 일괄 해제하거나 SQLite 상태·감사 이력을 삭제하지 않는다.

## Open Questions

- 없음
