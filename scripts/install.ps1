# voidx installer for Windows — downloads a standalone Python, creates an
# isolated venv, and installs voidx. No Python, pip, or npm required.
#
# Usage:
#   irm https://raw.githubusercontent.com/.../install.ps1 | iex
#   # or:
#   powershell -File install.ps1
#
# Environment variables:
#   $env:VOIDX_VERSION       — version to install (default: 2.3.0)
#   $env:VOIDX_HOME          — install directory (default: $env:LOCALAPPDATA\voidx)
#   $env:VOIDX_PYTHON_MIRROR — mirror for python-build-standalone downloads
#   $env:VOIDX_PIP_INDEX     — custom PyPI index URL

$ErrorActionPreference = "Stop"

# Suppress progress bars for faster downloads; restore at script exit.
$ProgressPreference = 'SilentlyContinue'

# Force TLS 1.2+ — Windows PowerShell 5.1 defaults to TLS 1.0 which GitHub rejects.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Version = if ($env:VOIDX_VERSION) { $env:VOIDX_VERSION } else { "3.1.2" }
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
$Marker = "$Version`n$PbsTag`n$PbsCpython`n$PbsTarget`n"

# ── Legacy cleanup ──────────────────────────────────────────────────────────
# Remove voidx installed via system Python (pip/pipx) from v1.x era.
# NOTE: try/catch wrappers are required for PowerShell 5.1 compatibility.
# In PS 5.1, redirecting a native commandʼs stderr with 2>$null wraps each
# line in a NativeCommandError, which $ErrorActionPreference="Stop" treats
# as a terminating error — killing the script. try/catch swallows this safely.
if (Get-Command pip -ErrorAction SilentlyContinue) {
    try { $PipResult = pip show voidx 2>$null } catch { $PipResult = $null }
    if ($PipResult -and ($PipResult | Select-String "^Version:")) {
        $PipVersion = ($PipResult | Select-String "^Version:").Line.Split(" ")[1]
        Write-Host "  ⚠️  Found pip-installed voidx $PipVersion, uninstalling…" -ForegroundColor Yellow
        try { pip uninstall voidx -y 2>$null } catch {}
        Write-Host "  ✅ Uninstalled pip-installed voidx" -ForegroundColor Green
    }
}
if (Get-Command pipx -ErrorAction SilentlyContinue) {
    try { $PipxResult = pipx list 2>$null } catch { $PipxResult = $null }
    if ($PipxResult -and ($PipxResult | Select-String "voidx")) {
        Write-Host "  ⚠️  Found pipx-installed voidx, uninstalling…" -ForegroundColor Yellow
        try { pipx uninstall voidx 2>$null } catch {}
        Write-Host "  ✅ Uninstalled pipx-installed voidx" -ForegroundColor Green
    }
}
if (Get-Command npm -ErrorAction SilentlyContinue) {
    try { $NpmResult = npm list -g @chikhamx/voidx 2>$null } catch { $NpmResult = $null }
    if ($NpmResult -and ($NpmResult | Select-String "@chikhamx/voidx@")) {
        $NpmVersion = ($NpmResult | Select-String "@chikhamx/voidx@").Line.Trim() -replace ".*@chikhamx/voidx@([^\s]+).*", '$1'
        Write-Host "  ⚠️  Found npm-installed voidx $NpmVersion, uninstalling…" -ForegroundColor Yellow
        try { npm uninstall -g @chikhamx/voidx 2>$null } catch {}
        Write-Host "  ✅ Uninstalled npm-installed voidx" -ForegroundColor Green
    }
}

# Old npm-venv directory from v2.x early releases
$OldNpmVenv = Join-Path $VoidxHome "npm-venv"
if (Test-Path $OldNpmVenv) {
    Write-Host "  ⚠️  Found old npm-venv directory, removing…" -ForegroundColor Yellow
    Remove-Item $OldNpmVenv -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "  ✅ Removed old npm-venv directory" -ForegroundColor Green
}

# Old voidx venv from previous installer runs — remove stale Scripts from PATH
$OldVenvScripts = Join-Path $VoidxHome "venv\Scripts"
if (Test-Path $OldVenvScripts) {
    $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($UserPath -like "*$OldVenvScripts*") {
        Write-Host "  ⚠️  Found old venv Scripts in user PATH, removing…" -ForegroundColor Yellow
        $Cleaned = ($UserPath -split ";" | Where-Object { $_ -ne $OldVenvScripts }) -join ";"
        [Environment]::SetEnvironmentVariable("Path", $Cleaned, "User")
        $env:Path = ($env:Path -split ";" | Where-Object { $_ -ne $OldVenvScripts }) -join ";"
        Write-Host "  ✅ Removed old venv Scripts from user PATH" -ForegroundColor Green
    }
}

