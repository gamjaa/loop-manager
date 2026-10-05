$ErrorActionPreference = 'Stop'

$previousEncoding = [Console]::OutputEncoding
$previousPythonEncoding = $env:PYTHONIOENCODING
$root = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $root
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $env:PYTHONIOENCODING = 'utf-8'
    $venvPython = Join-Path $root '.venv/Scripts/python.exe'
    if (Test-Path -LiteralPath $venvPython) {
        & $venvPython -u -m loop_manager_internal.verification.runner
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        py -3 -u -m loop_manager_internal.verification.runner
    } else {
        python -u -m loop_manager_internal.verification.runner
    }
    $verifyExitCode = $LASTEXITCODE
} finally {
    [Console]::OutputEncoding = $previousEncoding
    $env:PYTHONIOENCODING = $previousPythonEncoding
    Pop-Location
}
exit $verifyExitCode
