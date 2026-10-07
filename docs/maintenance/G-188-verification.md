# G-188 검증과 비용 비교

2026-10-07, 기준 revision `44a5a916598669631ffd7183e399b656f2656ed5`와
`loop-manager/G-188` 워크트리의 최초 변경 후보를 비교했다. 최초 후보의 코드·테스트 diff
SHA-256은 `3f43f8207f93617f0f6511523bedffbc27e154f2d3843250f0a4b63fcf1df1e3`이다
(`git diff --binary -- loop_manager_internal tests | sha256sum`). 자동 Manager 상태나
Reviewer 성공 기록은 생성하지 않았다.

사용자 요청에 따른 재검토에서 호출 경로 identity 거부와 고정 Git 호출 예산을
보강했다. 재검토 후보의 코드·테스트 diff SHA-256은
`99137840519e5a7e24fbd5c4f4cb3f4529619311bb9be51032226b12e4c8ce07`이며,
관리 rebase 뒤 현재 base `60b956ddc1b5f24f0d4cfcc53fb1ecfd996b097a`와 HEAD
`798e2816c439d758ab0cdebfaac645801c952327` 사이에서도 같다. 현재 patch의 재현 명령은
`git diff --binary 60b956ddc1b5f24f0d4cfcc53fb1ecfd996b097a...HEAD -- loop_manager_internal tests | sha256sum`이다.
전체 verify 비교와 원본 측정의 기준 revision `44a5a916598669631ffd7183e399b656f2656ed5`는
당시 실행의 역사적 기록으로 유지한다. 원본 진단에서 추출한 환경·단계·snapshot 이유·전체 Git 경로 비용은
`G-188-costs.json`에 함께 보존한다. 아래 최초 비교 기록과 재검토 후보의 기록을 구분한다.

## 환경과 집계

- Linux host `7d78783ca307`, Python 3.12.14, Git 2.52.0, CPU quota 4·affinity 5.
- 전체 verify는 두 실행 모두 `auto`, 실제 최대 4 worker였다. 같은 환경과
  197개 모듈을 실행했지만 CPU 부하·메모리 여유·배정 시점은 각각의 원본 allocation에 남는다.
- Review 비용 비교는 별도 interpreter에서 같은 PASS·FAIL 교정 시나리오를
  직렬 실행했다. 각 실행의 Git seed 최초 생성도 포함하며 다른 verify는 실행하지 않았다.
- `git_command_timing`으로 준비·관측 밖을 포함한 전체 Git subprocess 수·시간을
  수집했다. snapshot 내부 명령만을 전체 비용으로 표시하지 않는다.
- 단계 시간은 원본 `EventWriter`의 실제 경과 시간이다. seed·copy·confirmation처럼
  상위 단계에 포함된 시간은 상위 시간과 합산하지 않는다. 병렬 Git 누적 시간도
  전체 verify 경과 시간과 구분한다.

## 같은 Review 시나리오의 준비 재사용

`_run_review_sequence`로 프로토콜 오류 → PASS와 프로토콜 오류 → 유효 FAIL →
교정 Worker → verify → PASS를 실행했다. 변경 전에는 각각 독립 준비했고,
변경 후에는 첫 흐름의 Review 진입 경계를 두 번째 흐름에 복원했다.

| 측정 | 독립 준비 | 경계 재사용 |
|---|---:|---:|
| 시나리오 전체 Git 명령 | 1,269 | 1,074 |
| 전체 Git 누적 시간 | 1.946s | 1.885s |
| 전체 snapshot | 22 | 20 |
| 시나리오 경과 시간 | 2.499s | 2.489s |
| 초기 준비 횟수 / 누적 시간 | 2 / 0.095s | 1 / 0.066s |
| 첫 Worker·verify 준비 횟수 / 누적 시간 | 2 / 1.127s | 1 / 0.594s |
| Git seed 최초 생성 | 0.026s | 0.027s |
| 경계 첫 저장 | 해당 없음 | 0.040s |
| 자료 identity 확인 | 해당 없음 | 0.019s |
| 경계 복원 | 해당 없음 | 0.023s |
| 복사 하위 단계(저장·복원 합계) | 해당 없음 | 0.011s |
| 파일·DB 재현 확인 하위 단계 | 해당 없음 | 0.038s |
| 새 Git snapshot 확인(저장·복원 합계) | 해당 없음 | 0.062s |
| 경계 정리 | <0.001s | 0.003s |

