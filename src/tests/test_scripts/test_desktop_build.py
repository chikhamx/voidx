from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[3]
BUILD_SCRIPT = ROOT / "desktop" / "build.sh"


def test_macos_build_uses_app_bundle_then_native_hdiutil_dmg() -> None:
    script = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert 'npm run build -- --bundles app' in script
    assert 'hdiutil create' in script
    assert 'voidx_${BACKEND_VERSION}_${MACOS_BUNDLE_ARCH}.dmg' in script
    assert 'ln -s /Applications' in script


def test_clean_detaches_macos_dmg_mounts_before_removing_bundle() -> None:
    script = BUILD_SCRIPT.read_text(encoding="utf-8")

    detach_call = script.index("  detach_stale_macos_dmg_mounts\n")
    bundle_cleanup = script.index('  rm -rf "$TAURI_DIR/target/release/bundle"')

    assert detach_call < bundle_cleanup
