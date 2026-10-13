---
name: specification-docs
description: Create, review, and refine PRODUCT.md, ARCHITECTURE.md, requirements/*.md, and FUTURE_IDEAS.md through conversation with the user. Use when defining product behavior, documenting a requested change, recording technical design or deferred ideas, finding missing scenarios or edge cases, or deciding whether a specification is ready for implementation.
---

# Specification Docs

사용자와 대화하며 제품 문서를 작성하거나 검토한다. 명시적인 작업 모드를 요구하지 말고 요청과 기존 문서 상태에 필요한 절차만 수행한다.

## Templates

새 파일은 Skill 디렉터리를 기준으로 다음 템플릿을 읽고 구조를 유지한다.

- `../../../docs/PRODUCT.template.md`
- `../../../docs/ARCHITECTURE.template.md`
- `../../../docs/REQUIREMENT.template.md`

대상 프로젝트에 같은 템플릿이 있으면 대상 프로젝트의 파일을 우선한다. 기존 문서가 있으면 템플릿으로 덮어쓰지 않고 필요한 부분만 수정한다.

## Reserved Paths

이 Skill이 사용하는 `.agents/skills/specification-docs/`와 `SKILL.md`, `docs/PRODUCT.md`,
`docs/ARCHITECTURE.md`, `docs/FUTURE_IDEAS.md`, `docs/requirements/`와 그 아래 `archive/`,
세 문서의 `docs/*.template.md` 경로는 예약 경로다. Skill·템플릿·문서를 다른 위치로 옮기거나
이름을 바꾸어 조회 경로와 상대 참조를 깨뜨리지 않는다. 예약은 위치 고정이며 모든 파일의
생성 의무가 아니다. 문서 내용은 이 Skill의 작성 범위에서 갱신하고 Requirement의 archive
취급은 아래 Lifecycle을 따른다.

## Document Boundaries

| 문서 | 중심 질문 | 작성 방향 |
|---|---|---|
| PRODUCT | 제품이 무엇을 보장하는가? | 현재 동작·정책·상태·예외·불변식·완료 조건. 구현 세부와 작업 이력은 넣지 않는다. |
| ARCHITECTURE | 그 보장을 어떤 구조로 지키는가? | 기술 선택의 이유·책임 경계·정보 원본·상태 전이·보존·격리·검증·복구·외부 연동 계약. 제품 정책과 설치 안내는 반복하지 않는다. |
| Requirement | 무엇을 왜 바꾸며 어떻게 확인하는가? | 한 변경의 목적·원하는 동작·검증 가능한 Acceptance Criteria. 불필요한 내부 설계·구현 계획·실행 상태는 넣지 않는다. |
| FUTURE_IDEAS | 지금 구현하지 않을 아이디어는 무엇인가? | 범위 밖 아이디어만 간결히 보존한다. 상세 구현 계획으로 확장하거나 현재 계획의 근거로 승격하지 않는다. |

초기 제품은 `PRODUCT.md`에서 정의할 수 있다. 새 기능·정책 변경은 관련 활성 Requirement를 먼저 찾아 갱신하며 별도 문서 작성 요청을 기다리지 않는다. 신규 작성 전 활성 Requirement 전체를 대상으로 제목·키워드뿐 아니라 변경 목적·제품 규칙·동작 시나리오·영향 범위를 비교한다. 여러 문서가 관련되면 필요한 기존 문서를 함께 갱신한다. 관련 문서가 없거나 독립 목표를 결합하기 부적절할 때만 새 문서를 만든다. 조회 실패·내용 확인 불가는 관련 계획이 없다는 근거가 아니다. 구현이 완료된 뒤 계속 유지할 규칙만 PRODUCT에 반영하고 구현 세부·실행 계획이나 상태를 Requirement에 넣지 않는다.

제품 정책과 설계는 사용자와 확정한 내용을 반영하고, 미결정 사항은 임의로 확정하지 않는다.

## Quality Guidance

기존 문서·템플릿을 먼저 확인한다. 갱신은 설명 추가보다 관련 절의 재작성·통합을 우선한다. 새 요구사항과 기존 조건을 하나의 설명으로 구성하고 반복 정책·같은 의미의 문장·분산된 예외·대체된 설계·문서 목적 밖 구현 세부·작업 이력을 정리한다.

첫 문단과 각 절은 해당 질문의 답부터 제시한다. 정책의 조건·예외·결과는 소유 문서 한 절에 모으고 다른 곳은 요약·링크로 연결한다. 순서가 의미를 바꾸는 흐름만 단계로, 조건별 허용·거부·보존 결과는 표나 짧은 항목으로 쓴다. 현재 구조와 미구현 목표를 구분한다.

Context는 문제·목적, Requirement는 원하는 동작·정책·제약, Acceptance Criteria는 객관적인 충족 결과를 기록하며 세 절을 유지한다. Requirement와 Acceptance Criteria를 합치지 않는다. Product Impact는 지속할 제품 규칙을 별도로 식별할 때, Non-Goals는 범위 오해를 막을 때만 둔다. Open Questions는 작성 중 사용하고 확정 뒤 빈 절·해결된 질문을 제거한다. 비어 있거나 중복인 절과 참고 자료의 긴 복사를 강제하지 않으며 필요한 근거만 링크·경로로 연결한다.

구현자가 제품 정책을 임의로 정하지 않아도 될 수준까지 명세하되 안전하게 결정할 기술 선택은 고정하지 않는다. 요청 밖 기능·확장 시나리오·설정·관리 도구를 요구사항에 추가하지 않고 필요한 사용자 결정 또는 FUTURE_IDEAS로 분리한다.

편집 전후의 적용 조건·판단 주체·제품 규칙/불변식·정상/실패/예외 흐름·권한/거부·상태 전이·데이터 보존/재개·Acceptance Criteria·설계 근거를 대조해 의미와 완전성을 보존한다. 길이·항목 수·토큰 수에 기계적인 제한을 두지 않는다.

## Write Scope

프로젝트의 다른 파일은 문맥 확인을 위해 읽을 수 있지만, 일반적인 Skill 작업에서 생성·수정·삭제할 수 있는 파일은 다음으로 제한한다.

- `docs/PRODUCT.md`
- `docs/ARCHITECTURE.md`
- `docs/requirements/` 바로 아래의 활성 Requirement
- `docs/FUTURE_IDEAS.md`

코드, 테스트, 설정, 템플릿, 다른 Skill 파일을 포함한 나머지 파일은 수정하지 않는다. 필요한 변경을 발견하면 이유와 대상만 사용자에게 알린다.

## Workflow

1. 대상 프로젝트의 기존 `PRODUCT.md`, Requirement, `ARCHITECTURE.md`와 관련 템플릿을 확인한다.
2. 사용자 의도·정상/실패 시나리오·참고 자료와 기존 제품 규칙을 비교한다. 기존 문서·코드로 확인할 사실은 먼저 조사하고 확정된 사실·가정·사용자 결정을 구분한다.
3. 새 기능·정책 변경은 관련 활성 Requirement를 작성·갱신하거나 충분한 기존 활성 문서를 재사용한다. 다른 문서도 요청에 필요한 가장 좁은 범위만 수정한다.
4. 주요 정상 흐름, 실패 흐름, 상태 전이, 권한, Product Invariant, Non-Goal과 edge case의 누락을 찾는다.
5. 제품 정책·동작 의미를 임의로 정하지 않는다. 구현을 막을 질문에는 배경·선택지·영향·합리적인 권장안을 간결하게 제공한다. 내부 기술 선택은 과도하게 질문하지 않는다. 확정 전 질문은 대화나 검토용 초안에서 관리하고 답변은 해당 본문에 통합한다. 중요한 미결정 사항을 해소하기 전에는 활성 Requirement로 저장·게시하지 않는다.
6. 편집 후 Quality Guidance에 따라 문서별 역할·링크·정합성과 의미 보존을 확인한다.
7. 구현 가능한 수준인지 Ready Check를 수행하고 남은 결정 사항을 짧게 보고한다.

사용자가 아이디어만 설명하면 초안을 작성한다. 기존 문서를 제시하면 검토와 보완을 우선한다. Open Question에 답하면 관련 부분만 갱신한다. 사용자가 누락 검토를 요청하면 파일을 불필요하게 다시 쓰지 않고 발견 사항을 먼저 설명한다.

## Requirement Lifecycle

새 Requirement를 만들기 직전에 프로젝트 루트에서 다음 명령을 실행하고 출력된 ID를 파일명과 첫 제목에 사용한다.

```sh
python .agents/skills/specification-docs/scripts/next_requirement_id.py
```

이 명령은 활성·archive Requirement의 원본 ID 중 최댓값 다음 ID를 한 줄로 출력한다. archive 디렉터리가 없으면 빈 집합으로 취급한다. 결과는 로컬 작성용 후보이며 예약이 아니므로 문서를 만들기 직전에 조회한다. Skill은 파일을 직접 나열해 번호를 계산하지 않는다. 조회가 실패하면 번호를 계산하거나 기존 문서에서 추정하지 말고 오류를 해결한다.

새 Requirement는 frontmatter나 별도 상태 없이 제품에 원하는 변화와 Acceptance Criteria를 기록하는 순수 문서다. 기존 문서에 유효한 `id`와 `title` frontmatter가 있으면 두 값을 원본으로 사용한다. frontmatter가 없으면 첫 `# [R-ID] 제목` 제목을 사용한다. frontmatter가 잘못됐거나 필요한 값이 없으면 본문이나 파일명으로 대체하지 않는다. 기존 Requirement에 frontmatter를 일괄 추가하지 않는다. 사용자 결정이 필요한 항목을 해소하고 Ready Check를 통과한 뒤 `docs/requirements/` 바로 아래에 저장한다. 파일 경로는 안정적인 식별자로 유지한다.

`docs/requirements/archive/`의 Requirement는 배경과 과거 결정을 확인하기 위해 읽기만 한다. 현재 변경의 Requirement로 재사용하거나 수정·삭제하지 않고 활성 위치로 되돌리지 않는다. 보관 문서와 관련된 새 변경은 사용자 요청을 기준으로 새로운 고유 ID·경로의 Requirement를 작성한다. 보관 문서의 ID·경로를 다시 쓰거나 원문을 편집용 복사본으로 만들지 않는다. 이 Skill의 기존 Requirement 재사용·갱신은 활성 문서에만 적용한다.

명세 작성 중 활성 Requirement를 완료로 간주해 보관 위치로 옮기지 않는다.

## Ready Check

다음은 작성 완료 전 품질 검사다. 모든 항목을 문서의 별도 절로 추가하는 양식이 아니며 실제 변경에 필요한 조건만 확인한다.

- 주요 정상 흐름과 실패 흐름이 정의돼 있다.
- 중요한 상태와 허용되는 상태 전이가 정의돼 있다.
- 권한과 거부 조건이 필요한 경우 정의돼 있다.
- 핵심 Product Invariant와 Non-Goal이 분명하다.
- Requirement에는 검증 가능한 Acceptance Criteria가 있다.
- 구현자가 임의로 제품 정책을 정해야 하는 미결정 사항이 없다.
- PRODUCT, Requirement와 ARCHITECTURE가 서로 모순되지 않는다.
- 대상 프로젝트의 문서와 제공된 작업 지침에서 필요한 규칙과 판단 조건을 확인할 수 있다.

명시적인 문서 작성 요청은 해당 사용자 명세를 함께 작성하는 위임으로 취급한다. 새 기능·정책 변경 요청도 관련 Requirement 작성·갱신의 위임으로 취급한다. 이 Skill 단계에서는 명세를 작성하며 코드 구현을 함께 수행하지 않는다.
