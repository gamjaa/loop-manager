$ErrorActionPreference = 'Stop'

$previousEncoding = [Console]::OutputEncoding
$previousPythonEncoding = $env:PYTHONIOENCODING
$root = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $root
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $env:PYTHONIOENCODING = 'utf-8'
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
    Pop-Location
}
exit $verifyExitCode
