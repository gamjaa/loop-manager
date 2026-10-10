---
id: G-230
title: Requirement 원본 식별과 Skill ID 조회
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-071] Remaining Manager-Owned Deterministic Work.md
depends_on: []
complexity: medium
risk: low
priority: high
---

## Purpose

등록·DB·Git·모델 없이 신규 Requirement 번호를 조회하는 배포 가능한 명령과 일관된 원본 식별 규칙을 제공한다. 로컬 번호 조회는 후보만 반환하며 문서 교정이나 예약을 수행하지 않는다.

## Acceptance Criteria

- 프로젝트 루트의 `python .agents/skills/specification-docs/scripts/next_requirement_id.py`가 활성·archive·미추적 문서의 원본 ID 최대값 다음 ID 한 줄만 반환한다. 빈 집합은 R-001, archive 부재·번호 공백·기존 중복은 허용한다.
- 유효한 frontmatter의 id/title을 우선하며 없는 기존 Requirement는 첫 제목을 사용한다. 파일명·첫 제목 R-012와 frontmatter R-100이면 R-101을 반환하고 파일은 수정하지 않는다. 잘못된 frontmatter·필수 식별 정보·인코딩·접근 실패와 활성 디렉터리 누락은 ID 없이 오류·실패 종료 코드다.
- Skill은 새 문서를 만들기 직전에 명령 결과를 사용하며 모델이 번호를 계산하지 않는다. 원본 규칙과 템플릿·기존 문서 취급이 일치하고 기존 Requirement에 frontmatter를 일괄 추가하지 않는다.
- 두 copy-project-files 스크립트로 독립 프로젝트에 명령과 필요한 파일이 배포된다. 기존 대상 덮어쓰기 보호를 유지하고 Manager 내부 모듈·설치·원격 접속에 의존하지 않는다. 조회 전후 문서·Git·상태가 같으며 번호를 예약하지 않는다.

## Implementation

기존 제한된 frontmatter parser·파일 관측과 복사 경계를 먼저 확인한다. Skill 내부의 배포 가능한 최소 식별 로직을 Manager의 후속 번호 검사에서도 재사용할 수 있게 하되 파서 사본·범용 문서 프레임워크를 만들지 않는다. Skill·템플릿은 사용자 작성 규칙과 조회 명령만 안내한다. PRODUCT·ARCHITECTURE의 해당 식별·배포 책임을 구현과 같은 PR에서 반영한다.

## Validation

- 파일 fixture의 targeted unit test로 빈 집합·archive 없음·미추적·공백·중복·frontmatter 우선·본문 fallback·손상·접근 실패를 확인한다. 성공값·오류·stdout 한 줄과 파일 불변을 검사하고 Git·DB·Agent 경계는 호출 시 실패하게 둔다.
- 전체 verify는 실제 복사 스크립트의 배포·바이트·충돌 보호와 미등록 프로젝트 실행 연결을 확인한다. 테스트는 고정 샘플을 사용하며 Skill 정책 원문 문구를 검사하지 않는다. 명령의 실행 가능한 원본은 실제 검증 입력에 포함한다.

## Progress

- [x] eb797d3a의 R-071·Skill·복사 스크립트를 정적으로 확인했다. 조회 scripts 디렉터리와 명령·조회 지침은 없다. 구현·테스트 실행 없음.
- [ ] 식별·조회·배포 구현과 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- complexity medium: frontmatter/기존 제목의 식별·오류와 독립 배포를 연결하지만 동작 경계는 명확하다. risk low: 읽기 전용 후보 조회이며 잘못된 결과는 수정·재조회할 수 있다. 후속 자동 편집은 별도 계획이다.
- 현재 Skill은 frontmatter 없는 작성 기준을 유지한다. 기존 frontmatter 문서의 읽기 호환과 원본 기준을 지원하며 조회만으로 불일치를 교정하지 않는다. catalog에 이 기능의 비종료 계획은 없다.
