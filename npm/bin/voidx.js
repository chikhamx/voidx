#!/usr/bin/env node
"use strict";

const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const pkg = require("../package.json");

// ── Configuration ──────────────────────────────────────────────────────────

const PBS_TAG = "20260602";
const PBS_PYTHON_MAJOR = "3.12";

// ── Main ───────────────────────────────────────────────────────────────────

function main(argv = process.argv.slice(2), env = process.env) {
  try {
    const python = selectPython(env);
    const venvDir = resolveVenvDir(env);
    ensureVenv(python, venvDir, env);
    const executable = resolveVoidxExecutable(venvDir);
    const child = spawn(executable, argv, { stdio: "inherit" });
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

  // 2. Bundled Python (preferred)
  const bundledBin = resolveBundledPythonBin(env);
  if (bundledBin && fs.existsSync(bundledBin)) {
    const candidate = { command: bundledBin, args: [], label: "bundled" };
    const probe = probePython(candidate);
    if (probe.ok && isCompatible(probe.version)) {
      return candidate;
    }
  }

  // 3. System Python fallback (for upgrades from v1.x where bundled Python was not used)
  const candidates = [
    { command: "python3", args: [], label: "python3" },
    { command: "python", args: [], label: "python" },
    { command: "python3.13", args: [], label: "python3.13" },
    { command: "python3.12", args: [], label: "python3.12" },
    { command: "python3.11", args: [], label: "python3.11" },
  ];
  if (process.platform === "win32") {
    candidates.push({ command: "py", args: ["-3"], label: "py -3" });
  }

  const oldVersions = [];
  for (const candidate of candidates) {
    const probe = probePython(candidate);
    if (!probe.ok) {
      continue;
    }
    if (isCompatible(probe.version)) {
      return candidate;
    }
    oldVersions.push(`${probe.versionText} at ${candidate.label}`);
  }

  if (oldVersions.length > 0) {
    throw new Error(
      `voidx requires Python 3.11+. Found ${oldVersions.join(", ")}.\n` +
      "Install Python 3.11+ or reinstall the npm package to get the bundled runtime:\n" +
      "  npm install -g @chikhamx/voidx"
    );
  }
  throw new Error(
    "voidx requires Python 3.11+. No Python found.\n" +
    "Install Python 3.11+ or reinstall the npm package to get the bundled runtime:\n" +
    "  npm install -g @chikhamx/voidx"
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
    : path.join(pythonDir, "python", "bin", `python${PBS_PYTHON_MAJOR}`);
}

function resolveVenvDir(env) {
  if (env.VOIDX_NPM_VENV) {
    return path.resolve(env.VOIDX_NPM_VENV);
  }
  const dataHome = resolveDataHome(env);

  // Prefer install.sh venv if it exists and has voidx — avoids duplicate environments
  const installShVenv = path.join(dataHome, "voidx", "venv");
  const installShExe = path.join(installShVenv, "bin", "voidx");
  if (process.platform === "win32") {
    // install.sh is not available on Windows, skip
  } else if (fs.existsSync(installShExe)) {
    return installShVenv;
  }

  return path.join(dataHome, "voidx", "npm-venv");
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
  if (env.VOIDX_NPM_SKIP_BOOTSTRAP === "1") {
    if (!fs.existsSync(executable)) {
      throw new Error(`voidx executable not found in ${venvDir}.`);
    }
    return;
  }

  const markerPath = path.join(venvDir, ".voidx-npm-version");
  const installShMarkerPath = path.join(venvDir, ".voidx-install-version");
  const packageSpec = env.VOIDX_NPM_PACKAGE_SPEC || `voidx==${pkg.version}`;
  // Marker must match postinstall.js format (includes PBS_TAG + PBS_CPYTHON)
  const marker = `${pkg.version}\n${packageSpec}\n${PBS_TAG}\n3.12.13\n`;
  if (fs.existsSync(executable) && (readFile(markerPath) === marker || fs.existsSync(installShMarkerPath))) {
    debug(env, `Using cached environment at ${venvDir}`);
    return;
  }

  fs.mkdirSync(path.dirname(venvDir), { recursive: true });
  const venvPython = resolveVenvPython(venvDir);

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

  if (!fs.existsSync(executable) || readFile(markerPath) !== marker) {
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

    const pipEnv = Object.assign({}, env, {
      PIP_NO_INPUT: "1",
      PIP_DISABLE_PIP_VERSION_CHECK: "1",
      PYTHON_KEYRING_BACKEND: "keyring.backends.null.Keyring",
    });

    const pipArgs = ["-m", "pip", "install", "--upgrade", "--no-cache-dir", "--progress-bar", "on"];

    // Support custom PyPI index
    const pipIndex = env.VOIDX_NPM_PIP_INDEX;
    if (pipIndex) {
      pipArgs.push("-i", pipIndex);
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
      throw new Error(
        `Failed to install ${packageSpec}: ${result.error.message}`
      );
    }
    if (result.status !== 0) {
      throw new Error(`Failed to install ${packageSpec}. See errors above.`);
    }
  }

  fs.writeFileSync(markerPath, marker);
}

// ── Helpers ────────────────────────────────────────────────────────────────

function readFile(filePath) {
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
