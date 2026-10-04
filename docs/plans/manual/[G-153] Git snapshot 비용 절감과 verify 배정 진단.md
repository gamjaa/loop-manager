---
id: G-153
title: Git snapshot 비용 절감과 verify 배정 진단
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-058] Test Timing and Agent-Free Test Execution.md
  - docs/requirements/[R-065] 실제 부하 기반 verify 병렬 실행.md
depends_on: []
complexity: medium
risk: medium
priority: normal
---

## Purpose

사용자가 제공한 서버 분석의 반복 Git 조회 비용과 2코어 환경의 확대 제한을 개선한다. 현재 원격 main `61d64a3`에서 직접 작업하며 자동 Planner 입력과 SQLite 상태를 만들지 않는 manual 기록으로 유지한다.

## Acceptance Criteria

- verify 기록에서 snapshot 호출 지점·횟수·시간과 명령 종류별 Git 호출 수·시간을 연결할 수 있다. Git 인자·출력·config 값 등 비밀 가능 데이터는 기록하지 않는다.
- Git operation 경로 13개를 하나의 명령으로 조회하며 worktree별 Git 경로 해석과 매 snapshot의 실제 파일 내용 검사를 보존한다. 줄바꿈 등으로 묶음 출력을 해석할 수 없으면 기존 개별 조회로 돌아간다.
- CPU 판단 기록에 proc/system 부하, cgroup 부하, 짧은 평균, snapshot 나이, 실행 수와 미반영 CPU·메모리 예약을 포함한다. 고정 1코어 여유 대신 제한된 확대 여유와 평균 부하를 사용하며 순간 과부하·quota·상한·메모리·관측 실패 보호를 유지한다.
- 미시작·종료 불명 예약은 계속 보호하며 실제 반영 여부를 증명하지 않은 예약의 중복 차감을 결함이라고 단정하거나 제거하지 않는다.
- scope·rebase·publication·소유권 재검증 시점과 테스트 ID·수집·fixture 수명·종료 계약은 유지한다. snapshot 장기 캐시와 모듈 분할은 추가하지 않는다.

## Implementation

1. 기존 verify timing 경계에 snapshot별 비용 집계를 추가한다. 계측은 활성 verify writer가 있을 때만 수행하고 한 snapshot에 집계해 Git 호출마다 파일을 flush하지 않는다.
2. `scope_observation`의 operation 경로 조회를 반복 `--git-path`를 쓰는 단일 `rev-parse`로 묶는다. 안전 검사는 새로 수집한다.
3. 자원 sampler에 짧은 부하 평균과 각 출처의 부하를 기록하고 계산·예약 입력을 allocation 결과에 남긴다. CPU 여유 기준을 작게 완화하되 즉시 과부하 제한과 미반영 예약 보호는 보존한다.
4. 반복 거절도 낮은 빈도로 기록해 같은 거절 이유가 이어지는 구간의 snapshot·예약 변화가 사라지지 않도록 한다. 기존 summary/report에 snapshot 집계를 포함한다.

## Validation

- Worker: Git operation 묶음 결과·호출 수·worktree 격리·변경 재관측·해석 fallback, snapshot 비용·오류·비활성 계측, 자원 평균·출처·과부하·quota·예약·거절 진단의 관련 targeted unit test를 실행한다.
- 기존 targeted 회귀: merge operation 거부, index flag와 숨긴 파일 변경, 다른 worktree 상태 변경, rebase/publication 재검증의 관련 case를 실행한다.
- 기존 Windows/Linux verify 스크립트는 runner 전체 manifest와 compile을 이미 실행하므로 유지한다. 전체 verify와 동일 환경 직렬/auto 반복 비교는 Manager가 Agent 밖에서 수행한다.
- 서버 비교는 같은 revision의 직렬/auto 및 변경 전후 동일 범위·반복 계측으로 Git 호출 수·CPU 시간·경과 시간·실행 수·거절 사유를 비교한다. 제공된 404초와 과거 691/686초로 절감률을 확정하지 않는다.

## Progress

- [x] 현재 명세·코드·원격 기준과 서버 분석 확인
- [x] 비용 계측과 operation 조회 묶음 구현
- [x] CPU 확대 조건과 allocation 진단 구현
- [x] 관련 targeted unit test: 35개 통과, 기존 scope·publication 회귀 7개 통과
- [x] scoped Draft PR 변경과 검증 기록 준비
- [ ] Manager 전체 verify·서버 반복 비교·읽기 전용 Review·사용자 Merge

## Decision Log

- 2026-10-04: 사용자의 로컬 구현·PR 요청으로 R-058/R-065의 기존 범위 안에서 수행한다. PRODUCT·Requirement·ARCHITECTURE와 AGENTS는 수정하지 않는다.
- 2026-10-04: 추가 계측 없이 중복 snapshot 재사용 구간을 추정하지 않는다. 먼저 확실한 13회 경로 조회를 묶으며 publication 직전 등 재검증은 유지한다.
- 2026-10-04: 모듈 분리는 앞선 개선 후 남는 종료 병목의 실측을 보고 판단한다. 현재 manifest·fixture 계약은 그대로 보존한다.
- 2026-10-04: 확대 보호 여유는 `min(1, cpus × 0.25)`로 정했다. 2코어에서 부하 1.23~1.5코어 구간에 확대를 허용하되 높은 부하까지 무조건 배정하지 않는다. 이는 관측된 worker CPU 수요를 0.5코어라고 확정한 값이 아니라 CPU 상한 내 확대의 초기 보호 기준이다. 최초 실행 문턱, 순간 95% 제한, 미반영 예약의 1코어 보호·메모리·quota·관측 실패 규칙을 유지한다. 1초 지수 평균과 순간 부하 중 큰 값을 써서 과부하는 즉시 제한하고 회복 후 확대는 늦춘다.
- 2026-10-04: `py -3 -B -m unittest tests.test_git_snapshot_cost tests.test_verify_resources tests.test_verify_runtime.TimingAndManifestTests tests.test_verify_runtime.RunnerPoolTests`의 35개가 통과했다. merge operation·index flag·숨긴 변경·주 worktree 변화·다른 worktree 변화·공유 metadata·publication exact lease의 기존 7개 case도 통과했다. 전체 verify는 실행하지 않았다.
- 2026-10-04: `test_interrupted_rebase_rejects_changed_preserved_file`은 Windows에서 `KeyboardInterrupt not raised`로 실패했고, `git archive HEAD`로 만든 변경 전 `61d64a3` 복사본에서도 같은 case·지점·메시지로 재현됐다. 해당 실패를 이번 변경의 성공 근거로 취급하지 않으며 Manager Linux 검증에 남긴다. Linux 전용 `test_manual_completion_with_new_base_and_changed_inputs_reconfirms_latest_candidate`는 기존 `skipIf(os.name == "nt")`로 skip됐다.
- 2026-10-04: snapshot 비용은 활성 verify writer가 있을 때만 수집한다. `_worktree_catalog`의 기존 단일 `git worktree list` 경계와 나머지 명시적 Git effects를 합산하며, snapshot 밖의 Git·테스트 fixture 전체 호출 수는 이 집계에 포함하지 않는다. 계측 활성·비활성 시간 차이도 서버 비교에서 구분해야 한다.
