#!/usr/bin/env node
"use strict";

// Post-install: download a standalone Python, create venv, pip install voidx.
// Uses only the bundled Python — never falls back to the system Python.

const { createWriteStream, existsSync, mkdirSync, readFileSync, unlinkSync, rmSync } = require("fs");
const { spawnSync } = require("child_process");
const { join } = require("path");
const os = require("os");
const path = require("path");

const pkg = require("../package.json");
const {
  installVerifyAndRepair,
  resolveBundledCliWheel,
  verifyPair,
  writeMarkerAtomic,
} = require("./runtime-install");

// ── Python build metadata ──────────────────────────────────────────────────
// Pin to a specific python-build-standalone release tag and CPython version.
// Update these when upgrading the bundled Python.
const PBS_TAG = "20260602";
const PBS_CPYTHON = "3.12.13";
const PBS_RELEASE_BASE = `https://github.com/astral-sh/python-build-standalone/releases/download`;

// ── Platform mapping ───────────────────────────────────────────────────────
function getPlatformInfo() {
  const platform = os.platform();
  const arch = os.arch();

  const map = {
    "darwin-arm64":  { target: "aarch64-apple-darwin" },
    "darwin-x64":    { target: "x86_64-apple-darwin" },
    "linux-arm64":   { target: "aarch64-unknown-linux-gnu" },
    "linux-x64":     { target: "x86_64-unknown-linux-gnu" },
    "win32-arm64":   { target: "aarch64-pc-windows-msvc" },
    "win32-x64":     { target: "x86_64-pc-windows-msvc" },
  };

  const key = `${platform}-${arch}`;
  const entry = map[key];
  if (!entry) {
    return null;
  }
  return { platform, arch, target: entry.target };
}

function getPbsFilename(target) {
  return `cpython-${PBS_CPYTHON}+${PBS_TAG}-${target}-install_only_stripped.tar.gz`;
}

// ── Paths ──────────────────────────────────────────────────────────────────
function resolveDataHome(env) {
  if (env.VOIDX_NPM_HOME) return path.resolve(env.VOIDX_NPM_HOME);
  if (process.platform === "win32") {
    return env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local");
  }
  return env.XDG_DATA_HOME || path.join(os.homedir(), ".local", "share");
}

function resolvePythonDir(env) {
  return path.join(resolveDataHome(env), "voidx", "python");
}

function resolveVenvDir(env) {
  if (env.VOIDX_NPM_VENV) return path.resolve(env.VOIDX_NPM_VENV);
  // Use the same venv directory as install.sh — single environment, no duplicates
  return path.join(resolveDataHome(env), "voidx", "venv");
}

function resolveVenvPython(venvDir) {
  return process.platform === "win32"
    ? path.join(venvDir, "Scripts", "python.exe")
    : path.join(venvDir, "bin", "python");
}

function resolveVoidxExecutable(venvDir) {
  return process.platform === "win32"
    ? path.join(venvDir, "Scripts", "voidx.exe")
    : path.join(venvDir, "bin", "voidx");
}

function resolveBundledPython(pythonDir, platform) {
  // install_only_stripped layout:
  //   unix:   python/python/bin/python3
  //   win32:  python/python/python.exe
  const installDir = path.join(pythonDir, "python");
  if (platform === "win32") {
    return path.join(installDir, "python.exe");
  }
  return path.join(installDir, "bin", "python3");
}

