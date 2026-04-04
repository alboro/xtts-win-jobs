param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$DataRoot = Join-Path $ProjectRoot ".data"

New-Item -ItemType Directory -Force -Path $DataRoot | Out-Null
$env:XDG_DATA_HOME = $DataRoot
$env:TEMP = Join-Path $ProjectRoot ".tmp"
$env:TMP = $env:TEMP
$env:COQUI_TOS_AGREED = "1"
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null

if (-not (Test-Path -LiteralPath $PythonExe)) {
    Write-Error "Virtual environment not found at '.venv'. Run '.\scripts\bootstrap_windows.ps1' first."
    exit 1
}

& $PythonExe -m tts_win @Rest
exit $LASTEXITCODE
