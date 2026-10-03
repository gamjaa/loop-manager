# Architecture

<!--
이 파일은 참고용 목차다. 프로젝트에 필요한 절만 선택하거나 다른 구조로 재구성해도 된다.
PRODUCT.md의 목표와 동작을 기술적으로 어떻게 구현하고 유지하는지 작성한다.
사용자 기능 목록, 설치 튜토리얼, ExecPlan 상세 형식은 중복해서 작성하지 않는다.
-->

## Related Documents

- 제품 목표와 규칙: `docs/PRODUCT.md` (선택)
- 개별 요구사항: `docs/requirements/*.md` (선택)
- ExecPlan 작성 규칙: `docs/PLANS.md`

<!-- PRODUCT.md와 requirements/*.md 중 하나 이상은 프로젝트에 존재해야 한다. -->

## Overview

<!-- 전체 구조와 실행 환경을 한 문단으로 작성한다. -->

## Architecture Drivers

<!-- 설계에 영향을 주는 제품 요구사항과 기술적 제약만 작성한다. -->

## System Context

<!-- 프로그램, 사용자, 파일 시스템, 외부 CLI와 서비스의 경계를 표현한다. -->

```text
[User]
  ↓
[Application] ─→ [External System]
  ↓
[Persistent State]
```

## Responsibilities

<!-- 주요 실행 주체별 책임과 하지 않는 일을 작성한다. 클래스 설계는 필요할 때만 추가한다. -->

## Technical Choices

| 항목 | 선택 | 이유 |
|---|---|---|
| 언어와 버전 |  |  |
| 의존성 관리 |  |  |
| 지원 환경 |  |  |
| 외부 도구 |  |  |

## Sources of Truth and Persistence

<!--
설정, 작업 상태, 외부 시스템 상태의 원본과 저장 위치를 작성한다.
프로그램 재실행 시 어떤 정보로 작업을 복구하는지도 작성한다.
-->

## Execution Model

<!--
작업 선택, 동시 실행 범위, 프로세스 실행 경계, 세션 재사용 범위를 작성한다.
PRODUCT.md의 사용자 흐름을 반복하지 말고 이를 보장하는 방법만 작성한다.
-->

## State Transition Enforcement

<!-- PRODUCT.md의 상태 전이를 누가, 어디에서, 어떻게 검증하고 저장하는지 작성한다. -->

## External Integrations

<!-- 외부 CLI와 서비스별 입력, 출력, 인증, 실패 처리 방식을 작성한다. -->

## Verification and Recovery

<!-- 테스트 실패, 프로세스 중단, 재시도, 재실행 후 복구 방법을 작성한다. -->

## Repository Safety

<!-- 기존 변경 보존, branch/worktree, commit, push와 PR 생성 경계를 작성한다. -->

## Security

<!-- 입력 검증, 명령 실행, 자격 증명, 로그와 비가역 작업의 보호 방법을 작성한다. -->

## Architecture Change Policy

<!--
저장 형식, 상태 모델, 인증, 실행 격리 또는 외부 infrastructure처럼
구현 전에 사용자 승인이 필요한 아키텍처 변경을 작성한다.
-->