// ── Legacy cleanup ──────────────────────────────────────────────────────────
// Remove voidx installed via system Python (pip/pipx) from v1.x era,
// and old npm-venv directory from v2.x early releases.
function cleanupLegacy(env) {
  const isWin = process.platform === "win32";

  // pip
  for (const cmd of isWin ? ["pip"] : ["pip3", "pip"]) {
    try {
      const result = spawnSync(cmd, ["show", "voidx"], {
        encoding: "utf8",
        windowsHide: true,
      });
      if (!result.error && result.status === 0 && (result.stdout || "").includes("Name: voidx")) {
        const versionMatch = (result.stdout || "").match(/Version:\s*(\S+)/);
        const version = versionMatch ? versionMatch[1] : "unknown";
        console.error(`  ⚠️  Found pip-installed voidx ${version} (${cmd}), uninstalling…`);
        const uninstallResult = spawnSync(cmd, ["uninstall", "voidx", "-y"], {
          encoding: "utf8",
          windowsHide: true,
          stdio: "inherit",
        });
        if (uninstallResult.status === 0) {
          console.error("  ✅ Uninstalled pip-installed voidx");
        } else {
          console.error(`  ⚠️  Failed to uninstall voidx via ${cmd}. Run manually: ${cmd} uninstall voidx`);
        }
      }
    } catch (err) {
      // spawnSync sets result.error for ENOENT (handled by status check above);
      // this catch only handles truly unexpected synchronous throws.
      console.error(`  ⚠️  Failed to check ${cmd}: ${err.message}`);
    }
  }

  // pipx
  try {
    const result = spawnSync("pipx", ["list"], { encoding: "utf8", windowsHide: true });
    if (!result.error && result.status === 0 && /^voidx\b/m.test(result.stdout || "")) {
      console.error("  ⚠️  Found pipx-installed voidx, uninstalling…");
      const uninstallResult = spawnSync("pipx", ["uninstall", "voidx"], {
        encoding: "utf8",
        windowsHide: true,
        stdio: "inherit",
      });
      if (uninstallResult.status === 0) {
        console.error("  ✅ Uninstalled pipx-installed voidx");
      } else {
        console.error("  ⚠️  Failed to uninstall voidx via pipx. Run manually: pipx uninstall voidx");
      }
    }
  } catch (err) {
    // spawnSync sets result.error for ENOENT (handled by status check above);
    // this catch only handles truly unexpected synchronous throws.
    console.error(`  ⚠️  Failed to check pipx: ${err.message}`);
  }

  // Old npm-venv directory
  const dataHome = resolveDataHome(env);
  const oldNpmVenv = path.join(dataHome, "voidx", "npm-venv");
  if (existsSync(oldNpmVenv)) {
    console.error("  ⚠️  Found old npm-venv directory, removing…");
    try {
      rmSync(oldNpmVenv, { recursive: true, force: true });
      console.error("  ✅ Removed old npm-venv directory");
    } catch (err) {
      console.error(`  Failed to remove old npm-venv: ${err.message}`);
    }
  }
}

// ── Download with retry ────────────────────────────────────────────────────
const MAX_DOWNLOAD_RETRIES = 3;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function downloadFileWithRetry(url, dest, retries = MAX_DOWNLOAD_RETRIES) {
  for (let attempt = 1; attempt <= retries; attempt++) {
    try {
      return await downloadFile(url, dest);
    } catch (err) {
      // Clean up partial download
      try { unlinkSync(dest); } catch {}
      if (attempt < retries) {
        const delay = Math.pow(2, attempt) * 1000;
        console.error(`    Download attempt ${attempt}/${retries} failed: ${err.message}`);
        console.error(`    Retrying in ${delay / 1000}s…`);
        await sleep(delay);
      } else {
        throw err;
      }
    }
  }
}

function downloadFile(url, dest) {
  return new Promise((resolve, reject) => {
    const doRequest = (currentUrl, redirects = 0) => {
      if (redirects > 5) {
        return reject(new Error(`Too many redirects downloading ${url}`));
      }
      const mod = currentUrl.startsWith("https") ? require("https") : require("http");
      mod.get(currentUrl, { timeout: 30000 }, (res) => {
        if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
          return doRequest(res.headers.location, redirects + 1);
        }
        if (res.statusCode !== 200) {
          return reject(new Error(`HTTP ${res.statusCode} downloading ${currentUrl}`));
        }
        const total = parseInt(res.headers["content-length"] || "0", 10);
        let downloaded = 0;
        let lastPercent = -1;

        res.on("data", (chunk) => {
          downloaded += chunk.length;
          if (total > 0) {
            const pct = Math.floor((downloaded / total) * 100);
            if (pct !== lastPercent && pct % 10 === 0) {
              lastPercent = pct;
              process.stderr.write(`    ${pct}% (${Math.round(downloaded / 1024 / 1024)}/${Math.round(total / 1024 / 1024)} MB)\n`);
            }
          }
        });

        const stream = createWriteStream(dest);
        res.pipe(stream);
        stream.on("finish", () => {
          stream.close();
          resolve(dest);
        });
        stream.on("error", reject);
        res.on("error", reject);
      }).on("error", reject);
    };
    doRequest(url);
  });
}

