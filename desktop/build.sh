#!/usr/bin/env bash
# voidx desktop one-click build script.
# Builds the backend image and frontend (../../frontend), then runs `tauri build`
# to produce native desktop bundles (dmg/app on macOS, msi on Windows,
# deb/appimage on Linux).
#
# Usage:
#   ./build.sh            # build backend image + frontend + tauri bundle
#   ./build.sh --clean    # remove old build artifacts first
#   ./build.sh --no-frontend  # skip frontend build (assume dist is fresh)
#   ./build.sh --help     # show this help
#
# The backend image contains the platform Python runtime, dependencies, and
# current voidx wheel. At runtime Tauri installs it into ~/.voidx/runtime.

set -euo pipefail

if [ "$(uname -s)" = "Darwin" ]; then
  # create-dmg invokes Perl, which can fail when macOS exposes an unavailable C.UTF-8 locale.
  export LC_ALL=C
  export LANG=C
  export LC_CTYPE=C
fi

# --- Paths -----------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_DIR="$SCRIPT_DIR"
ROOT_DIR="$(cd "$DESKTOP_DIR/.." && pwd)"
FRONTEND_DIR="$ROOT_DIR/frontend"
TAURI_DIR="$DESKTOP_DIR/tauri"
BUNDLE_DIR="$TAURI_DIR/target/release/bundle"
BACKEND_RESOURCE_DIR="$TAURI_DIR/resources/backend"
BACKEND_WORK_DIR="$TAURI_DIR/target/desktop-backend-build"

if [ "$(uname -s)" = "Darwin" ]; then
  export PATH="$DESKTOP_DIR/tools:$PATH"
fi

restore_backend_resource_placeholder() {
  mkdir -p "$BACKEND_RESOURCE_DIR"
  touch "$BACKEND_RESOURCE_DIR/.gitkeep"
}
trap restore_backend_resource_placeholder EXIT

# --- Args ------------------------------------------------------------------
CLEAN=0
NO_FRONTEND=0
SHOW_HELP=0
for arg in "$@"; do
  case "$arg" in
    --clean) CLEAN=1 ;;
    --no-frontend) NO_FRONTEND=1 ;;
    --help|-h) SHOW_HELP=1 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

if [ "$SHOW_HELP" = "1" ]; then
  sed -n '2,15p' "$0"
  exit 0
fi

# --- Logging ---------------------------------------------------------------
if [ -t 1 ] && command -v tput >/dev/null 2>&1; then
  GREEN="$(tput setaf 2)"; YELLOW="$(tput setaf 3)"; RED="$(tput setaf 1)"; BOLD="$(tput bold)"; RESET="$(tput sgr0)"
else
  GREEN=""; YELLOW=""; RED=""; BOLD=""; RESET=""
fi
log()  { echo "${BOLD}[build]${RESET} $*"; }
ok()   { echo "${GREEN}✓${RESET} $*"; }
warn() { echo "${YELLOW}!${RESET} $*" >&2; }
die()  { echo "${RED}✗${RESET} $*" >&2; exit 1; }

detach_stale_macos_dmg_mounts() {
  [ "$(uname -s)" = "Darwin" ] || return 0
  command -v hdiutil >/dev/null 2>&1 || return 0

  hdiutil info | awk -v prefix="$BUNDLE_DIR/macos/" '
    /^image-path/ {
      path = $0
      sub(/^image-path[[:space:]]*:[[:space:]]*/, "", path)
      next
    }
    /^\/dev\// {
      if (index(path, prefix) == 1) print $1
      path = ""
    }
  ' | while IFS= read -r device; do
    [ -n "$device" ] || continue
    if ! hdiutil detach "$device" >/dev/null 2>&1; then
      hdiutil detach -force "$device" >/dev/null 2>&1 || warn "could not detach stale DMG device $device"
    fi
  done
}

create_macos_dmg() {
  local app_bundle="$BUNDLE_DIR/macos/voidx.app"
  local staging_dir
  local dmg_path

  case "$(uname -m)" in
    arm64) MACOS_BUNDLE_ARCH="aarch64" ;;
    x86_64) MACOS_BUNDLE_ARCH="x86_64" ;;
    *) MACOS_BUNDLE_ARCH="$(uname -m)" ;;
  esac
  dmg_path="$BUNDLE_DIR/macos/voidx_${BACKEND_VERSION}_${MACOS_BUNDLE_ARCH}.dmg"
  staging_dir="$(mktemp -d "$BUNDLE_DIR/macos/.dmg-staging.XXXXXX")"

  cp -R "$app_bundle" "$staging_dir/voidx.app"
  ln -s /Applications "$staging_dir/Applications"
  rm -f "$dmg_path"
  if ! hdiutil create \
    -volname "voidx" \
    -srcfolder "$staging_dir" \
    -ov \
    -format UDZO \
    "$dmg_path"; then
    rm -rf "$staging_dir"
    return 1
  fi
  rm -rf "$staging_dir"
  [ -f "$dmg_path" ] || die "macOS DMG was not produced at $dmg_path"
  ok "macOS DMG ready at $dmg_path"
}

# --- Rust/Cargo PATH bootstrap ---------------------------------------------
if [ -z "${CARGO_HOME:-}" ]; then
  CARGO_HOME="$HOME/.cargo"
fi
if [ -f "$CARGO_HOME/env" ]; then
  # shellcheck disable=SC1091
  . "$CARGO_HOME/env"
fi

# --- Dependency checks

# --- Dependency checks -----------------------------------------------------
check_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing dependency: '$1' (please install it first)"
}

