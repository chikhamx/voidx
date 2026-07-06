# TUI 模块独立化 — 技术设计文档

## Status

Approved — 方案 A（顶层独立包）已选定，待实施。

## Context

当前 TUI（纯终端 UI）实现位于 `src/voidx/ui/tui/`，共 19 个 `.py` 文件（含 `__init__.py`）、4162 行，是 `src/voidx/ui/` 的一个子目录。仓库顶层已有 `frontend/`（web 前端，独立 npm 项目）和 `desktop/`（Tauri 桌面壳）两个平级的 UI 实现层。TUI 作为终端 UI 实现，在物理位置上应与 `frontend/`、`desktop/` 平级，以体现"三种前端实现"的对称结构。

TUI 依赖的通用 UI 基础设施（`output/`、`tools/`、`protocol/`、`gateway/`）是 web/desktop/TUI 共用的，必须留在 `src/voidx/ui/` 内。

### 现状依赖关系

```
src/voidx/ui/
├── tui/            ← 本次迁移目标（19 文件，纯终端 UI）
├── output/         ← 通用输出层（dock/tree/events/types）— 共用，保留
├── protocol/       ← v2 JSON-RPC 协议 — 共用，保留
├── gateway/        ← WebSocket 网关（web 前端用）— 共用，保留
├── tools/          ← UI 侧工具（clipboard/file_picker/skill_picker）— 共用，保留
├── command_catalog.py / commands.py / session.py / transcript.py / frontend.py
```

**TUI 向上被引用点（运行时仅 1 处直接引用）：**
- `src/voidx/runtime/ui.py:183` — `_LazyAttr("voidx.ui.tui", "PureTui")` lazy import
- `src/voidx/agent/graph/run_loop.py:23,174` — `from voidx.runtime.ui import PureTui` 并实例化（通过 `runtime.ui` 间接引用，不直接 import TUI，无需改）

**TUI 向下依赖：**
- `voidx.ui.output.*`（dock/tree/events/types/formatting）
- `voidx.ui.tools.*`（clipboard_image/clipboard_text/file_picker/skill_picker/attachment_tokens）
- `voidx.logging`、`voidx.paths`、`voidx.config`、`voidx.skills.service`、`voidx.llm.usage`

**动态引用（易漏改）：**
- `src/voidx/ui/tui/clipboard_mixin.py:71,79` — `sys.modules.get("voidx.ui.tui.app")` 硬编码字符串

**测试：** `tests/test_ui/tui/` 下 21 个 `.py` 文件（18 个 `test_*.py` + `__init__.py` + `conftest.py` + `tui_helpers.py`），通过 `from voidx.ui.tui import PureTui` 引用。迁移时 `conftest.py` 和 `tui_helpers.py` 也需改 import（`tui_helpers.py:13` 有 `from voidx.ui.tui import PureTui`）。

## Goals and Non-Goals

### Goals

- TUI 物理目录从 `src/voidx/ui/tui/` 迁出为顶层独立包 `tui/`，与 `frontend/`、`desktop/` 平级
- TUI 作为独立 Python 包 `voidx_tui` 发布，依赖 `voidx`
- 通用 UI 基础设施（output/tools/protocol/gateway）保留在 `src/voidx/ui/` 内
- 所有现有测试通过，运行时行为不变
- 打包流程支持双 wheel 构建（`voidx` + `voidx_tui`）

### Non-Goals

- 不重构 TUI 内部代码结构
- 不改动 `output/`、`tools/`、`protocol/`、`gateway/` 的内容
- 不调整 web/desktop 前端
- 不改 desktop Rust 侧（sidecar 通过 `voidx` CLI 启动，不直接 import TUI）

## Architecture

### 方案对比

