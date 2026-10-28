@echo off

setlocal

powershell -NoProfile -ExecutionPolicy Bypass -Command "if (-not (Get-NetTCPConnection -LocalPort 7100 -State Listen -ErrorAction SilentlyContinue)) { exit 1 }" >nul 2>&1

if errorlevel 1 (

    echo Flixer is not running. Please start Flixer first.

    exit /b 1

)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$r = Invoke-RestMethod -Uri 'http://127.0.0.1:7100/api/cache/clear' -Method Post -TimeoutSec 600; if ($r.ok) { Write-Host ('Deleted: ' + $r.data.deleted) ; exit 0 } else { Write-Host ('Failed: ' + $r.error); exit 1 }"

exit /b %errorlevel%

