---
name: specification-docs
description: Create, review, and refine PRODUCT.md, ARCHITECTURE.md, requirements/*.md, and FUTURE_IDEAS.md from the Loop Manager templates through conversation with the user. Use when defining product behavior, documenting a requested change, recording technical design or deferred ideas, finding missing scenarios or edge cases, or deciding whether a specification is ready for Planner.
---

# Specification Docs

사용자와 대화하며 제품 문서를 작성하거나 검토한다. 명시적인 작업 모드를 요구하지 말고 요청과 기존 문서 상태에 필요한 절차만 수행한다.

## Templates

새 파일은 Skill 디렉터리를 기준으로 다음 템플릿을 읽고 구조를 유지한다.

- `../../../docs/PRODUCT.template.md`
- `../../../docs/ARCHITECTURE.template.md`
- `../../../docs/REQUIREMENT.template.md`

대상 프로젝트에 같은 템플릿이 있으면 대상 프로젝트의 파일을 우선한다. 기존 문서가 있으면 템플릿으로 덮어쓰지 않고 필요한 부분만 수정한다.

## Document Boundaries

- `PRODUCT.md`: 현재 Merge된 제품에서 참이어야 하는 사용자 동작, 경계, 상태와 장기 규칙
- `requirements/*.md`: 제품에 원하는 변화, 이유, Acceptance Criteria와 제품 영향
- `ARCHITECTURE.md`: PRODUCT와 Requirement를 보장하는 구조, 기술 선택, 책임, 저장과 복구 방식
- `FUTURE_IDEAS.md`: 현재 범위 밖이라 구현하지 않지만 나중에 검토할 가치가 있는 아이디어

초기 제품은 `PRODUCT.md`에서 정의할 수 있다. 운영 중 새 기능이나 정책 변경은 Requirement에서 시작하고, 구현이 완료된 뒤 계속 유지할 규칙만 `PRODUCT.md`에 반영한다. 구현 세부를 PRODUCT나 Requirement에 넣거나 사용자 동작을 ARCHITECTURE에 중복하지 않는다.

## Quality Guidance

문서를 작성하거나 검토하기 전에 기존 문서와 템플릿의 재사용 가능성을 확인하고, 현재 요청에 필요하지 않은 구조·파일·표현을 추가하지 않는다. PRODUCT·Requirement·ARCHITECTURE의 역할을 유지한 채 중복과 장황함을 줄인다. 간결화를 위해 결정, 예외, 실패 흐름, Acceptance Criteria의 의미나 필요한 완전성을 생략하지 않는다.

## Write Scope

프로젝트의 다른 파일은 문맥 확인을 위해 읽을 수 있지만, 일반적인 Skill 작업에서 생성·수정·삭제할 수 있는 파일은 다음으로 제한한다.

- `docs/PRODUCT.md`
- `docs/ARCHITECTURE.md`
- `docs/requirements/*.md`
- `docs/FUTURE_IDEAS.md`

코드, 테스트, 설정, README, AGENTS, 템플릿, 다른 Skill 파일을 포함한 나머지 파일은 수정하지 않는다. 필요한 변경을 발견하면 이유와 대상만 사용자에게 알린다.

`docs/plans/*.md`는 다른 Agent가 작업 중일 수 있으므로 생성·수정·삭제하지 않는다. 사용자가 특정 ExecPlan 파일에 대한 구체적인 변경을 명시적으로 요청한 경우에만 그 파일과 요청한 범위에 한해 예외로 수정할 수 있다. 일반적인 문서 정리나 정합성 보완 요청을 ExecPlan 수정 권한으로 확대 해석하지 않는다.

## Workflow

1. 대상 프로젝트의 기존 `PRODUCT.md`, Requirement, `ARCHITECTURE.md`와 관련 템플릿을 확인한다.
2. 사용자 입력에서 확정된 사실, 가정, 사용자 결정이 필요한 항목을 구분한다.
3. 새 문서를 만들지 기존 문서를 보완할지 판단하고 가장 좁은 문서 범위만 수정한다.
4. 주요 정상 흐름, 실패 흐름, 상태 전이, 권한, Product Invariant, Non-Goal과 edge case의 누락을 찾는다.
5. 제품 정책을 임의로 결정하지 않는다. 구현 방향을 바꿀 질문은 사용자에게 먼저 묻고 `Open Questions`에 남긴다.
6. 답변을 반영한 뒤 중복과 문서 간 모순을 다시 확인한다.
7. 구현 가능한 수준인지 Ready Check를 수행하고 남은 결정 사항을 짧게 보고한다.

사용자가 아이디어만 설명하면 초안을 작성한다. 기존 문서를 제시하면 검토와 보완을 우선한다. Open Question에 답하면 관련 부분만 갱신한다. 사용자가 누락 검토를 요청하면 파일을 불필요하게 다시 쓰지 않고 발견 사항을 먼저 설명한다.

## Requirement Lifecycle

Requirement는 frontmatter나 별도 상태 없이 제품에 원하는 변화와 Acceptance Criteria를 기록하는 순수 문서다. `docs/requirements/` 바로 아래의 모든 Requirement가 Planner 입력이 되므로, 사용자 결정이 필요한 항목을 해소하고 Ready Check를 통과한 뒤 해당 위치에 저장한다. 파일 경로는 안정적인 식별자로 유지한다.

완료된 Requirement는 사용자가 직접 archive하지 않는다. 마지막 관련 ExecPlan의 구현 PR에서 Loop Manager가 eligibility를 다시 확인한 뒤 내용 변경 없이 `docs/requirements/archive/`로 자동 이동한다. archive PR이 Merge되기 전에는 활성 Requirement로서 Planner 입력에 남는다. 관련 비종료 ExecPlan이 남아 있거나 archive 충돌 또는 source 관계의 불확실성이 있으면 자동 이동하지 않고 활성 위치에 보존한다. 대화형 문서 작성 과정에서도 이 eligibility를 우회해 Requirement를 이동하지 않는다.

## Ready Check

다음을 확인한다.

- 주요 정상 흐름과 실패 흐름이 정의돼 있다.
- 중요한 상태와 허용되는 상태 전이가 정의돼 있다.
- 권한과 거부 조건이 필요한 경우 정의돼 있다.
- 핵심 Product Invariant와 Non-Goal이 분명하다.
- Requirement에는 검증 가능한 Acceptance Criteria가 있다.
- 구현자가 임의로 제품 정책을 정해야 하는 미결정 사항이 없다.
- PRODUCT, Requirement와 ARCHITECTURE가 서로 모순되지 않는다.

문서 작성 요청은 사용자가 소유한 명세를 함께 작성하라는 명시적 위임으로 취급한다. 이 Skill 작업에서는 코드 구현을 함께 수행하지 않으며, ExecPlan은 위 Write Scope의 명시적 예외가 없으면 변경하지 않는다.