# ── Ensure PATH and verify installation ──────────────────────────────────────
function Ensure-PathAndVerify {
    $VoidxScriptsDir = Join-Path $VenvDir "Scripts"
    $CurrentPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($CurrentPath -notlike "*$VoidxScriptsDir*") {
        # Prepend so the new voidx takes priority over any stale installations
        [Environment]::SetEnvironmentVariable("Path", "$VoidxScriptsDir;$CurrentPath", "User")
        $env:Path = "$VoidxScriptsDir;$env:Path"
        Write-Host "  ✅ Prepended $VoidxScriptsDir to user PATH" -ForegroundColor Green
    }

    # Remove conflicting voidx from PATH
    $FirstVoidx = Get-Command voidx -ErrorAction SilentlyContinue
    if ($FirstVoidx) {
        $FirstPath = if ($FirstVoidx.CommandType -eq "Application") { $FirstVoidx.Source } else { $FirstVoidx.Definition }
        if ($FirstPath -and ($FirstPath -ne $VoidxBin)) {
            Write-Host "  ⚠️  Found conflicting voidx: $FirstPath" -ForegroundColor Yellow

            # winget
            if (Get-Command winget -ErrorAction SilentlyContinue) {
                try { $WingetResult = winget list voidx 2>$null } catch { $WingetResult = $null }
                if ($WingetResult -and ($WingetResult | Select-String "voidx")) {
                    Write-Host "  ⚠️  Uninstalling winget-installed voidx…" -ForegroundColor Yellow
                    try { winget uninstall voidx 2>$null } catch {}
                    Write-Host "  ✅ Uninstalled winget-installed voidx" -ForegroundColor Green
                }
            }

            # scoop
            if (Get-Command scoop -ErrorAction SilentlyContinue) {
                try { $ScoopResult = scoop list voidx 2>$null } catch { $ScoopResult = $null }
                if ($ScoopResult -and ($ScoopResult | Select-String "voidx")) {
                    Write-Host "  ⚠️  Uninstalling scoop-installed voidx…" -ForegroundColor Yellow
                    try { scoop uninstall voidx 2>$null } catch {}
                    Write-Host "  ✅ Uninstalled scoop-installed voidx" -ForegroundColor Green
                }
            }

            # Remove stale exe in common locations
            $StaleLocations = @(
                (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\voidx.exe"),
                (Join-Path $env:LOCALAPPDATA "scoop\shims\voidx.exe")
            )
            foreach ($Stale in $StaleLocations) {
                if ((Test-Path $Stale) -and ($Stale -ne $VoidxBin)) {
                    Write-Host "  ⚠️  Removing stale: $Stale" -ForegroundColor Yellow
                    Remove-Item $Stale -Force -ErrorAction SilentlyContinue
                    Write-Host "  ✅ Removed $Stale" -ForegroundColor Green
                }
            }
        }
    }

    # Verify installation
    $ActualVersion = $null
    try {
        $VerOutput = & $VoidxBin --version 2>$null
        if ($VerOutput -match '(\d+\.\d+\.\d+)') {
            $ActualVersion = $Matches[1]
        }
    } catch {}

    if ($ActualVersion -and ($ActualVersion -ne $Version)) {
        Write-Host "  ⚠️  Installed version ($ActualVersion) does not match expected ($Version)" -ForegroundColor Yellow
        Write-Host "  ⚠️  Another voidx may be in PATH. Check:" -ForegroundColor Yellow
        $AllVoidx = Get-Command voidx -All -ErrorAction SilentlyContinue
        foreach ($V in $AllVoidx) {
            $RealPath = if ($V.CommandType -eq "Application") { $V.Source } else { $V.Definition }
            if ($RealPath -ne $VoidxBin) {
                Write-Host "  ⚠️    $RealPath" -ForegroundColor Yellow
            }
        }
        Write-Host "  ℹ️  Remove the old version or ensure $VoidxScriptsDir is first in PATH" -ForegroundColor Cyan
    } elseif ($ActualVersion) {
        Write-Host "  ✅ Version verified: voidx $ActualVersion" -ForegroundColor Green
    }
}

# ── Check if already installed ──────────────────────────────────────────────
if ((Test-Path $VoidxBin) -and (Test-Path $MarkerPath)) {
    $Existing = Get-Content $MarkerPath -Raw -ErrorAction SilentlyContinue
    if ($Existing -eq $Marker) {
        Write-Host "  ✅ voidx $Version already installed at $VenvDir" -ForegroundColor Green
        Ensure-PathAndVerify
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
                Invoke-WebRequest -Uri $PbsUrl -OutFile $TmpPath -UseBasicParsing
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
    try {
        $TarResult = & tar -xzf "$ArchivePath" -C "$PythonDir" 2>&1
    } catch {
        $TarResult = $_.Exception.Message
    }
    $TarExit = $LASTEXITCODE
    if ($TarExit -ne 0) {
        # Remove corrupted archive AND partially-extracted Python directory
        # so the next run re-downloads instead of reusing broken files.
        Remove-Item -Path $ArchivePath -Force -ErrorAction SilentlyContinue
        Remove-Item -Path $PythonDir -Recurse -Force -ErrorAction SilentlyContinue
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
    # Clear PYTHONPATH so the venv doesnʼt accidentally inherit system site-packages.
    $env:PYTHONPATH = ""
    & $BundledPython -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ❌ Failed to create virtual environment" -ForegroundColor Red
        exit 1
    }
}

# Upgrade pip
try { & $VenvPython -m pip install --upgrade pip --no-cache-dir 2>$null } catch {}
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
    Write-Host "  This is usually a network issue. Try:" -ForegroundColor Red
    Write-Host "    1. Use a PyPI mirror: `$env:VOIDX_PIP_INDEX='https://pypi.tuna.tsinghua.edu.cn/simple'"
    Write-Host "    2. Retry: powershell -File install.ps1"
    exit 1
}

# ── Write marker ────────────────────────────────────────────────────────────
Set-Content -Path $MarkerPath -Value $Marker -NoNewline

Ensure-PathAndVerify

Write-Host ""
Write-Host "  ✅ voidx $Version installed!" -ForegroundColor Green
Write-Host ""
Write-Host "  Run: voidx" -ForegroundColor Cyan
Write-Host "  (Restart your terminal if 'voidx' is not found)" -ForegroundColor DarkGray
