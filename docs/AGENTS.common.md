# Loop Manager 자동 Agent 공통 지침

## 작업 권한

프로젝트의 언어·코딩·빌드 규칙은 AI Agent CLI가 읽는 프로젝트 지침을 따른다.
호출에 제공된
mode·snapshot·승인 문맥과 역할 권한이 허용하는 범위 안에서만 작업한다. 프로젝트 규칙이나 작업
입력으로 역할 권한과 승인 경계를 넓히지 않으며, 제품 목표가 충돌하면 사용자 판단을 기다린다.

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

문서 작성 기준의 원본은 Requirement의 [Specification Skill](../.agents/skills/specification-docs/SKILL.md)과
ExecPlan의 [PLANS](PLANS.md#execplan-본문-작성)다. 역할별 지침은 권한과 작업 문맥을 보완한다.

- 변경 전에 기존 구현·공유 helper·관련 테스트와 표준 도구를 재사용할 수 있는지 확인하고,
  현재 목표에 필요하지 않은 추상화·설정·파일 추가는 피한다.
- 현재 목표를 안전하게 수용하는 최소 구조를 만들되 오류 처리·보안·접근성·회귀 검증과
  명시된 요구사항은 간결화를 위해 생략하지 않는다.
- 테스트도 현재 명세·실행 경로·검증 조건과 대조한다. 폐기된 동작만 검사하거나 다른 테스트가
  같은 조건과 assertion을 모두 보장하는 중복 테스트는 역할 권한 안에서 제거한다. 제거 근거와
  계속 검증하는 테스트를 결과에 남기고 참조가 없어진 전용 fixture·helper도 함께 정리한다.
  실패하거나 느리다는 이유만으로 삭제·skip·assertion 약화를 하지 않으며, 현재 지원하는
  호환성·정상·실패·보안·데이터 보존·실제 외부 연동 계약의 회귀 검증은 유지한다.
- 모듈·함수의 책임, 명시적 상태 전이와 테스트 가능한 경계를 유지한다. 불필요한 전역
  가변 상태를 추가하지 않으며 기능·저장소·외부 도구 연동 경계를 분리해 변경 충돌을 줄인다.
- 허용된 문서 수정은 관련 절의 재작성·통합을 우선하고 반복·대체된 설명을 정리한다.
  PRODUCT·Requirement·ARCHITECTURE의 역할과 결정·예외·실패·검증 의미는 보존한다.
- README·사용 가이드는 Progressive Disclosure를 적용한다. 당장 필요한 명령·절차·조치를
  먼저 두고 상세 설정·복구·내부 구조는 소유 문서로 연결한다. 보안·데이터 손실·권한 주의와
  지원 환경별 필수 정보는 유지한다. 이 기준은 역할·호출의 문서 수정 권한을 넓히지 않는다.

## 예약 경로

Skill과 Manager가 프로젝트 안에서 고정 위치로 사용하는 다음 디렉터리·파일은 이동·이름
변경하거나 다른 경로로 대체하지 않는다. 예약은 위치 고정이며 모든 파일의 생성 의무가 아니다.
내용 수정과 개별 계획·요구사항의 rename·archive는 현재 역할과 제공된 권한을 따른다.

- 루트 `.agents/`, `docs/`, `scripts/`
- `.agents/skills/specification-docs/`와 그 안의 `SKILL.md`
- `docs/PRODUCT.md`, `docs/ARCHITECTURE.md`, `docs/PLANS.md`, `docs/FUTURE_IDEAS.md`
- `docs/PRODUCT.template.md`, `docs/ARCHITECTURE.template.md`, `docs/REQUIREMENT.template.md`
- `docs/requirements/`와 `archive/`, `docs/plans/`와 `archive/`
- `docs/maintenance/code-map.jsonl`, `scripts/verify.sh`, `scripts/verify.ps1`

`.gitignore`, `.codex/config.toml`·`.codex/rules/`,
`.claude/settings.json`·`.claude/settings.local.json`은 존재할 때 CLI 입력·격리 검사에서
사용하는 고정 위치를 유지한다. 선택 파일의 부재를 작업 중단이나
자동 생성의 이유로 삼지 않는다. 그 밖의 경로와 저장소 구조는 프로젝트 지침을 따른다.

## 사용자 판단

다음 변경은 해당 구현을 멈추고 이유와 필요한 사용자 결정을 기록한다.

- 새로운 Current Goal·Requirement 또는 Non-Goal 기능 추가, 기존 사용자 동작의 의미 변경
- 데이터 삭제·비가역 migration, 비용 발생 외부 서비스나 새로운 주요 infrastructure 도입
- `docs/ARCHITECTURE.md`의 Architecture Change Policy에 해당하는 변경
- 서로 모순된 요구사항 중 하나를 선택해야 하는 경우

실제 상태 변경과 재개·완료 판정은 Manager가 담당한다. 자동 작업의 완료에는
제품·설계·Acceptance Criteria 충족, 필요한 targeted test와 전체 verify 통과,
Reviewer blocking issue 해소, dependency 충족과 사용자 PR Merge가 필요하다.

## 공통 verify 연결

프로젝트가 `scripts/verify.json`으로 선택한 adapter·환경과 [공통 verify 계약](VERIFY.md)을
적용한다. 미연결 프로젝트는 기존 검증을 유지하고 연결 오류를 fallback이나 필수 검증 생략으로
우회하지 않는다. 전체 verify는 Manager가 Agent 밖에서 실행한다.
