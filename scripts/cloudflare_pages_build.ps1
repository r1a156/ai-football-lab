$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Out = Join-Path $Root "dist"

Write-Host "CLOUDFLARE_PAGES_BUILD_START"
Write-Host "ROOT=$Root"

$statePath = Join-Path $Root "data\state.json"
$livePath = Join-Path $Root "data\live-state.json"

if (-not (Test-Path $statePath)) { throw "Missing required data file: data\state.json" }
if (-not (Test-Path $livePath)) { throw "Missing required data file: data\live-state.json" }

$state = Get-Content -Raw -Encoding UTF8 $statePath | ConvertFrom-Json
$analysisCount = @($state.dailyAnalysis).Count
$expressCount = @($state.expresses).Count
$invalidExpress = @($state.expresses | Where-Object { @($_.legs).Count -ne 5 }).Count

if ($analysisCount -lt 1 -or $analysisCount -gt 15) { throw "Refusing Pages build: expected 1..15 dailyAnalysis rows, found $analysisCount" }
$expectedExpressCount = [Math]::Min(3, [Math]::Floor($analysisCount / 5))
if ($expressCount -ne $expectedExpressCount) { throw "Refusing Pages build: expected $expectedExpressCount expresses for $analysisCount analyses, found $expressCount" }
if ($invalidExpress -ne 0) { throw "Refusing Pages build: every express must contain exactly 5 legs" }

if (Test-Path $Out) {
    Remove-Item $Out -Recurse -Force
}

New-Item -ItemType Directory -Force -Path $Out | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Out "assets") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Out "data") | Out-Null

Copy-Item (Join-Path $Root "index.html") (Join-Path $Out "index.html") -Force
Copy-Item (Join-Path $Root "assets\*") (Join-Path $Out "assets") -Recurse -Force

$requiredData = @(
    "state.json",
    "live-state.json"
)

$optionalData = @(
    "ai_daily_analysis.json",
    "last-update-report.json",
    "provider-health.json"
)

foreach ($name in $requiredData) {
    $source = Join-Path $Root ("data\" + $name)
    if (-not (Test-Path $source)) { throw "Missing required data file: $name" }
    Copy-Item $source (Join-Path $Out ("data\" + $name)) -Force
}

foreach ($name in $optionalData) {
    $source = Join-Path $Root ("data\" + $name)
    if (Test-Path $source) {
        Copy-Item $source (Join-Path $Out ("data\" + $name)) -Force
    }
}

$headers = @"
/index.html
  Cache-Control: no-store, no-cache, must-revalidate, max-age=0

/data/*
  Cache-Control: no-store, no-cache, must-revalidate, max-age=0

/assets/*
  Cache-Control: public, max-age=3600
"@

[System.IO.File]::WriteAllText(
    (Join-Path $Out "_headers"),
    $headers,
    (New-Object System.Text.UTF8Encoding($false))
)

New-Item -ItemType File -Force -Path (Join-Path $Out ".nojekyll") | Out-Null

$distState = Get-Content -Raw -Encoding UTF8 (Join-Path $Out "data\state.json") | ConvertFrom-Json
$distAnalysisCount = @($distState.dailyAnalysis).Count
$distExpressCount = @($distState.expresses).Count
$distExpectedExpressCount = [Math]::Min(3, [Math]::Floor($distAnalysisCount / 5))
if ($distAnalysisCount -lt 1 -or $distAnalysisCount -gt 15) { throw "Dist verification failed: dailyAnalysis" }
if ($distExpressCount -ne $distExpectedExpressCount) { throw "Dist verification failed: expresses" }

Write-Host "CLOUDFLARE_PAGES_BUILD=GREEN"
Write-Host "ANALYSIS=$analysisCount"
Write-Host "EXPRESSES=$expressCount"
Write-Host "OUTPUT_DIR=$Out"
