#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

const VERIFY_PROBE = String.raw`
import importlib
import json
import os
import re
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version

current_directory = os.path.normcase(os.path.realpath(os.getcwd()))
sys.path = [
    entry
    for entry in sys.path
    if entry and os.path.normcase(os.path.realpath(entry)) != current_directory
]

def installed_version(name):
    try:
        return version(name)
    except PackageNotFoundError:
        return None
    except Exception:
        return None

payload = {
    "core_version": installed_version("voidx"),
    "cli_version": installed_version("voidx-cli"),
    "core_import": False,
    "cli_import": False,
    "entrypoint_ok": False,
    "entrypoint_version": None,
}

try:
    importlib.import_module("voidx")
    payload["core_import"] = True
except Exception as exc:
    payload["core_error"] = str(exc)

try:
    importlib.import_module("voidx_cli")
    payload["cli_import"] = True
except Exception as exc:
    payload["cli_error"] = str(exc)

try:
    completed = subprocess.run(
        [sys.argv[1], "--version"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = (completed.stdout or completed.stderr or "").strip()
    payload["entrypoint_ok"] = completed.returncode == 0
    match = re.search(r"\d+\.\d+\.\d+(?:[A-Za-z0-9.+-]*)?", output)
    payload["entrypoint_version"] = match.group(0) if match else None
except Exception as exc:
    payload["entrypoint_error"] = str(exc)

print(json.dumps(payload))
`;

function resolveBundledCliWheel(npmDir, version) {
  const filename = `voidx_cli-${version}-py3-none-any.whl`;
  const wheelPath = path.join(npmDir, filename);
  if (!fs.existsSync(wheelPath)) {
    throw new Error(`Bundled ${filename} not found.`);
  }
  return wheelPath;
}

function pipEnvironment(env) {
  return {
    ...env,
    PIP_NO_INPUT: "1",
    PIP_DISABLE_PIP_VERSION_CHECK: "1",
    PYTHON_KEYRING_BACKEND: "keyring.backends.null.Keyring",
  };
}

function buildPipArgs({ coreSpec, cliSpec, forceReinstall, env }) {
  const args = [
    "-m",
    "pip",
    "install",
    "--upgrade",
    "--no-cache-dir",
    "--progress-bar",
    "on",
  ];
  if (forceReinstall) {
    args.push("--force-reinstall");
  }
  const pipIndex = env.VOIDX_NPM_PIP_INDEX;
  if (pipIndex) {
    args.push("-i", pipIndex);
    try {
      args.push("--trusted-host", new URL(pipIndex).hostname);
    } catch {}
  }
  args.push(coreSpec, cliSpec);
  return args;
}

function installPair({
  venvPython,
  coreSpec,
  cliSpec,
  env,
  forceReinstall = false,
  runner = spawnSync,
}) {
  const result = runner(
    venvPython,
    buildPipArgs({ coreSpec, cliSpec, forceReinstall, env }),
    {
      encoding: "utf8",
      stdio: "inherit",
      windowsHide: true,
      env: pipEnvironment(env),
    }
  );
  if (result.error) {
    throw new Error(`pip install failed: ${result.error.message}`);
  }
  if (result.status !== 0) {
    throw new Error("pip install failed. See errors above.");
  }
  return result;
}

function verifyPair({
  venvPython,
  executable,
  expectedVersion,
  env,
  runner = spawnSync,
}) {
  const result = runner(
    venvPython,
    ["-c", VERIFY_PROBE, executable],
    {
      encoding: "utf8",
      windowsHide: true,
      env,
    }
  );
  if (result.error || result.status !== 0) {
    const detail = result.error
      ? result.error.message
      : (result.stderr || result.stdout || `verification exited ${result.status}`).trim();
    return {
      ok: false,
      coreVersion: null,
      cliVersion: null,
      message: `Installation verification failed: ${detail}`,
    };
  }

  let payload;
  try {
    payload = JSON.parse((result.stdout || "").trim());
  } catch (error) {
    return {
      ok: false,
      coreVersion: null,
      cliVersion: null,
      message: `Installation verification returned invalid data: ${error.message}`,
    };
  }

  const coreVersion = payload.core_version || null;
  const cliVersion = payload.cli_version || null;
  const failures = [];
  if (!coreVersion) failures.push("voidx is not installed");
  if (!cliVersion) failures.push("voidx-cli is not installed");
  if (!payload.core_import) failures.push("voidx is not importable");
  if (!payload.cli_import) failures.push("voidx-cli is not importable");
  if (!payload.entrypoint_ok) failures.push("voidx entry point failed");
  if (coreVersion && cliVersion && coreVersion !== cliVersion) {
    failures.push(`package versions differ (${coreVersion} != ${cliVersion})`);
  }
  if (coreVersion && payload.entrypoint_version !== coreVersion) {
    failures.push(
      `entry point version differs (${payload.entrypoint_version || "missing"} != ${coreVersion})`
    );
  }
  if (coreVersion !== expectedVersion) {
    failures.push(`voidx version is ${coreVersion || "missing"}, expected ${expectedVersion}`);
  }
  if (cliVersion !== expectedVersion) {
    failures.push(`voidx-cli version is ${cliVersion || "missing"}, expected ${expectedVersion}`);
  }

  return {
    ok: failures.length === 0,
    coreVersion,
    cliVersion,
    message: failures.length === 0
      ? `Verified voidx and voidx-cli ${expectedVersion}`
      : failures.join("; "),
  };
}

function installVerifyAndRepair(options) {
  const installFn = options.installFn || installPair;
  const verifyFn = options.verifyFn || verifyPair;
  let initialInstallError = null;
  try {
    installFn({ ...options, forceReinstall: false });
  } catch (error) {
    initialInstallError = error;
  }
  let verification = verifyFn(options);
  if (verification.ok) {
    return verification;
  }
  let repairInstallError = null;
  try {
    installFn({ ...options, forceReinstall: true });
  } catch (error) {
    repairInstallError = error;
  }
  verification = verifyFn(options);
  if (!verification.ok) {
    const installError = repairInstallError || initialInstallError;
    const detail = installError
      ? `${verification.message}; ${installError.message}`
      : verification.message;
    throw new Error(detail);
  }
  return verification;
}

function writeMarkerAtomic(markerPath, marker) {
  fs.mkdirSync(path.dirname(markerPath), { recursive: true });
  const temporaryPath = `${markerPath}.${process.pid}.tmp`;
  try {
    fs.writeFileSync(temporaryPath, marker);
    fs.renameSync(temporaryPath, markerPath);
  } finally {
    try {
      fs.unlinkSync(temporaryPath);
    } catch {}
  }
}

module.exports = {
  installPair,
  installVerifyAndRepair,
  resolveBundledCliWheel,
  verifyPair,
  writeMarkerAtomic,
};
