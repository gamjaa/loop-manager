---
name: specification-docs
description: Create, review, and refine PRODUCT.md, ARCHITECTURE.md, requirements/*.md, and FUTURE_IDEAS.md through conversation with the user. Use when defining product behavior, documenting a requested change, recording technical design or deferred ideas, finding missing scenarios or edge cases, or deciding whether a specification is ready for Planner.
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

AGENTS.md는 공통 작업 원칙, 이 Skill은 사람이 읽는 문서의 작성 기준을 담당한다. ExecPlan은 `docs/PLANS.md`와 제공된 역할별 지침을 따른다. 문서 정리를 이유로 작업 권한이나 실행 상태를 바꾸지 않는다.

| 문서 | 중심 질문 | 작성 방향 |
|---|---|---|
| PRODUCT | 제품이 무엇을 보장하는가? | 현재 동작·핵심 규칙·상태·완료 조건. 구현 기술과 단계별 처리 방식은 ARCHITECTURE에 둔다. |
| ARCHITECTURE | 그 보장을 어떤 구조로 지키는가? | 책임 경계·정보 원본·저장·실행 격리·검증·복구 원리. 제품 정책과 설치 안내를 반복하지 않는다. |
| Requirement | 무엇을 왜 바꾸며 어떻게 확인하는가? | 원하는 변화·이유·검증 가능한 Acceptance Criteria. 구현 계획과 실행 상태를 넣지 않는다. |
| FUTURE_IDEAS | 지금 구현하지 않을 아이디어는 무엇인가? | 현재 범위 밖의 보존 가치가 있는 아이디어만 기록한다. 현재 계획의 근거로 승격하지 않는다. |
| ExecPlan | 이번 작업에서 무엇을 구현·검증하는가? | Planner가 공통 형식으로 작성한다. 이 Skill에서는 역할 구분을 확인하며 아래의 기존 편집 제한을 유지한다. |

초기 제품은 `PRODUCT.md`에서 정의할 수 있다. 운영 중 새 기능이나 정책 변경은 Requirement에서 시작하고, 구현이 완료된 뒤 계속 유지할 규칙만 `PRODUCT.md`에 반영한다. 구현 세부를 PRODUCT나 Requirement에 넣거나 사용자 동작을 ARCHITECTURE에 중복하지 않는다.

## Quality Guidance

문서를 작성하거나 검토하기 전에 기존 문서와 템플릿의 재사용 가능성을 확인하고, 현재 요청에 필요하지 않은 구조·파일·표현을 추가하지 않는다. PRODUCT·Requirement·ARCHITECTURE의 역할을 유지한 채 중복과 장황함을 줄인다. 간결화를 위해 결정, 예외, 실패 흐름, Acceptance Criteria의 의미나 필요한 완전성을 생략하지 않는다.

문서의 첫 문단과 각 절은 해당 질문의 답부터 제시한다. 동일 정책의 조건·예외·결과는 소유 문서의 한 절에서 정의하고, 다른 절과 문서는 필요한 요약과 링크로 연결한다. 순서가 제품 의미를 바꾸는 흐름만 단계로 쓰고, 조건별 허용·거부·보존 결과는 비교하기 쉬운 표나 짧은 항목으로 표현한다.

상세 구현 순서·파일·함수 목록과 작업 이력은 현재 판단에 필요한 만큼만 남긴다. 현재 구조와 미구현 목표 설계를 구분하고 대체된 설계를 현행 규칙처럼 남기지 않는다. 간결화 전후에 적용 조건·판단 주체·허용 및 거부·실패 시 보존과 재개·검증 기준을 대조한다. 단순한 줄 수·항목 수 제한으로 필요한 완전성을 줄이지 않는다.

작업·권한·검증·완료 규칙은 AGENTS와 PLANS에, 제품 규칙·변경 요청·설계 조건은 각 제품 문서에 유지한다. 대상 프로젝트의 문서와 제공된 작업 지침에서 필요한 규칙과 판단 조건을 확인할 수 있어야 하며, 간결화를 이유로 소유 문서에서 생략하지 않는다.

## Write Scope

프로젝트의 다른 파일은 문맥 확인을 위해 읽을 수 있지만, 일반적인 Skill 작업에서 생성·수정·삭제할 수 있는 파일은 다음으로 제한한다.

- `docs/PRODUCT.md`
- `docs/ARCHITECTURE.md`
- `docs/requirements/*.md`
- `docs/FUTURE_IDEAS.md`

코드, 테스트, 설정, AGENTS, 템플릿, 다른 Skill 파일을 포함한 나머지 파일은 수정하지 않는다. 필요한 변경을 발견하면 이유와 대상만 사용자에게 알린다.

`docs/plans/*.md`는 다른 Agent가 작업 중일 수 있으므로 생성·수정·삭제하지 않는다. 사용자가 특정 ExecPlan 파일에 대한 구체적인 변경을 명시적으로 요청한 경우에만 그 파일과 요청한 범위에 한해 예외로 수정할 수 있다. 일반적인 문서 정리나 정합성 보완 요청을 ExecPlan 수정 권한으로 확대 해석하지 않는다.

## Workflow

1. 대상 프로젝트의 기존 `PRODUCT.md`, Requirement, `ARCHITECTURE.md`와 관련 템플릿을 확인한다.
2. 사용자 입력에서 확정된 사실, 가정, 사용자 결정이 필요한 항목을 구분한다.
3. 새 문서를 만들지 기존 문서를 보완할지 판단하고 가장 좁은 문서 범위만 수정한다.
4. 주요 정상 흐름, 실패 흐름, 상태 전이, 권한, Product Invariant, Non-Goal과 edge case의 누락을 찾는다.
5. 제품 정책을 임의로 결정하지 않는다. 구현 방향을 바꿀 질문은 사용자에게 먼저 묻고 `Open Questions`에 남긴다.
6. 답변과 편집을 반영한 뒤 문서별 역할·중복·상호 링크와 현재 규칙의 일치를 확인한다. 기존 조건·예외·실패·검증 의미가 빠지지 않았는지 대조한다.
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
- 대상 프로젝트의 문서와 제공된 작업 지침에서 필요한 규칙과 판단 조건을 확인할 수 있다.

문서 작성 요청은 사용자가 소유한 명세를 함께 작성하라는 명시적 위임으로 취급한다. 이 Skill 작업에서는 코드 구현을 함께 수행하지 않으며, ExecPlan은 위 Write Scope의 명시적 예외가 없으면 변경하지 않는다.
