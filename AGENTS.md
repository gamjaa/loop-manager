# Project instructions

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
- 완료된 Requirement는 마지막 관련 ExecPlan의 구현 PR에서 Loop Manager가 eligibility를 다시 확인한 뒤 내용 변경 없이 `docs/requirements/archive/`로 자동 이동한다. archive PR이 Merge되기 전에는 활성 Planner 입력으로 남으며, 관련 비종료 ExecPlan, archive 충돌 또는 source 관계 불확실성이 있으면 이동하지 않는다.
- `docs/REQUIREMENT.template.md`와 `docs/requirements/archive/`는 작업 입력으로 읽지 않는다.

시스템 설계:
- `docs/ARCHITECTURE.md`

Loop Manager가 수행하는 모든 구현 작업은 크기와 관계없이 `docs/plans` 하위에 ExecPlan을 작성한 뒤 진행한다.
ExecPlan 작성 규칙:
- `docs/PLANS.md`

ExecPlan의 실행 상태, 재시도 횟수, 세션과 작업 공간 정보는 Loop Manager의 SQLite가 관리하며 ExecPlan frontmatter에 기록하지 않는다. `depends_on`에는 먼저 Merge돼야 하는 ExecPlan ID를 기록하며, 모든 선행 상태가 `MERGED`가 되기 전에는 Worker를 시작하지 않는다.

구현 전 관련 문서를 확인한다.

Planner는 `docs/plans/*.md`만 생성하거나 수정한다.

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