log "checking dependencies..."
check_cmd node
check_cmd npm
check_cmd cargo
ok "node $(node -v), npm $(npm -v), cargo $(cargo --version 2>&1 | awk '{print $1" "$2}')"

# --- Clean -----------------------------------------------------------------
if [ "$CLEAN" = "1" ]; then
  log "cleaning old build artifacts..."
  if [ "$(uname -s)" = "Darwin" ] && [ -d "$BUNDLE_DIR/macos" ]; then
    detach_stale_macos_dmg_mounts
  fi
  rm -rf "$TAURI_DIR/target/release/bundle"
  rm -rf "$FRONTEND_DIR/dist"
  ok "cleaned"
fi

# --- Backend image build ---------------------------------------------------
log "building bundled backend image..."
[ -f "$ROOT_DIR/python.py" ] || die "python launcher not found at $ROOT_DIR/python.py"
[ -f "$ROOT_DIR/scripts/package.py" ] || die "package builder not found at $ROOT_DIR/scripts/package.py"

BACKEND_VERSION="$(cd "$ROOT_DIR" && "$ROOT_DIR/python.py" -c 'from scripts.desktop_backend_manifest import BACKEND_VERSION; print(BACKEND_VERSION)')"
[ -n "$BACKEND_VERSION" ] || die "could not read backend version from src/voidx/platform/version.py"
BACKEND_TARGET="$(cd "$ROOT_DIR" && "$ROOT_DIR/python.py" -c 'from scripts.desktop_backend_manifest import target_triple; print(target_triple())')"
INSTALL_ROOT="${VOIDX_INSTALL_ROOT:-${VOIDX_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/voidx}}"
WHEEL_DIR="$BACKEND_WORK_DIR/wheels"

rm -rf "$BACKEND_WORK_DIR" "$BACKEND_RESOURCE_DIR"
mkdir -p "$WHEEL_DIR" "$BACKEND_RESOURCE_DIR"
(
  cd "$ROOT_DIR"
  "$ROOT_DIR/python.py" scripts/package.py \
    --format wheel \
    --out-dir "$WHEEL_DIR" \
    --clean
)
WHEEL_PATH="$(find "$WHEEL_DIR" -maxdepth 1 -type f -name "voidx-${BACKEND_VERSION}-*.whl" ! -name 'voidx_cli-*' -print -quit)"
[ -n "$WHEEL_PATH" ] || die "backend wheel for version $BACKEND_VERSION was not produced"
(
  cd "$ROOT_DIR"
  "$ROOT_DIR/python.py" -m scripts.build_desktop_backend \
    --output "$BACKEND_RESOURCE_DIR" \
    --install-root "$INSTALL_ROOT" \
    --wheel "$WHEEL_PATH" \
    --version "$BACKEND_VERSION" \
    --target "$BACKEND_TARGET"
)
[ -f "$BACKEND_RESOURCE_DIR/manifest.json" ] || die "backend image manifest was not produced"
[ -d "$BACKEND_RESOURCE_DIR/python" ] || die "backend image Python runtime was not produced"
[ -d "$BACKEND_RESOURCE_DIR/site-packages" ] || die "backend image site-packages was not produced"
ok "backend image ready at $BACKEND_RESOURCE_DIR ($BACKEND_TARGET)"

# --- Frontend build --------------------------------------------------------
if [ "$NO_FRONTEND" = "0" ]; then
  log "building frontend in $FRONTEND_DIR..."
  [ -f "$FRONTEND_DIR/package.json" ] || die "frontend package.json not found at $FRONTEND_DIR/package.json"
  ( cd "$FRONTEND_DIR" && npm install --no-audit --no-fund )
  ( cd "$FRONTEND_DIR" && npm run build )
  [ -d "$FRONTEND_DIR/dist" ] || die "frontend build did not produce dist/"
  ok "frontend dist ready at $FRONTEND_DIR/dist"
else
  warn "skipping frontend build (--no-frontend); assuming $FRONTEND_DIR/dist is fresh"
  [ -d "$FRONTEND_DIR/dist" ] || die "frontend dist missing — run without --no-frontend first"
fi

# --- Tauri build -----------------------------------------------------------
log "building tauri app in $DESKTOP_DIR..."
[ -f "$DESKTOP_DIR/package.json" ] || die "desktop package.json not found"
( cd "$DESKTOP_DIR" && npm install --no-audit --no-fund )
if [ "$(uname -s)" = "Darwin" ] && [ -d "$BUNDLE_DIR/macos" ]; then
  detach_stale_macos_dmg_mounts
  find "$BUNDLE_DIR/macos" -maxdepth 1 -type f \
    \( -name "voidx_*.dmg" -o -name "rw.*.dmg" \) -delete
fi
if [ "$(uname -s)" = "Darwin" ]; then
  ( cd "$DESKTOP_DIR" && npm run build -- --bundles app )
  create_macos_dmg
else
  ( cd "$DESKTOP_DIR" && npm run build )
fi
ok "tauri build complete"

# --- Report artifacts ------------------------------------------------------
log "bundle artifacts:"
if [ -d "$BUNDLE_DIR" ]; then
  find "$BUNDLE_DIR" -maxdepth 3 -type f \( -name "*.dmg" -o -name "*.app" -o -name "*.msi" -o -name "*.deb" -o -name "*.AppImage" -o -name "*.rpm" \) -print0 2>/dev/null | while IFS= read -r -d '' f; do
    size="$(du -h "$f" | awk '{print $1}')"
    ok "$(basename "$f")  ($size)  -> $f"
  done
else
  warn "no bundle directory found at $BUNDLE_DIR"
fi

echo
ok "done. native bundles are under: $BUNDLE_DIR"
