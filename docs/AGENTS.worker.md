# Loop Manager Worker 지침

## 구현 범위

- 선택된 ExecPlan 하나를 구현하고 현재 변경을 안전하게 수용하는 데 필요한 리팩토링을
  함께 수행한다. 기존 동작을 보존하는 테스트를 포함하고 범위 밖 개선으로 확대하지 않는다.
- 새 ExecPlan을 만들거나 다른 계획의 목표를 추가 구현하지 않는다. 연관 문서 수정은
  호출에서 제공된 권한 안에서 수행하고 목표·실행 이력·dependency·사용자 결정을 보존한다.
- 현재 작업에 필요한 PRODUCT·ARCHITECTURE·활성 Requirement·기존 ExecPlan·공통 및 역할별
  지침·사용 안내를 함께 갱신할 수 있다. 새 목표·Non-Goal·사용자 동작 의미 변경은 사용자 판단을
  기다린다. 문서 수정으로 원래 요청·Acceptance Criteria·필수 검증·실패를 약화하거나 사용자
  결정을 대신 작성·변경하지 않는다. 다른 계획의 ID·실행 이력·dependency·사용자 결정은 보존한다.
- Requirement·ExecPlan 구현으로 바뀌는 제품 동작·장기 규칙은 PRODUCT에,
  설계·책임 경계·계약은 ARCHITECTURE에 코드와 같은 PR에서 반영한다. 필요한 관련 절만
  갱신하고 목표·필수 검증·사용자 결정을 보존한다.
- Requirement의 완료 판단·archive 이동은 Manager의 책임이며 Worker가 수행하지 않는다.
  `docs/PLANS.md` 수정은 게시된 ExecPlan이 해당 변경을 명시한 경우에만 허용한다.

## 검증

- 수정한 코드와 새로 작성하거나 변경한 테스트를 검증하는 최소 범위의 targeted unit test만
  실행한다. 개별 test case·class·module 또는 이에 준하는 좁은 단위를 사용한다.
- 재시도는 미해결 검증 실패와 그 원인에 관련된 변경을 우선한다. 성공한 작업을 불필요하게
  반복하거나 새 기능으로 확대하지 않되 필요한 리팩토링·불필요한 코드 제거는 허용한다.
  targeted test 실패는 같은 세션에서 수정하고 동일 테스트로 재검증한다. 문서·설정 전용 변경이나
  실행 가능한 관련 unit test가 없으면 범위를 확대하지 않고 구체적인 사유를 명시한다.
- 실패 명령은 `FAILED_COMMAND:`, 수정 후 동일 명령은 `RERUN:` 줄로 보고하고
  재실행 결과를 그 뒤에 명시한다. 성공·실패가 섞인 요약을 통과로 보고하지 않는다.
- 전체 verify의 실제 입력을 `scripts/verify-inputs.json` version 1 상대 경로/glob 목록으로
  스크립트와 함께 유지한다. 코드·fixture·의존성·검증 설정 변경과 실제로 읽는 문서·`.agents/`를
  반영하고 저장소 밖·동적 입력을 확인할 수 없으면 재사용 가능하다고 주장하지 않는다.
- 프로젝트 도구와 현재 실행 환경에 맞는 전체 검증 스크립트를 생성·유지한다.
  Windows는 `scripts/verify.ps1`, Linux는 `scripts/verify.sh`를 사용하며 필요한 전체
  테스트와 검증을 실행하고 실패 시 0이 아닌 종료 코드를 반환해야 한다.
- 전체 test suite·integration·end-to-end test, 프로젝트 전체 lint·build·type check와
  전체 verify는 직접 실행하지 않는다. Manager가 Agent 밖에서 실행하며 실패 출력만
  같은 Worker 세션에 전달한다.

PR_SUMMARY에는 최종 diff의 문서별 경로·주요 내용·이유·제품 및 설계 영향을 설명한다. 추가·
삭제·rename도 포함하고 rebase 뒤 실제 변경에 맞게 갱신한다. 결과는 완료한 변경·미해결 문제·
다음 필요한 작업을 구분한다. 실제 실행하지 않은 검증을 성공으로 보고하지 않는다.

## 연결된 adapter의 테스트 작성

- 선택 adapter의 수집 범위·지원 버전은 `scripts/verify.json`과 [VERIFY.md](VERIFY.md)를 확인한다.
  새 테스트는 기존 assertion·fixture·native 결과 의미를 유지하며 수집 누락을 skip으로 대체하지 않는다.
- 독립 실행 단위의 Git·DB·임시 경로·포트를 격리한다. 공유 fixture·순서 의존은 해당 작업 범위에서
  격리하거나 직렬 단계로 선언하며, 테스트마다 자원 배정·예약·시간 기록을 구현하지 않는다.
- 실제 Agent CLI/API는 mock·stub·고정 출력으로 검증한다. 실패·부분 결과·fixture·중단 위치의
  진단과 종료 불명 예약을 보존하며 필요한 실제 Git·DB·OS 계약 검증을 제거하지 않는다.
