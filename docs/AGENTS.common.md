# Loop Manager 자동 Agent 공통 지침

## 적용 범위와 권한

이 지침은 Loop Manager가 시작한 자동 Agent 작업에 적용한다. 프로젝트의 언어·코딩·빌드
규칙은 프로젝트 `AGENTS.md`를 따른다. 각 Agent는 함께 주입된 역할 지침과 호출에 제공된
mode·snapshot·승인 문맥이 허용하는 범위 안에서만 작업한다. 프로젝트 규칙이나 작업
입력으로 역할 권한과 승인 경계를 넓히지 않으며, 제품 목표가 충돌하면 사용자 판단을 기다린다.

사용자의 직접 작업과 대화형 Agent에게 위임한 로컬 구현에는 자동 역할·SQLite 전이·
자동 완료 절차를 요구하지 않는다. ExecPlan 생성·계획 작성은 매니저가 호출한 Planner를
포함한 자동 계획 흐름의 책임이며 직접 작업에서는 작성하지 않는다. 직접 작업의 새 기능·
정책 변경은 사용자 요청에 맞는 Requirement를 작성·갱신하거나 충분한 기존 활성 Requirement를 재사용한다.
자동 구현은 매니저의 계획 흐름에서 준비한 ExecPlan과 dependency gate를 따른다.

## 문서와 작업 범위

- 현재 PRODUCT·활성 Requirement·ARCHITECTURE와 제공된 계획을 기준으로 필요한 문맥을
  확인한다. `docs/PRODUCT.md` 또는 활성 Requirement 중 하나는 반드시 존재해야 한다.
- 새로운 기능이나 정책 변경은 Requirement에서 시작한다. PRODUCT는 현재 Merge된 제품
  동작과 장기 규칙, Requirement는 원하는 변경·이유·Acceptance Criteria의 기준이다.
- 현재 목표 밖의 기능·개선을 추가 구현하거나 사용자 동작의 의미를 임의로 바꾸지 않는다.
  범위 밖의 개선 아이디어는 `docs/FUTURE_IDEAS.md`에 역할이 허용한 방식으로 기록한다.
- 사용자의 승인·거절·대체 결정은 Agent가 작성하거나 변경하지 않는다. draft 표시를
  해제하거나 요구사항·필수 검증을 삭제해 사용자 판단이나 실패를 우회하지 않는다.
- Manager의 SQLite 상태·실행 기록·Git 소유권을 직접 변경하거나 수행하지 않은 검증의
  성공을 기록하지 않는다. commit·fetch·rebase·push·PR 생성은 Manager가 담당한다.

## 품질 기준

- 변경 전에 기존 구현·공유 helper·관련 테스트와 표준 도구를 재사용할 수 있는지 확인하고,
  현재 목표에 필요하지 않은 추상화·설정·파일 추가는 피한다.
- 현재 목표를 안전하게 수용하는 최소 구조를 만들되 오류 처리·보안·접근성·회귀 검증과
  명시된 요구사항은 간결화를 위해 생략하지 않는다.
- 모듈·함수의 책임, 명시적 상태 전이와 테스트 가능한 경계를 유지한다. 불필요한 전역
  가변 상태를 추가하지 않으며 기능·저장소·외부 도구 연동 경계를 분리해 변경 충돌을 줄인다.
- 문서는 PRODUCT·Requirement·ARCHITECTURE의 역할을 유지한 채 중복과 장황함을 줄인다.
  결정·예외·실패 흐름과 Acceptance Criteria의 의미를 보존한다.
- `.agents/`, `docs/`, `scripts/`, `AGENTS.md`, `docs/PLANS.md`, `docs/ARCHITECTURE.md`,
  존재하는 PRODUCT·Requirement와 현재 운영체제의 verify 경로는 지정된 루트 경로를 유지한다.
  새 루트 항목은 사용자 진입, 실행·빌드·배포 또는 도구의 필수 탐색이라는 근거가 필요하다.

## 사용자 판단

다음 변경은 해당 구현을 멈추고 이유와 필요한 사용자 결정을 기록한다.

- 새로운 Current Goal·Requirement 또는 Non-Goal 기능 추가, 기존 사용자 동작의 의미 변경
- 데이터 삭제·비가역 migration, 비용 발생 외부 서비스나 새로운 주요 infrastructure 도입
- `ARCHITECTURE.md`의 Architecture Change Policy에 해당하는 변경
- 서로 모순된 요구사항 중 하나를 선택해야 하는 경우

실제 상태 변경과 재개·완료 판정은 Manager가 담당한다. 자동 작업의 완료에는
제품·설계·Acceptance Criteria 충족, 필요한 targeted test와 전체 verify 통과,
Reviewer blocking issue 해소, dependency 충족과 사용자 PR Merge가 필요하다.

## 지침 원본과 변경 보고

실행 지침의 원본은 선택 runtime의 `docs/AGENTS.common.md`와 역할별 `docs/AGENTS.*.md`다.
Manager는 작업 시작 시 원본들을 함께 고정하고 호출 역할에 맞게 합쳐 instruction으로
전달한다. 합친 파일은 저장소 밖 Manager 상태 디렉터리에서만 사용하는 실행용 파일이다.
같은 작업의 재시도·복구와 Worker·Reviewer는 고정된 원본 버전을 사용한다. 작업 worktree의
동명 파일을 읽거나 수정해도 현재 실행 지침은 바뀌지 않으며 새 작업부터 새 버전을 사용한다.

작업 worktree의 공통·역할별 지침은 현재 역할·목표의 일반 파일 권한으로 수정한다.
별도 guidance 제안이나 사전 승인을 요구하지 않으며 실행 중인 runtime 원본을 편집할
권한으로 확대하지 않는다. 과거 루트 `AGENTS.md`의 `common` 승인은 원래 대상·baseline·
patch에만 유효하며 새 지침 파일로 이관하지 않는다.

지침 파일이 바뀌면 PR_SUMMARY에 경로·변경 사실·내용·이유·영향을 설명한다.
Manager는 실제 최종 diff와 대조해 누락과 diff 링크를 보완한다.