| 维度 | 方案 A：顶层独立包 `tui/` | 方案 B：包内提升 `src/voidx/tui/` |
|------|------------------------|--------------------------------|
| 物理位置 | 仓库顶层 `tui/src/voidx_tui/` | `src/voidx/tui/`（与 `ui/` 平级） |
| import 路径 | `voidx_tui.*` | `voidx.tui.*` |
| Python 包 | 独立包 `voidx_tui`，依赖 `voidx` | 仍在 `voidx` 包内 |
| 默认安装 | `voidx` 需声明对 `voidx_tui` 的默认运行时依赖，避免只安装主 wheel 后 TUI 启动失败 | 自动随主包安装 |
| 打包影响 | 需改 `package.py` 支持多包，或新增第二个 pyproject；开发需双 `pip install -e` | 零影响，`setuptools.packages.find` 自动发现 |
| 循环依赖 | `voidx_tui` → `voidx`（正常）；`voidx` → `voidx_tui`（lazy，运行时需 voidx_tui 已安装） | 无（同包内 lazy import） |
| 与 frontend 平级语义 | ✅ 物理顶层目录 | ⚠️ 在 src/voidx 内，非顶层 |
| 迁移成本 | 高（打包/安装/CI 全要改） | 低（纯文件移动 + import 改名） |
| 风险 | 高（wheel 构建、开发环境、desktop sidecar 启动链路） | 低 |

### 选定方案：A（顶层独立包）

**理由：**
1. 用户明确要求"要改就改彻底"——TUI 作为第三种前端实现，应在仓库顶层与 `frontend/`、`desktop/` 物理平级，结构对称性最强。
2. 独立包 `voidx_tui` 可独立版本化、独立发布，未来若 TUI 需单独发版（如 desktop 打包只需 TUI wheel）更灵活。
3. 循环依赖可控：`voidx_tui` → `voidx` 是正常依赖方向；`voidx` → `voidx_tui` 通过 `runtime/ui.py` 的 `_LazyAttr` lazy import，运行时才解析，不构成安装期循环。主包发布必须确保 `voidx_tui` 被默认安装；开发环境需用同一条命令安装两个 editable：`pip install -e . -e ./tui`。
4. 打包改造边界清晰但不能只追加 `return` 后逻辑：`package.py` 需重构为主包与 TUI 包顺序构建，并覆盖 `wheel` / `sdist` / `all` 三种格式。

### 方案 A 迁移后的目录结构

```
仓库根/
├── src/voidx/              ← 主包（不变）
│   ├── ui/                 ← 保留：通用 UI 基础设施
│   │   ├── output/
│   │   ├── protocol/
│   │   ├── gateway/
│   │   ├── tools/
│   │   ├── command_catalog.py
│   │   ├── commands.py
│   │   ├── session.py
│   │   ├── transcript.py
│   │   └── frontend.py
│   ├── runtime/
│   ├── agent/
│   └── ...
├── tui/                    ← 新增：独立包
│   ├── pyproject.toml      ← voidx_tui 包定义
│   └── src/voidx_tui/      ← 从 src/voidx/ui/tui/ 迁移至此
│       ├── __init__.py
│       ├── app.py
│       ├── activity.py
│       ├── choice_mixin.py
│       ├── clipboard_mixin.py
│       ├── helpers.py
│       ├── input.py
│       ├── overlays.py
│       ├── panels.py
│       ├── parser.py
│       ├── render_activity.py
│       ├── render_frame.py
│       ├── render_input.py
│       ├── render_status.py
│       ├── render_todo.py
│       ├── renderer.py
│       ├── state.py
│       ├── terminal_mixin.py
│       └── text_prompt_mixin.py
├── frontend/               ← 不变
├── desktop/                ← 不变
├── tests/test_ui/tui/      ← 不变（import 路径改）
├── pyproject.toml          ← 改：pytest pythonpath 加 tui/src
└── scripts/package.py      ← 改：支持双包构建
```

### Import 变更规则

| 原路径 | 新路径 | 出现位置 |
|--------|--------|---------|
| `voidx.ui.tui` | `voidx_tui` | TUI 内部所有自引用、runtime/ui.py、测试 |
| `voidx.ui.tui.app`（sys.modules 字符串） | `voidx_tui.app` | clipboard_mixin.py:71,79 |
| `voidx.ui.output.*` | 不变 | TUI 内部 |
| `voidx.ui.tools.*` | 不变 | TUI 内部 |
| `voidx.logging/paths/config/skills/llm` | 不变 | TUI 内部 |

