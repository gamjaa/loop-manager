# Product

<!--
사용자나 시스템이 관찰할 수 있는 목표, 규칙, 상태와 결과를 작성한다.
구현 기술, 저장 형식, 클래스와 프로세스 구성은 ARCHITECTURE.md에 작성한다.
PRODUCT.md는 현재 Merge된 제품 동작과 장기 규칙의 기준으로 유지한다.
새로운 기능이나 정책 변경은 requirements/*.md에 먼저 기록한다.
PRODUCT.md는 사용자가 작성하며, Worker는 Requirement 완료 과정에서 앞으로도 지켜야 할 규칙만 코드와 같은 PR에 반영할 수 있다.
REQUIREMENT.template.md와 requirements/archive/는 작업 입력에서 제외한다.
requirements/ 바로 아래의 Requirement는 별도 상태 없이 모두 Planner 입력으로 사용한다.
PRODUCT.md와 Requirement 문서 중 하나 이상은 프로젝트에 존재해야 한다.
-->

## Summary

<!-- 제품이 누구의 어떤 문제를 해결하는지 한 문단으로 작성한다. -->

## Target Users

<!-- 주요 사용자와 사용 목적을 작성한다. -->

## Scope

### Current Goals

<!-- 현재 구현해야 하는 사용자 관점의 기능을 작성한다. -->

### Non-Goals

<!-- 현재 구현하지 않는 기능과 제품 경계를 작성한다. -->

## Product Invariants

<!-- 구현 방식과 관계없이 항상 지켜야 하는 규칙을 작성한다. -->

## Product Flows

<!-- 순서를 바꾸면 제품의 의미가 달라지는 흐름만 작성한다. -->

### [Flow Name]

(정상) 흐름:
1. <!-- 정상 흐름 -->

실패:
- <!-- 실패 시 결과와 변경되면 안 되는 상태 -->

## States and Transitions

<!-- 상태가 있는 기능에만 상태의 의미와 허용되는 전환을 작성한다. -->

```text
[INITIAL] → [NEXT] → [COMPLETED]
```

## Business Rules

<!-- 조건에 따라 허용하거나 거부해야 하는 동작을 작성한다. -->
