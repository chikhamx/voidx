"""Regression test: wheel verify probe must not run with cwd=ROOT.

The workspace root contains ``voidx.py`` (the CLI launcher). When the
verify probe runs ``python -c "import voidx"`` with cwd=ROOT, Python
imports the root-level ``voidx.py`` as a module instead of the wheel
installed in the temp venv, causing ModuleNotFoundError.

The probe subprocess must use a cwd that does not contain ``voidx.py``
(typically the temp venv directory or None for the system default).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import package  # type: ignore[import-not-found]


def test_verify_probe_does_not_use_root_cwd(tmp_path: Path) -> None:
    """The import probe must not run with cwd=ROOT (where voidx.py lives)."""
    voidx_wheel = tmp_path / "voidx-9.9.9-py3-none-any.whl"
    voidx_wheel.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    cli_wheel = tmp_path / "voidx_cli-9.9.9-py3-none-any.whl"
    cli_wheel.write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    captured_runs: list[dict] = []

    def fake_run(cmd, *args, **kwargs):
        captured_runs.append({"cmd": list(cmd), "cwd": kwargs.get("cwd", None)})
        cmd_list = [str(c) for c in cmd]
        if len(cmd_list) >= 3 and cmd_list[1] == "-m" and cmd_list[2] == "venv":
            venv_path = Path(cmd_list[-1])
            (venv_path / "bin").mkdir(parents=True, exist_ok=True)
            (venv_path / "bin" / "python").write_text("#!/bin/sh\n")
            (venv_path / "bin" / "pip").write_text("#!/bin/sh\n")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if "pip" in cmd_list[0]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if "-c" in cmd_list:
            return subprocess.CompletedProcess(cmd, 0, "voidx 9.9.9\nvoidx_cli 9.9.9\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with mock.patch.object(package.subprocess, "run", side_effect=fake_run):
        package._verify_wheels(tmp_path)

    probe_calls = [r for r in captured_runs if "-c" in r["cmd"]]
    assert probe_calls, "no import probe subprocess.run call captured"
    probe_cwd = probe_calls[0]["cwd"]
    assert probe_cwd is not None, (
        "probe ran with cwd=None; subprocess.run defaults to the calling "
        "process cwd (ROOT), where voidx.py shadows the installed wheel"
    )
    assert probe_cwd != ROOT, (
        f"probe ran with cwd=ROOT ({ROOT}); voidx.py at root shadows the "
        f"installed wheel package, causing ModuleNotFoundError"
    )
