import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == 'win32', 'Windows verify 스크립트 검증')
class VerifyWindowsTests(unittest.TestCase):
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
            temporary = root.parent / '.cache/verify/tmp'
            self.assertFalse(temporary.is_relative_to(root))
            sentinel = temporary / 'existing-file'
            harness = Path(directory) / 'check.ps1'
            harness.write_text(r'''
$ErrorActionPreference = 'Stop'
$originalLocation = (Get-Location).Path
$keys = @('TEMP', 'TMP', 'TMPDIR', 'PYTHONIOENCODING')
$before = @{}
foreach ($key in $keys) { $before[$key] = [Environment]::GetEnvironmentVariable($key, 'Process') }
$encoding = [Console]::OutputEncoding
function py {
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
$expected = Join-Path (Split-Path -Parent $env:VERIFY_TEST_ROOT) '.cache/verify/tmp'
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
            for configured in (False, True):
                for result in ('0', '7', 'throw'):
                    with self.subTest(configured=configured, result=result):
                        environment = dict(os.environ, VERIFY_TEST_PYTHON=sys.executable,
                                           VERIFY_TEST_ROOT=str(root), VERIFY_TEST_RESULT=result)
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