// ── Extract ────────────────────────────────────────────────────────────────
function extractTarGz(archive, destDir) {
  // Use the system tar command — available on macOS, Linux, and modern Windows (10+)
  const result = spawnSync("tar", ["-xzf", archive, "-C", destDir], {
    encoding: "utf8",
    windowsHide: true,
  });
  if (result.error) {
    throw new Error(`Failed to extract Python: ${result.error.message}`);
  }
  if (result.status !== 0) {
    throw new Error(`Failed to extract Python (tar exited ${result.status}).`);
  }
}

// ── Version marker ─────────────────────────────────────────────────────────
function readMarker(markerPath) {
  try { return readFileSync(markerPath, "utf8"); } catch { return ""; }
}

// ── pip setup ──────────────────────────────────────────────────────────────
function upgradePip(venvPython, env) {
  // Upgrade pip first to avoid resolver bugs in old versions
  const pipUpgradeEnv = Object.assign({}, env, {
    PIP_NO_INPUT: "1",
    PIP_DISABLE_PIP_VERSION_CHECK: "1",
    PYTHON_KEYRING_BACKEND: "keyring.backends.null.Keyring",
  });
  const pipUpgradeResult = spawnSync(
    venvPython,
    ["-m", "pip", "install", "--upgrade", "pip", "--no-cache-dir"],
    { encoding: "utf8", stdio: "inherit", windowsHide: true, env: pipUpgradeEnv }
  );
  if (pipUpgradeResult.error || pipUpgradeResult.status !== 0) {
    console.error("  ⚠️  Failed to upgrade pip, continuing with current version…");
  }
}

