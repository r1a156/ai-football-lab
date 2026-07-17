$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectPath = "D:\ai-football-lab"
Set-Location $ProjectPath

git pull origin main

if ($LASTEXITCODE -ne 0) {
    throw "Ошибка git pull."
}

$Report = Get-Content `
    -Path "data\last-update-report.json" `
    -Raw `
    -Encoding UTF8 |
    ConvertFrom-Json

$State = Get-Content `
    -Path "data\state.json" `
    -Raw `
    -Encoding UTF8 |
    ConvertFrom-Json

function Get-OptionalProperty {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Object,

        [Parameter(Mandatory = $true)]
        [string]$Name,

        [object]$DefaultValue = "—"
    )

    if (
        $null -ne $Object -and
        $Object.PSObject.Properties.Name -contains $Name
    ) {
        return $Object.$Name
    }

    return $DefaultValue
}

$PredictionCount = @($State.predictions).Count
$HistoryCount = @($State.history).Count

$AnalysisModel = Get-OptionalProperty `
    -Object $State.meta `
    -Name "analysisModel" `
    -DefaultValue "Не вызывалась"

$Details = $Report.details

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " AI FOOTBALL LAB — РЕЗУЛЬТАТ PIPELINE" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

Write-Host "Статус pipeline:          $($Report.status)" -ForegroundColor Cyan
Write-Host "Сообщение:                $($Report.message)" -ForegroundColor White
Write-Host "Режим сайта:              $($State.meta.mode)" -ForegroundColor White
Write-Host "Статус анализа:           $($State.meta.analysisStatus)" -ForegroundColor Cyan
Write-Host "Модель:                   $AnalysisModel" -ForegroundColor White
Write-Host "Опубликовано прогнозов:   $PredictionCount" -ForegroundColor White
Write-Host "Записей истории:          $HistoryCount" -ForegroundColor White
Write-Host ""

if ($null -ne $Details) {
    Write-Host "Детали получения данных:" -ForegroundColor White

    foreach ($Property in $Details.PSObject.Properties) {
        Write-Host ("  {0}: {1}" -f $Property.Name, $Property.Value)
    }

    Write-Host ""
}

if ($PredictionCount -gt 0) {
    $State.predictions |
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
    Write-Host "Прогнозы пока не опубликованы." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "AI_FOOTBALL_LAB_PIPELINE_CHECK=GREEN" -ForegroundColor Green