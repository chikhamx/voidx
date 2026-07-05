#!/usr/bin/env bash
# voidx desktop one-click build script.
# Builds the frontend (../../frontend) then runs `tauri build` to produce
# native desktop bundles (dmg/app on macOS, msi on Windows, deb/appimage on Linux).
#
# Usage:
#   ./build.sh            # build frontend + tauri bundle
#   ./build.sh --clean    # remove old build artifacts first
#   ./build.sh --no-frontend  # skip frontend build (assume dist is fresh)
#   ./build.sh --help     # show this help
#
# The Python backend is NOT bundled. At runtime the Tauri app resolves the
# voidx Python interpreter via resolve_python() in tauri/src/main.rs
# (.venv, ~/.local/share/voidx/venv, PATH, etc.). This script keeps that
# contract: it only builds the native shell + frontend assets.

set -euo pipefail

# --- Paths -----------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_DIR="$SCRIPT_DIR"
ROOT_DIR="$(cd "$DESKTOP_DIR/.." && pwd)"
FRONTEND_DIR="$ROOT_DIR/frontend"
TAURI_DIR="$DESKTOP_DIR/tauri"
BUNDLE_DIR="$TAURI_DIR/target/release/bundle"

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
  sed -n '2,16p' "$0"
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

# --- Rust/Cargo PATH bootstrap ---------------------------------------------
# rustup installs cargo as a proxy under ~/.cargo/bin, but that dir is only
# added to PATH when ~/.cargo/env is sourced (typically via ~/.zshrc /
# ~/.bash_profile). Non-interactive shells (CI, tool runners) don't load
# those files, so cargo appears "missing" even though it's installed.
# Source the env file if present so the dependency check below succeeds.
if [ -z "${CARGO_HOME:-}" ]; then
  CARGO_HOME="$HOME/.cargo"
fi
if [ -f "$CARGO_HOME/env" ]; then
  # shellcheck disable=SC1091
  . "$CARGO_HOME/env"
fi

# --- Dependency checks -----------------------------------------------------
check_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing dependency: '$1' (please install it first)"
}

log "checking dependencies..."
check_cmd node
check_cmd npm
check_cmd cargo
# tauri-cli is invoked via npm script in desktop/, so just ensure npm deps resolve.
ok "node $(node -v), npm $(npm -v), cargo $(cargo --version 2>&1 | awk '{print $1" "$2}')"

# --- Clean -----------------------------------------------------------------
if [ "$CLEAN" = "1" ]; then
  log "cleaning old build artifacts..."
  rm -rf "$TAURI_DIR/target/release/bundle"
  rm -rf "$FRONTEND_DIR/dist"
  ok "cleaned"
fi

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
( cd "$DESKTOP_DIR" && npm run build )
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