복원 관리 비용과 현재 Git 확인까지 포함해 명령은 195회, snapshot은 2회 줄었다.
전체 경과 시간 차이는 이 표본에서는 작으므로 특정 시간 절감률을 보장하지 않는다.
재사용은 한 테스트 안의 동일 소유 경로에 한정하며 별도 테스트·worker 사이에
Git·원격·DB·receipt를 공유하지 않는다.

## rebase 채택의 중첩 관측

`test_adoption_shares_owner_inspection_but_reobserves_confirmation`은 같은 실제 Git
저장소에서 기존 소비 순서와 공유 순서를 비교한다. durable owner의 응답과
저장 호출만 주입하고 Git snapshot·index·ref·등록·config·파일 조회는 실제로 실행한다.
실제 SQLite transaction의 낡은 관측 거부는 별도 unit과 전체 복구 테스트가 보호한다.

| 측정 | 소유권과 채택의 독립 검사 | 같은 검사 구간 공유 |
|---|---:|---:|
| snapshot | 6 | 4 |
| 전체 관측 경로 Git 명령 | 102 | 68 |
| Git 누적 시간(targeted 실행) | 0.129s | 0.087s |
| 구간 경과 시간(targeted 실행) | 0.144s | 0.097s |

최초 이중 관측과 SQLite 확정의 새 이중 관측은 각각 유지한다. 소유권 소비자는
전체 expectation·generation이 일치할 때만 전달된 관측을 쓰며 게시 owner 판정은
별도로 수행한다. `adoption_inspect`, `adoption_owner`, `adoption_confirm` 단계와
snapshot caller chain으로 최초 검사·소유권 소비·필수 재검증을 구분한다.

## assertion과 보호 계약의 대응

| 기존 또는 보호 계약 | 현재 검증 |
|---|---|
| `test_review_protocol_error_is_corrected_to_pass_in_same_session` | `test_review_protocol_correction_preserves_pass_and_fail_transitions`, `corrected_to_fail=False`: 실제 첫 Worker·verify·Review, 동일 Reviewer session, 오류 prompt, 교정 1회, 이력 미소비, PR_OPEN |
| `test_review_protocol_error_is_corrected_to_fail_without_worker_retry` | 같은 테스트의 `corrected_to_fail=True`: 프로토콜 교정은 Worker를 호출하지 않으며 유효 FAIL 뒤 교정 Worker·새 verify·Review, review 실패 전이 1회, 오류 prompt 제거, PR_OPEN |
| 축소한 최초 Worker·verify assertion | 첫 변형에서 실제 실행을 유지하고 복원 변형은 최초 Worker·verify가 재실행되지 않음을 역할 순서와 호출 수로 확인 |
| 실패·교정 변형의 독립성 | 기존 `test_review_protocol_errors_do_not_retry_worker_or_consume_history`: 7개 오류 변형의 독립 교정 횟수·session·prompt·차단·빈 실패/승격 이력; 복원한 Worker version은 교정 시 실제 새 파일을 생성 |
| 파일/mode·Git·SQLite/TEMP·journal 복원과 실패/중단/병렬 격리 | 기존 `test_recovery_boundary`의 실제 Git/index/ref/원격·DB·TEMP 복원, 자료 손상·DB 불일치·자료 부재·KeyboardInterrupt·병렬 경로 독립성 |
| 전체 입력에 한정된 공유·별도 owner 거절 | 확장한 `test_ownership_reuses_only_matching_inspect_evidence`: repo/identity/worktree/branch/target/plan/path/before HEAD/role/입력/허용 범위/generation/remote 불일치는 재관측, owner 거절은 유지 |
| 검사 뒤 외부 상태 변경 | 새 `test_adoption_shares_inspection_and_rejects_drift_at_transaction`: 두 검사 사이 remote drift를 실제 SQLite transaction에서 거절, checkpoint·기존 verify 결과·미게시 상태 보존 |
| 불필요한 snapshot·Git 명령 회귀 | 새 실제 Git 비용 unit의 snapshot 6→4회 및 고정 Git 명령 102→68회 assertion; 양쪽 명령 수가 함께 늘어도 실패 |
| 다른 경로·시나리오의 준비 재사용 거부 | 같은 Review 변형 테스트에서 foreign directory·publish 설정 불일치를 복원 전에 거부하고 뒤의 정상 변형이 독립 경계에서 재개함을 확인 |
| 정상·다중 충돌·게시·중단·재시작 | 기존 managed rebase lifecycle, publication rebase recovery, PR conflict corrections와 전체 verify 유지 |
| 최초 import 계약 | `test_each_module_imports_first_without_loading_the_entry_point`의 새 interpreter 검사를 유지; 전체 실행 27.981s→27.450s |

