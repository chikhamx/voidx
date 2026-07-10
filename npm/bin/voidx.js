#!/usr/bin/env node
"use strict";

const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const pkg = require("../package.json");
const {
  installVerifyAndRepair,
  resolveBundledCliWheel,
  verifyPair,
  writeMarkerAtomic,
} = require("./runtime-install");

// ── Configuration ──────────────────────────────────────────────────────────

const PBS_TAG = "20260602";
const PBS_CPYTHON = "3.12.13";
const PBS_PYTHON_MAJOR = "3.12";

// ── Main ───────────────────────────────────────────────────────────────────

function main(argv = process.argv.slice(2), env = process.env) {
  try {
    const python = selectPython(env);
    const venvDir = resolveVenvDir(env);
    ensureVenv(python, venvDir, env);
    const executable = resolveVoidxExecutable(venvDir);
    const childEnv = {
      ...env,
      VOIDX_LAUNCHED_BY_NPM: "1",
      VOIDX_NPM_PACKAGE_VERSION: pkg.version,
    };
    const child = spawn(executable, argv, { stdio: "inherit", env: childEnv });
    child.on("exit", (code, signal) => {
      if (signal) {
        process.kill(process.pid, signal);
        return;
      }
      process.exit(code === null ? 1 : code);
    });
    child.on("error", (error) => {
      fail(`Failed to start voidx: ${error.message}`);
    });
  } catch (error) {
    fail(error.message);
  }
}

// ── Python selection ───────────────────────────────────────────────────────

function selectPython(env) {
  // 1. Explicit override (for advanced users / debugging)
  const explicit = env.VOIDX_PYTHON;
  if (explicit) {
    const candidate = { command: explicit, args: [], label: explicit };
    const probe = probePython(candidate);
    if (!probe.ok) {
      throw new Error(probe.reason || `Unable to run Python at ${explicit}.`);
    }
    if (!isCompatible(probe.version)) {
      throw new Error(
        `voidx requires Python 3.11+. Found Python ${probe.versionText} at ${explicit}.`
      );
    }
    return candidate;
  }

  // 2. Bundled Python only — voidx runs in its own isolated environment
  const bundledBin = resolveBundledPythonBin(env);
  if (bundledBin && fs.existsSync(bundledBin)) {
    const candidate = { command: bundledBin, args: [], label: "bundled" };
    const probe = probePython(candidate);
    if (probe.ok && isCompatible(probe.version)) {
      return candidate;
    }
  }

  // 3. Bundled Python not found — try to bootstrap it (postinstall may have failed)
  if (env.VOIDX_NPM_SKIP_BOOTSTRAP !== "1") {
    console.error("\n⚙️  Bundled Python not found, running setup…\n");
    const postinstallScript = path.join(path.dirname(__filename), "postinstall.js");
    if (fs.existsSync(postinstallScript)) {
      const result = spawnSync(process.execPath, [postinstallScript], {
        stdio: "inherit",
        windowsHide: true,
        env: { ...env },
      });
      if (result.status !== 0) {
        console.error("  Setup failed. Try reinstalling:");
        console.error("    npm install -g @chikhamx/voidx");
      }
    }
  }

  // Retry after bootstrap
  if (bundledBin && fs.existsSync(bundledBin)) {
    const candidate = { command: bundledBin, args: [], label: "bundled" };
    const probe = probePython(candidate);
    if (probe.ok && isCompatible(probe.version)) {
      return candidate;
    }
  }

  throw new Error(
    "voidx bundled Python not found. Reinstall to set up the isolated runtime:\n" +
    "  npm install -g @chikhamx/voidx\n" +
    "  or: curl -fsSL https://raw.githubusercontent.com/chikhamx/voidx/master/scripts/install.sh | bash"
  );
}

function probePython(candidate) {
  const code = [
    "import sys",
    "print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')",
  ].join("; ");
  const result = spawnSync(candidate.command, [...candidate.args, "-c", code], {
    encoding: "utf8",
    windowsHide: true,
  });
  if (result.error) {
    return { ok: false, reason: result.error.message };
  }
  if (result.status !== 0) {
    return { ok: false, reason: (result.stderr || "").trim() };
  }
  const versionText = (result.stdout || "").trim();
  const version = parseVersion(versionText);
  if (!version) {
    return { ok: false, reason: `Unable to parse Python version: ${versionText}` };
  }
  return { ok: true, version, versionText };
}

function parseVersion(value) {
  const match = /^(\d+)\.(\d+)\.(\d+)/.exec(value);
  if (!match) {
    return null;
  }
  return match.slice(1).map((part) => Number.parseInt(part, 10));
}

function isCompatible(version) {
  return version[0] > 3 || (version[0] === 3 && version[1] >= 11);
}

// ── Paths ──────────────────────────────────────────────────────────────────

function resolveDataHome(env) {
  if (env.VOIDX_NPM_HOME) {
    return path.resolve(env.VOIDX_NPM_HOME);
  }
  if (process.platform === "win32") {
    return env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local");
  }
  return env.XDG_DATA_HOME || path.join(os.homedir(), ".local", "share");
}

function resolvePythonDir(env) {
  if (env.VOIDX_NPM_PYTHON_DIR) {
    return path.resolve(env.VOIDX_NPM_PYTHON_DIR);
  }
  return path.join(resolveDataHome(env), "voidx", "python");
}

function resolveBundledPythonBin(env) {
  const pythonDir = resolvePythonDir(env);
  return process.platform === "win32"
    ? path.join(pythonDir, "python", "python.exe")
    : path.join(pythonDir, "python", "bin", "python3");
}

