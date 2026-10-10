---
id: G-232
title: Requirement 중복 선택과 식별 정보 교정
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-071] Remaining Manager-Owned Deterministic Work.md
depends_on:
  - G-231
complexity: medium
risk: high
priority: high
---

## Purpose

현재 원격 문서·기존 작업 참조·기본 branch 반영 순서에서 교정 대상과 다음 번호를 결정하고, 허용된 활성 문서의 ID·제목·파일명·확정 링크를 일관되게 바꾸는 최소 편집을 만든다. 실제 원격 게시는 후속 작업이 담당한다.

## Acceptance Criteria

- 활성·archive 원본 ID의 최대 번호 다음부터 교정 대상의 파일명 텍스트 오름차순으로 번호를 배정한다. 공백·중복 때문에 실패하거나 빈 번호를 채우지 않고 고유 ID를 재번호화하지 않는다.
- draft·미착수·실행·MERGED·archived ExecPlan과 저장 관계의 참조를 모두 확인한다. 나중에 반영됐어도 참조된 문서를 보존하고 참조 확인 실패는 미확인으로 남긴다. 여러 참조 문서·archive끼리 중복은 보존한다.
- 참조 없는 활성 중복은 원격 기본 branch에 먼저 해당 ID로 반영된 문서를 유지한다. 같은 시점·순서 불명확은 locale과 무관한 파일명 텍스트 정렬로 뒤 문서를 바꾼다. archive와 참조 없는 활성의 충돌에서는 archive를 보존한다.
- 원본 제목 변경은 ID를 유지한 파일명·첫 제목 및 있는 frontmatter title 동기화다. 중복 ID 교정은 있는 frontmatter id와 첫 제목·파일명을 모두 갱신한다. 기타 필드·요구사항·완료 조건은 보존하며 일부 표현만 바뀐 결과를 완료로 취급하지 않는다.
- 수정 가능한 문서의 확정된 상대 링크만 갱신한다. 기존 sources·archived 문서·사용자 결정은 수정하지 않는다. 제목 누락·사용 불가 파일명·목적지 충돌·기존 source를 같은 문서로 보존할 수 없는 rename은 해당 변경과 이유를 보류한다. 의미·모호한 본문을 모델로 보충하지 않는다.

## Implementation

G-230의 원본 해석·G-231의 source 관계를 사용해 번호 배정·보호 대상 선택·최소 편집 산출을 Git/게시 효과와 분리한다. 기존 catalog·원격 이력·저장 관계를 입력으로 제공하며 모델 없이 검증 가능한 결과를 만든다. 일반 문서 전체를 재포맷하지 않고 바뀐 식별 표현과 확정 링크만 편집한다. PRODUCT·ARCHITECTURE의 관련 규칙과 책임을 구현에 맞춘다.

## Validation

- 순수 선택과 실제 파일 편집의 targeted unit test로 최대 R-010→R-011/R-012, 반영 순서 반전과 나중 참조 발견, 정렬 tie, 둘 다 참조·archive 중복·frontmatter 불일치·필드 보존·부분 수정 거부를 확인한다.
- 전체 verify는 source 원문 보존·새 archived source 거부·모호함/관측 실패 보류·목적지 덮어쓰기 부재·모델 호출 부재를 확인한다. 반영 이력 관측은 mock하고 선택 함수 자체는 실행한다.

## Progress

- [x] eb797d3a에 Requirement 중복 선택·제목 기반 동기화가 없음을 확인했다. 기존 ExecPlan batch 교정은 별도 완료 구현이며 재구현하지 않는다. 구현·테스트 실행 없음.
- [x] `catalog/requirement_corrections.py`에 참조·원격 반영 순서 기반 선택, 최소 식별자 편집, 상대 링크 갱신과 보류 결과를 추가하고 targeted unit 12개를 통과시켰다. 원격 게시와 복구는 별도 계획 범위다.
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- complexity medium: 선택 우선순위·보호 조건·편집 책임이 명시돼 있다. risk high: 참조된 문서를 재번호화하거나 목적지를 덮으면 기존 실행 근거가 달라진다. 원문·관계와 보류 결과를 보존하고 잘못된 게시 이후에는 Git/작업 관계의 수동 복구가 필요하다.
- 이 작업의 편집 산출은 원격 게시 완료를 뜻하지 않는다. G-233은 동일 결과를 최신 참조·원격 경쟁과 대조해 게시한다.
