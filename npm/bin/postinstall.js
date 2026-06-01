#!/usr/bin/env node
"use strict";

// Lightweight environment check that runs after npm install.
// Detects Python 3.11+ and prints a hint if missing. Never blocks installation.

const { spawnSync } = require("child_process");

function main() {
  const probe = detectPython();
  if (probe.ok) {
    console.error(`\n✅ voidx: Python ${probe.versionText} found at "${probe.path}"\n`);
    return;
  }

  console.error("\n⚠️  voidx requires Python 3.11+, but no compatible Python was found.\n");
  console.error(hint());
  console.error(`\nOnce Python 3.11+ is installed, voidx will bootstrap the rest on first run.\n`);
}

function detectPython() {
  const candidates = [
    "python3", "python",
    "python3.13", "python3.12", "python3.11",
  ];
  if (process.platform === "win32") {
    candidates.push("py");
  }

  for (const cmd of candidates) {
    const args = process.platform === "win32" && cmd === "py" ? ["-3.11"] : [];
    const result = spawnSync(cmd, [...args, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"], {
      encoding: "utf8",
      windowsHide: true,
    });
    if (result.error || result.status !== 0) continue;
    const versionText = (result.stdout || "").trim();
    const match = /^(\d+)\.(\d+)\.\d+/.exec(versionText);
    if (!match) continue;
    const major = Number.parseInt(match[1], 10);
    const minor = Number.parseInt(match[2], 10);
    if (major > 3 || (major === 3 && minor >= 11)) {
      return { ok: true, versionText, path: cmd };
    }
  }
  return { ok: false };
}

function hint() {
  if (process.platform === "darwin") {
    return "  Install:  brew install python@3.12";
  }
  if (process.platform === "linux") {
    return "  Install via your package manager, e.g.:\n" +
           "    sudo apt install python3.12    (Debian/Ubuntu)\n" +
           "    sudo dnf install python3.12    (Fedora)";
  }
  if (process.platform === "win32") {
    return "  Install:  https://python.org/downloads";
  }
  return "  Install Python 3.11+ from https://python.org/downloads";
}

main();
