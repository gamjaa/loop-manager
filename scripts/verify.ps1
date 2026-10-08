$ErrorActionPreference = 'Stop'

$previousEncoding = [Console]::OutputEncoding
$previousPythonEncoding = $env:PYTHONIOENCODING
$previousTemp = $env:TEMP
$previousTmp = $env:TMP
$previousTmpDir = $env:TMPDIR
$root = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $root
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $env:PYTHONIOENCODING = 'utf-8'
    $tempDirectory = Join-Path $root '.cache/verify/tmp'
    New-Item -ItemType Directory -Path $tempDirectory -Force | Out-Null
    $env:TEMP = $tempDirectory
    $env:TMP = $tempDirectory
    $env:TMPDIR = $tempDirectory
    $venvPython = Join-Path $root '.venv/Scripts/python.exe'
    $pythonArguments = @()
    if (Test-Path -LiteralPath $venvPython) {
        $pythonCommand = $venvPython
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        $pythonCommand = 'py'
        $pythonArguments = @('-3')
    } else {
        $pythonCommand = 'python'
    }
    & $pythonCommand @pythonArguments -u -m loop_manager_internal.verification.runner
    $verifyExitCode = $LASTEXITCODE
} finally {
    [Console]::OutputEncoding = $previousEncoding
    $env:PYTHONIOENCODING = $previousPythonEncoding
    $env:TEMP = $previousTemp
    $env:TMP = $previousTmp
    $env:TMPDIR = $previousTmpDir
    Pop-Location
}
exit $verifyExitCode
