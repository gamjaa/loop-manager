---
name: specification-docs
description: Create, review, and refine PRODUCT.md, ARCHITECTURE.md, and requirements/*.md from the Loop Manager templates through conversation with the user. Use when defining product behavior, documenting a requested change, recording technical design, finding missing scenarios or edge cases, or deciding whether a specification is ready for Planner.
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

초기 제품은 `PRODUCT.md`에서 정의할 수 있다. 운영 중 새 기능이나 정책 변경은 Requirement에서 시작하고, 구현이 완료된 뒤 계속 유지할 규칙만 `PRODUCT.md`에 반영한다. 구현 세부를 PRODUCT나 Requirement에 넣거나 사용자 동작을 ARCHITECTURE에 중복하지 않는다.

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

Requirement는 frontmatter나 별도 상태 없이 제품에 원하는 변화와 Acceptance Criteria를 기록하는 순수 문서다. `docs/requirements/` 바로 아래의 모든 Requirement가 Planner 입력이 되므로, 사용자 결정이 필요한 항목을 해소하고 Ready Check를 통과한 뒤 해당 위치에 저장한다. 파일 경로를 안정적인 식별자로 사용하며 완료 후 archive 이동은 사용자가 수행한다.

## Ready Check

다음을 확인한다.

- 주요 정상 흐름과 실패 흐름이 정의돼 있다.
- 중요한 상태와 허용되는 상태 전이가 정의돼 있다.
- 권한과 거부 조건이 필요한 경우 정의돼 있다.
- 핵심 Product Invariant와 Non-Goal이 분명하다.
- Requirement에는 검증 가능한 Acceptance Criteria가 있다.
- 구현자가 임의로 제품 정책을 정해야 하는 미결정 사항이 없다.
- PRODUCT, Requirement와 ARCHITECTURE가 서로 모순되지 않는다.

문서 작성 요청은 사용자가 소유한 명세를 함께 작성하라는 명시적 위임으로 취급한다. 이 Skill 작업에서는 코드 구현이나 ExecPlan 생성을 함께 수행하지 않는다.
