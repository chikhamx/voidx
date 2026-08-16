from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.desktop_backend_manifest import (
    BACKEND_API,
    BACKEND_VERSION,
    MANIFEST_SCHEMA_VERSION,
    build_manifest,
    hash_image_tree,
    target_triple,
)


def test_target_triple_maps_supported_desktop_platforms() -> None:
    assert target_triple("Darwin", "arm64") == "aarch64-apple-darwin"
    assert target_triple("Darwin", "x86_64") == "x86_64-apple-darwin"
    assert target_triple("Windows", "AMD64") == "x86_64-pc-windows-msvc"
    assert target_triple("Linux", "aarch64") == "aarch64-unknown-linux-gnu"


def test_target_triple_rejects_unknown_platform() -> None:
    with pytest.raises(ValueError, match="unsupported desktop target"):
        target_triple("Plan9", "sparc")


def test_image_hash_is_deterministic_and_excludes_manifest(tmp_path: Path) -> None:
    (tmp_path / "python").mkdir()
    (tmp_path / "python" / "bin").mkdir()
    (tmp_path / "python" / "bin" / "python").write_bytes(b"python")
    (tmp_path / "site-packages").mkdir()
    (tmp_path / "site-packages" / "dependency.py").write_text("value = 1\n")
    (tmp_path / "manifest.json").write_text("old\n")
    (tmp_path / ".gitkeep").write_text("")

    first = hash_image_tree(tmp_path)
    (tmp_path / "manifest.json").write_text("new\n")
    (tmp_path / ".gitkeep").unlink()
    second = hash_image_tree(tmp_path)

    assert first == second
    assert len(first) == 64


def test_manifest_contains_runtime_contract() -> None:
    manifest = build_manifest(
        version="3.8.0",
        target="aarch64-apple-darwin",
        image_fingerprint="a" * 64,
        python_relative="python/bin/python",
        site_packages_relative="site-packages",
        source_revision="commit-1",
    )

    assert manifest == {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "backend_version": "3.8.0",
        "backend_api": BACKEND_API,
        "target": "aarch64-apple-darwin",
        "image_fingerprint": "a" * 64,
        "python_relative": "python/bin/python",
        "site_packages_relative": "site-packages",
        "source_revision": "commit-1",
    }
    json.dumps(manifest)


def test_build_image_replaces_old_voidx_package_and_writes_manifest(tmp_path: Path) -> None:
    from zipfile import ZIP_DEFLATED, ZipFile

    from scripts.build_desktop_backend import build_image

    python_root = tmp_path / "python-root"
    (python_root / "bin").mkdir(parents=True)
    (python_root / "bin" / "python").write_bytes(b"python-runtime")
    source_site = tmp_path / "source-site"
    (source_site / "voidx").mkdir(parents=True)
    (source_site / "voidx" / "old.py").write_text("old\n")
    (source_site / "old.pth").write_text("old\n")
    (source_site / "voidx-3.4.4.dist-info").mkdir()
    (source_site / "voidx-3.4.4.dist-info" / "METADATA").write_text("old\n")
    (source_site / "dependency.py").write_text("dependency\n")

    wheel = tmp_path / "voidx-3.8.0-py3-none-any.whl"
    with ZipFile(wheel, "w", ZIP_DEFLATED) as archive:
        archive.writestr("voidx/__init__.py", '__version__ = "3.8.0"\n')
        archive.writestr("voidx/new.py", "new\n")
        archive.writestr("voidx-3.8.0.dist-info/METADATA", "Name: voidx\n")
    output = tmp_path / "image"
    output.mkdir()
    (output / ".gitkeep").write_text("")
    manifest = build_image(
        output_dir=output,
        python_root=python_root,
        site_packages=source_site,
        wheel=wheel,
        version="3.8.0",
        target="aarch64-apple-darwin",
        source_revision="commit-1",
    )

    assert not (output / ".gitkeep").exists()
    assert (output / "python/bin/python").read_bytes() == b"python-runtime"
    assert (output / "site-packages/dependency.py").read_text() == "dependency\n"
    assert (output / "site-packages/voidx/new.py").read_text() == "new\n"
    assert not (output / "site-packages/voidx/old.py").exists()
    assert not (output / "site-packages/voidx-3.4.4.dist-info").exists()
    assert not (output / "site-packages/old.pth").exists()
    assert manifest["image_fingerprint"] == hash_image_tree(output)
    assert json.loads((output / "manifest.json").read_text())["backend_api"] == BACKEND_API



