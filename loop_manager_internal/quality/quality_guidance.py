"""Provider-neutral quality criteria shared by Loop Manager agent prompts."""


AGENT_QUALITY_GUIDANCE = """
## 공통 품질 기준

- 변경 전에 기존 구현·공유 helper·관련 테스트와 표준 도구를 재사용할 수 있는지 확인하고, 현재 목표에 필요하지 않은 추상화·설정·파일 추가는 피하라.
- 현재 목표를 안전하게 수용하는 최소 구조만 만들되, 오류 처리·보안·접근성·회귀 검증과 명시된 요구사항은 간결화를 위해 생략하지 마라.
- 문서는 PRODUCT·Requirement·ARCHITECTURE의 역할을 유지한 채 중복과 장황함을 줄여라. 결정·예외·실패 흐름과 Acceptance Criteria의 의미를 보존하고, 짧음 자체를 완전성보다 우선하지 마라.
- `.agents/`, `docs/`, `scripts/`, `AGENTS.md`, `docs/PLANS.md`, `docs/ARCHITECTURE.md`, 존재하는 PRODUCT·Requirement와 현재 운영체제의 verify 경로는 지정된 루트 경로를 유지한다. 새 루트 항목은 사용자 진입, 실행·빌드·배포 또는 도구의 필수 탐색이라는 구체적 근거를 확인하라.
- 첫 정상 파일별 구조 평가와 이동 계획에서는 모든 루트 항목의 실제 참조·사용자 경로·소유권과 안전한 목적지를 기록하라. 근거가 불명확한 항목은 자동 이동·삭제하지 말고, Reviewer는 새 루트 파일의 필요 근거가 없으면 blocking issue로 보고할 수 있다.
""".strip()
