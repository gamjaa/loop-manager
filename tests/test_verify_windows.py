import os
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from tests.support.git import isolated_git_test, make_remote_repository


@unittest.skipUnless(sys.platform == 'win32', 'Windows verify 스크립트 검증')
@isolated_git_test
class VerifyWindowsTests(unittest.TestCase):
    def fixture(self, directory, source):
        root = make_remote_repository(directory)
        scripts = root / 'scripts'
        scripts.mkdir(exist_ok=True)
        original = Path(__file__).resolve().parents[1]
        for name in ('verify.ps1', 'verify_windows.py'):
            shutil.copyfile(original / 'scripts' / name, scripts / name)
        (scripts / 'verify-windows-tests.json').write_text(json.dumps(['tests.test_smoke']))
        tests = root / 'tests'
        tests.mkdir(exist_ok=True)
        (tests / '__init__.py').write_text('')
        (tests / 'test_smoke.py').write_text(source, encoding='utf-8')
        (scripts / 'verify.json').write_text(json.dumps({'version': 1, 'adapter': 'unittest',
            'environment': {'python': 'launcher'}, 'stages': [
                {'id': 'tests', 'kind': 'tests', 'cwd': '.', 'execution': 'independent',
                 'collection': {'directory': 'tests', 'pattern': 'test*.py'}},
                {'id': 'compile', 'kind': 'command', 'cwd': '.',
                 'argv': ['{python}', '-c', 'compile("VALUE = 1", "fixture", "exec")']}]}))
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith('LOOP_MANAGER_VERIFY_')}
        environment.update(LOOP_MANAGER_VERIFY_RUNNER=str(original / 'loop_manager_internal/verification/entrypoint.py'),
            LOOP_MANAGER_VERIFY_RUNTIME_PYTHON=sys.executable, LOOP_MANAGER_VERIFY_VERSION='1',
            LOOP_MANAGER_VERIFY_LOG_DIR=str(Path(directory) / 'logs'), LOOP_MANAGER_VERIFY_TIMEOUT='90',
            LOOP_MANAGER_VERIFY_PROJECT_ROOT=str(root), USERPROFILE=str(Path(directory) / 'user'))
        return root, environment

    def test_real_launcher_runs_both_phases_and_preserves_native_failure(self):
        powershell = shutil.which('pwsh') or shutil.which('powershell')
        if powershell is None or shutil.which('wsl.exe') is None:
            self.skipTest('PowerShell·WSL이 필요합니다.')
        for expression, expected in (('True', True), ('os.name == "posix"', False)):
            with self.subTest(native_pass=expected), tempfile.TemporaryDirectory() as directory:
                source = ('import os, unittest\nclass Smoke(unittest.TestCase):\n'
                          f'    def test_case(self):\n        self.assertTrue({expression})\n')
                root, environment = self.fixture(directory, source)
                completed = subprocess.run([powershell, '-NoProfile', '-File', str(root / 'scripts/verify.ps1')],
                    cwd=root, env=environment, capture_output=True, text=True, encoding='utf-8', timeout=100)
                self.assertEqual(completed.returncode == 0, expected, completed.stdout + completed.stderr)
                result = json.loads((Path(directory) / 'logs/result.json').read_text(encoding='utf-8'))
                self.assertEqual((result['wsl'], result['windows'], result['passed']),
                                 ('PASS', 'PASS' if expected else 'FAIL', expected))
                linux = json.loads((Path(directory) / 'logs-wsl/summary.json').read_text(encoding='utf-8'))
                self.assertTrue(linux['result']['passed'])
                self.assertTrue(any(p['name'] == 'stage:compile' for p in linux['phases']))
                native = json.loads((Path(directory) / 'logs-windows/summary.json').read_text(encoding='utf-8'))
                self.assertEqual(native['counts'], {'success' if expected else 'failure': 1})
                self.assertTrue(json.loads((Path(directory) / 'logs-wsl/cleanup.json').read_text())['removed'])
                self.assertEqual((root / 'tests/test_smoke.py').read_text(), source)

    def test_killed_windows_launcher_stops_linux_children_and_removes_its_copy(self):
        from loop_manager_internal.execution.verify_process import run_verify_process
        powershell = shutil.which('pwsh') or shutil.which('powershell')
        if powershell is None or shutil.which('wsl.exe') is None:
            self.skipTest('PowerShell·WSL이 필요합니다.')
        with tempfile.TemporaryDirectory() as directory:
            root, environment = self.fixture(directory,
                'import time, unittest\nclass Smoke(unittest.TestCase):\n'
                '    def test_case(self):\n        time.sleep(120)\n')
            passed, timed_out, output = run_verify_process(
                [powershell, '-NoProfile', '-File', str(root / 'scripts/verify.ps1')],
                root, 8, environment=environment, job_scope=True)
            self.assertFalse(passed, output)
            self.assertTrue(timed_out, output)
            saved = json.loads((Path(directory) / 'logs/snapshot.json').read_text(encoding='utf-8'))
            bundle = Path(saved['bundle'])
            self.assertTrue(bundle.is_relative_to(Path(directory)))
            receipt = bundle / 'cleanup.json'
            deadline = time.monotonic() + 12
            while not receipt.is_file() and time.monotonic() < deadline:
                time.sleep(0.2)
            self.assertTrue(receipt.is_file(), 'Windows 모체 종료 후 WSL 정리 근거가 없습니다.')
            cleanup = json.loads(receipt.read_text(encoding='utf-8'))
            self.assertTrue(cleanup['stopped'], cleanup)
            self.assertTrue(cleanup['removed'], cleanup)

    def test_temp_directory_is_inherited_and_environment_is_restored(self):
        powershell = shutil.which('pwsh') or shutil.which('powershell')
        if powershell is None:
            self.skipTest('PowerShell 없음')
        source = Path(__file__).resolve().parents[1] / 'scripts/verify.ps1'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'checkout'
            scripts = root / 'scripts'
            scripts.mkdir(parents=True)
            shutil.copyfile(source, scripts / 'verify.ps1')
            user_profile = Path(directory) / 'user'
            harness = Path(directory) / 'check.ps1'
            harness.write_text(r'''
$ErrorActionPreference = 'Stop'
$originalLocation = (Get-Location).Path
$keys = @('TEMP', 'TMP', 'TMPDIR', 'PYTHONIOENCODING')
$before = @{}
foreach ($key in $keys) { $before[$key] = [Environment]::GetEnvironmentVariable($key, 'Process') }
$encoding = [Console]::OutputEncoding
function py {
    if ($args[-1] -notlike '*verify_windows.py') { throw '자체 Windows 검증 진입점을 사용하지 않았습니다.' }
    $script:observed = (& $env:VERIFY_TEST_PYTHON -c 'import json, os, tempfile; print(json.dumps([os.getcwd(), tempfile.gettempdir(), os.environ["TEMP"], os.environ["TMP"], os.environ["TMPDIR"]]))') | ConvertFrom-Json
    if ($env:VERIFY_TEST_RESULT -eq 'throw') { throw '테스트용 예외' }
    $global:LASTEXITCODE = [int]$env:VERIFY_TEST_RESULT
}
$text = Get-Content -LiteralPath (Join-Path $env:VERIFY_TEST_ROOT 'scripts/verify.ps1') -Raw
# exit만 관측값 저장으로 바꿔 호출자 환경의 복원을 검사한다.
$text = $text.Replace('exit $verifyExitCode', '$script:observedExitCode = $verifyExitCode')
$checkPath = Join-Path $env:VERIFY_TEST_ROOT 'scripts/checked-verify.ps1'
Set-Content -LiteralPath $checkPath -Value $text -Encoding UTF8
try {
    . $checkPath
    if ($env:VERIFY_TEST_RESULT -eq 'throw') { throw '예외가 누락됐습니다.' }
    if ($script:observedExitCode -ne [int]$env:VERIFY_TEST_RESULT) { throw '종료 코드가 바뀌었습니다.' }
} catch {
    if ($env:VERIFY_TEST_RESULT -ne 'throw' -or $_.Exception.Message -ne '테스트용 예외') { throw }
}
$expected = $env:VERIFY_TEST_TEMP_DIR
if ($script:observed[0] -ne $env:VERIFY_TEST_ROOT) { throw 'checkout에서 실행되지 않았습니다.' }
foreach ($value in $script:observed[1..4]) {
    if ($value -ne $expected) { throw "잘못된 자식 임시 경로: $value" }
}
foreach ($key in $keys) {
    if ([Environment]::GetEnvironmentVariable($key, 'Process') -cne $before[$key]) { throw "복원되지 않은 환경: $key" }
}
if ((Get-Location).Path -ne $originalLocation) { throw '작업 경로가 복원되지 않았습니다.' }
if ([Console]::OutputEncoding.CodePage -ne $encoding.CodePage) { throw '출력 인코딩이 복원되지 않았습니다.' }
''', encoding='utf-8-sig')
            for configured, custom_state in ((False, False), (True, False), (True, True)):
                temporary = user_profile / '.cache/loop-manager/verify/tmp'
                self.assertFalse(temporary.is_relative_to(root))
                sentinel = temporary / 'existing-file'
                for result in ('0', '7', 'throw'):
                    with self.subTest(configured=configured, custom_state=custom_state, result=result):
                        environment = dict(os.environ, USERPROFILE=str(user_profile), VERIFY_TEST_PYTHON=sys.executable,
                                           VERIFY_TEST_ROOT=str(root), VERIFY_TEST_RESULT=result,
                                           VERIFY_TEST_TEMP_DIR=str(temporary))
                        environment.pop('LOOP_MANAGER_STATE_DIR', None)
                        if custom_state:
                            environment['LOOP_MANAGER_STATE_DIR'] = str(Path(directory) / 'custom-state')
                        for key in ('TEMP', 'TMP', 'TMPDIR'):
                            environment.pop(key, None)
                            if configured:
                                environment[key] = str(Path(directory) / key)
                        completed = subprocess.run(
                            [powershell, '-NoProfile', '-File', str(harness)],
                            cwd=directory, env=environment, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding='utf-8',
                            errors='replace', timeout=30,
                        )
                        self.assertEqual(completed.returncode, 0, completed.stdout)
                        self.assertTrue(temporary.is_dir())
                        if sentinel.exists():
                            self.assertEqual(sentinel.read_text(encoding='utf-8'), '보존')
                        else:
                            sentinel.write_text('보존', encoding='utf-8')


if __name__ == '__main__':
    unittest.main()
