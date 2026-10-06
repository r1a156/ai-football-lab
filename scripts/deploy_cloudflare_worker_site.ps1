$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$BuildScript = Join-Path $PSScriptRoot "cloudflare_pages_build.ps1"
$Out = Join-Path $Root "dist"
$WorkerName = "ai-football-site"
$CompatibilityDate = "2026-10-05"

Set-Location $Root

Write-Host "CLOUDFLARE_WORKER_SITE_DEPLOY_START"
Write-Host "WORKER=$WorkerName"
Write-Host "ROOT=$Root"

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $BuildScript
if ($LASTEXITCODE -ne 0) {
    throw "Static-site build failed with exit code $LASTEXITCODE"
}

$npx = Get-Command npx.cmd -ErrorAction SilentlyContinue
if (-not $npx) {
    throw "npx.cmd was not found. Install Node.js or make npm/npx available in PATH."
}

$wranglerLines = @(
    & npx.cmd wrangler@latest deploy --name $WorkerName --assets $Out --compatibility-date $CompatibilityDate 2>&1 |
        Tee-Object -Variable wranglerStream
)
$deployExit = $LASTEXITCODE
if ($deployExit -ne 0) {
    throw "Wrangler Worker static-assets deploy failed with exit code $deployExit"
}

$deploymentUrl = $null
foreach ($line in (@($wranglerStream) + @($wranglerLines))) {
    $text = [string]$line
    $match = [regex]::Match($text, 'https://[A-Za-z0-9.-]+\.workers\.dev')
    if ($match.Success) {
        $deploymentUrl = $match.Value.TrimEnd('/')
    }
}

if (-not $deploymentUrl) {
    throw "Worker deployed but Wrangler did not return a workers.dev URL."
}

Write-Host "CLOUDFLARE_WORKER_SITE_DEPLOY=GREEN"
Write-Host "WORKER=$WorkerName"
Write-Host "DEPLOYMENT_URL=$deploymentUrl"

$verifyOk = $false
$lastVerifyError = $null

for ($attempt = 1; $attempt -le 5; $attempt++) {
    try {
        $stamp = [DateTime]::UtcNow.Ticks
        $publicState = Invoke-RestMethod -UseBasicParsing -Uri "$deploymentUrl/data/state.json?v=$stamp" -Headers @{ "Cache-Control" = "no-cache" }
        $publicIndex = (Invoke-WebRequest -UseBasicParsing -Uri "$deploymentUrl/?v=$stamp" -Headers @{ "Cache-Control" = "no-cache" }).Content

        $publicAnalysis = @($publicState.dailyAnalysis).Count
        $publicExpresses = @($publicState.expresses).Count
        $newLoader = $publicIndex -match "15\.6\.2-strict-day-rollover"

        $expectedPublicExpresses = [Math]::Min(3, [Math]::Floor($publicAnalysis / 5))
        if ($publicAnalysis -ge 1 -and $publicAnalysis -le 15 -and $publicExpresses -eq $expectedPublicExpresses -and $newLoader) {
            $verifyOk = $true
            Write-Host "PUBLIC_VERIFY=GREEN"
            Write-Host "PUBLIC_ANALYSIS=$publicAnalysis"
            Write-Host "PUBLIC_EXPRESSES=$publicExpresses"
            Write-Host "PUBLIC_LOADER=15.6.2-strict-day-rollover"
            Write-Host "PUBLIC_URL=$deploymentUrl"
            break
        }

        $lastVerifyError = "analysis=$publicAnalysis expresses=$publicExpresses loader=$newLoader"
    }
    catch {
        $lastVerifyError = $_.Exception.Message
    }

    if ($attempt -lt 5) {
        Start-Sleep -Seconds 2
    }
}

if (-not $verifyOk) {
    throw "Worker static site deployed but public verification failed: $lastVerifyError"
}