## Data Model

不涉及数据模型变更。

## API Contract

### TUI 对外导出（不变）

- `voidx_tui.PureTui` — 主类，由 `runtime/ui.py` lazy import
- `voidx_tui._dump_transcript_log` — 内部工具
- `voidx_tui._ENTER_TERMINAL_SEQUENCE` / `_EXIT_TERMINAL_SEQUENCE` / `_rendered_row_count` — helpers 导出

### 调用方变更

```python
# src/voidx/runtime/ui.py
- PureTui = _LazyAttr("voidx.ui.tui", "PureTui")
+ PureTui = _LazyAttr("voidx_tui", "PureTui")

# src/voidx/agent/graph/run_loop.py
# 通过 runtime.ui 间接引用，无需直接改
```

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| clipboard_mixin 的 sys.modules 字符串漏改 | 运行时 paste 功能静默降级到系统实现；通过 grep 全局搜索 `voidx.ui.tui` 确保零残留 |
| 测试 import 路径未更新 | pytest 收集失败，立即暴露；迁移时同步改 tests/test_ui/tui/ 下 21 个文件（含 conftest.py、tui_helpers.py） |
| 遗漏 TUI 内部相对 import | TUI 内部用绝对 import（`from voidx.ui.tui.x import`），需全量替换为 `from voidx_tui.x import` |
| 开发环境未双 pip install | 运行时 `voidx_tui` import 失败，TUI 启动报错；开发文档需说明用同一条命令安装两个 editable：`pip install -e . -e ./tui` |
| 默认发布未安装 `voidx_tui` | 用户只安装 `voidx` wheel 后默认 TUI 启动失败；主包需声明 `voidx_tui=={version}` 运行时依赖，NPM/sidecar 发布需安装双 wheel |
| wheel 构建顺序错误 | `voidx_tui` wheel 依赖 `voidx`，构建须先 `voidx` 后 `voidx_tui`；`package.py` 的 pip fallback 对本地构建使用 `--no-deps` 避免重复拉取 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 选方案 A（顶层独立包） | 方案 B（包内提升） | 用户要求"改彻底"；TUI 作为第三种前端实现应与 frontend/desktop 物理平级，独立包可独立版本化发布 |
| 保留通用 UI 基础设施在 ui/ | 一并迁出 | output/tools/protocol 是 web/desktop/TUI 共用，不属于 TUI 专属 |
| 不改 TUI 内部文件名/结构 | 重命名/拆分 | 非 Non-Goal，本次只做物理迁移 |
| `voidx_tui` 版本与 `voidx` 同步 | 独立版本号 | 简化发布流程，`package.py --check-only` 必须检查 `src/voidx/__init__.py` 与 `tui/src/voidx_tui/__init__.py` 完全一致 |

## Open Questions

- [x] 用户是否接受方案 B（包内提升），还是坚持方案 A（物理顶层独立包）？→ **选定方案 A**
- [x] 若选方案 A，是否接受开发环境需双 `pip install -e` 和 package.py 改造？→ **接受**

## Implementation Plan

### 1. 新建 tui/ 包结构

```bash
mkdir -p tui/src/voidx_tui
```

### 2. 移动文件（git mv 保留历史）

```bash
git mv src/voidx/ui/tui/__init__.py tui/src/voidx_tui/__init__.py
git mv src/voidx/ui/tui/app.py tui/src/voidx_tui/app.py
# ... 其余 17 个文件同理
```

### 3. 批量替换 import（TUI 内部 + 测试）

```bash
# TUI 内部自引用
grep -rl 'voidx\.ui\.tui' tui/src/voidx_tui/ | xargs sed -i '' 's/voidx\.ui\.tui/voidx_tui/g'
# 测试
grep -rl 'voidx\.ui\.tui' tests/test_ui/tui/ | xargs sed -i '' 's/voidx\.ui\.tui/voidx_tui/g'
# runtime/ui.py
sed -i '' 's/voidx\.ui\.tui/voidx_tui/g' src/voidx/runtime/ui.py
```

