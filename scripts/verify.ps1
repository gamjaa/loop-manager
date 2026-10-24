$ErrorActionPreference = 'Stop'

$previousEncoding = [Console]::OutputEncoding
$previousPythonEncoding = $env:PYTHONIOENCODING
$previousTemp = $env:TEMP
$previousTmp = $env:TMP
$previousTmpDir = $env:TMPDIR
$previousVerifyTempDir = $env:LOOP_MANAGER_VERIFY_TEMP_DIR
$root = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $root
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $env:PYTHONIOENCODING = 'utf-8'
    $tempDirectoryInput = $env:LOOP_MANAGER_VERIFY_TEMP_DIR
    if ($null -eq $tempDirectoryInput) {
        $tempDirectoryInput = Join-Path $env:USERPROFILE '.cache/loop-manager/verify/tmp'
    }
    if ([string]::IsNullOrWhiteSpace($tempDirectoryInput)) {
        throw 'Temporary directory input is empty.'
    }
    try {
        if ([System.IO.Path]::IsPathRooted($tempDirectoryInput)) {
            $tempDirectory = [System.IO.Path]::GetFullPath($tempDirectoryInput)
        } else {
            $tempDirectory = [System.IO.Path]::GetFullPath((Join-Path $root $tempDirectoryInput))
        }
        New-Item -ItemType Directory -Path $tempDirectory -Force | Out-Null
        if (-not (Test-Path -LiteralPath $tempDirectory -PathType Container)) {
            throw 'Temporary path is not a directory.'
        }
        $probePath = Join-Path $tempDirectory ('.loop-manager-verify-' + [Guid]::NewGuid().ToString('N'))
        $probe = $null
        try {
            $probe = [System.IO.File]::Open($probePath, [System.IO.FileMode]::CreateNew,
                [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
            $probe.Dispose()
            $probe = $null
            Remove-Item -LiteralPath $probePath -ErrorAction Stop
        } finally {
            if ($null -ne $probe) { $probe.Dispose() }
            if (Test-Path -LiteralPath $probePath) {
                Remove-Item -LiteralPath $probePath -Force -ErrorAction SilentlyContinue
            }
        }
    } catch {
        throw "Loop Manager verify temporary directory error ($tempDirectoryInput): $($_.Exception.Message)"
    }
    $env:LOOP_MANAGER_VERIFY_TEMP_DIR = $tempDirectory
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
    & $pythonCommand @pythonArguments -u (Join-Path $PSScriptRoot 'verify_windows.py')
    $verifyExitCode = $LASTEXITCODE
} finally {
    [Console]::OutputEncoding = $previousEncoding
    $env:PYTHONIOENCODING = $previousPythonEncoding
    $env:TEMP = $previousTemp
    $env:TMP = $previousTmp
    $env:TMPDIR = $previousTmpDir
    $env:LOOP_MANAGER_VERIFY_TEMP_DIR = $previousVerifyTempDir
    Pop-Location
}
exit $verifyExitCode