## 전체 verify와 모듈 누적 비용

| 측정 | 변경 전 | 변경 후 |
|---|---:|---:|
| 성공 / 기존 Windows 전용 skip | 3,017 / 2 | 3,018 / 2 |
| 모듈 | 197 | 197 |
| 전체 verify 경과 시간 | 185.994s | 180.503s |
| 전체 Git 명령 | 217,849 | 216,967 |
| 전체 Git 누적 시간 | 470.517s | 451.448s |
| 전체 snapshot / 내부 Git 명령 | 4,985 / 89,400 | 4,956 / 88,878 |
| Review protocol 개별 테스트 합계 / 모듈 경과 | 3.134s / 3.137s | 2.736s / 2.738s |
| Recovery history 개별 테스트 합계 / 모듈 경과 | 12.139s / 12.146s | 11.072s / 11.078s |
| Current rebase adoption 개별 테스트 합계 / 모듈 경과 | 0.403s / 0.405s | 0.459s / 0.460s |
| Git snapshot cost 개별 테스트 합계 / 모듈 경과 | 0.582s / 0.585s | 0.956s / 0.959s |
| Recovery boundary 개별 테스트 합계 / 모듈 경과 | 0.454s / 0.456s | 0.505s / 0.506s |

두 기존 protocol test ID를 하나의 변형 테스트로 합치고 회귀 테스트 두 개를
추가했으므로 runner의 `scope_matches=false`, `observed_environment_matches=true`,
`comparable=false`다. 위 대응표는 원래 시나리오·assertion의 보존 근거이며 manifest가
그대로라고 보고하지 않는다. protocol 개별 시간은 변경 전 PASS 1.099s·FAIL 2.035s,
변경 후 두 변형 합계 2.736s다. recovery history에는 현재 Git 확인을 추가해
snapshot이 119→126회 늘었으며, 관측 검증 비용을 생략해서 절감을 만들지 않았다.
전체 시간 차이는 부하·배정 차이도 포함한 관측값이며 준비 재사용만의 효과로 해석하지 않는다.
두 전체 실행 모두 미완료 단계·미집계 Git 명령이 없고 전체 compile까지 성공했다.

## 실행 결과와 원본 자료

- Targeted: guard를 설치한 `unittest`로 `test_review_protocol_recovery`,
  `RecoveryHistoryTest.test_review_protocol_errors_do_not_retry_worker_or_consume_history`,
  `test_recovery_boundary`, `test_git_snapshot_cost`, `test_execution_current_rebase_adoption_phase`:
  **46개 PASS**, `/tmp/G-188-targeted.log`.
- 전체: 변경 전·후 `bash scripts/verify.sh` **PASS**. 최초 sandbox 실행은 loopback
  socket 생성이 거부돼 실패했으며 같은 명령을 허용된 실행 환경에서 재실행했다.
  테스트·mock·skip·timeout을 변경해 환경 실패를 우회하지 않았다.
- Targeted 최초 선택자의 class 이름 오류, 새 비용 fixture의 context 누락,
  drift 검증의 기대 exception 오류는 교정 후 해당 unit 재실행 및 위 46개 검증과
  현재 후보의 전체 verify에서 모두 통과했다.
- 변경 전 전체 원본: `.cache/verify/2f06e5ee0b7444d0a7b5af6d110121c4/`;
  변경 후: `/tmp/G-188-after-verify/`, stdout `/tmp/G-188-after-verify.log`.
- 전체 runner 비교: `/tmp/G-188-full-comparison-compact.json`.
- 동일 Review 시나리오 원본: `/tmp/G-188-review-before-isolated/`,
  `/tmp/G-188-review-after-isolated/`; 각각의 `.log` 파일과
  `/tmp/G-188-measure-review.py`가 실행 입력·단계 기록을 보존한다.

기존 inactive journal lazy 관측, snapshot 내부 조회 통합, 초기 Git seed와
rebase·publication 경계 재사용은 유지했다. 전체 verify·import 최초 실행·실제
Git/SQLite 경쟁·중단 복구의 범위와 timeout은 변경하지 않았다.

