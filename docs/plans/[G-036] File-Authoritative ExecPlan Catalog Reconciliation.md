---
id: G-036
title: File-Authoritative ExecPlan Catalog Reconciliation
sources:
  - docs/requirements/[R-017] File-Authoritative ExecPlan Source Reconciliation.md
depends_on:
  - G-031
  - G-032
  - G-049
complexity: high
risk: critical
priority: critical
---

## Purpose

최신으로 고정한 `origin/<base-branch>`의 전체 ExecPlan 파일을 계획 선언 정보의 원본으로 삼고, SQLite의 현재 선언 cache를 같은 ID의 유효한 파일에 원자적으로 맞춘다. 최초 게시 경로·commit·hash는 불변 감사 정보로 분리하고, 유효한 선언 변경은 오류로 만들지 않으며 잘못된 catalog만 실제 원인 파일을 가리키는 프로젝트 범위 진단으로 격리한다.

## Acceptance Criteria

- reconciliation은 하나의 고정된 원격 base revision에서 G-032의 active 및 archived catalog를 함께 읽고, regular file 여부, UTF-8/frontmatter, ID와 경로·제목 대응, source 경로 형식과 존재, 전체 ID·경로 고유성, dependency 존재 및 cycle을 SQLite 변경 전에 모두 검증한다.
- 같은 ID의 유효한 파일은 현재 경로·제목·sources·depends_on·complexity·risk·priority·본문 hash와 현재 plan revision SHA가 달라도 기존 실행 row와 대응하며, 이 차이만으로 `PLAN_BLOCKED`, `PROJECT_BLOCKED` 또는 planning error를 만들지 않는다.
- SQLite schema와 repository API는 최초 게시 commit SHA·active 경로·content hash를 불변 감사 및 PR 링크 정보로 보존하고, 최신 선언 경로·metadata·content hash·revision SHA를 별도의 현재 cache로 제공한다. additive migration은 기존 설치의 값을 손실 없이 두 역할로 초기화한다.
- 최신 선언 cache 갱신은 READY, 실행 중, PR_OPEN, 차단 또는 MERGED 상태와 재시도·실패 이력, Agent session, branch, worktree, workspace 소유권 및 PR 연결을 변경하지 않는다.
- 전체 catalog가 유효하고 원격 revision 및 대상 row의 상태·소유권 snapshot이 그대로일 때만 모든 달라진 row를 하나의 transaction으로 갱신한다. 원격 base 또는 SQLite snapshot이 바뀌면 일부 row를 적용하지 않고 최신 상태에서 다시 계산한다.
- source 추가·제거는 Requirement 관계와 입력 fingerprint의 다음 계산에 최신 값을 제공하고, dependency 변경은 검증된 전체 graph로 다음 READY 선택에 반영되며, priority 변경은 현재 작업을 선점하지 않고 다음 선택부터 적용된다.
- complexity 또는 risk 변경은 최초 Worker level cache를 다시 계산한다. 아직 시작하지 않은 READY 계획은 새 최초 level을 사용하고, 실행 이력이 있는 계획의 effective level과 Review 승격 이력은 낮추거나 지우지 않는다.
- current path와 title 변경은 compact catalog, 로그·알림 metadata와 이후 파일 조회에 반영되지만, 기존 PR의 ExecPlan 링크는 최초 게시 commit과 최초 active path를 계속 사용한다.
- Requirement archive source 관계와 후보 계산은 동기화된 현재 catalog를 사용한다. 최신 원격 `sources`와 과거 SQLite cache의 차이는 archive 오류가 아니며 같은 실행에서 새 관계로 후보를 다시 계산한다.
- G-031의 원격 `sources`가 최초 SQLite 값과 달라도 G-031 row를 갱신한 뒤 독립적인 G-035의 archive eligibility와 선택을 계속하고 G-035를 계획 내부 차단으로 바꾸지 않는다.
- 원격 ExecPlan 누락, invalid frontmatter, symlink·비regular entry, 존재하지 않는 source, ID 불명·중복 또는 dependency 오류에서는 선언 cache를 부분 갱신하지 않는다. G-031의 구조화된 project/catalog 원인에 원격 revision, 실제 문제 ExecPlan 경로와 누락 source를 기록하고 무관한 ExecPlan row를 차단하지 않는다.
- catalog 오류 fingerprint는 원인 ExecPlan과 필요한 source blob을 포함한다. 해당 원격 파일을 수정해 push하면 다음 실행에서 자동 재검증할 수 있고, dirty 기본 worktree와 push되지 않은 로컬 파일은 판단에 포함하지 않는다.
- PR 상태 동기화 뒤와 compact catalog 생성, Planner·Worker 선택, Requirement archive eligibility, 차단 재평가가 선언 정보를 읽기 전에 같은 reconciliation service를 사용한다. 반복 호출과 push 후 SQLite 갱신 전 중단은 부작용 없이 같은 최신 cache로 수렴한다.
- 장기 source-of-truth, 최초 게시 감사 정보와 현재 선언 cache의 구분 및 catalog 오류 범위를 `docs/PRODUCT.md`에 반영하되 Requirement와 ARCHITECTURE는 수정하지 않는다.

