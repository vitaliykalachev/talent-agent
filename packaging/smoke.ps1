# Дымовой тест распакованной сборки: запуск через «Запустить.bat», как у клиента.
param([Parameter(Mandatory)][string]$Dir)
$ErrorActionPreference = "Stop"
$log = Join-Path $env:RUNNER_TEMP "agent.log"
$bat = Join-Path $Dir "Запустить.bat"
$cmd = Start-Process cmd.exe -PassThru -WindowStyle Hidden `
    -ArgumentList "/c", "`"`"$bat`" < NUL > `"$log`" 2>&1`""
try {
    python (Join-Path $PSScriptRoot "smoke.py") $Dir $log
    $code = $LASTEXITCODE
    $py = Get-Process python -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -like "$Dir*" }
    $peak = ($py | Measure-Object PeakWorkingSet64 -Maximum).Maximum
    Write-Host ("Пик памяти python.exe: {0:N0} МБ" -f ($peak / 1MB))
} finally {
    taskkill /T /F /PID $cmd.Id | Out-Null
    Write-Host "--- вывод окна агента ---"
    Get-Content $log -Encoding utf8
}
exit $code
