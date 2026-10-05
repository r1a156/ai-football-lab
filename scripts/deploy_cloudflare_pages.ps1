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
