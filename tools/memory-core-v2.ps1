param([ValidateSet('preflight','ingest','compare','overview','ask','demo','metamorphic','continue')][string]$Mode='preflight',[string]$Question='')
$ErrorActionPreference='Stop'
$taskRoot=Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot
$taskPython=Join-Path $taskRoot 'outputs/runtime/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Run uv venv outputs/runtime and install requirements-memory-v2.lock into that environment first.' }
if (-not (Test-Path -LiteralPath 'outputs/private/corpus/snapshot-multiwindow.json')) { throw 'Missing local private frozen corpus; run the bounded freeze command in the report.' }
$env:PYTHONPATH=Join-Path $taskRoot 'src'
$env:HF_HUB_DISABLE_TELEMETRY='1'
$env:OTEL_SDK_DISABLED='true'
if ($Mode -eq 'continue') {
    foreach ($taskStage in @('ingest','compare','demo','metamorphic')) {
        & $taskPython -X utf8 -m allday_asr.memory_v2.cli $taskStage
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
} else {
    & $taskPython -X utf8 -m allday_asr.memory_v2.cli $Mode --question $Question
}
exit $LASTEXITCODE
