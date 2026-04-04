param(
    [string]$PythonVersion = "3.11",
    [string]$TorchIndexUrl = "https://download.pytorch.org/whl/cu128",
    [string]$TorchVersion = "2.8.0"
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvPath = Join-Path $ProjectRoot ".venv"
$PythonExe = Join-Path $VenvPath "Scripts\python.exe"
$PythonLauncher = $null
$PipCacheDir = Join-Path $ProjectRoot ".pip-cache"
$TempDir = Join-Path $ProjectRoot ".tmp"

Write-Host "Project root: $ProjectRoot"

New-Item -ItemType Directory -Force -Path $PipCacheDir | Out-Null
New-Item -ItemType Directory -Force -Path $TempDir | Out-Null

$env:PIP_CACHE_DIR = $PipCacheDir
$env:TEMP = $TempDir
$env:TMP = $TempDir

if (Get-Command py -ErrorAction SilentlyContinue) {
    $PythonLauncher = "py"
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $PythonLauncher = "python"
} else {
    throw "Neither py.exe nor python.exe was found. Install Python from https://www.python.org/downloads/windows/ first."
}

if ($PythonLauncher -eq "py") {
    & py -$PythonVersion -c "import sys; print(sys.version)" 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Python $PythonVersion is not installed for py.exe. Install Python $PythonVersion or pass -PythonVersion."
    }
} else {
    & python -c "import sys; print(sys.version)" 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "python.exe exists but could not be started."
    }
    $DetectedPythonVersion = (& python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
    if ($DetectedPythonVersion -ne $PythonVersion) {
        Write-Warning "Requested PythonVersion=$PythonVersion, but python.exe points to $DetectedPythonVersion. Continuing with python.exe."
    }
}

if (-not (Test-Path -LiteralPath $VenvPath)) {
    Write-Host "Creating virtual environment..."
    if ($PythonLauncher -eq "py") {
        & py -$PythonVersion -m venv $VenvPath
    } else {
        & python -m venv $VenvPath
    }
}

Write-Host "Upgrading pip/setuptools/wheel..."
& $PythonExe -m pip install --upgrade --no-cache-dir pip setuptools wheel

Write-Host "Installing PyTorch + torchaudio with CUDA wheels..."
& $PythonExe -m pip install --no-cache-dir "torch==$TorchVersion" "torchaudio==$TorchVersion" --index-url $TorchIndexUrl

Write-Host "Installing project in editable mode..."
& $PythonExe -m pip install --no-cache-dir -e $ProjectRoot

$ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
if ($null -eq $ffmpeg) {
    Write-Warning "ffmpeg was not found in PATH. Install ffmpeg before trying non-WAV references or chunked synthesis."
} else {
    Write-Host "ffmpeg: $($ffmpeg.Source)"
}

Write-Host ""
Write-Host "Bootstrap complete."
Write-Host "Next commands:"
Write-Host "  .\tts-win.ps1 --doctor"
Write-Host "  .\tts-win.ps1 `"Привет, мир`" .\output\hello.wav .\voices\reference.wav"
