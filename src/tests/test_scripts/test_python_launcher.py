from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _load_launcher():
    spec = importlib.util.spec_from_file_location("voidx_python_launcher", ROOT / "python.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_current_runtime_resolves_python_and_site_packages(tmp_path: Path) -> None:
    launcher = _load_launcher()
    data_root = tmp_path / ".voidx"
    runtime = data_root / "runtime" / "versions" / ("a" * 64)
    python = runtime / "python" / "bin" / "python"
    site_packages = runtime / "site-packages"
    python.parent.mkdir(parents=True)
    site_packages.mkdir(parents=True)
    python.write_bytes(b"python")
    manifest = {
        "image_fingerprint": "a" * 64,
        "python_relative": "python/bin/python",
        "site_packages_relative": "site-packages",
    }
    current = data_root / "runtime" / "current.json"
    current.parent.mkdir(parents=True, exist_ok=True)
    current.write_text(json.dumps(manifest), encoding="utf-8")

    assert launcher._current_runtime(data_root) == (python, site_packages)


def test_current_runtime_rejects_path_escape(tmp_path: Path) -> None:
    launcher = _load_launcher()
    data_root = tmp_path / ".voidx"
    runtime = data_root / "runtime" / "versions" / ("a" * 64)
    runtime.mkdir(parents=True)
    current = data_root / "runtime" / "current.json"
    current.write_text(
        json.dumps(
            {
                "image_fingerprint": "a" * 64,
                "python_relative": "../python",
                "site_packages_relative": "site-packages",
            }
        ),
        encoding="utf-8",
    )

    assert launcher._current_runtime(data_root) is None



def test_runtime_env_prioritizes_bundled_site_and_safe_path(tmp_path: Path, monkeypatch) -> None:
    launcher = _load_launcher()
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "existing"))
    site_packages = tmp_path / "site-packages"

    env = launcher._runtime_env(site_packages, tmp_path / "data")
    paths = env["PYTHONPATH"].split(launcher.os.pathsep)

    assert paths[0] == str(site_packages)
    assert paths.index(str(launcher._WORKSPACE_ROOT)) > paths.index(str(site_packages))
    assert env["PYTHONSAFEPATH"] == "1"
    assert env["PYTHONNOUSERSITE"] == "1"
    assert env["VOIDX_HOME"] == str(tmp_path / "data")
