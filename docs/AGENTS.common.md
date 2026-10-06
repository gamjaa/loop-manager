# Loop Manager 자동 실행 공통 지침

최초 자동 규칙은 구현 기준 `5dd89b7127abec7f08f10fc373f3e4c29dbef7ee`의 루트
`AGENTS.md`에서 생성했다. 프로젝트별 본문 복사를 없애고 자동 역할·승인·검증 규칙을
작업별 호출 버전으로 보존하기 위해 분리했으며 호출마다 다시 생성하지 않는다.

이 지침은 Loop Manager가 시작한 자동 Agent 작업에만 적용한다. 프로젝트의
언어·코딩·빌드 규칙은 프로젝트 `AGENTS.md`를 따른다. 프로젝트 규칙으로 자동
작업의 권한이나 승인 경계를 넓히지 않는다. 제품 목표의 충돌은 사용자 판단을 기다린다.

사용자의 직접 작업과 대화형 Agent에게 위임한 로컬 구현에는 자동 Worker 역할,
SQLite 전이, ExecPlan 선작성이나 아래의 자동 완료 절차를 요구하지 않는다.
새 기능·정책 변경은 직접 작업도 Requirement에서 시작하며 관련 명세를 확인한다.
자동 구현은 크기와 관계없이 Planner가 준비한 ExecPlan과 dependency gate를 따른다.
직접 구현과 활성 Requirement가 원격 base에 반영되면 실제 미충족 작업만 다시
계획한다. 충족된 구현의 중복 계획·소급 ExecPlan·자동 검증 성공 합성을 하지 않는다.
로컬 전용 입력 제외와 기존 Requirement archive·동일 입력 반복 억제는 유지한다.

## 문서와 자동 작업 역할

프로젝트 목적과 요구사항:
- `docs/PRODUCT.md`
- `docs/requirements/*.md`

존재하는 문서를 기준으로 작업한다. `docs/PRODUCT.md`와 `docs/requirements/*.md` 중 하나는 반드시 존재해야 한다.

문서 역할:
- `PRODUCT.md`는 현재 Merge된 제품 동작과 장기 규칙의 기준이다.
- `docs/requirements/*.md`는 원하는 변경, 이유와 Acceptance Criteria를 기록한다.
- 새로운 기능이나 정책 변경은 Requirement에서 시작한다.
- `PRODUCT.md`, `docs/requirements/*.md`, `docs/ARCHITECTURE.md`는 사용자가 작성하며 AI는 기본적으로 수정하지 않는다.
- 사용자가 Specification Skill을 이용한 문서 작성을 명시적으로 요청한 경우에는 해당 문서를 함께 작성하거나 수정할 수 있다.
- Requirement 구현 결과가 앞으로도 지켜야 할 제품 규칙이면 예외적으로 코드와 같은 PR에서 `PRODUCT.md`를 갱신할 수 있다.
- Requirement는 frontmatter나 상태가 없는 순수 문서다.
- `docs/requirements/` 바로 아래의 모든 Requirement는 Planner 입력으로 사용한다.
- `docs/REQUIREMENT.template.md`와 `docs/requirements/archive/`는 작업 입력으로 읽지 않는다.

시스템 설계:
- `docs/ARCHITECTURE.md`

ExecPlan 작성 규칙:
- `docs/PLANS.md`

ExecPlan의 실행 상태, 재시도 횟수, 세션과 작업 공간 정보는 Loop Manager의 SQLite가 관리하며 ExecPlan frontmatter에 기록하지 않는다. `depends_on`에는 먼저 Merge돼야 하는 ExecPlan ID를 기록하며, 모든 선행 상태가 `MERGED`가 되기 전에는 Worker를 시작하지 않는다.

구현 전 관련 문서를 확인한다.

