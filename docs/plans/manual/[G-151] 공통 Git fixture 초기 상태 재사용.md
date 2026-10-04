---
id: G-151
title: 공통 Git fixture 초기 상태 재사용
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-058] Test Timing and Agent-Free Test Execution.md
depends_on: []
complexity: medium
risk: medium
priority: normal
---

## Purpose

서버의 G-135 작업을 유지하면서 공통 Git fixture가 매 호출마다 반복하는 초기 저장소 생성·설정·commit·push를 로컬에서 먼저 줄인다. 준비된 초기 상태를 프로세스 안에서 재사용하고 각 테스트에는 독립된 실제 저장소를 제공한다. 서버 Planner의 새 실행 항목으로 중복 등록하지 않도록 직접 구현 기록은 manual 경로에 둔다.

## Acceptance Criteria

- `tests/support/git.py`의 공통 helper는 초기 seed를 필요할 때 한 번 준비하고 각 호출의 repo·remote·index·refs·객체 파일·작업 파일을 독립 복사한다. origin은 해당 호출의 remote를 가리킨다.
- import 시 Git·임시 디렉터리를 만들지 않는다. seed 초기화 실패는 정리하고 성공한 seed는 테스트 프로세스 종료에 정리한다. 실패한 초기화를 캐시하지 않는다.
- 기존 초기 파일·commit·main·tracking·반환 경로와 실제 Git 동작의 계약을 보존한다. 호출별 원격 변경·rebase·정리와 config 수정이 다른 테스트나 seed로 전파되지 않는다.
- 변경된 helper와 테스트에 직접 관련된 targeted unit test를 실행하고 필수 전체 검증을 줄이지 않는다. 실제 서버 verify 시간 절감과 전체 Git 회귀는 Manager 실행 뒤에만 확인된 것으로 기록한다.

## Implementation

1. 기존 초기화 본문을 독립 helper로 보존하고, 표준 라이브러리의 프로세스 내 단일 seed 캐시와 TemporaryDirectory 종료 정리를 사용한다.
2. repo와 bare remote를 각 테스트의 경로로 복사한다. 새 위치의 Git 초기화·origin을 재설정해 seed 경로 참조와 공유 object 연결이 남지 않게 한다.
3. 원래 초기화 unit assertion을 유지하고, 재사용·독립 복사·경로 재설정·초기화 실패 정리를 작은 filesystem·fake Git unit test로 확인한다.
4. 구현과 targeted 결과를 이 문서에 기록하고 별도 branch에 push한다. 이후 G-149는 같은 개선을 반복하지 않고 실제 시간 기록과 남은 병목을 확인한다.

## Validation

- Worker: 새 seed 캐시 unit test, 기존 공통 Git 초기화와 import 무부작용 unit case만 실행한다. Git 실행은 fake로 주입해 재사용 횟수, 독립 파일·refs와 origin 인자, 실패 정리를 직접 검증한다.
- Manager: 기존 전체 Git suite에서 push·rebase·cleanup·source-sync 동작과 test fixture 격리를 확인하고 서버의 전후 verify 시간을 비교한다.

## Progress

- [x] 초기 seed 재사용과 독립 복사 구현
- [x] targeted unit test 6개 통과와 scoped diff 확인
- [ ] branch push
- [ ] Agent 밖의 전체 verify, 읽기 전용 Review와 사용자 Merge

## Decision Log

- `py -3 -B -m unittest tests.test_git_fixture_seed tests.test_support_fixtures.SupportFixtureTests.test_git_factory_uses_explicit_repository_and_preserves_seed_files tests.test_support_fixtures.SupportFixtureTests.test_imports_create_no_resources_or_discoverable_cases`: 6개 통과. Windows sandbox 임시 디렉터리 권한 오류 후 같은 command를 정상 권한으로 다시 실행했다.
- 기존 초기화는 Git subprocess 8회이며, seed 준비 후 각 독립 복사는 2회다. 파일 복사 비용과 전체 verify의 실제 시간 변화는 측정하지 않았다. 전체 verify의 discovery 범위와 스크립트는 유지한다.
- 사용자는 서버가 G-135를 실행 중인 동안 후속 계획에서 필요한 작은 부분을 로컬에서 선행 구현·push하도록 요청했다.
- G-135의 Git 정리·게시 adapter, G-143의 DB 수명, G-144의 시나리오 구조, G-145의 process 종료를 수정하지 않는다. 이 변경에 해당 계획의 Merge 의존성은 필요하지 않다.
- G-148의 시간 기록과 G-150의 부하 기반 병렬 실행은 구현하지 않으며 R-058 완료 또는 실제 절감률을 주장하지 않는다. 제거되는 반복 초기화의 확인과 서버 실행 시간 측정은 구분한다.
