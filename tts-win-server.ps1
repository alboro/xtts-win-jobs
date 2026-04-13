param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir = Join-Path $ProjectRoot ".venv"
$PythonExe = Join-Path $VenvDir "Scripts\python.exe"
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

function Resolve-Runner {
    $cfgPath = Join-Path $VenvDir "pyvenv.cfg"
    $sitePackages = Join-Path $VenvDir "Lib\site-packages"
    $basePython = $null

    if (Test-Path -LiteralPath $cfgPath) {
        foreach ($line in Get-Content -LiteralPath $cfgPath -Encoding UTF8) {
            if ($line -match '^\s*executable\s*=\s*(.+?)\s*$') {
                $basePython = $matches[1].Trim()
                break
            }
            if ($line -match '^\s*home\s*=\s*(.+?)\s*$' -and -not $basePython) {
                $candidate = Join-Path $matches[1].Trim() "python.exe"
                $basePython = $candidate
            }
        }
    }

    if ($basePython -and (Test-Path -LiteralPath $basePython) -and (Test-Path -LiteralPath $sitePackages)) {
        $existingPythonPath = $env:PYTHONPATH
        $extraPaths = @((Join-Path $ProjectRoot "src"), $sitePackages)
        if ($existingPythonPath) {
            $extraPaths += $existingPythonPath
        }
        $env:PYTHONPATH = ($extraPaths -join ";")
        $env:VIRTUAL_ENV = $VenvDir
        return $basePython
    }

    return $PythonExe
}

$Runner = Resolve-Runner
& $Runner -m tts_win.server @Rest
exit $LASTEXITCODE
