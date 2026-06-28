# voidx Python launcher — locates the venv Python under VOIDX_HOME and forwards
# all arguments. Resolves the same install directory as scripts/install.ps1.
#
# Usage:
#   .\python.ps1 -m pytest tests/ -v
#   .\python.ps1 scripts\package.py
#
# Environment:
#   $env:VOIDX_HOME — install directory (default: $env:LOCALAPPDATA\voidx)

$ErrorActionPreference = "Stop"

$VoidxHome = if ($env:VOIDX_HOME) { $env:VOIDX_HOME } else { Join-Path $env:LOCALAPPDATA "voidx" }
$Py = Join-Path $VoidxHome "venv\Scripts\python.exe"

if (-not (Test-Path $Py)) {
    Write-Host ""
    Write-Host "  ❌ voidx venv Python not found at $Py" -ForegroundColor Red
    Write-Host "     Run scripts/install.ps1 to create it, or set `$env:VOIDX_HOME to your install directory." -ForegroundColor DarkGray
    Write-Host ""
    exit 1
}

& $Py @args
exit $LASTEXITCODE
