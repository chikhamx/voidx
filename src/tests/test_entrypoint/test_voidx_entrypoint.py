from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_voidx_script_prioritizes_workspace_source_over_installed_package(
    tmp_path: Path,
) -> None:
    installed_root = tmp_path / "installed"
    fake_package = installed_root / "voidx"
    fake_package.mkdir(parents=True)
    (fake_package / "__init__.py").write_text("", encoding="utf-8")
    (fake_package / "main.py").write_text(
        "def cli():\n"
        "    print('loaded fake installed voidx')\n"
        "    return 0\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(installed_root),
            str(REPO_ROOT / "src"),
            str(REPO_ROOT / "tui"),
        ]
    )

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "voidx.py"), "--version"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "loaded fake installed voidx" not in result.stdout
    assert result.stdout.startswith("voidx v")



def test_voidx_script_uses_shared_runtime_when_voidx_home_is_data_root(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / ".voidx"
    fingerprint = "a" * 64
    runtime = data_root / "runtime" / "versions" / fingerprint
    runtime.mkdir(parents=True)
    python_path = runtime / ("python.exe" if os.name == "nt" else "python")
    python_path.write_bytes(Path(sys.executable).read_bytes())
    python_path.chmod(0o755)
    (runtime / "site-packages").mkdir()
    (data_root / "runtime" / "current.json").write_text(
        json.dumps(
            {
                "image_fingerprint": fingerprint,
                "python_relative": python_path.name,
                "site_packages_relative": "site-packages",
            }
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["VOIDX_HOME"] = str(data_root)
    env["PYTHONPATH"] = os.pathsep.join(
        entry for entry in sys.path if entry and Path(entry).is_dir()
    )
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "voidx.py"), "--version"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("voidx v")

def test_print_version_uses_void_console(monkeypatch) -> None:
    voidx_main = importlib.import_module("voidx.bootstrap.command_line")

    printed = []

    class FakeConsole:
        def print(self, value):
            printed.append(value)

    monkeypatch.setattr(voidx_main, "_vconsole", lambda: FakeConsole())

    voidx_main._print_version()

    assert len(printed) == 1
    assert printed[0].startswith("voidx v")


def test_select_start_session_signature_only_keeps_resume_and_console() -> None:
    import inspect
    voidx_main = importlib.import_module("voidx.bootstrap.command_line")

    assert list(inspect.signature(voidx_main._select_start_session).parameters) == ["resume", "vconsole"]


def test_bootstrap_statically_exports_cli_without_dynamic_attribute_hook() -> None:
    bootstrap = importlib.import_module("voidx.bootstrap")
    command_line = importlib.import_module("voidx.bootstrap.command_line")

    assert bootstrap.cli is command_line.cli
    assert "__getattr__" not in vars(bootstrap)
