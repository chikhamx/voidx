#!/usr/bin/env bash
# voidx 旧版清理脚本
# 清理 v1.x 时代通过系统 Python / pip / pipx 安装的 voidx
#
# 用法:
#   curl -fsSL https://raw.githubusercontent.com/chikhamx/voidx/master/scripts/cleanup-legacy.sh | bash

set -euo pipefail

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

CLEANED=0

# ── 1. 检测 pip 安装的 voidx ──────────────────────────────────────────────
detect_pip() {
    local cmd="$1"
    if ! command -v "$cmd" &>/dev/null; then
        return
    fi

    local result
    result=$("$cmd" show voidx 2>/dev/null || true)
    if [ -z "$result" ]; then
        return
    fi

    local version location
    version=$(echo "$result" | grep "^Version:" | awk '{print $2}')
    location=$(echo "$result" | grep "^Location:" | awk '{print $2}')

    if [ -n "$version" ]; then
        warn "发现 pip 安装的 voidx ${version}（${cmd}，路径: ${location}）"
        if "$cmd" uninstall voidx -y 2>/dev/null; then
            ok "已卸载 pip 安装的 voidx（${cmd}）"
            CLEANED=$((CLEANED + 1))
        else
            err "卸载失败，请手动执行: ${cmd} uninstall voidx"
        fi
    fi
}

# pip3 / pip
detect_pip "pip3"
detect_pip "pip"

# ── 2. 检测 pipx 安装的 voidx ─────────────────────────────────────────────
if command -v pipx &>/dev/null; then
    if pipx list 2>/dev/null | grep -q "voidx"; then
        local_version=$(pipx list 2>/dev/null | grep -A1 "voidx" | grep "version" | awk '{print $NF}' || echo "unknown")
        warn "发现 pipx 安装的 voidx ${local_version}"
        if pipx uninstall voidx 2>/dev/null; then
            ok "已卸载 pipx 安装的 voidx"
            CLEANED=$((CLEANED + 1))
        else
            err "卸载失败，请手动执行: pipx uninstall voidx"
        fi
    fi
fi

# ── 3. 检测残留的符号链接 ─────────────────────────────────────────────────
check_link() {
    local link_path="$1"
    if [ ! -e "$link_path" ]; then
        return
    fi

    local target
    target=$(readlink "$link_path" 2>/dev/null || echo "")

    # 如果指向系统 Python 的 site-packages 或 pipx，属于旧版
    # 指向 ~/.local/share/voidx/venv/ 或 npm-venv/ 的是 v2.x，不清理
    if [ -n "$target" ]; then
        case "$target" in
            */site-packages/*|*/dist-packages/*|*/.local/pipx/*|*/pipx/venvs/*)
                warn "发现旧版符号链接: ${link_path} → ${target}"
                rm -f "$link_path"
                ok "已删除旧版符号链接: ${link_path}"
                CLEANED=$((CLEANED + 1))
                ;;
        esac
    fi

    # 悬空链接（目标不存在）
    if [ -L "$link_path" ] && [ ! -e "$target" ]; then
        warn "发现悬空符号链接: ${link_path} → ${target}"
        rm -f "$link_path"
        ok "已删除悬空符号链接: ${link_path}"
        CLEANED=$((CLEANED + 1))
    fi
}

check_link "${HOME}/.local/bin/voidx"
check_link "/usr/local/bin/voidx"

# ── 4. 检测系统 Python site-packages 中的残留 ─────────────────────────────
for python_cmd in python3 python python3.12 python3.11; do
    if ! command -v "$python_cmd" &>/dev/null; then
        continue
    fi
    site_dir=$("$python_cmd" -c "import site; print(site.getsitepackages()[0])" 2>/dev/null || continue)
    if [ -d "${site_dir}/voidx" ]; then
        warn "发现系统 Python 中的 voidx 残留: ${site_dir}/voidx"
        info "请手动清理: ${python_cmd} -m pip uninstall voidx -y"
    fi
done

# ── 5. 检测旧版配置目录 ────────────────────────────────────────────────────
OLD_CONFIG="${HOME}/.voidx"
if [ -d "${OLD_CONFIG}" ]; then
    # v2.x 也用 ~/.voidx/skills，只清理非 skills 的旧文件
    old_files=()
    for f in "${OLD_CONFIG}"/*; do
        case "$(basename "$f")" in
            skills) ;;  # v2.x 技能目录，保留
            *) old_files+=("$f") ;;
        esac
    done
    if [ ${#old_files[@]} -gt 0 ]; then
        info "旧版配置目录 ${OLD_CONFIG} 中有残留文件:"
        for f in "${old_files[@]}"; do
            printf "    %s\n" "$(basename "$f")"
        done
        info "如需清理，请手动执行: rm -rf ${OLD_CONFIG}（会同时删除 v2.x 技能）"
    fi
fi

# ── 结果 ────────────────────────────────────────────────────────────────────
printf "\n"
if [ "$CLEANED" -gt 0 ]; then
    printf "${GREEN}${BOLD}✅ 清理完成，共清理 ${CLEANED} 项${NC}\n\n"
    printf "现在可以使用以下命令安装 voidx 2.1.0:\n"
    printf "  ${CYAN}curl -fsSL https://raw.githubusercontent.com/chikhamx/voidx/master/scripts/install.sh | bash${NC}\n\n"
else
    printf "${GREEN}${BOLD}✅ 未发现旧版 voidx 安装${NC}\n\n"
fi
