# Loop Manager 개발 지침

문서 작성·검토 기준은 [Specification Skill](.agents/skills/specification-docs/SKILL.md)을 따른다.

## 용어

- 계획: Requirement
- 작업: ExecPlan
- 설계: PRODUCT, ARCHITECTURE
- 베이스/기본/원본 브랜치: checkout 받은 브랜치
- 작업 브랜치: G 작업 등을 수행하기 위해 분리한 브랜치

## 저장소 구조

- 새 루트 항목은 사용자 진입, 실행·빌드·배포 또는 도구의 필수 탐색이라는 근거가 필요하다.
  그 근거가 없는 새 루트 항목은 Review에서 blocking issue로 보고한다.

## 작업 중 실행

- 작업 중에는 `loop_manager.py run`을 직접 실행하지 않는다.

## 실행 지침 원본 수정

이 저장소의 공통·역할별 `docs/AGENTS.*.md` 원본 파일은 현재 역할·목표의 일반 문서 권한과
PR 보고 규칙에 따라 수정한다. 파일을 수정해도 현재 작업에 고정된 Loop Manager instruction과
실행 권한은 바뀌지 않는다. 공통·역할별 원본에는 등록 프로젝트 전반에 적용되는 자동 실행
규칙만 둔다.

## 검증

변경한 동작을 확인하는 최소 범위의 unit test를 실행한다. 문서만 변경했거나 실행 가능한
관련 테스트가 없으면 그 사유를 결과에 명시한다.

자체 테스트를 추가·변경할 때는 [실행 경계와 시나리오 분리 기준](docs/ARCHITECTURE.md#자체-테스트의-git-경계)을 따른다.
실제 Git·CLI 실행에는 확인할 효과와 assertion의 근거가 있어야 하며, 조건 조합이나 확인 이후의 교정·게시를 불필요하게 반복하지 않는다.
