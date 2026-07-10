import json
import os
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")


def _base_env(**extra: str) -> dict[str, str]:
    """Minimal env for subprocess calls — includes OS-required vars on Windows."""
    env = {"PATH": os.environ.get("PATH", "")}
    if sys.platform == "win32":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
    env.update(extra)
    return env


def test_release_metadata_check_passes():
    result = subprocess.run(
        [sys.executable, "scripts/package.py", "--check-only"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr


def test_npm_package_matches_python_version():
    npm_package = json.loads((ROOT / "npm" / "package.json").read_text())

    from voidx import __version__ as python_version

    assert npm_package["version"] == python_version
    assert npm_package["bin"]["voidx"] == "bin/voidx.js"
    assert (ROOT / "npm" / "bin" / "voidx.js").is_file()



def test_tui_package_versions_and_dependency_pins_match_python_version():
    from voidx import __version__ as python_version
    from voidx_cli import __version__ as tui_version

    root_pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    tui_pyproject = tomllib.loads((ROOT / "tui" / "pyproject.toml").read_text())

    assert tui_version == python_version
    assert f"voidx=={python_version}" in tui_pyproject["project"]["dependencies"]


def test_release_docs_cover_tui_package_artifacts():
    release_doc = (ROOT / "docs" / "releasing.md").read_text()

    assert "voidx-cli" in release_doc
    assert "dist/voidx_cli-<version>-py3-none-any.whl" in release_doc
    assert "dist/voidx_cli-<version>.tar.gz" in release_doc

def test_npm_launcher_marks_python_environment():
    source = (ROOT / "npm" / "bin" / "voidx.js").read_text(encoding="utf-8")

    assert 'VOIDX_LAUNCHED_BY_NPM: "1"' in source
    assert "VOIDX_NPM_PACKAGE_VERSION: pkg.version" in source


def test_python_package_includes_bundled_skill_templates():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    package_data = pyproject["tool"]["setuptools"]["package-data"]["voidx.skills"]

    assert "bundled/*/SKILL.md" in package_data


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_npm_launcher_syntax_is_valid():
    result = subprocess.run(
        [NODE, "--check", "npm/bin/voidx.js"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_npm_launcher_rejects_old_explicit_python(tmp_path):
    if sys.platform == "win32":
        pytest.skip("uses a POSIX fake executable")
    fake_python = tmp_path / "python"
    fake_python.write_text("#!/bin/sh\nif [ \"$1\" = \"-c\" ]; then echo '3.10.9'; exit 0; fi\nexit 1\n")
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)

    env = _base_env(
        VOIDX_PYTHON=str(fake_python),
        VOIDX_NPM_HOME=str(tmp_path / "home"),
    )
    result = subprocess.run(
        [NODE, "npm/bin/voidx.js", "version"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert "requires Python 3.11+" in result.stderr
    assert "3.10.9" in result.stderr


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_npm_launcher_reports_missing_bundled_python_without_bootstrap(tmp_path):
    """voidx.js fails quickly with reinstall guidance when bundled Python is missing."""
    if sys.platform == "win32":
        pytest.skip("uses a POSIX environment")
    env = _base_env(
        VOIDX_NPM_HOME=str(tmp_path / "home"),
        VOIDX_NPM_SKIP_BOOTSTRAP="1",
    )
    result = subprocess.run(
        [NODE, "npm/bin/voidx.js", "version"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert result.returncode == 1
    assert "bundled python not found" in result.stderr.lower()
    assert "npm install -g @chikhamx/voidx" in result.stderr


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_npm_postinstall_does_not_export_probeSystemPython():
    """postinstall.js must not export probeSystemPython after removing system Python fallback."""
    result = subprocess.run(
        [NODE, "-e", "const m = require('./npm/bin/postinstall.js'); console.log(typeof m.probeSystemPython)"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "undefined"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_npm_launcher_selectPython_uses_bundled_only(tmp_path):
    """selectPython must return bundled Python when it exists, without searching system."""
    if sys.platform == "win32":
        pytest.skip("uses a POSIX fake executable")
    # Create a fake bundled Python
    home = tmp_path / "home"
    python_dir = home / "voidx" / "python" / "python" / "bin"
    python_dir.mkdir(parents=True)
    fake_python = python_dir / "python3"
    fake_python.write_text("#!/bin/sh\nif [ \"$1\" = \"-c\" ]; then echo '3.12.0'; exit 0; fi\nexit 1\n")
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)
    env = _base_env(VOIDX_NPM_HOME=str(home))
    result = subprocess.run(
        [NODE, "-e", (
            "const m = require('./npm/bin/voidx.js');"
            "const p = m.selectPython(process.env);"
            "console.log(p.label)"
        )],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "bundled"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_npm_launcher_selectPython_bootstraps_bundled_or_reports_clear_error(tmp_path):
    """selectPython bootstraps bundled Python or reports a clear setup error."""
    env = _base_env(VOIDX_NPM_HOME=str(tmp_path / "home"), VOIDX_NPM_SKIP_BOOTSTRAP="1")
    result = subprocess.run(
        [NODE, "-e", (
            "const m = require('./npm/bin/voidx.js');"
            "try { const p = m.selectPython(process.env); console.log('OK:' + p.label); }"
            "catch(e) { console.log('ERROR:' + e.message); }"
        )],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    # Bootstrap logs may precede the final probe result.
    stdout = result.stdout.strip()
    assert "OK:bundled" in stdout or "ERROR:" in stdout


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_npm_postinstall_exports_downloadFileWithRetry():
    """postinstall.js must export downloadFileWithRetry for retry logic."""
    result = subprocess.run(
        [NODE, "-e", "const m = require('./npm/bin/postinstall.js'); console.log(typeof m.downloadFileWithRetry)"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "function"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_npm_postinstall_cleans_partial_download_on_failure(tmp_path):
    """downloadFileWithRetry must clean up partial files on failure."""
    dest = str(tmp_path / "partial.tar.gz")
    # Write a partial file to simulate a previous failed download
    Path(dest).write_text("partial data")
    assert Path(dest).exists()
    result = subprocess.run(
        [NODE, "-e", (
            "const m = require('./npm/bin/postinstall.js');"
            f"m.downloadFileWithRetry('https://127.0.0.1:1/invalid', '{dest.replace(chr(92), '/')}', 1)"
            ".then(() => console.log('OK'))"
            ".catch(e => console.log('FAILED:' + e.message));"
        )],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.stdout.startswith("FAILED:")
    # Partial file must be cleaned up
    assert not Path(dest).exists()


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_npm_postinstall_supports_python_mirror_env(tmp_path):
    """postinstall.js must use VOIDX_NPM_PYTHON_MIRROR for download URL."""
    result = subprocess.run(
        [NODE, "-e", (
            "const m = require('./npm/bin/postinstall.js');"
            "const url = m.buildPythonDownloadUrl('https://mirror.example.com', '20260602', 'test.tar.gz');"
            "console.log(url);"
        )],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "https://mirror.example.com/20260602/test.tar.gz"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_npm_launcher_rebuilds_corrupted_venv(tmp_path):
    """voidx.js must rebuild venv when it exists but python binary is missing."""
    if sys.platform == "win32":
        pytest.skip("uses a POSIX fake executable")
    home = tmp_path / "home"
    venv_dir = home / "voidx" / "npm-venv"
    # Create a corrupted venv: directory exists but no python binary
    venv_dir.mkdir(parents=True)
    (venv_dir / "lib").mkdir()  # some venv-like structure
    # Create fake bundled Python
    python_dir = home / "voidx" / "python" / "python" / "bin"
    python_dir.mkdir(parents=True)
    fake_python = python_dir / "python3"
    fake_python.write_text("#!/bin/sh\nif [ \"$1\" = \"-c\" ]; then echo '3.12.0'; exit 0; fi\nexit 1\n")
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)
    env = _base_env(VOIDX_NPM_HOME=str(home))
    # The launcher should detect the corrupted venv and try to rebuild
    # (it will fail because the fake python can't create a real venv,
    # but the error should mention venv creation, not a mysterious failure)
    result = subprocess.run(
        [NODE, "npm/bin/voidx.js", "version"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 1
    # Should mention venv creation failure (meaning it detected corruption and tried to rebuild)
    assert "venv" in result.stderr.lower() or "virtual environment" in result.stderr.lower()


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_npm_postinstall_uses_pip_no_cache_dir():
    """postinstall.js pip install must use --no-cache-dir to avoid cache conflicts."""
    source = (ROOT / "npm" / "bin" / "postinstall.js").read_text()
    assert "--no-cache-dir" in source


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_npm_postinstall_upgrades_pip_before_install():
    """postinstall.js must upgrade pip before installing voidx."""
    source = (ROOT / "npm" / "bin" / "postinstall.js").read_text()
    # Must have a pip upgrade step before the main pip install
    # The args are in array form: ["-m", "pip", "install", "--upgrade", "pip", ...]
    assert '"--upgrade", "pip"' in source or "'--upgrade', 'pip'" in source


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_npm_postinstall_supports_pip_index_env():
    """postinstall.js must pass VOIDX_NPM_PIP_INDEX as -i to pip."""
    source = (ROOT / "npm" / "bin" / "postinstall.js").read_text()
    runtime_source = (ROOT / "npm" / "bin" / "runtime-install.js").read_text()
    assert 'require("./runtime-install")' in source
    assert "VOIDX_NPM_PIP_INDEX" in runtime_source
    assert '"-i"' in runtime_source or "'-i'" in runtime_source


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_npm_launcher_supports_pip_index_env():
    """voidx.js must pass VOIDX_NPM_PIP_INDEX as -i to pip."""
    source = (ROOT / "npm" / "bin" / "voidx.js").read_text()
    runtime_source = (ROOT / "npm" / "bin" / "runtime-install.js").read_text()
    assert 'require("./runtime-install")' in source
    assert "VOIDX_NPM_PIP_INDEX" in runtime_source
    assert '"-i"' in runtime_source or "'-i'" in runtime_source


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_npm_launcher_forwards_args_to_managed_voidx(tmp_path):
    if sys.platform == "win32":
        pytest.skip("uses a POSIX fake executable")
    venv = tmp_path / "venv"
    bin_dir = venv / "bin"
    bin_dir.mkdir(parents=True)
    fake_voidx = bin_dir / "voidx"
    fake_voidx.write_text("#!/bin/sh\nprintf 'voidx args:'\nprintf '%s|' \"$@\"\nprintf '\\n'\n")
    fake_voidx.chmod(fake_voidx.stat().st_mode | stat.S_IXUSR)

    env = _base_env(
        VOIDX_PYTHON=sys.executable,
        VOIDX_NPM_VENV=str(venv),
        VOIDX_NPM_SKIP_BOOTSTRAP="1",
    )
    result = subprocess.run(
        [NODE, "npm/bin/voidx.js", "version", "--plain"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "voidx args:version|--plain|\n"


def _run_node_json(script: str) -> object:
    result = subprocess.run(
        [NODE, "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_runtime_installer_builds_one_pair_pip_command():
    payload = _run_node_json(
        """
        const runtime = require('./npm/bin/runtime-install.js');
        const calls = [];
        runtime.installPair({
          venvPython: '/managed/python',
          coreSpec: 'voidx==9.0.0',
          cliSpec: '/package/voidx_cli-9.0.0-py3-none-any.whl',
          env: {},
          runner: (command, args) => {
            calls.push({ command, args });
            return { status: 0 };
          },
        });
        console.log(JSON.stringify(calls));
        """
    )

    assert len(payload) == 1
    assert payload[0]["command"] == "/managed/python"
    assert "voidx==9.0.0" in payload[0]["args"]
    assert "/package/voidx_cli-9.0.0-py3-none-any.whl" in payload[0]["args"]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_runtime_installer_requires_exact_bundled_cli_wheel(tmp_path):
    npm_dir = str(tmp_path).replace("\\", "/")
    payload = _run_node_json(
        f"""
        const runtime = require('./npm/bin/runtime-install.js');
        try {{
          runtime.resolveBundledCliWheel('{npm_dir}', '9.0.0');
          console.log(JSON.stringify({{ ok: true }}));
        }} catch (error) {{
          console.log(JSON.stringify({{ ok: false, message: error.message }}));
        }}
        """
    )

    assert payload["ok"] is False
    assert "voidx_cli-9.0.0-py3-none-any.whl" in payload["message"]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_runtime_installer_verifies_both_metadata_versions():
    payload = _run_node_json(
        """
        const runtime = require('./npm/bin/runtime-install.js');
        const result = runtime.verifyPair({
          venvPython: '/managed/python',
          executable: '/managed/voidx',
          expectedVersion: '9.0.0',
          env: {},
          runner: () => ({
            status: 0,
            stdout: JSON.stringify({
              core_version: '9.0.0',
              cli_version: '3.5.1',
              core_import: true,
              cli_import: true,
              entrypoint_ok: true,
              entrypoint_version: '9.0.0',
            }),
            stderr: '',
          }),
        });
        console.log(JSON.stringify(result));
        """
    )

    assert payload["ok"] is False
    assert payload["coreVersion"] == "9.0.0"
    assert payload["cliVersion"] == "3.5.1"


def test_runtime_installer_probe_excludes_current_directory_from_imports():
    source = (ROOT / "npm" / "bin" / "runtime-install.js").read_text()

    assert "os.getcwd()" in source
    assert "sys.path =" in source


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_runtime_installer_force_repairs_pair_only_once():
    payload = _run_node_json(
        """
        const runtime = require('./npm/bin/runtime-install.js');
        const installs = [];
        const verifications = [
          { ok: false, message: 'mismatch' },
          { ok: false, message: 'still mismatched' },
        ];
        try {
          runtime.installVerifyAndRepair({
            venvPython: '/managed/python',
            executable: '/managed/voidx',
            coreSpec: 'voidx==9.0.0',
            cliSpec: '/package/voidx_cli-9.0.0-py3-none-any.whl',
            expectedVersion: '9.0.0',
            env: {},
            installFn: (options) => installs.push(options.forceReinstall === true),
            verifyFn: () => verifications.shift(),
          });
        } catch (error) {
          console.log(JSON.stringify({ installs, message: error.message }));
        }
        """
    )

    assert payload["installs"] == [False, True]
    assert "still mismatched" in payload["message"]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_runtime_installer_verifies_and_repairs_after_initial_pip_failure():
    payload = _run_node_json(
        """
        const runtime = require('./npm/bin/runtime-install.js');
        const installs = [];
        let verificationCount = 0;
        const result = runtime.installVerifyAndRepair({
          venvPython: '/managed/python',
          executable: '/managed/voidx',
          coreSpec: 'voidx==9.0.0',
          cliSpec: '/package/voidx_cli-9.0.0-py3-none-any.whl',
          expectedVersion: '9.0.0',
          env: {},
          installFn: (options) => {
            installs.push(options.forceReinstall === true);
            if (!options.forceReinstall) {
              throw new Error('initial pip failed');
            }
          },
          verifyFn: () => {
            verificationCount += 1;
            return verificationCount === 1
              ? { ok: false, message: 'partial install' }
              : { ok: true, message: 'repaired' };
          },
        });
        console.log(JSON.stringify({ installs, verificationCount, result }));
        """
    )

    assert payload["installs"] == [False, True]
    assert payload["verificationCount"] == 2
    assert payload["result"]["ok"] is True


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_runtime_installer_writes_marker_atomically(tmp_path):
    marker_path = tmp_path / ".voidx-install-version"
    marker_js = str(marker_path).replace("\\", "/")
    parent_js = str(tmp_path).replace("\\", "/")
    payload = _run_node_json(
        f"""
        const fs = require('fs');
        const runtime = require('./npm/bin/runtime-install.js');
        runtime.writeMarkerAtomic('{marker_js}', '9.0.0\\n20260602\\n3.12.13\\n');
        const leftovers = fs.readdirSync('{parent_js}')
          .filter((name) => name.includes('.tmp'));
        console.log(JSON.stringify({{
          content: fs.readFileSync('{marker_js}', 'utf8'),
          leftovers,
        }}));
        """
    )

    assert payload["content"].startswith("9.0.0\n")
    assert payload["leftovers"] == []


def test_npm_postinstall_uses_shared_pair_installer():
    source = (ROOT / "npm" / "bin" / "postinstall.js").read_text()

    assert 'require("./runtime-install")' in source
    assert "resolveBundledCliWheel" in source
    assert "installVerifyAndRepair" in source
    assert "writeMarkerAtomic" in source
    assert "Bundled ${wheelPattern} not found" not in source


def test_npm_launcher_recovery_uses_shared_pair_installer():
    source = (ROOT / "npm" / "bin" / "voidx.js").read_text()

    assert 'require("./runtime-install")' in source
    assert "resolveBundledCliWheel" in source
    assert "installVerifyAndRepair" in source
    assert "writeMarkerAtomic" in source


def test_npm_cached_marker_is_verified_before_returning():
    for relative in ("npm/bin/postinstall.js", "npm/bin/voidx.js"):
        source = (ROOT / relative).read_text()
        marker_check = source.index("readMarker(markerPath) === marker")
        verify_call = source.index("verifyPair", marker_check)
        cached_return = source.index("return;", marker_check)

        assert marker_check < verify_call < cached_return


def test_npm_launcher_repairs_marker_matching_invalid_cache():
    source = (ROOT / "npm" / "bin" / "voidx.js").read_text()
    invalid_cache = source.index("Cached environment is invalid")
    mark_for_install = source.index("needsInstall = true", invalid_cache)
    install_branch = source.index("if (needsInstall)", mark_for_install)

    assert invalid_cache < mark_for_install < install_branch


def test_npm_check_includes_runtime_installer():
    package = json.loads((ROOT / "npm" / "package.json").read_text())

    assert "node --check bin/runtime-install.js" in package["scripts"]["check"]


def test_upgrade_docs_keep_core_and_cli_on_the_same_install_path():
    readme = (ROOT / "README.md").read_text()
    usage_guide = (ROOT / "docs" / "usage-guide.md").read_text()

    assert "python -m pip install --upgrade voidx voidx-cli" in readme
    assert "npm update -g @chikhamx/voidx" in readme
    assert "npm update -g @chikhamx/voidx" in usage_guide
    assert "npm 安装" in usage_guide
    assert "不能使用 `/upgrade now`" in usage_guide


@pytest.mark.skip(reason="Run manually: ./python.py -m pytest -k wheel_install_verify")
def test_wheel_install_verify():
    """Build both wheels and verify they install correctly in a temp venv."""
    result = subprocess.run(
        [sys.executable, "scripts/package.py", "--format", "wheel", "--clean", "--skip-checks", "--verify"],
        cwd=ROOT,
        capture_output=True, text=True,
        timeout=120,
    )
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
    assert result.returncode == 0
