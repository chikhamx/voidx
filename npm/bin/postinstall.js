#!/usr/bin/env node
"use strict";

// Post-install: download a standalone Python, create venv, pip install voidx.
// Uses only the bundled Python — never falls back to the system Python.

const { createWriteStream, existsSync, mkdirSync, readFileSync, writeFileSync, unlinkSync, rmSync } = require("fs");
const { spawnSync } = require("child_process");
const { join, dirname } = require("path");
const os = require("os");
const fs = require("fs");
const path = require("path");

const pkg = require("../package.json");

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
  return path.join(resolveDataHome(env), "voidx", "npm-venv");
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

function writeMarker(markerPath, content) {
  mkdirSync(dirname(markerPath), { recursive: true });
  writeFileSync(markerPath, content);
}

// ── pip install ────────────────────────────────────────────────────────────
function pipInstall(venvPython, packageSpec, env) {
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

  const pipEnv = Object.assign({}, env, {
    PIP_NO_INPUT: "1",
    PIP_DISABLE_PIP_VERSION_CHECK: "1",
    PYTHON_KEYRING_BACKEND: "keyring.backends.null.Keyring",
  });

  const pipArgs = ["-m", "pip", "install", "--upgrade", "--no-cache-dir", "--progress-bar", "on"];

  // Support custom PyPI index for users behind firewalls or in regions with slow PyPI access
  const pipIndex = env.VOIDX_NPM_PIP_INDEX;
  if (pipIndex) {
    pipArgs.push("-i", pipIndex);
    // Extract host for --trusted-host when using a custom index
    try {
      const indexUrl = new URL(pipIndex);
      pipArgs.push("--trusted-host", indexUrl.hostname);
    } catch {}
  }

  pipArgs.push(packageSpec);

  const result = spawnSync(
    venvPython,
    pipArgs,
    { encoding: "utf8", stdio: "inherit", windowsHide: true, env: pipEnv }
  );
  if (result.error) {
    throw new Error(`pip install failed: ${result.error.message}`);
  }
  if (result.status !== 0) {
    throw new Error("pip install failed. See errors above.");
  }
}

// ── Main ───────────────────────────────────────────────────────────────────
async function main() {
  const env = process.env;
  const venvDir = resolveVenvDir(env);
  const venvPython = resolveVenvPython(venvDir);
  const executable = resolveVoidxExecutable(venvDir);
  const packageSpec = env.VOIDX_NPM_PACKAGE_SPEC || `voidx==${pkg.version}`;
  const markerPath = path.join(venvDir, ".voidx-npm-version");
  const marker = `${pkg.version}\n${packageSpec}\n${PBS_TAG}\n${PBS_CPYTHON}\n`;

  // Already set up and up-to-date?
  if (existsSync(executable) && readMarker(markerPath) === marker) {
    console.error(`\n✅ voidx ${pkg.version} ready (cached)\n`);
    return;
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
      try { fs.unlinkSync(archivePath); } catch {}
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

  // Step 3: pip install
  console.error("  [3/3] Installing voidx and dependencies…");
  pipInstall(venvPython, packageSpec, env);

  // Done
  writeMarker(markerPath, marker);
  console.error(`\n✅ voidx ${pkg.version} installed! Run: voidx\n`);
}

// ── URL builder (for mirror support) ──────────────────────────────────────

function buildPythonDownloadUrl(mirrorBase, tag, filename) {
  return `${mirrorBase}/${tag}/${filename}`;
}

main().catch((err) => {
  console.error(`\n❌ Setup failed: ${err.message}\n`);
  process.exit(1);
});

module.exports = {
  buildPythonDownloadUrl,
  downloadFileWithRetry,
};
