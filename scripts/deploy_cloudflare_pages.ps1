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

$wranglerOutput = @(
    & npx.cmd wrangler@latest pages deploy $Out --project-name $ProjectName --branch main --commit-dirty=true 2>&1 |
        Tee-Object -Variable wranglerStream
)
$deployExit = $LASTEXITCODE
if ($deployExit -ne 0) {
    throw "Wrangler Pages deploy failed with exit code $deployExit"
}

$deploymentUrl = $null
$allWranglerLines = @($wranglerStream) + @($wranglerOutput)
foreach ($line in $allWranglerLines) {
    $text = [string]$line
    $match = [regex]::Match($text, 'https://[A-Za-z0-9-]+\.ai-football-lab\.pages\.dev')
    if ($match.Success) {
        $deploymentUrl = $match.Value
    }
}
if (-not $deploymentUrl) {
    $deploymentUrl = "https://ai-football-lab.pages.dev"
}

Write-Host "CLOUDFLARE_PAGES_DEPLOY=GREEN"
Write-Host "PROJECT=$ProjectName"
Write-Host "DEPLOYMENT_URL=$deploymentUrl"

$PublicBases = @(
    $deploymentUrl,
    "https://ai-football-lab.pages.dev"
) | Select-Object -Unique

$verifyOk = $false
$lastVerifyError = $null
$verifiedBase = $null

foreach ($PublicBase in $PublicBases) {
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        try {
            $stamp = [DateTime]::UtcNow.Ticks
            $publicState = Invoke-RestMethod -UseBasicParsing -Uri "$PublicBase/data/state.json?v=$stamp" -Headers @{ "Cache-Control" = "no-cache" }
            $publicIndex = (Invoke-WebRequest -UseBasicParsing -Uri "$PublicBase/?v=$stamp" -Headers @{ "Cache-Control" = "no-cache" }).Content

            $publicAnalysis = @($publicState.dailyAnalysis).Count
            $publicExpresses = @($publicState.expresses).Count
            $newLoader = $publicIndex -match "15\.6\.2-strict-day-rollover"

            $expectedPublicExpresses = [Math]::Min(3, [Math]::Floor($publicAnalysis / 5))
        if ($publicAnalysis -ge 1 -and $publicAnalysis -le 15 -and $publicExpresses -eq $expectedPublicExpresses -and $newLoader) {
                $verifyOk = $true
                $verifiedBase = $PublicBase
                Write-Host "PUBLIC_VERIFY=GREEN"
                Write-Host "PUBLIC_BASE=$verifiedBase"
                Write-Host "PUBLIC_ANALYSIS=$publicAnalysis"
                Write-Host "PUBLIC_EXPRESSES=$publicExpresses"
                Write-Host "PUBLIC_LOADER=15.6.2-strict-day-rollover"
                break
            }

            $lastVerifyError = "$PublicBase analysis=$publicAnalysis expresses=$publicExpresses loader=$newLoader"
        }
        catch {
            $lastVerifyError = "$PublicBase $($_.Exception.Message)"
        }

        if ($attempt -lt 5) {
            Start-Sleep -Seconds 3
        }
    }
    if ($verifyOk) {
        break
    }
}

if (-not $verifyOk) {
    throw "Cloudflare Pages deployed but public verification failed on deployment and canonical URLs: $lastVerifyError"
}
