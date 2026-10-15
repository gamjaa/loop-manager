---
id: G-253
title: 자체 Windows verify의 명시적 임시 경로 선택
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-067] 프로젝트 공통 verify 실행 규약.md
  - docs/requirements/[R-086] 영향 기반 부분 검증과 일반 선행 Review.md
depends_on:
  - G-265
  - G-266
  - G-267
  - G-268
complexity: medium
risk: low
priority: normal
---

## Purpose

자체 Windows verify의 launcher·복사 bundle·Windows 핵심 회귀가 사용하는 임시 파일시스템을 단독/Manager 실행 모두 명시적으로 선택하게 한다. 사용자 프로필 기본값과 이미 구현된 WSL 전체 → Windows 핵심 회귀를 유지하고, Windows Python 시작 전에 절대 경로를 적용해 진단과 비교 조건을 일치시킨다. WSL 내부의 실행별 `/tmp/` 프로젝트·임시 폴더는 별도 Linux 경계로 유지한다.

## Acceptance Criteria

- 자체 Windows의 단독·Manager launcher에서 동일한 명시 입력으로 임시 경로를 선택한다. 미지정은 현재 %USERPROFILE%\.cache\loop-manager\verify\tmp이며 상대 경로는 실행 checkout 기준으로 해석한다. 공백·비ASCII 경로도 실제 Windows Python의 tempfile 선택과 bundle 생성·native 자식에 적용된다. WSL의 전용 `/tmp/`를 Windows 경로로 대체하지 않는다.
- Python 조회/실행을 포함한 자식 시작 전에 최종 절대 경로를 TEMP·TMP·TMPDIR에 적용한다. 실제 선택 경로를 기존 run 진단/summary에 Windows launcher·native·WSL 단계로 구분해 남기고 입력 문자열이나 checkout 위치만으로 선택 성공을 표시하지 않는다. 현재 Windows 부분 요청은 두 단계를 실행하지 않는 `self_windows_requires_full` FALLBACK으로 유지하며, 그 준비·환경 복원도 검사하되 실행하지 않은 native/WSL 경로를 실효 관측으로 합성하지 않는다.
- 지정 경로 생성·사용 실패는 경로 오류와 실패 종료 코드다. 기본값이나 다른 디스크로 조용히 대체하지 않으며 검증 누락을 성공으로 처리하지 않는다.
- 정상·검증 실패·예외 뒤 호출자의 TEMP/TMP/TMPDIR·Python 인코딩·console 인코딩·cwd를 원래 존재/값으로 복원한다. 공유 임시 루트의 기존 파일과 다른 실행의 자료는 보존하며 전체 폴더 삭제·보안 제품 설정 변경을 수행하지 않는다.
- 다른 등록 프로젝트의 launcher/env를 일괄 변경하지 않는다. ARCHITECTURE의 Windows 경로 설명·docs/VERIFY.md와 OPERATIONS의 바로 필요한 선택 방법/오류/복구 안내를 실제 입력 계약에 맞춘다. 기존 interpreter·protocol/timeout·exit code·version 1 수집과 native 선택 목록을 유지한다. WSL 전체와 Windows 핵심 회귀의 결과를 각각 보고하고 두 단계 성공을 Windows native 전체 suite 성공으로 표시하지 않는다.

## Implementation

- [ ] 확인한 `scripts/verify.ps1`의 초기 환경 저장·checkout 이동·temp 생성·finally 복원을 확장한다. 자체 launcher에 한 가지 문서화된 선택 입력을 제공하고 Manager 호출에서도 같은 입력을 사용한다. 운영 설정 전체나 공통 adapter 선언에 무관한 설정을 추가하지 않는다.
- [ ] 확인한 `scripts/verify_windows.py:snapshot()`·`windows_main()`·`native_main()`의 bundle 생성과 native 자식, `wsl_main()`의 별도 TMPDIR/TMP/TEMP 적용을 대조한다. Windows 실효 경로와 WSL 전용 경로를 기존 진단에 연결하고 경로 사용 실패를 정상 FALLBACK이나 다른 디스크 선택으로 숨기지 않는다. 별도 임시 경로 관리 서비스나 두 단계 launcher를 새로 만들지 않는다.
- [ ] 확인한 `tests/test_verify_windows.py`의 환경 복원/native 연결 회귀와 `tests/test_verify_windows_launcher.py`의 snapshot·부분 FALLBACK·정리 fixture를 재사용한다. G-248의 단계별 진단 연결과 G-295의 전체 시간 상한 처리는 각각의 기존 경계를 소비하며 이 작업에서 재구현하지 않는다. 관련 ARCHITECTURE·VERIFY·OPERATIONS 설명만 실제 선택 계약에 맞춘다.