## 재검토 결론과 최종 후보

G-188의 네 Acceptance Criteria를 R-070의 준비 복원·관측 유효 범위·회귀 보호·
효과 확인 계약과 다시 대조했다. Review 진입 전의 실제 Worker·verify 실행과
진입 뒤 실패/교정 전이는 유지하며, 기존 rebase·publication 준비 재사용도 보존한다.
실제 파일·Git·DB 복원과 현재 확인을 성공 결과 재사용과 구분하고, 소유권 소비자의
전체 입력 비교·게시 owner 판정·SQLite 확정의 새 이중 관측을 확인했다.

재검토에서 다음 두 보완점을 해결했다.

- helper가 다른 `directory` 인자를 받아도 저장 경로를 복원하던 동작을 수정했다.
  다른 canonical 경로와 publish 설정 불일치를 복원 전에 거부하며 뒤의 정상
  변형이 같은 독립 경계를 복원할 수 있음을 검증한다.
- 상대적 명령 감소만 확인하던 비용 unit에 고정 관측 예산(102·68회)을 추가했다.
  두 경로에 Git 명령이 함께 늘어나는 회귀도 실패한다. 시간 상한은 추가하지 않았다.

최종 후보의 targeted **46개 PASS**(`/tmp/G-188-review-targeted.log`), 전체 verify
**3,018개 성공·기존 Windows 전용 skip 2개·전체 compile PASS**
(`/tmp/G-188-final-verify/`, `/tmp/G-188-final-verify.log`)를 확인했다.
전체 경과 179.695s, Git 명령 216,959회·누적 450.057s,
snapshot 4,956회·내부 Git 명령 88,878회다. 미완료 단계·미집계 Git은 없다.
같은 Review 시나리오를 직렬로 다시 실행해 1,074 Git 명령·20 snapshot,
경과 2.380s·Git 누적 1.788s를 확인했다(`/tmp/G-188-review-final-isolated/`).
변경 전·최초 후보·최종 후보의 환경·개별/모듈 누적 비용과 단계·이유별 snapshot,
Git 명령 수·시간은 함께 게시하는 `G-188-costs.json`에 보존했다.

당시 후보의 대화형 검토에서 게시할 blocking issue는 없었다. 이 결과는 사용자 요청에 따른
대화형 재검토이며 자동 Manager/Reviewer 성공 기록이나 사용자 Merge를 대신하지 않는다.

## 관리 rebase 뒤 현재 HEAD 확인

2026-10-07, 현재 HEAD `798e2816c439d758ab0cdebfaac645801c952327`의 base
`60b956ddc1b5f24f0d4cfcc53fb1ecfd996b097a` 대비 최종 diff 전체를 다시 확인했다.
변경은 12개 경로에 걸쳐 있으며 코드·테스트 patch hash는 위의 reviewed hash와 같다.
이 재검토에서 과거 후보의 테스트 성공을 현재 결과로 사용하지 않았다.

현재 HEAD에서 다음 최소 범위 targeted unit test를 실행해 **6개 PASS**를 확인했다.

```text
python3 -m unittest -v tests.test_review_protocol_recovery.ReviewProtocolRecoveryTest.test_review_protocol_correction_preserves_pass_and_fail_transitions tests.test_recovery_boundary.RecoveryBoundaryTests.test_real_git_and_sqlite_restore_without_rewriting_identity tests.test_recovery_boundary.RecoveryBoundaryTests.test_independent_parallel_variants_and_failed_variant_restore tests.test_git_snapshot_cost.GitSnapshotCostTests.test_adoption_shares_owner_inspection_but_reobserves_confirmation tests.test_execution_current_rebase_adoption_phase.CurrentRebaseAdoptionTests.test_adoption_shares_inspection_and_rejects_drift_at_transaction tests.test_execution_current_rebase_adoption_phase.CurrentRebaseAdoptionTests.test_ownership_reuses_only_matching_inspect_evidence
```

이번 Worker 세션에서는 `scripts/verify.sh`와 전체 verify를 실행하지 않았다. 위의 과거
전체 verify 결과는 rebase 뒤 후보의 검증으로 간주하지 않으며, 현재 후보의 전체 verify와
독립 Review는 Manager 작업으로 남아 있다.
