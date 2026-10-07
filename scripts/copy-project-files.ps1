[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$TargetPath
)

$ErrorActionPreference = 'Stop'
$sourceRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path

if (-not (Test-Path -LiteralPath $TargetPath -PathType Container)) {
    throw "대상 디렉터리가 존재하지 않습니다: $TargetPath"
}

$target = (Resolve-Path -LiteralPath $TargetPath).Path
$conflicts = @(
    (Join-Path $target '.agents')
    (Join-Path $target 'docs')
) | Where-Object { $null -ne (Get-Item -LiteralPath $_ -Force -ErrorAction SilentlyContinue) }
$agentsPath = Join-Path $target 'AGENTS.md'
$existingAgents = Get-Item -LiteralPath $agentsPath -Force -ErrorAction SilentlyContinue
if ($existingAgents -and ($existingAgents.PSIsContainer -or
        ($existingAgents.Attributes -band [IO.FileAttributes]::ReparsePoint))) {
    $conflicts += "$agentsPath (일반 파일이 아님)"
}

if ($conflicts) {
    throw "이미 존재하는 파일 또는 폴더가 있습니다: $($conflicts -join ', ')"
}

if (-not $existingAgents) {
    $minimalAgents = @'
# Project instructions

프로젝트 전용 언어·코딩·빌드 규칙은 이 파일에 기록한다.
문서 작성·검토 기준은 [Specification Skill](.agents/skills/specification-docs/SKILL.md)을 따른다.
'@
    $stream = [IO.File]::Open($agentsPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
    try {
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes($minimalAgents + "`n")
        $stream.Write($bytes, 0, $bytes.Length)
    } finally { $stream.Dispose() }
}
Copy-Item -LiteralPath (Join-Path $sourceRoot '.agents') -Destination (Join-Path $target '.agents') -Recurse

$directories = @(
    (Join-Path $target 'docs')
    (Join-Path $target 'docs/plans')
    (Join-Path $target 'docs/requirements')
    (Join-Path $target 'docs/requirements/archive')
)
New-Item -ItemType Directory -Path $directories | Out-Null

Copy-Item -LiteralPath (Join-Path $sourceRoot 'docs/PLANS.md') -Destination (Join-Path $target 'docs/PLANS.md')
Copy-Item -LiteralPath (Join-Path $sourceRoot 'docs/FUTURE_IDEAS.md') -Destination (Join-Path $target 'docs/FUTURE_IDEAS.md')
Get-ChildItem -LiteralPath (Join-Path $sourceRoot 'docs') -Filter '*.template.md' |
    Copy-Item -Destination (Join-Path $target 'docs')

Copy-Item -LiteralPath (Join-Path $sourceRoot 'docs/PRODUCT.template.md') -Destination (Join-Path $target 'docs/PRODUCT.md')
Copy-Item -LiteralPath (Join-Path $sourceRoot 'docs/ARCHITECTURE.template.md') -Destination (Join-Path $target 'docs/ARCHITECTURE.md')

Write-Output "Loop Manager 프로젝트 파일을 복사했습니다: $target"