## Implementation

- G-032의 active/historical catalog parser 위에 revision-pinned `ExecPlanDefinition` snapshot을 만들고 path, parsed metadata, normalized body/byte hash와 revision을 plan ID별로 결합한다. source 존재와 전체 dependency graph 검증이 끝난 snapshot만 reconciliation 입력으로 허용한다.
- `exec_plans`에 최초 게시 정보와 최신 선언 cache를 구분하는 additive column 또는 정규화된 보조 table을 추가한다. 기존 `path`, `plan_sha`, `content_hash`의 의미를 암묵적으로 바꾸지 말고 migration 및 모든 repository accessor에서 immutable/current 역할을 명시한다.
- row별 expected 상태·소유권 및 기존 current revision을 compare-and-swap 조건으로 검증한 뒤 catalog 전체 변경을 한 transaction으로 저장한다. 최신 source 관계, initial Worker level과 compact metadata를 같은 transaction 결과에서 읽게 한다.
- 현재 `_execplan_metadata_identity_error`와 Requirement archive의 stored-vs-file equality 오류를 ID 대응 및 catalog 유효성 검사로 교체한다. 최초 게시 metadata는 현재 파일과의 동일성 검사가 아니라 immutable link/audit 생성에만 사용한다.
- G-031 project-block repository에 catalog 오류 code와 원인 path/source fingerprint를 추가하고, 정상적인 declaration delta와 invalid catalog를 분리하는 결과 type을 둔다.

## Validation

- Worker는 catalog parser/reconciliation repository와 Requirement archive source 관계의 직접 관련 test class 또는 새 reconciliation test module만 targeted unit test로 실행한다.
- title, active/archived current path, sources, depends_on, complexity, risk, priority와 본문이 각각 바뀐 같은-ID 파일이 상태·세션·workspace·PR 이력을 보존하면서 한 transaction으로 cache를 갱신하는지 검증한다.
- source 추가·제거에 따른 archive 후보 변화, dependency graph 재검증과 READY 선택, priority 다음 선택, 최초/effective Worker level의 비감소 규칙을 검증한다.
- 최초 게시 SHA·경로·hash와 immutable PR 링크는 유지되고 compact catalog·로그용 metadata 및 현재 file lookup만 최신 선언을 쓰는지 검증한다.
- 원격 base race, stale row snapshot과 transaction 중단에서 부분 갱신 없이 재시도하고 같은 revision의 반복 reconciliation이 멱등인지 검증한다.
- 누락·invalid·symlink 계획, 중복·변경 ID, 없는 source/dependency와 cycle이 실제 파일을 가리키는 project/catalog 오류가 되며 원인 수정 뒤 자동 재검증되는지 확인한다.
- G-031/G-035 source 불일치 회귀에서 독립 계획이 차단되지 않고 최신 관계로 archive eligibility 및 선택을 계속하는지 검증한다.
- 전체 verify에서 Planner publication·maintenance revision, active/archived catalog 복구, scoped blocking, compact catalog와 Requirement archive 기존 동작이 약화되지 않아야 한다.

## Progress

- [ ] 고정 revision의 전체 선언 snapshot 및 catalog 검증 구현
- [ ] 최초 게시 감사 정보와 현재 선언 cache schema·migration 분리
- [ ] 원자적 reconciliation 및 stale snapshot 재시도 구현
- [ ] archive·selection·compact catalog·차단 조회 경계 통합
- [ ] project/catalog 오류 fingerprint와 G-031/G-035 회귀 테스트 추가

## Decision Log

- G-028/G-029가 plan revision publication을, G-031이 plan/project 차단 범위를, G-032가 active/archive 전체 catalog를 제공한 뒤 하나의 reconciliation 경계로 통합한다.
- ID만 실행 이력의 안정적인 identity로 사용한다. 다른 선언 필드는 최신 신뢰 원격 파일을 따르되, 최초 게시 정보는 기존 PR의 불변 링크와 감사 목적으로 별도 보존한다.
- 실행 중 변경에 따른 단계별 무효화와 worktree 재개는 G-037로 분리해 이 계획의 catalog transaction과 오류 격리를 독립적으로 검증한다.