function resolveVenvDir(env) {
  if (env.VOIDX_NPM_VENV) {
    return path.resolve(env.VOIDX_NPM_VENV);
  }
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

// ── Venv setup ─────────────────────────────────────────────────────────────

function ensureVenv(python, venvDir, env) {
  const executable = resolveVoidxExecutable(venvDir);
  const venvPython = resolveVenvPython(venvDir);
  if (env.VOIDX_NPM_SKIP_BOOTSTRAP === "1") {
    if (!fs.existsSync(executable)) {
      throw new Error(`voidx executable not found in ${venvDir}.`);
    }
    return;
  }

  const markerPath = path.join(venvDir, ".voidx-install-version");
  const marker = `${pkg.version}\n${PBS_TAG}\n${PBS_CPYTHON}\n`;
  let needsInstall = !fs.existsSync(executable) || readMarker(markerPath) !== marker;
  if (fs.existsSync(executable) && readMarker(markerPath) === marker) {
    const verification = verifyPair({
      venvPython,
      executable,
      expectedVersion: pkg.version,
      env,
    });
    if (verification.ok) {
      debug(env, `Using cached environment at ${venvDir}`);
      return;
    }
    console.error(`  Cached environment is invalid: ${verification.message}`);
    needsInstall = true;
  }

  // v2 → v3 migration: clean up legacy data before first v3 run
  runV2CleanupIfNeeded(venvDir, env);

  fs.mkdirSync(path.dirname(venvDir), { recursive: true });

  // If venv exists but is corrupted (python binary missing), nuke and rebuild
  if (fs.existsSync(venvDir) && !fs.existsSync(venvPython)) {
    console.error("  Existing venv is corrupted, rebuilding…");
    try {
      fs.rmSync(venvDir, { recursive: true, force: true });
    } catch (err) {
      console.error(`  Failed to remove corrupted venv: ${err.message}`);
    }
  }

  const isFresh = !fs.existsSync(venvPython);
  if (isFresh) {
    console.error(
      "\n⚙️  Setting up voidx environment (this only happens once)...\n"
    );
    const venvResult = spawnSync(
      python.command,
      [...python.args, "-m", "venv", venvDir],
      { encoding: "utf8", stdio: "inherit", windowsHide: true }
    );
    if (venvResult.error) {
      throw new Error(
        `Failed to create the Python virtual environment: ${venvResult.error.message}`
      );
    }
    if (venvResult.status !== 0) {
      throw new Error(
        "Failed to create the Python virtual environment. See errors above."
      );
    }
  }

  if (needsInstall) {
    const packageSpec = env.VOIDX_NPM_PACKAGE_SPEC || `voidx==${pkg.version}`;
    console.error(
      `\n📦 Downloading ${packageSpec} and dependencies… ` +
        "(1–2 minutes on first run)\n"
    );

    // Upgrade pip first to avoid resolver bugs
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
  }

  writeMarkerAtomic(markerPath, marker);
}

// ── v2 → v3 migration ─────────────────────────────────────────────────────

function runV2CleanupIfNeeded(venvDir, env) {
  const markerPath = path.join(venvDir, ".voidx-install-version");
  const oldMarker = readMarker(markerPath).trim();
  if (!oldMarker) return; // fresh install, nothing to migrate

  const oldVersion = parseVersion(oldMarker.split("\n")[0]);
  if (!oldVersion) return;

  // Only run cleanup when upgrading from < 3.0.0
  const v3 = [3, 0, 0];
  if (
    oldVersion[0] > v3[0] ||
    (oldVersion[0] === v3[0] && oldVersion[1] > v3[1]) ||
    (oldVersion[0] === v3[0] && oldVersion[1] === v3[1] && oldVersion[2] >= v3[2])
  ) {
    return;
  }

  console.error("\n🔄 Upgrading from v2 — cleaning up legacy data…\n");

  const venvPython = resolveVenvPython(venvDir);
  if (!fs.existsSync(venvPython)) return; // venv not built yet, will clean after install

  // Download and run the cleanup script from GitHub
  const scriptUrl =
    "https://raw.githubusercontent.com/chikhamx/voidx/master/scripts/clean_v2_data.py";
  const tmpScript = path.join(os.tmpdir(), "voidx-clean-v2-data.py");

  try {
    const curlResult = spawnSync(
      process.platform === "win32" ? "curl.exe" : "curl",
      ["-fsSL", scriptUrl, "-o", tmpScript],
      { encoding: "utf8", windowsHide: true, timeout: 15000 }
    );
    if (curlResult.error || curlResult.status !== 0) {
      console.error("  ⚠️  Could not download v2 cleanup script, skipping.");
      return;
    }

    const pyResult = spawnSync(
      venvPython,
      [tmpScript],
      { encoding: "utf8", stdio: "inherit", windowsHide: true, timeout: 30000 }
    );
    if (pyResult.error || pyResult.status !== 0) {
      console.error("  ⚠️  v2 cleanup script failed, continuing anyway.");
    }

    try { fs.unlinkSync(tmpScript); } catch {}
  } catch (err) {
    console.error(`  ⚠️  v2 cleanup error: ${err.message}`);
  }
}

// ── Helpers ────────────────────────────────────────────────────────────────

function readMarker(filePath) {
  try {
    return fs.readFileSync(filePath, "utf8");
  } catch {
    return "";
  }
}

function debug(env, message) {
  if (env.VOIDX_NPM_DEBUG === "1") {
    console.error(`[voidx npm] ${message}`);
  }
}

function fail(message) {
  console.error(message);
  process.exit(1);
}

if (require.main === module) {
  main();
}

module.exports = {
  isCompatible,
  parseVersion,
  resolveDataHome,
  resolveVenvDir,
  resolveBundledPythonBin,
  selectPython,
};