def test_resolve_runtime_inputs_from_install_root(tmp_path: Path) -> None:
    from scripts.build_desktop_backend import resolve_runtime_inputs

    install_root = tmp_path / "voidx"
    (install_root / "python" / "python" / "bin").mkdir(parents=True)
    (install_root / "python" / "python" / "bin" / "python").write_bytes(b"python")
    site_packages = install_root / "venv" / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)

    python_root, resolved_site_packages = resolve_runtime_inputs(install_root)

    assert python_root == install_root / "python" / "python"
    assert resolved_site_packages == site_packages



def test_desktop_build_generates_backend_image_before_tauri() -> None:
    build_script = (Path(__file__).resolve().parents[3] / "desktop" / "build.sh").read_text(
        encoding="utf-8"
    )

    image_step = build_script.index("scripts.build_desktop_backend")
    tauri_step = build_script.index("npm run build")

    assert image_step < tauri_step
    assert "--install-root" in build_script
    assert "resources/backend" in build_script



def test_desktop_build_and_tauri_use_canonical_backend_version() -> None:
    root = Path(__file__).resolve().parents[3]
    build_script = (root / "desktop" / "build.sh").read_text(encoding="utf-8")
    tauri_config = json.loads(
        (root / "desktop" / "tauri" / "tauri.conf.json").read_text(encoding="utf-8")
    )

    assert "from scripts.desktop_backend_manifest import BACKEND_VERSION" in build_script
    assert tauri_config["version"] == BACKEND_VERSION



def test_desktop_build_restores_backend_resource_placeholder() -> None:
    build_script = (Path(__file__).resolve().parents[3] / "desktop" / "build.sh").read_text(
        encoding="utf-8"
    )

    assert "restore_backend_resource_placeholder()" in build_script
    assert "trap restore_backend_resource_placeholder EXIT" in build_script
    assert 'touch "$BACKEND_RESOURCE_DIR/.gitkeep"' in build_script


def test_desktop_build_uses_portable_locale_for_macos_bundling() -> None:
    build_script = (Path(__file__).resolve().parents[3] / "desktop" / "build.sh").read_text(
        encoding="utf-8"
    )

    assert "export LC_ALL=C" in build_script
    assert "export LANG=C" in build_script
    assert "export LC_CTYPE=C" in build_script


def test_desktop_build_clears_stale_macos_dmg_outputs() -> None:
    build_script = (Path(__file__).resolve().parents[3] / "desktop" / "build.sh").read_text(
        encoding="utf-8"
    )

    assert 'find "$BUNDLE_DIR/macos"' in build_script
    assert '"voidx_*.dmg"' in build_script
    assert '"rw.*.dmg"' in build_script


def test_desktop_build_does_not_skip_release_metadata_checks() -> None:
    build_script = (Path(__file__).resolve().parents[3] / "desktop" / "build.sh").read_text(
        encoding="utf-8"
    )

    assert "--skip-checks" not in build_script


def test_desktop_build_detaches_stale_macos_dmg_mounts_before_cleanup() -> None:
    build_script = (Path(__file__).resolve().parents[3] / "desktop" / "build.sh").read_text(
        encoding="utf-8"
    )

    assert "detach_stale_macos_dmg_mounts" in build_script
    assert 'hdiutil detach "$device"' in build_script
    assert build_script.index("detach_stale_macos_dmg_mounts") < build_script.index(
        'find "$BUNDLE_DIR/macos"'
    )


def test_desktop_build_uses_hdiutil_retry_wrapper_on_macos() -> None:
    root = Path(__file__).resolve().parents[3]
    build_script = (root / "desktop" / "build.sh").read_text(encoding="utf-8")
    wrapper = (root / "desktop" / "tools" / "hdiutil").read_text(encoding="utf-8")

    assert 'export PATH="$DESKTOP_DIR/tools:$PATH"' in build_script
    assert '"$status" -eq 2' in wrapper
    assert '"$status" -eq 16' in wrapper
    assert 'exec /usr/bin/hdiutil "$@"' in wrapper
