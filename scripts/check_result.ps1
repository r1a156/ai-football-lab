$ErrorActionPreference = "Stop"

$ProjectPath = "D:\ai-football-lab"
Set-Location $ProjectPath

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " AI FOOTBALL LAB - PIPELINE RESULT" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

Write-Host "[1/4] Pulling latest data from GitHub..." -ForegroundColor Yellow

git pull origin main

if ($LASTEXITCODE -ne 0) {
    throw "git pull failed."
}

$ReportPath = Join-Path $ProjectPath "data\last-update-report.json"
$StatePath = Join-Path $ProjectPath "data\state.json"

if (-not (Test-Path $ReportPath)) {
    throw "Missing file: data\last-update-report.json"
}

if (-not (Test-Path $StatePath)) {
    throw "Missing file: data\state.json"
}

Write-Host "[2/4] Reading JSON files..." -ForegroundColor Yellow

$Report = Get-Content `
    -Path $ReportPath `
    -Raw `
    -Encoding UTF8 |
    ConvertFrom-Json

$State = Get-Content `
    -Path $StatePath `
    -Raw `
    -Encoding UTF8 |
    ConvertFrom-Json

Write-Host "[3/4] JSON files loaded" -ForegroundColor Green

$Predictions = @()

if ($null -ne $State.predictions) {
    $Predictions = @($State.predictions)
}

$History = @()

if ($null -ne $State.history) {
    $History = @($State.history)
}

$PredictionCount = $Predictions.Count
$HistoryCount = $History.Count

$AnalysisModel = "NOT_CALLED"

if (
    $null -ne $State.meta -and
    $null -ne $State.meta.PSObject.Properties["analysisModel"]
) {
    $AnalysisModel = [string]$State.meta.analysisModel
}

$AnalysisStatus = "UNKNOWN"

if (
    $null -ne $State.meta -and
    $null -ne $State.meta.PSObject.Properties["analysisStatus"]
) {
    $AnalysisStatus = [string]$State.meta.analysisStatus
}

$Mode = "UNKNOWN"

if (
    $null -ne $State.meta -and
    $null -ne $State.meta.PSObject.Properties["mode"]
) {
    $Mode = [string]$State.meta.mode
}

$CurrentBank = "UNKNOWN"

if (
    $null -ne $State.bank -and
    $null -ne $State.bank.PSObject.Properties["current"]
) {
    $CurrentBank = [string]$State.bank.current
}

Write-Host ""
Write-Host "---------------- PIPELINE REPORT ----------------" -ForegroundColor White
Write-Host ("Pipeline status:       {0}" -f $Report.status) -ForegroundColor Cyan
Write-Host ("Message:               {0}" -f $Report.message) -ForegroundColor White
Write-Host ("Timestamp:             {0}" -f $Report.timestamp) -ForegroundColor White

Write-Host ""
Write-Host "---------------- SITE STATE ---------------------" -ForegroundColor White
Write-Host ("Mode:                  {0}" -f $Mode) -ForegroundColor Cyan
Write-Host ("Analysis status:       {0}" -f $AnalysisStatus) -ForegroundColor Cyan
Write-Host ("Analysis model:        {0}" -f $AnalysisModel) -ForegroundColor White
Write-Host ("Published predictions: {0}" -f $PredictionCount) -ForegroundColor White
Write-Host ("History records:       {0}" -f $HistoryCount) -ForegroundColor White
Write-Host ("Virtual bank:          {0}" -f $CurrentBank) -ForegroundColor White

if ($null -ne $Report.details) {
    Write-Host ""
    Write-Host "---------------- DATA DETAILS -------------------" -ForegroundColor White

    foreach ($Property in $Report.details.PSObject.Properties) {
        Write-Host ("{0}: {1}" -f $Property.Name, $Property.Value)
    }
}

if ($PredictionCount -gt 0) {
    Write-Host ""
    Write-Host "---------------- PREDICTIONS --------------------" -ForegroundColor Green

    $Predictions |
        Select-Object `
            date,
            time,
            league,
            home,
            away,
            pick,
            confidence,
            fairOdds,
            risk |
        Format-Table -AutoSize
}
else {
    Write-Host ""
    Write-Host "No predictions were published." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "[4/4] Check completed" -ForegroundColor Green
Write-Host ""
Write-Host "AI_FOOTBALL_LAB_PIPELINE_CHECK=GREEN" -ForegroundColor Green