일반 Planner는 새 `docs/plans/*.md`를 생성하고, 현재 PRODUCT/Requirement 범위 밖이지만 보존 가치가 있는 아이디어만 `docs/FUTURE_IDEAS.md`에 생성하거나 수정할 수 있다.
예외적으로 프로그램이 SQLite 상태와 Git revision에 결합된 mutable READY snapshot을 제공한 경우에만, 일반 Planner는 snapshot 안의 미착수 READY ExecPlan을 같은 ID로 수정·이름 변경·삭제할 수 있다.
일반 Planner는 snapshot 밖의 기존 ExecPlan, 실행된 계획, MERGED·archive 계획 및 앞에서 허용한 파일 외의 파일을 수정·이름 변경·삭제할 수 없다.
예외적으로 프로그램이 SQLite의 활성 유지보수 단계에서 명시한 barrier 또는 post-refactor revalidation mode와 대상 snapshot을 제공한 경우에만, 유지보수 Planner는 상태가 READY이고 branch, worktree, workspace reservation, Worker·Reviewer session과 PR이 없는 기존 ExecPlan을 개정할 수 있다. barrier mode는 기존 metadata·본문·dependency를 보존하고 검증된 리팩터링 DAG의 모든 말단 ID만 depends_on에 추가한다. revalidation mode는 ID·경로·title·sources·Purpose와 원래 제품 목표 및 범위를 보존하면서 최신 구조에 필요한 Acceptance Criteria의 경로 표현, Implementation, Validation, Progress, Decision Log와 dependency만 개정하며 목표 보존을 확정할 수 없는 변경은 거부한다.
유지보수 Planner도 기존 파일의 삭제·이름 변경, 대상 snapshot 밖 계획의 수정 및 AGENTS.md 변경을 할 수 없다. 이 예외는 일반 Planner 권한이나 사용자의 직접 문서 편집 규칙을 변경하지 않는다.
Planner는 `docs/FUTURE_IDEAS.md`의 항목을 같은 계획 주기의 ExecPlan 범위나 현재 미충족 작업의 근거로 사용하지 않는다.
Planner와 Reviewer는 `AGENTS.md` 변경을 제안하거나 적용하지 않는다.
Worker는 현재 ExecPlan에 꼭 필요한 프로젝트 루트 `AGENTS.md` 변경을 발견하면 승인 전에 수정하지 않고 선택된 ExecPlan의 `Decision Log`에 제안과 빈 `User Decision`을 기록한 뒤 사용자 판단을 기다린다. 신규 제안의 Target은 `AGENTS.md`, Scope는 self 저장소도 `project`다. 저장된 과거 `common` 결정은 원래 루트 대상·baseline·patch에만 유효하며 새 파일로 이관하지 않는다.

Worker는 구현, 관련 targeted unit test 작성·실행과 전체 검증 스크립트 생성을 담당한다.

Worker는 자신이 수정한 코드와 새로 작성하거나 변경한 테스트를 직접 검증하는 최소 범위의 unit test만 실행한다. targeted unit test가 실패하면 같은 세션에서 원인을 수정하고 해당 테스트를 다시 실행한다. 문서·설정만 변경했거나 실행 가능한 관련 unit test가 없으면 그 사유를 명시한다.

Worker는 프로젝트가 사용하는 언어, 프레임워크와 도구에 맞춰 현재 실행 환경의 전체 검증 스크립트를 생성하고 유지한다.
- Windows: `scripts/verify.ps1`
- Linux: `scripts/verify.sh`

전체 검증 스크립트는 프로젝트에 필요한 전체 테스트와 검증을 실행하고, 실패 시 0이 아닌 종료 코드를 반환해야 한다.

Worker는 전체 test suite, integration test, end-to-end test, 프로젝트 전체 lint·build·type check와 전체 검증 스크립트를 직접 실행하지 않는다. Loop Manager 코드가 전체 검증 스크립트를 AI Agent 밖에서 실행하고, 실패한 경우에만 출력 내용을 같은 Worker 세션에 전달한다.

Reviewer는 파일을 수정하지 않고 읽기 전용으로 검토한다. Worker의 targeted unit test와 Loop Manager의 전체 verify가 통과한 결과를 넘겨받으며, 테스트 command나 전체 검증 스크립트를 다시 실행하지 않는다. 테스트 누락과 검증 약화는 코드, 테스트, verify 스크립트의 diff와 전달된 결과를 기준으로 판단한다.

## Code Quality and Change Structure

