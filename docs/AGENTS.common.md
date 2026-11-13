# Loop Manager Agent 공통 지침

프로젝트의 언어·빌드·파일 배치 규칙은 CLI가 읽는 프로젝트 지침을 따른다. 현재 PRODUCT·ARCHITECTURE·활성 Requirement와 선택된 ExecPlan의 범위에서만 판단한다.

- 실제로 충돌하는 활성 Requirement의 해당 규칙만 높은 R-ID가 우선한다. 충돌하지 않는 규칙·Acceptance Criteria는 보존한다. archive 문서는 과거 근거이며 새 목표로 되살리지 않는다.
- 중요한 미확정 정책·사용자 결정을 임의로 확정하거나 실패·필수 검증을 문서 수정으로 없애지 않는다.
- 사용자 checkout, 다른 G, Provider 인증·설정, Manager DB를 변경하지 않는다. ignored 파일·캐시 생성만으로 권한 위반을 추정하지 않는다.
- Commit·Fetch·Merge·Push·PR 조작은 Manager가 담당한다. 공개 이력을 Rebase·Force push·Reset으로 고치지 않는다.
- 기존 helper·표준 도구를 재사용하고 현재 목표에 필요한 최소 변경을 한다. 오류 처리·사용자 작업물 보존·보안·접근성과 필요한 회귀는 생략하지 않는다.
- 비밀값·개인 환경정보를 결과·PR·알림에 싣지 않는다. 실제 수행한 테스트와 확인하지 못한 결과를 구분한다.
- 결과는 호출에서 지정한 JSON 객체 하나로 반환한다. Agent는 의미 판단을 제안하며 실행 단계·검증·PR·완료 상태는 Manager만 확정한다.
