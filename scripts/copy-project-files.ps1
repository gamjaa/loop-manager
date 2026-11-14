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
if ($conflicts) {
    throw "이미 존재하는 파일 또는 폴더가 있습니다: $($conflicts -join ', ')"
}

Copy-Item -LiteralPath (Join-Path $sourceRoot '.agents') -Destination (Join-Path $target '.agents') -Recurse

$directories = @(
    (Join-Path $target 'docs')
    (Join-Path $target 'docs/plans')
    (Join-Path $target 'docs/plans/archive')
    (Join-Path $target 'docs/requirements')
    (Join-Path $target 'docs/requirements/archive')
)
New-Item -ItemType Directory -Path $directories | Out-Null

Copy-Item -LiteralPath (Join-Path $sourceRoot 'docs/PLANS.md') -Destination (Join-Path $target 'docs/PLANS.md')
Set-Content -LiteralPath (Join-Path $target 'docs/FUTURE_IDEAS.md') -Value '# 후속 아이디어' -Encoding utf8
Get-ChildItem -LiteralPath (Join-Path $sourceRoot 'docs') -Filter '*.template.md' |
    Copy-Item -Destination (Join-Path $target 'docs')

Copy-Item -LiteralPath (Join-Path $sourceRoot 'docs/PRODUCT.template.md') -Destination (Join-Path $target 'docs/PRODUCT.md')
Copy-Item -LiteralPath (Join-Path $sourceRoot 'docs/ARCHITECTURE.template.md') -Destination (Join-Path $target 'docs/ARCHITECTURE.md')

Write-Output "Loop Manager 프로젝트 파일을 복사했습니다: $target"