- Worker는 Current Goals를 구현하면서 현재 변경을 안전하게 수용하는 데 필요한 리팩토링을 함께 수행한다. 이를 별도 개선 작업으로 미루지 않는다.
- 코드는 책임이 분명한 모듈과 함수, 명시적인 상태 전이와 테스트 가능한 경계를 유지해야 한다. 하나의 파일이나 함수에 새로운 책임을 계속 누적하거나 불필요한 전역 가변 상태를 추가하지 않는다.
- 서로 다른 ExecPlan이나 작업자가 독립된 변경을 준비할 때 같은 핵심 파일을 반복해서 수정하지 않도록 기능 경계, 저장소 계층과 외부 도구 연동 경계를 분리해 충돌 범위를 줄인다.
- 리팩토링은 현재 Goal 또는 Requirement에 필요한 범위로 제한하고 기존 동작을 보존하는 테스트를 포함한다. 범위 밖의 대규모 구조 변경은 구현하지 않고 `docs/FUTURE_IDEAS.md`에 기록한다.
- Reviewer는 변경 후 코드가 읽기, 테스트, 오류 복구와 후속 변경에 적합한 구조인지 검토하며, 명백한 책임 혼합이나 과도한 결합이 남으면 blocking issue로 보고한다.

AI는 Current Goals를 달성하는 데 필요한:
- 구현
- 테스트
- 오류 처리
- 보안 수정
- 명백한 코드 결함 수정
을 자체적으로 계획할 수 있다.

명시된 Current Goals 밖의 개선 아이디어를 발견하면 구현하지 말고 `docs/FUTURE_IDEAS.md`에 기록한다.

## Completion Criteria

다음을 모두 만족해야 관련 ExecPlan을 완료한 것으로 본다.

- PRODUCT.md 또는 Requirement, ARCHITECTURE.md와 ExecPlan의 Acceptance Criteria를 만족한다.
- 관련 targeted test와 프로젝트가 정의한 전체 검증이 통과한다.
- Reviewer의 blocking issue가 없다.
- 모든 `depends_on` ExecPlan이 `MERGED` 상태다.
- 연결된 PR이 사용자에 의해 Merge됐다.

모든 관련 ExecPlan이 완료돼야 해당 Current Goal 또는 Requirement를 완료한 것으로 본다.

## Human Approval Required

다음 변경은 구현 전에 사용자 승인이 필요하다.

- 새로운 Current Goal 또는 Requirement 추가
- Non-Goal에 포함된 기능 추가
- 기존 사용자 동작의 의미 변경
- 데이터 삭제 또는 비가역적 migration
- 결제 또는 비용이 발생하는 외부 서비스 도입
- 새로운 주요 infrastructure 도입
- ARCHITECTURE.md의 Architecture Change Policy에 해당하는 변경
- 요구사항이 서로 모순되어 하나를 선택해야 하는 경우

이 경우 SQLite의 ExecPlan 상태를 `BLOCKED`로 변경하고 해당 구현을 중단한다. 사용자가 ExecPlan 또는 그 근거가 된 PRODUCT/Requirement를 수정하면 Loop Manager가 변경을 감지해 작업을 재개한다.

## 공통 지침 원본과 변경 보고

호출에 주입된 지침은 선택 runtime의 `docs/AGENTS.common.md`에서 확정한 작업별
snapshot이다. Worker·Reviewer·재시도·새 세션 복구에서도 같은 버전을 사용한다.
작업 worktree의 동명 파일을 읽거나 수정해도 실행 지침이 바뀌지 않는다.
Merge 뒤 새 runtime에서 시작하는 새 작업만 새 공통 버전을 선택한다.

작업 worktree의 `docs/AGENTS.common.md`는 일반 작업 파일이다. 현재 역할과 목표의
일반 파일 권한으로 수정하며 별도 guidance 제안이나 사전 승인을 요구하지 않는다.
실행 중인 runtime 원본을 편집할 권한으로 확대하지 않는다. 공통 파일이 바뀌면
PR_SUMMARY에 경로·변경 사실·내용·이유·영향을 설명한다. Manager는 실제 최종 diff와
대조해 빠진 사실과 최종 diff 링크를 보완하고 Reviewer는 일반 변경과 함께 검토한다.
