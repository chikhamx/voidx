# voidx installer for Windows — downloads a standalone Python, creates an
# isolated venv, and installs voidx. No Python, pip, or npm required.
#
# Usage:
#   irm https://raw.githubusercontent.com/.../install.ps1 | iex
#   # or:
#   powershell -File install.ps1
#
# Environment variables:
#   $env:VOIDX_VERSION       — version to install (default: 2.1.0)
#   $env:VOIDX_HOME          — install directory (default: $env:LOCALAPPDATA\voidx)
#   $env:VOIDX_PYTHON_MIRROR — mirror for python-build-standalone downloads
#   $env:VOIDX_PIP_INDEX     — custom PyPI index URL

$ErrorActionPreference = "Stop"

# Force TLS 1.2+ — Windows PowerShell 5.1 defaults to TLS 1.0 which GitHub rejects.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Version = if ($env:VOIDX_VERSION) { $env:VOIDX_VERSION } else { "2.1.0" }
$PbsTag = "20260602"
$PbsCpython = "3.12.13"
$PbsReleaseBase = "https://github.com/astral-sh/python-build-standalone/releases/download"

# ── Platform detection ──────────────────────────────────────────────────────
# Use PROCESSOR_ARCHITECTURE env var — works on all PowerShell versions (5.1+).
# RuntimeInformation.OSArchitecture is unreliable on Windows PowerShell 5.1.
$ProcArch = $env:PROCESSOR_ARCHITECTURE
if ($ProcArch -eq "AMD64") { $PbsTarget = "x86_64-pc-windows-msvc" }
elseif ($ProcArch -eq "ARM64") { $PbsTarget = "aarch64-pc-windows-msvc" }
else {
    Write-Host "  ❌ Unsupported architecture: $ProcArch" -ForegroundColor Red
    Write-Host "     voidx supports: Windows x64/arm64" -ForegroundColor Red
    Write-Host "     PROCESSOR_ARCHITECTURE=$ProcArch" -ForegroundColor DarkGray
    exit 1
}

$PbsFilename = "cpython-$PbsCpython+$PbsTag-$PbsTarget-install_only_stripped.tar.gz"
$PbsMirror = if ($env:VOIDX_PYTHON_MIRROR) { $env:VOIDX_PYTHON_MIRROR } else { $PbsReleaseBase }
$PbsUrl = "$PbsMirror/$PbsTag/$PbsFilename"

# ── Paths ───────────────────────────────────────────────────────────────────
$VoidxHome = if ($env:VOIDX_HOME) { $env:VOIDX_HOME } else { Join-Path $env:LOCALAPPDATA "voidx" }
$PythonDir = Join-Path $VoidxHome "python"
$VenvDir = Join-Path $VoidxHome "venv"
$BundledPython = Join-Path $PythonDir "python\python.exe"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$VoidxBin = Join-Path $VenvDir "Scripts\voidx.exe"
$MarkerPath = Join-Path $VenvDir ".voidx-install-version"
$Marker = "$Version`n$PbsTag`n$PbsCpython`n"

# ── Legacy cleanup ──────────────────────────────────────────────────────────
# Remove voidx installed via system Python (pip/pipx) from v1.x era.
if (Get-Command pip -ErrorAction SilentlyContinue) {
    $PipResult = pip show voidx 2>$null
    if ($PipResult -and ($PipResult | Select-String "^Version:")) {
        $PipVersion = ($PipResult | Select-String "^Version:").Line.Split(" ")[1]
        Write-Host "  ⚠️  Found pip-installed voidx $PipVersion, uninstalling…" -ForegroundColor Yellow
        pip uninstall voidx -y 2>$null
        Write-Host "  ✅ Uninstalled pip-installed voidx" -ForegroundColor Green
    }
}
if (Get-Command pipx -ErrorAction SilentlyContinue) {
    $PipxResult = pipx list 2>$null
    if ($PipxResult -and ($PipxResult | Select-String "voidx")) {
        Write-Host "  ⚠️  Found pipx-installed voidx, uninstalling…" -ForegroundColor Yellow
        pipx uninstall voidx 2>$null
        Write-Host "  ✅ Uninstalled pipx-installed voidx" -ForegroundColor Green
    }
}

# ── Check if already installed ──────────────────────────────────────────────
if ((Test-Path $VoidxBin) -and (Test-Path $MarkerPath)) {
    $Existing = Get-Content $MarkerPath -Raw -ErrorAction SilentlyContinue
    if ($Existing -eq $Marker) {
        Write-Host "  ✅ voidx $Version already installed at $VenvDir" -ForegroundColor Green
        exit 0
    }
}

Write-Host ""
Write-Host "  🐍 Installing voidx $Version…" -ForegroundColor Cyan
Write-Host ""

# ── Step 1: Download Python ─────────────────────────────────────────────────
Write-Host "  [1/3] Setting up Python runtime" -ForegroundColor Yellow