// ── Main ───────────────────────────────────────────────────────────────────
async function main() {
  const env = process.env;
  const venvDir = resolveVenvDir(env);
  const venvPython = resolveVenvPython(venvDir);
  const executable = resolveVoidxExecutable(venvDir);
  const packageSpec = env.VOIDX_NPM_PACKAGE_SPEC || `voidx==${pkg.version}`;
  const markerPath = path.join(venvDir, ".voidx-install-version");
  const marker = `${pkg.version}\n${PBS_TAG}\n${PBS_CPYTHON}\n`;

  // ── Legacy cleanup ────────────────────────────────────────────────────────
  // Remove voidx installed via system Python (pip/pipx) from v1.x era.
  // Also remove old npm-venv directory from v2.x early releases.
  // Runs before marker check so legacy cleanup happens even when already installed.
  cleanupLegacy(env);

  // Already set up and up-to-date?
  if (existsSync(executable) && readMarker(markerPath) === marker) {
    const verification = verifyPair({
      venvPython,
      executable,
      expectedVersion: pkg.version,
      env,
    });
    if (verification.ok) {
      console.error(`\n✅ voidx ${pkg.version} ready (cached)\n`);
      return;
    }
    console.error(`  ⚠️  Cached environment is invalid: ${verification.message}`);
  }

  // Step 1: Download bundled Python (required — no system Python fallback)
  const platformInfo = getPlatformInfo();
  if (!platformInfo) {
    console.error(`\n  ❌ Unsupported platform: ${os.platform()}-${os.arch()}\n`);
    console.error("     voidx npm package supports: macOS (x64/arm64), Linux (x64/arm64), Windows (x64/arm64)\n");
    process.exit(1);
  }

  console.error(`\n🐍 Setting up voidx ${pkg.version}…\n`);
  console.error("  [1/3] Downloading Python runtime…");

  const pythonDir = resolvePythonDir(env);
  const pbsFilename = getPbsFilename(platformInfo.target);
  const pbsUrlBase = env.VOIDX_NPM_PYTHON_MIRROR || PBS_RELEASE_BASE;
  const pbsUrl = `${pbsUrlBase}/${PBS_TAG}/${pbsFilename}`;
  const archivePath = path.join(pythonDir, pbsFilename);
  const bundledPython = resolveBundledPython(pythonDir, platformInfo.platform);

  if (!existsSync(bundledPython)) {
    try {
      mkdirSync(pythonDir, { recursive: true });

      if (!existsSync(archivePath)) {
        console.error(`    Downloading ${pbsFilename}…`);
        await downloadFileWithRetry(pbsUrl, archivePath);
        console.error("    Download complete.");
      }

      console.error("    Extracting Python runtime…");
      extractTarGz(archivePath, pythonDir);
      console.error("    Extraction complete.");

      // Clean up archive to save disk
      try { unlinkSync(archivePath); } catch {}
    } catch (err) {
      // Clean up partial archive on failure
      try { unlinkSync(archivePath); } catch {}
      console.error(`\n  ❌ Failed to download Python runtime: ${err.message}\n`);
      console.error("  This is usually a network issue. Try:");
      console.error("    1. Use a mirror: VOIDX_NPM_PYTHON_MIRROR=https://npmmirror.com/mirrors/python-standalone");
      console.error("    2. Retry: npm install -g @chikhamx/voidx");
      console.error("    3. Debug: VOIDX_NPM_DEBUG=1 npm install -g @chikhamx/voidx\n");
      process.exit(1);
    }
  } else {
    console.error("    Using cached Python runtime.");
  }

  // Step 2: Create venv (rebuild if corrupted)
  console.error("  [2/3] Creating virtual environment…");

  // If venv exists but is corrupted (python binary missing), nuke and rebuild
  if (existsSync(venvDir) && !existsSync(venvPython)) {
    console.error("    Existing venv is corrupted, rebuilding…");
    try {
      rmSync(venvDir, { recursive: true, force: true });
    } catch (err) {
      console.error(`    Failed to remove corrupted venv: ${err.message}`);
    }
  }

  if (!existsSync(venvPython)) {
    const venvResult = spawnSync(
      bundledPython,
      ["-m", "venv", venvDir],
      { encoding: "utf8", stdio: "inherit", windowsHide: true }
    );
    if (venvResult.error) {
      console.error(`\n  ❌ Failed to create venv: ${venvResult.error.message}\n`);
      process.exit(1);
    }
    if (venvResult.status !== 0) {
      console.error("\n  ❌ Failed to create venv. See errors above.\n");
      process.exit(1);
    }
  }

  // Step 3: install and verify the exact core/CLI pair
  console.error("  [3/3] Installing and verifying voidx…");
  upgradePip(venvPython, env);
  const npmDir = path.resolve(__dirname, "..");
  const wheelPath = resolveBundledCliWheel(npmDir, pkg.version);
  installVerifyAndRepair({
    venvPython,
    executable,
    coreSpec: packageSpec,
    cliSpec: wheelPath,
    expectedVersion: pkg.version,
    env,
  });

  // Done
  writeMarkerAtomic(markerPath, marker);
  console.error(`\n✅ voidx ${pkg.version} installed! Run: voidx\n`);
}

// ── URL builder (for mirror support) ──────────────────────────────────────

function buildPythonDownloadUrl(mirrorBase, tag, filename) {
  return `${mirrorBase}/${tag}/${filename}`;
}

if (require.main === module) {
  main().catch((err) => {
    console.error(`\n❌ Setup failed: ${err.message}\n`);
    process.exit(1);
  });
}

module.exports = {
  buildPythonDownloadUrl,
  downloadFileWithRetry,
};
