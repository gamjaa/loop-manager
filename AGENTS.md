# Loop Manager 개발 지침

문서 작성·검토 기준은 [Specification Skill](.agents/skills/specification-docs/SKILL.md)을 따른다.

## 저장소 구조

- `.agents/`, `docs/`, `scripts/`, `AGENTS.md`, `docs/PLANS.md`, `docs/ARCHITECTURE.md`,
  존재하는 PRODUCT·Requirement와 현재 운영체제의 verify 경로는 지정된 루트 경로를 유지한다.
- 새 루트 항목은 사용자 진입, 실행·빌드·배포 또는 도구의 필수 탐색이라는 근거가 필요하다.
  그 근거가 없는 새 루트 항목은 Review에서 blocking issue로 보고한다.

## 실행 지침 원본 수정

이 저장소의 공통·역할별 `docs/AGENTS.*.md` 원본 파일은 현재 역할·목표의 일반 문서 권한과
PR 보고 규칙에 따라 수정한다. 프로젝트 루트 `AGENTS.md` 변경의 사전 승인 대상에 포함하지
않는다. 파일을 수정해도 현재 작업에 고정된 Loop Manager instruction과 실행 권한은 바뀌지
않는다. 공통·역할별 원본에는 등록 프로젝트 전반에 적용되는 자동 실행 규칙만 둔다.

## 검증

변경한 동작을 확인하는 최소 범위의 unit test를 실행한다. 문서만 변경했거나 실행 가능한
관련 테스트가 없으면 그 사유를 결과에 명시한다.
