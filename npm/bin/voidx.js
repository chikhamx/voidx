#!/usr/bin/env node
"use strict";

const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const pkg = require("../package.json");

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
      fail(`Failed to start voidx from npm-managed environment: ${error.message}`);
    });
  } catch (error) {
    fail(error.message);
  }
}

function selectPython(env) {
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

  const candidates = [
    { command: "python3", args: [], label: "python3" },
    { command: "python", args: [], label: "python" },
  ];
  if (process.platform === "win32") {
    candidates.push({ command: "py", args: ["-3.11"], label: "py -3.11" });
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
    throw new Error(`voidx requires Python 3.11+. Found ${oldVersions.join(", ")}.`);
  }
  throw new Error(
    "voidx npm launcher requires Python 3.11+. Install Python or set VOIDX_PYTHON."
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

function ensureVenv(python, venvDir, env) {
  const executable = resolveVoidxExecutable(venvDir);
  if (env.VOIDX_NPM_SKIP_BOOTSTRAP === "1") {
    if (!fs.existsSync(executable)) {
      throw new Error(`voidx executable not found in ${venvDir}.`);
    }
    return;
  }

  const markerPath = path.join(venvDir, ".voidx-npm-version");
  const packageSpec = env.VOIDX_NPM_PACKAGE_SPEC || `voidx==${pkg.version}`;
  const marker = `${pkg.version}\n${packageSpec}\n`;
  if (fs.existsSync(executable) && readFile(markerPath) === marker) {
    debug(env, `Using cached npm-managed environment at ${venvDir}`);
    return;
  }

  fs.mkdirSync(path.dirname(venvDir), { recursive: true });
  const venvPython = resolveVenvPython(venvDir);
  if (!fs.existsSync(venvPython)) {
    debug(env, `Creating npm-managed Python environment at ${venvDir}`);
    runChecked(
      python.command,
      [...python.args, "-m", "venv", venvDir],
      "Failed to create the npm-managed Python environment.",
      env
    );
  }

  debug(env, `Installing ${packageSpec} into ${venvDir}`);
  runChecked(
    venvPython,
    ["-m", "pip", "install", "--upgrade", packageSpec],
    `Failed to install ${packageSpec} into the npm-managed Python environment.`,
    env
  );
  fs.writeFileSync(markerPath, marker);
}

function runChecked(command, args, errorMessage, env) {
  const result = spawnSync(command, args, {
    encoding: "utf8",
    stdio: env.VOIDX_NPM_DEBUG === "1" ? "inherit" : "pipe",
    windowsHide: true,
  });
  if (result.error) {
    throw new Error(`${errorMessage} ${result.error.message}`);
  }
  if (result.status !== 0) {
    const stderr = result.stderr ? result.stderr.trim() : "";
    throw new Error(stderr ? `${errorMessage} ${stderr}` : errorMessage);
  }
}

function resolveVenvDir(env) {
  if (env.VOIDX_NPM_VENV) {
    return path.resolve(env.VOIDX_NPM_VENV);
  }
  return path.join(resolveDataHome(env), "voidx", "npm-venv");
}

function resolveDataHome(env) {
  if (env.VOIDX_NPM_HOME) {
    return path.resolve(env.VOIDX_NPM_HOME);
  }
  if (process.platform === "win32") {
    return env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local");
  }
  return env.XDG_DATA_HOME || path.join(os.homedir(), ".local", "share");
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
  selectPython,
};
