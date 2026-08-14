"""Build the self-contained backend image embedded in the desktop bundle."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from zipfile import ZipFile

from scripts.desktop_backend_manifest import (
    BACKEND_VERSION,
    build_manifest as make_manifest,
    hash_image_tree,
    target_triple,
)


def _copy_tree(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise FileNotFoundError(f"directory not found: {source}")
    shutil.copytree(source, destination, symlinks=True)


def _copy_site_packages(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise FileNotFoundError(f"site-packages directory not found: {source}")
    destination.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        if entry.name == "voidx" or entry.name.startswith("voidx-"):
            continue
        if entry.suffix == ".pth":
            continue
        target = destination / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, symlinks=True)
        else:
            shutil.copy2(entry, target, follow_symlinks=False)


def _safe_extract_wheel(wheel: Path, destination: Path) -> None:
    with ZipFile(wheel) as archive:
        root = destination.resolve()
        for member in archive.infolist():
            target = (destination / member.filename).resolve()
            if target != root and root not in target.parents:
                raise ValueError(f"wheel contains unsafe path: {member.filename}")
        archive.extractall(destination)


def _python_relative(python_root: Path) -> str:
    if (python_root / "bin" / "python").exists():
        return "python/bin/python"
    if (python_root / "bin" / "python3").exists():
        return "python/bin/python3"
    if (python_root / "Scripts" / "python.exe").exists():
        return "python/Scripts/python.exe"
    raise FileNotFoundError(f"Python executable not found under: {python_root}")


def _first_directory(paths: list[Path]) -> Path | None:
    return next((path for path in paths if path.is_dir()), None)


def resolve_runtime_inputs(install_root: Path) -> tuple[Path, Path]:
    """Resolve the standalone Python root and installed dependency directory."""
    install_root = install_root.expanduser().resolve()
    python_root = _first_directory(
        [
            install_root / "python" / "python",
            install_root / "python",
        ]
    )
    if python_root is None:
        raise FileNotFoundError(
            f"standalone Python runtime not found under {install_root}; expected python/python/"
        )

    site_candidates = [
        install_root / "venv" / "Lib" / "site-packages",
        *sorted((install_root / "venv" / "lib").glob("python*/site-packages")),
        install_root / "Lib" / "site-packages",
        *sorted((install_root / "lib").glob("python*/site-packages")),
    ]
    site_packages = _first_directory(site_candidates)
    if site_packages is None:
        raise FileNotFoundError(
            f"installed site-packages not found under {install_root}; expected venv/lib/python*/site-packages"
        )
    return python_root, site_packages


def build_image(
    *,
    output_dir: Path,
    python_root: Path,
    site_packages: Path,
    wheel: Path,
    version: str,
    target: str,
    source_revision: str,
) -> dict[str, object]:
    """Build one platform-specific backend image and return its manifest."""
    for path, label in ((python_root, "Python runtime"), (site_packages, "site-packages"), (wheel, "wheel")):
        if not path.exists():
            raise FileNotFoundError(f"{label} not found: {path}")

    output_dir = output_dir.resolve()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="voidx-backend-image-", dir=output_dir.parent) as staging_name:
        staging = Path(staging_name)
        image = staging / "image"
        image.mkdir()
        _copy_tree(python_root.resolve(), image / "python")
        _copy_site_packages(site_packages.resolve(), image / "site-packages")
        _safe_extract_wheel(wheel.resolve(), image / "site-packages")

        fingerprint = hash_image_tree(image)
        manifest = make_manifest(
            version=version,
            target=target,
            image_fingerprint=fingerprint,
            python_relative=_python_relative(python_root),
            site_packages_relative="site-packages",
            source_revision=source_revision,
        )
        (image / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if output_dir.exists():
            shutil.rmtree(output_dir)
        image.rename(output_dir)
    return manifest


def _git_revision(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--install-root", type=Path)
    parser.add_argument("--python-root", type=Path)
    parser.add_argument("--site-packages", type=Path)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--version", default=BACKEND_VERSION)
    parser.add_argument("--target", default=target_triple())
    parser.add_argument("--source-revision", default="")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]

    if args.install_root is not None:
        if args.python_root is not None or args.site_packages is not None:
            parser.error("--install-root cannot be combined with --python-root or --site-packages")
        python_root, site_packages = resolve_runtime_inputs(args.install_root)
    elif args.python_root is not None and args.site_packages is not None:
        python_root, site_packages = args.python_root, args.site_packages
    else:
        parser.error("provide --install-root or both --python-root and --site-packages")

    manifest = build_image(
        output_dir=args.output,
        python_root=python_root,
        site_packages=site_packages,
        wheel=args.wheel,
        version=args.version,
        target=args.target,
        source_revision=args.source_revision or _git_revision(root),
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