if (Test-Path $BundledPython) {
    Write-Host "  ✅ Using cached Python runtime" -ForegroundColor Green
} else {
    $ArchivePath = Join-Path $PythonDir $PbsFilename
    New-Item -ItemType Directory -Path $PythonDir -Force | Out-Null

    if (-not (Test-Path $ArchivePath)) {
        Write-Host "    Downloading $PbsFilename…"

        $Downloaded = $false
        $Retries = 3
        for ($i = 1; $i -le $Retries; $i++) {
            try {
                $TmpPath = "$ArchivePath.tmp"
                $ProgressPreference = 'SilentlyContinue'
                Invoke-WebRequest -Uri $PbsUrl -OutFile $TmpPath -UseBasicParsing
                $ProgressPreference = 'Continue'
                # Verify the download is not empty / trivially small
                $DownloadSize = (Get-Item $TmpPath).Length
                if ($DownloadSize -lt 1MB) {
                    Remove-Item -Path $TmpPath -Force -ErrorAction SilentlyContinue
                    throw "Downloaded file is only $DownloadSize bytes — likely incomplete or a redirect page."
                }
                Move-Item -Path $TmpPath -Destination $ArchivePath -Force
                $Downloaded = $true
                break
            } catch {
                Remove-Item -Path "$ArchivePath.tmp" -Force -ErrorAction SilentlyContinue
                Write-Host "    Download attempt $i/$Retries failed: $_" -ForegroundColor Yellow
                if ($i -lt $Retries) {
                    $Delay = [Math]::Pow(2, $i)
                    Write-Host "    Retrying in ${Delay}s…" -ForegroundColor Yellow
                    Start-Sleep -Seconds $Delay
                }
            }
        }

        if (-not $Downloaded) {
            Write-Host ""
            Write-Host "  ❌ Failed to download Python runtime after $Retries attempts" -ForegroundColor Red
            Write-Host ""
            Write-Host "  This is usually a network issue. Try:" -ForegroundColor Red
            Write-Host "    1. Use a mirror: `$env:VOIDX_PYTHON_MIRROR='https://npmmirror.com/mirrors/python-standalone'"
            Write-Host "    2. Retry: powershell -File install.ps1"
            Write-Host "    3. If you're in China, also set: `$env:VOIDX_PIP_INDEX='https://pypi.tuna.tsinghua.edu.cn/simple'"
            exit 1
        }
    }

    Write-Host "    Extracting Python runtime…"
    # Use Stop-Parsing to avoid PowerShell interpreting special chars in paths.
    # Quote paths for tar in case they contain spaces (e.g. C:\Users\User Name\).
    $TarResult = & tar -xzf "$ArchivePath" -C "$PythonDir" 2>&1
    $TarExit = $LASTEXITCODE
    if ($TarExit -ne 0) {
        # Remove corrupted archive so retry will re-download
        Remove-Item -Path $ArchivePath -Force -ErrorAction SilentlyContinue
        Write-Host "  ❌ Failed to extract Python runtime (tar exit code $TarExit)" -ForegroundColor Red
        Write-Host "     $TarResult" -ForegroundColor DarkGray
        Write-Host "     The downloaded archive may be incomplete. Re-run the installer to retry." -ForegroundColor DarkGray
        exit 1
    }
    Remove-Item -Path $ArchivePath -Force -ErrorAction SilentlyContinue
    Write-Host "  ✅ Python runtime ready" -ForegroundColor Green
}

# ── Step 2: Create venv ────────────────────────────────────────────────────
Write-Host "  [2/3] Creating virtual environment" -ForegroundColor Yellow

# If venv exists but is corrupted, rebuild
if ((Test-Path $VenvDir) -and -not (Test-Path $VenvPython)) {
    Write-Host "    Existing venv is corrupted, rebuilding…" -ForegroundColor Yellow
    Remove-Item -Path $VenvDir -Recurse -Force -ErrorAction SilentlyContinue
}

if (-not (Test-Path $VenvPython)) {
    & $BundledPython -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ❌ Failed to create virtual environment" -ForegroundColor Red
        exit 1
    }
}

# Upgrade pip
& $VenvPython -m pip install --upgrade pip --no-cache-dir 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ⚠️  Failed to upgrade pip, continuing with current version" -ForegroundColor Yellow
}

Write-Host "  ✅ Virtual environment ready" -ForegroundColor Green

# ── Step 3: Install voidx ──────────────────────────────────────────────────
Write-Host "  [3/3] Installing voidx $Version" -ForegroundColor Yellow

$PipArgs = @("-m", "pip", "install", "--upgrade", "--no-cache-dir", "--progress-bar", "on")

if ($env:VOIDX_PIP_INDEX) {
    $PipArgs += @("-i", $env:VOIDX_PIP_INDEX)
    try {
        $IndexUri = [System.Uri]::new($env:VOIDX_PIP_INDEX)
        $PipArgs += @("--trusted-host", $IndexUri.Host)
    } catch {}
}

$PipArgs += @("voidx==$Version")

$env:PIP_NO_INPUT = "1"
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
$env:PYTHON_KEYRING_BACKEND = "keyring.backends.null.Keyring"

& $VenvPython $PipArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "  ❌ pip install failed" -ForegroundColor Red
    Write-Host ""
    Write-Host "  This is usually a network issue. Try:"
    Write-Host "    1. Use a PyPI mirror: `$env:VOIDX_PIP_INDEX='https://pypi.tuna.tsinghua.edu.cn/simple'"
    Write-Host "    2. Retry: powershell -File install.ps1"
    exit 1
}

# ── Write marker ────────────────────────────────────────────────────────────
Set-Content -Path $MarkerPath -Value $Marker -NoNewline

# ── Add to PATH ──────────────────────────────────────────────────────────────
$VoidxScriptsDir = Join-Path $VenvDir "Scripts"
$CurrentPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($CurrentPath -notlike "*$VoidxScriptsDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$CurrentPath;$VoidxScriptsDir", "User")
    $env:Path = "$env:Path;$VoidxScriptsDir"
    Write-Host "  ✅ Added $VoidxScriptsDir to user PATH" -ForegroundColor Green
}

# ── Done ────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  ✅ voidx $Version installed!" -ForegroundColor Green
Write-Host ""
Write-Host "  Run: voidx" -ForegroundColor Cyan
Write-Host "  (Restart your terminal if 'voidx' is not found)" -ForegroundColor DarkGray
