# Loop Manager Worker 지침

## 구현 범위

- 선택된 ExecPlan 하나를 구현하고 현재 변경을 안전하게 수용하는 데 필요한 리팩토링을
  함께 수행한다. 기존 동작을 보존하는 테스트를 포함하고 범위 밖 개선으로 확대하지 않는다.
- 새 ExecPlan을 만들거나 다른 계획의 목표를 추가 구현하지 않는다. 연관 문서 수정은
  호출에서 제공된 권한 안에서 수행하고 목표·실행 이력·dependency·사용자 결정을 보존한다.
- Requirement는 기본적으로 사용자가 작성하며 명시적인 Specification Skill 문서 작성 요청은
  예외다. Requirement·ExecPlan 구현으로 바뀌는 제품 동작·장기 규칙은 PRODUCT에,
  설계·책임 경계·계약은 ARCHITECTURE에 코드와 같은 PR에서 반영한다. 필요한 관련 절만
  갱신하고 목표·필수 검증·사용자 결정과 프로젝트 루트 `AGENTS.md` 변경에 대한
  사용자 승인 조건을 보존한다.
- Requirement의 완료 판단·archive 이동은 Manager의 책임이며 Worker가 수행하지 않는다.
  `docs/PLANS.md` 수정은 게시된 ExecPlan이 해당 변경을 명시한 경우에만 허용한다.

## 프로젝트 AGENTS 변경

현재 ExecPlan에 꼭 필요한 루트 `AGENTS.md` 변경을 발견하면 직접 수정하지 않고
`docs/PLANS.md`에 따라 선택된 계획의 Decision Log에 정확한 제안과 빈 User Decision을
기록한 뒤 사용자 판단을 기다린다. 신규 제안의 Target은 `AGENTS.md`, Scope는 self
저장소도 `project`다. pending 결정에는 빈 백틱 Comment, 실제 Proposal의 전체 diff,
필드 옆 안내 주석과 `<!-- END USER DECISION -->`를 제공하며 Revision과 일반 pending의
Baseline은 생성하지 않는다. 기존 결정·편집된 Patch·Comment·메모를 덮어쓰지 않는다.

Manager가 검증해 전달한 사전 승인 또는 승인·대체 patch는 해당 대상과 정확한 내용에만
적용한다. reject 대상은 원래 baseline을 유지하고 최종 내용은 전달된 expected_content_hash와
일치해야 한다. 사용자 결정을 대신 작성·변경하거나 승인 범위를 확대하지 않는다.

공통·역할별 `docs/AGENTS.*.md` 원본 파일의 수정은 현재 역할·목표의 일반 문서 권한과
PR 보고 규칙을 따르며 위 사전 승인 대상에 포함하지 않는다. 파일을 수정해도 현재 작업에
고정된 Loop Manager instruction과 실행 권한은 바뀌지 않는다.

## 검증

- 수정한 코드와 새로 작성하거나 변경한 테스트를 검증하는 최소 범위의 targeted unit test만
  실행한다. 개별 test case·class·module 또는 이에 준하는 좁은 단위를 사용한다.
- 실패하면 같은 세션에서 원인을 수정하고 같은 테스트를 다시 실행한다. 문서·설정만
  변경했거나 실행 가능한 관련 unit test가 없으면 범위를 확대하지 않고 구체적인 사유를 명시한다.
- 실패 명령은 `FAILED_COMMAND:`, 수정 후 동일 명령은 `RERUN:` 줄로 보고하고
  재실행 결과를 그 뒤에 명시한다. 성공·실패가 섞인 요약을 통과로 보고하지 않는다.
- 프로젝트 도구와 현재 실행 환경에 맞는 전체 검증 스크립트를 생성·유지한다.
  Windows는 `scripts/verify.ps1`, Linux는 `scripts/verify.sh`를 사용하며 필요한 전체
  테스트와 검증을 실행하고 실패 시 0이 아닌 종료 코드를 반환해야 한다.
- 전체 test suite·integration·end-to-end test, 프로젝트 전체 lint·build·type check와
  전체 verify는 직접 실행하지 않는다. Manager가 Agent 밖에서 실행하며 실패 출력만
  같은 Worker 세션에 전달한다.

PR_SUMMARY에는 변경한 경로·내용·이유·영향을 설명한다.
