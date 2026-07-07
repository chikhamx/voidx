#!/usr/bin/env bash
# voidx dev installer — builds voidx + voidx-cli from source into an isolated venv.
#
# Temporary fallback while voidx-cli is blocked on PyPI (429 new-project quota).
# Does not touch PyPI; builds both wheels locally and pip-installs them.
#
# Usage:
#   bash scripts/install_dev.sh
#   # or from a clean clone:
#   curl -fsSL https://raw.githubusercontent.com/chikhamx/voidx/master/scripts/install_dev.sh | bash
#
# Environment variables:
#   VOIDX_DEV_SOURCE  — git URL or local path to voidx source (default: clone master)
#   VOIDX_HOME        — venv location (default: ~/.local/share/voidx/dev-venv)
#   VOIDX_BIN_DIR     — symlink location (default: ~/.local/bin)
#   VOIDX_PYTHON      — explicit python interpreter (default: auto-detect 3.11+)

set -euo pipefail

DEFAULT_SOURCE="https://github.com/chikhamx/voidx.git"
DEV_BRANCH="${VOIDX_DEV_BRANCH:-master}"

# ── Colors ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()  { printf "${CYAN}  ℹ${NC} %s\n" "$*"; }
ok()    { printf "${GREEN}  ✅${NC} %s\n" "$*"; }
warn()  { printf "${YELLOW}  ⚠${NC} %s\n" "$*"; }
err()   { printf "${RED}  ❌${NC} %s\n" "$*" >&2; }
step()  { printf "\n${BOLD}  [%s]${NC} %s\n" "$1" "$2"; }
die()   { err "$*"; exit 1; }

# ── Resolve paths ───────────────────────────────────────────────────────────
VOIDX_HOME="${VOIDX_HOME:-$HOME/.local/share/voidx/dev-venv}"
VOIDX_BIN_DIR="${VOIDX_BIN_DIR:-$HOME/.local/bin}"
SOURCE="${VOIDX_DEV_SOURCE:-$DEFAULT_SOURCE}"

# ── Python detection ────────────────────────────────────────────────────────
select_python() {
    if [ -n "${VOIDX_PYTHON:-}" ]; then
        if "$VOIDX_PYTHON" -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" 2>/dev/null; then
            echo "$VOIDX_PYTHON"
            return
        fi
        die "VOIDX_PYTHON ($VOIDX_PYTHON) is not Python 3.11+."
    fi

    for cmd in python3.12 python3.11 python3 python; do
        if command -v "$cmd" &>/dev/null; then
            if "$cmd" -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" 2>/dev/null; then
                echo "$cmd"
                return
            fi
        fi
    done
    die "Python 3.11+ not found. Install it or set VOIDX_PYTHON."
}

PYTHON_BIN="$(select_python)"
info "Using Python: $("$PYTHON_BIN" --version 2>&1)"

# ── Prepare source ──────────────────────────────────────────────────────────
TMP_SRC=""
cleanup() {
    if [ -n "$TMP_SRC" ] && [ -d "$TMP_SRC" ]; then
        rm -rf "$TMP_SRC"
    fi
}
trap cleanup EXIT

prepare_source() {
    if [ -d "$SOURCE" ] && [ -f "$SOURCE/pyproject.toml" ]; then
        # Local path — use directly
        SRC_DIR="$SOURCE"
        info "Using local source: $SRC_DIR"
    elif [[ "$SOURCE" == http* ]] || [[ "$SOURCE" == git@* ]]; then
        # Git URL — clone to temp
        TMP_SRC="$(mktemp -d -t voidx-dev-src-XXXXXX)"
        step "git" "Cloning $SOURCE ($DEV_BRANCH)…"
        git clone --depth 1 --branch "$DEV_BRANCH" "$SOURCE" "$TMP_SRC"
        SRC_DIR="$TMP_SRC"
        ok "Cloned to $SRC_DIR"
    else
        die "VOIDX_DEV_SOURCE must be a local path or git URL, got: $SOURCE"
    fi
}

# ── Build + install ─────────────────────────────────────────────────────────
build_and_install() {
    # Clean old dev venv
    if [ -d "$VOIDX_HOME" ]; then
        step "clean" "Removing old dev venv at $VOIDX_HOME…"
        rm -rf "$VOIDX_HOME"
    fi

    step "venv" "Creating isolated venv…"
    "$PYTHON_BIN" -m venv "$VOIDX_HOME"
    local venv_pip="$VOIDX_HOME/bin/pip"
    local venv_python="$VOIDX_HOME/bin/python"

    # Upgrade pip + install build tool
    "$venv_pip" install --upgrade pip --quiet
    "$venv_pip" install build --quiet

    # Build voidx wheel
    step "build" "Building voidx wheel…"
    "$venv_python" -m build --wheel --outdir /tmp/voidx-dev-dist "$SRC_DIR"
    ok "voidx wheel built"

    # Build voidx-cli wheel
    step "build" "Building voidx-cli wheel…"
    "$venv_python" -m build --wheel --outdir /tmp/voidx-dev-dist "$SRC_DIR/tui"
    ok "voidx-cli wheel built"

    # Install both wheels (no-deps for voidx-cli to avoid pulling voidx from PyPI)
    step "install" "Installing wheels into venv…"
    local dist_dir="/tmp/voidx-dev-dist"
    local voidx_wheel="$(ls -t "$dist_dir"/voidx-*.whl | head -1)"
    local cli_wheel="$(ls -t "$dist_dir"/voidx_cli-*.whl | head -1)"

    "$venv_pip" install --force-reinstall "$voidx_wheel" --quiet
    "$venv_pip" install --force-reinstall --no-deps "$cli_wheel" --quiet
    ok "Both packages installed"

    # Verify
    "$venv_python" -c "import voidx; import voidx_cli; print(f'voidx {voidx.__version__}, voidx_cli {voidx_cli.__version__}')"

    # Symlink
    mkdir -p "$VOIDX_BIN_DIR"
    local target="$VOIDX_HOME/bin/voidx"
    local link="$VOIDX_BIN_DIR/voidx"

    if [ -L "$link" ] || [ -e "$link" ]; then
        rm -f "$link"
    fi
    ln -s "$target" "$link"
    ok "Symlinked: $link → $target"
}

# ── Main ────────────────────────────────────────────────────────────────────
step "start" "voidx dev installer (source build, bypasses PyPI)"

prepare_source
build_and_install

echo ""
ok "Done! voidx is installed from source."
info "Run: voidx version"
info "Venv: $VOIDX_HOME"
info "To uninstall: rm -rf $VOIDX_HOME $VOIDX_BIN_DIR/voidx"
echo ""