## Validation

- Worker는 `tests/test_verify_windows_launcher.py:WindowsVerifyPipelineTests`와 `tests/test_verify_windows.py`의 환경 복원 fixture에서 변경한 최소 case로 미지정/절대/상대·공백·비ASCII 경로, 생성/사용 실패, 원래 env 부재/값·인코딩/cwd 및 공유 marker 보존을 검사한다. 현재 부분 요청 FALLBACK과 전체 두 단계 모두 준비·복원 조건을 확인하며 경로 오류를 선정 fallback으로 숨기지 않는다. 정책 조합은 명령/OS 관측 mock으로 확인한다.
- 기존 launcher의 실제 interpreter 연결 회귀를 확장해 Windows 단독/Manager에서 Python tempfile.gettempdir와 진단의 최종 절대 경로가 같은지 확인한다. 실제 PowerShell/Windows가 없는 환경의 mock을 플랫폼 검증 성공으로 기록하지 않는다.
- Manager의 실제 Windows 실행은 구현된 WSL 전체 → `scripts/verify-windows-tests.json`의 native 핵심 회귀를 사용한다. 선택한 Windows 임시 루트의 기존 marker와 다른 실행의 bundle은 보존하고, WSL은 실행별 `/tmp/` 복사본·유효한 Git·임시 환경 변수·자식 종료 후 전용 폴더 정리를 유지해야 한다. 두 단계의 실제 tempfile 경로와 환경 복원 자료를 각각 확인하며 WSL 전체 PASS와 native 핵심 회귀를 Windows 전체 verify PASS로 표시하지 않는다. 비용은 R-065의 같은 PC·코드·범위끼리 비교하며 경로 변경 자체로 절감량이나 환경 원인을 확정하지 않는다.

## Progress

- [x] 2026-10-10 50c699c8f9c149cecb0ddf9b081527942d8e2289의 R-067, 고정 profile temp를 설정하는 verify.ps1·runner metadata와 기존 launcher 회귀를 정적으로 읽었다. 구현·테스트·Windows 측정·verify는 실행하지 않았다.
- [ ] 경로 선택/실패/복원·안내·targeted unit 검증
- [ ] Manager 전체 verify·실제 Windows 연결·읽기 전용 Review·사용자 Merge

## Decision Log

- 최신 기준 `bbce8cd25cdaa34a547b736cfc2f5d20f96a2a6d`의 두 단계 launcher·native 목록·복사/실패/정리 회귀는 직접 반영된 구현으로 재사용한다. 이 작업은 그 도입을 다시 계획하지 않으며 남아 있는 명시적 Windows 임시 경로 선택·실효 관측만 보완한다. WSL `/tmp/` 격리와 native 기본 경로를 구분하며 실제 OS 실행·성능·verify 성공은 이번 정적 검토로 확정하지 않는다.

- 현재 verify.ps1은 기존 TEMP와 무관하게 사용자 프로필 기본 경로로 덮어쓴다. 환경 복원·공유 루트 보존은 이미 있으므로 재구현하지 않고 선택 입력·실효 경로 진단과 실패 조건만 보완한다.
- complexity medium: launcher의 경로/예외·환경 복원과 실제 Python/Manager 진단 연결이 명확한 책임이다. risk low: 선택 오류는 자체 검증 실패/임시 위치 설명에 한정하며 원본 env·공유 파일을 보존하고 입력 수정·재실행으로 복구한다. priority normal이다.
- G-248은 결과/자식/배정 진단을, 이 작업은 선택 입력과 실효 임시 경로를 담당한다. 두 작업은 기존 metadata를 재사용해 독립 검증하며 R-065의 우선순위를 필수 dependency로 만들지 않는다.
