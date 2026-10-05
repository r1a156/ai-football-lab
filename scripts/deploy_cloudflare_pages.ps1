$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$BuildScript = Join-Path $PSScriptRoot "cloudflare_pages_build.ps1"
$Out = Join-Path $Root "dist"
$ProjectName = "ai-football-lab"

Set-Location $Root

Write-Host "CLOUDFLARE_PAGES_DEPLOY_START"
Write-Host "PROJECT=$ProjectName"
Write-Host "ROOT=$Root"

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $BuildScript
if ($LASTEXITCODE -ne 0) {
    throw "Cloudflare Pages build failed with exit code $LASTEXITCODE"
}

$npx = Get-Command npx.cmd -ErrorAction SilentlyContinue
if (-not $npx) {
    throw "npx.cmd was not found. Install Node.js or make npm/npx available in PATH."
}

& npx.cmd wrangler@latest pages deploy $Out --project-name $ProjectName --branch main --commit-dirty=true
if ($LASTEXITCODE -ne 0) {
    throw "Wrangler Pages deploy failed with exit code $LASTEXITCODE"
}

Write-Host "CLOUDFLARE_PAGES_DEPLOY=GREEN"
Write-Host "PROJECT=$ProjectName"

$PublicBase = "https://ai-football-lab.pages.dev"
$verifyOk = $false
$lastVerifyError = $null

for ($attempt = 1; $attempt -le 5; $attempt++) {
    try {
        $stamp = [DateTime]::UtcNow.Ticks
        $publicState = Invoke-RestMethod -UseBasicParsing -Uri "$PublicBase/data/state.json?v=$stamp" -Headers @{ "Cache-Control" = "no-cache" }
        $publicIndex = (Invoke-WebRequest -UseBasicParsing -Uri "$PublicBase/?v=$stamp" -Headers @{ "Cache-Control" = "no-cache" }).Content

        $publicAnalysis = @($publicState.dailyAnalysis).Count
        $publicExpresses = @($publicState.expresses).Count
        $newLoader = $publicIndex -match "15\.6\.1-state-failsafe"

        if ($publicAnalysis -eq 15 -and $publicExpresses -eq 3 -and $newLoader) {
            $verifyOk = $true
            Write-Host "PUBLIC_VERIFY=GREEN"
            Write-Host "PUBLIC_ANALYSIS=$publicAnalysis"
            Write-Host "PUBLIC_EXPRESSES=$publicExpresses"
            Write-Host "PUBLIC_LOADER=15.6.1-state-failsafe"
            break
        }

        $lastVerifyError = "analysis=$publicAnalysis expresses=$publicExpresses loader=$newLoader"
    }
    catch {
        $lastVerifyError = $_.Exception.Message
    }

    if ($attempt -lt 5) {
        Start-Sleep -Seconds 3
    }
}

if (-not $verifyOk) {
    throw "Cloudflare Pages deployed but public verification failed: $lastVerifyError"
}