验证零残留：
```bash
grep -rn 'voidx\.ui\.tui' --include='*.py' src/ tests/ tui/ | grep -v __pycache__
# 应无输出
```

### 4. 新建 tui/pyproject.toml

```toml
[project]
name = "voidx_tui"
dynamic = ["version"]
description = "Terminal UI for voidx."
license = {text = "MIT"}
authors = [{name = "chikhamx"}]
requires-python = ">=3.11"
dependencies = [
    "voidx==3.4.4",  # 迁移时与 src/voidx/__init__.py 保持一致；发布时同步更新
    "rich>=13.9.0",
]

[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[tool.setuptools.dynamic]
version = {attr = "voidx_tui.__version__"}

[tool.setuptools.packages.find]
where = ["src"]
```

### 5. `tui/src/voidx_tui/__init__.py` 加同步版本号

```python
__version__ = "3.4.4"  # 必须与 src/voidx/__init__.py 保持一致
```

### 6. 改主 `pyproject.toml`（默认 TUI 依赖 + pytest pythonpath）

主包需默认依赖同步版本的 `voidx_tui`，避免只安装 `voidx` wheel 后默认 TUI 启动失败：

```toml
[project]
dependencies = [
    # ... 保留现有依赖
    "voidx_tui==3.4.4",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
pythonpath = ["src", "tui/src"]
testpaths = ["tests"]
```

### 7. 改造 `scripts/package.py`

不能在现有 build 分支后简单追加 TUI 构建，因为当前 `main()` 在构建主包后立即 `return`。需先抽出通用构建函数，再顺序构建主包和 TUI 包：

```python
def main() -> int:
    # ... argparse / metadata check / clean / out_dir setup 保持现有语义
    roots = [ROOT]
    tui_root = ROOT / "tui"
    if (tui_root / "pyproject.toml").exists():
        roots.append(tui_root)

    for package_root in roots:
        result = _build_package(package_root, out_dir, args.format)
        if result != 0:
            return result
    return 0


def _build_package(package_root: Path, out_dir: Path, fmt: str) -> int:
    if _has_module("build.__main__"):
        build_args = ["--wheel"] if fmt == "wheel" else ["--sdist"]
        if fmt == "all":
            build_args = ["--sdist", "--wheel"]
        return _run([sys.executable, "-m", "build", *build_args, "--outdir", str(out_dir), str(package_root)])
    if fmt == "wheel" and _has_module("pip"):
        return _run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--wheel-dir", str(out_dir), str(package_root)])
    # uv fallback 同样传 package_root
```

扩展 `_check_release_metadata` 检查 TUI 包版本、TUI 对主包的版本约束，以及主包对 TUI 的默认运行时依赖：

```python
tui_init = ROOT / "tui" / "src" / "voidx_tui" / "__init__.py"
if tui_init.exists():
    tui_text = tui_init.read_text()
    tui_match = re.search(r'__version__\s*=\s*"([^"]+)"', tui_text)
    tui_version = tui_match.group(1) if tui_match else ""
    if tui_version != project_version:
        errors.append(f"tui/src/voidx_tui/__init__.py version {tui_version} does not match {project_version}.")

    tui_pyproject_text = (ROOT / "tui" / "pyproject.toml").read_text()
    if f'voidx=={project_version}' not in tui_pyproject_text:
        errors.append(f"tui/pyproject.toml must depend on voidx=={project_version}.")

    pyproject_text = (ROOT / "pyproject.toml").read_text()
    if f'voidx_tui=={project_version}' not in pyproject_text:
        errors.append(f"pyproject.toml must depend on voidx_tui=={project_version}.")
```

### 8. 验证

```bash
# 安装双包
pip install -e . -e ./tui
# 跑 TUI 测试
./python.sh -m pytest tests/test_ui/tui/ -v
# 跑全量测试
./python.sh -m pytest tests/ -v
# 验证双 wheel 构建
./python.sh scripts/package.py --check-only
./python.sh scripts/package.py
ls dist/  # 应有 voidx-*.whl 和 voidx_tui-*.whl
```

不涉及数据模型变更。
