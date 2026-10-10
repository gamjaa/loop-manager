---
id: G-251
title: 최초 import 검사의 독립 모듈 분산
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-065] 실제 부하 기반 verify 병렬 실행.md
depends_on: []
complexity: medium
risk: medium
priority: normal
---

## Purpose

한 테스트 모듈에 집중된 최초 import 검사를 여러 독립 unittest 모듈로 분산한다. 각 구현 대상은 계속 새 interpreter에서 처음 import하고 기존 coordinator 예산과 전체 수집 완결성을 유지한다.

## Acceptance Criteria

- 현재 loop_manager entry point·구현 모듈과 기존 package initializer 검사 대상 집합을 코드에서 수집한다. 기존 검사 대상과 분할 뒤 대상이 같고 누락·중복이 없으며 새 구현 모듈도 포함한다. 과거 조사 당시 245개를 고정하지 않는다.
- 여러 독립 테스트 모듈을 기존 unittest adapter의 모듈 배정 단위로 제공한다. 각 대상은 새 interpreter에서 최초 import하며 순환 import·entry point 선행 로딩 금지·import 중 SQLite 연결/프로세스 생성 금지를 유지한다. 이미 import한 process를 재사용하지 않는다.
- coordinator의 자원/cap·serial/auto 선언과 timeout·자식 종료 보호를 지킨다. 테스트 worker 안에 별도 독립 프로세스 pool이나 runner를 만들지 않는다. 각 실패는 대상과 interpreter 오류를 식별하며 미실행 대상을 완료로 기록하지 않는다.
- 수집과 실행의 대상/테스트 identity·fixture 소유를 일치시킨다. 새 테스트 ID와 기존 검사 대상의 대응을 남기고 수집 재확인의 누락/중복·변경 실패를 유지한다. entry binding·runtime 복사·compile 등 다른 기존 회귀는 그대로 검사한다.
- ARCHITECTURE의 새 interpreter 예외와 분산 책임을 구현에 맞춰 설명한다. 실제 프로세스 검사를 mock으로 대체하거나 skip·timeout 확대·필수 compile 축소로 비용을 줄이지 않는다.

## Implementation

tests/test_internal_packages.py의 최초 import 대상 수집·검사 코드와 기존 tests/support·unittest discovery/manifest 경계를 재사용한다. 대상 집합과 안정적인 분할/테스트 identity의 최소 helper만 공유해 독립 모듈이 겹치지 않게 한다. 파일/함수별 분할 방식은 기존 수집·fixture 규칙을 확인해 결정하며 공통 adapter를 임의 동적 단위 runner로 바꾸지 않는다. import 금지 효과를 검사하는 기존 전용 회귀도 분할 뒤 동일한 검사 코드를 사용하도록 연결한다.

## Validation

- Worker는 대상 inventory/분할의 최소 unit case로 기존 집합 일치·새 모듈 포함·누락/중복 거부·수집/실행 identity 안정성과 guard 코드의 SQLite/process 거부를 확인한다. 분할 helper 자체의 조건 조합에는 process 관측을 mock하고 실제 새 interpreter 계약 회귀는 유지한다.
- test_verify_contract.py의 discovery/load_tests·fixture/manifest 적합성과 기존 실제 최초 import 회귀가 전체 verify에서 1/auto 모두 실행돼야 한다. 각 대상의 새 interpreter·entry point 무선행 로딩·오류 식별 및 coordinator 상한을 확인한다.
- Manager는 기존 전체 기준과 최종 전체 verify를 연결하고 분할 전후 대상/ID 대응·동일 Python/Git·계측/cache·자원/부하·worker 수·임시 경로에서 영향 모듈을 반복 비교해 중앙값을 기록한다. 테스트 ID 변경으로 범위가 줄어든 것을 속도 개선으로 보고하지 않으며 국소 비교마다 전체 suite를 반복하지 않는다.

## Progress

- [x] 2026-10-10 50c699c8f9c149cecb0ddf9b081527942d8e2289의 R-065와 test_internal_packages의 단일 모듈 최초 import 반복·package 검사 및 unittest 모듈 배정 계약을 정적으로 읽었다. 구현·테스트·성능 비교·verify는 실행하지 않았다.
- [ ] 집합/identity 보존 분산·targeted unit 검증
- [ ] Manager 전체 verify·대응/비교 기록·읽기 전용 Review·사용자 Merge

## Decision Log

- test_each_module_imports_first_without_loading_the_entry_point는 현재 구현 전체를 한 테스트의 subTest loop에서 새 Python으로 직렬 실행한다. 새 interpreter 계약은 이미 충족되지만 coordinator가 독립 대상 묶음을 분산할 수 없다.
- complexity medium: 기존 테스트 수집·대상 분할·새 interpreter 검사를 명확한 책임에서 연결한다. risk medium: 분할 실수는 import/부작용 회귀 누락이나 자원 예산 우회를 만든다. 대상 집합·manifest 대조와 기존 전체 회귀 및 테스트 rollback으로 복구한다. priority normal이다.
- G-222/G-223의 Git 정책 전환과 G-224의 실제 fixture 격리를 중복하지 않는다. 이 검사에는 새 interpreter 자체의 확인할 효과가 있으므로 실제 프로세스 예외를 유지한다.
