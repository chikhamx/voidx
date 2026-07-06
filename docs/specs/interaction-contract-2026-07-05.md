# 交互契约抽象层 — 技术设计文档

## Context

当前 voidx 的分层有问题：agent 层（核心）直接依赖 `PureTui`（前端实现）做人机交互，导致核心 → 前端的反向依赖。正确的分层应该是：

```
核心 (voidx)          ← agent/runtime/output/tools/protocol — 通用基础实现
  ▲
  │ 依赖核心，实现契约
  │
前端 (tui/web/desktop) ← 各端独立实现，互不依赖
```

本设计引入 `InteractionFrontend` Protocol 作为核心层定义的交互契约，agent 层只依赖契约，不依赖任何具体前端。同时把误放在前端的基础能力（剪贴板）归位核心层。

### 现状问题

1. **反向依赖**：`runtime/ui.py` lazy import `PureTui`，`run_loop.py` 实例化它，agent 层通过 `self.host.app` 调用 PureTui 的方法。核心依赖了前端实现。
2. **基础能力错放**：`paste_clipboard_image` 的基础实现在 `voidx.ui.tools.clipboard_image`（核心层），但 agent/slash/handler 绕道 `app.paste_clipboard_image()`（前端）调用，本该直接调核心层。
3. **鸭子类型**：agent 层到处用 `hasattr(app, "ask_choice")` 判断，没有类型契约。

## Goals and Non-Goals

### Goals

- 定义 `InteractionFrontend` Protocol，作为核心层对交互前端的契约
- agent 层只依赖 `InteractionFrontend`，不 import 任何前端实现
- `PureTui` 实现该 Protocol；未来 web/desktop 前端也能实现
- 通过依赖注入消除运行时反向依赖，tui 成为可选依赖
- 把 `paste_clipboard_image` 的调用从前端归位到核心层

### Non-Goals

- 不改动 PureTui 内部实现
- 不改动 web/gateway 现有 headless TUI 模式
- 不抽象输出方向（`AgentUiSink` Protocol 已覆盖）

## Architecture

### 分层原则

```
┌─────────────────────────────────────────────────┐
│ 核心层 (voidx)                                    │
│  - agent/runtime: 业务逻辑，定义 InteractionFrontend │
│  - ui/output: 通用输出 (dock/tree/events/types)    │
│  - ui/tools: 基础工具 (clipboard/file_picker/...)  │
│  - ui/protocol, ui/gateway: 协议与网关              │
└─────────────────────────────────────────────────┘
                    ▲
                    │ 实现契约，依赖核心
                    │
┌──────────┬────────┴───────┬──────────┐
│  tui     │     web        │  desktop │  ← 前端层
│ PureTui  │  WebFrontend   │  (壳)    │    各自独立
└──────────┘────────────────┴──────────┘
```

核心层不 import 任何前端层。前端层 import 核心层 + 实现契约。

### InteractionFrontend Protocol

放在 `src/voidx/ui/output/types.py`（与 `UiStatus`/`SubmitHandler`/`ThreadExecutionContext` 同处）。

只包含**各端必须各自实现的交互能力**，不含基础工具能力（剪贴板等走核心层 `ui/tools`）。

```python
from typing import Protocol, Any
from dataclasses import dataclass

class InteractionFrontend(Protocol):
    """交互前端契约：核心层对人机交互前端的抽象。

    各前端（TUI/Web/Desktop）实现此契约。核心层只依赖此类型，
    不依赖任何具体前端实现。
    """

    # ── 交互询问（阻塞，返回用户选择/输入）──
    async def ask_choice(
        self,
        prompt: str,
        choices: list[str | tuple[str, str, str]],
        selected: int = 0,
        anchor: str = "",
        details: list[dict[str, Any]] | None = None,
        timeout: float | None = None,
    ) -> str | None: ...

    async def ask_text(
        self,
        prompt: str,
        default: str = "",
        secret: bool = False,
        timeout: float | None = None,
    ) -> str | None: ...

    # ── 生命周期（启动前端事件循环）──
    async def run(self, on_submit: SubmitHandler) -> None: ...
    async def run_headless(self, on_submit: SubmitHandler) -> None: ...

    # ── 外部输入桥接（web gateway 注入输入）──
    def submit_external_input(
        self, text: str, *,
        thread_id: str = "",
        context: ThreadExecutionContext | None = None,
    ) -> None: ...

    def cancel_external_input(
        self, *, thread_id: str = "",
        context: ThreadExecutionContext | None = None,
    ) -> None: ...

    def set_external_command_handler(self, handler) -> None: ...
    def set_external_request_handler(self, handler) -> None: ...

    # ── 状态与渲染控制 ──
    @property
    def status(self) -> UiStatus: ...

    def invalidate(self) -> None: ...
    def consume_quiet_command(self, command: str) -> bool: ...
```

### 不纳入契约的方法（归位核心层）

| 方法 | 原位置 | 归位方案 |
|------|--------|---------|
| `paste_clipboard_image` | `PureTui.clipboard_mixin`（前端包装）+ `voidx.ui.tools.clipboard_image`（基础实现） | agent 层直接调 `voidx.ui.tools.clipboard_image.paste_clipboard_image(workspace)`，不走 app |
| `show_transient_output` | `PureTui.app`（仅 TUI 内部调用） | 不纳入契约，TUI 私有方法 |
| `queue_quiet_command` | `PureTui.app`（仅 TUI 内部调用） | 不纳入契约，TUI 私有方法 |

**`consume_quiet_command` 待定**：`run_loop.py:398` 调用它判断用户输入是否为静默命令。但 `queue_quiet_command`（入队）只在 TUI 内部调用——这是 TUI 特有的输入预处理机制，web 端不会有此队列。两种处理方案：
- 方案1：保留在契约中，web 端实现返回 False（无静默命令）
- 方案2：把静默命令逻辑移到 TUI 内部，run_loop 不调此方法

倾向方案1（最小改动），但需确认 web 端语义。

### 依赖注入：消除运行时反向依赖

**变更前**（`runtime/ui.py`）：
```python
PureTui = _LazyAttr("voidx.ui.tui", "PureTui")  # 核心层 import 前端
```

**变更后**：
```python
# runtime/ui.py — 核心层，不 import 任何前端
FrontendFactory = Callable[[UiStatus, list[tuple[str, str]]], InteractionFrontend]

_default_frontend_factory: FrontendFactory | None = None

def register_default_frontend(factory: FrontendFactory) -> None:
    """前端包注册自己为默认工厂（入口点调用）。"""
    global _default_frontend_factory
    _default_frontend_factory = factory

def create_frontend(status: UiStatus, commands: list[tuple[str, str]]) -> InteractionFrontend:
    """构造默认前端。若无前端注册，抛 RuntimeError。"""
    if _default_frontend_factory is None:
        raise RuntimeError("No frontend registered. Install voidx_tui or call register_default_frontend().")
    return _default_frontend_factory(status, commands)
```

```python
# tui 包入口（voidx_tui/__init__.py）— 前端层，注册自己
from voidx.runtime.ui import register_default_frontend
from voidx_tui.app import PureTui

register_default_frontend(lambda status, commands: PureTui(status, commands))
```

```python
# run_loop.py — 核心层，通过工厂注入，不 import tui
from voidx.runtime.ui import create_frontend

app = create_frontend(UiStatus(...), COMMANDS)
```

**效果**：
- 核心层（voidx）源码和运行时都不 import tui
- tui 包 import 核心层 + 注册工厂
- tui 未安装时，`create_frontend` 抛错；web 端可注册自己的工厂
- 无循环依赖，tui 可独立包化

### agent 层改造点

| 文件 | 变更 |
|------|------|
| `runtime/ui.py` | 删除 `PureTui = _LazyAttr(...)`，改为 `register_default_frontend` + `create_frontend` |
| `agent/graph/run_loop.py` | `PureTui(...)` → `create_frontend(...)`；类型标注 `InteractionFrontend` |
| `agent/graph/core/voidx_graph.py` | `self._app: PureTui` → `self._app: InteractionFrontend`；`def app() -> InteractionFrontend` |
| `agent/slash/handler.py:443-448` | `app.paste_clipboard_image()` → `paste_clipboard_image(workspace)`（直接调核心层） |
| `agent/slash/*.py` | 删除 `hasattr(app, "ask_choice")` 判断，直接调（Protocol 保证方法存在） |
| `agent/graph/tool_executor/helpers.py` | 同上，删 hasattr 判断 |
| `agent/graph/permissions.py` | `self._app.ask_choice` 类型标注不变，PureTui→InteractionFrontend |

## Data Model

无新增数据模型。复用现有 `UiStatus`、`SubmitHandler`、`ThreadExecutionContext`、`ClipboardImageResult`。

## API Contract

### InteractionFrontend Protocol
- **位置**：`src/voidx/ui/output/types.py`
- **实现者**：`PureTui`（tui 包）、`WebFrontend`（未来，web 包）
- **消费者**：agent 层所有需要交互的模块

### 前端注册 API
- `register_default_frontend(factory: FrontendFactory) -> None` — 前端包注册
- `create_frontend(status, commands) -> InteractionFrontend` — 核心层构造前端

### 剪贴板归位
- `paste_clipboard_image(workspace: str) -> ClipboardImageResult` — 已在 `voidx.ui.tools.clipboard_image`，agent 层直接调用

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| 无前端注册（tui 未安装且无其他前端） | `create_frontend` 抛 `RuntimeError`，启动失败并提示 |
| 前端实例为 None（纯 headless 无交互） | agent 层已有 `if app is None` 判断，保持不变 |
| `ask_choice` / `ask_text` 超时 | 方法签名含 `timeout`，返回 `None` |
| 前端未实现交互契约 | 不允许继续用 `hasattr` 鸭子类型降级；创建前端时应失败，或在 headless/web 路径显式注入符合 Protocol 的实现 |
| 无 TUI 包但默认 CLI 需要交互 | 与 TUI 独立包化方案保持一致：发布态 `voidx` 默认依赖同步版本 `voidx_tui`，避免默认 CLI 启动失败 |
| web 端暂未实现独立 `WebFrontend` | 本次保留现有 headless/gateway 行为；未来再用同一 `InteractionFrontend` 契约替换 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| Protocol 而非 ABC | ABC | 结构子类型，`PureTui` 无需继承声明，零侵入 |
| 工厂注册制而非 lazy import | 保留 `_LazyAttr` | 彻底消除运行时反向依赖；tui 真正可选 |
| 契约放 `output/types.py` | 新建 `interaction.py` | 与 UiStatus/SubmitHandler 强相关，集中管理 |
| `paste_clipboard_image` 归核心层 | 纳入契约 | 用户判断正确：基础功能，各端都需要，不该走 app |
| `show_transient_output`/`queue_quiet_command` 不纳入 | 纳入契约 | 仅 TUI 内部调用，非跨端语义 |
| `consume_quiet_command` 待定 | 纳入契约/归 TUI 私有 | run_loop 调用但语义偏 TUI 特有；倾向纳入契约，web 端返回 False |

## Open Questions

- [ ] `register_default_frontend` 是否需要支持多前端注册（按配置选）？当前判断：不需要，单前端足够，多前端未来再加
- [ ] web 模式当前走 headless TUI，是否本次就实现 `WebFrontend`？当前判断：不实现，只留契约，未来做

## TUI 物理独立包化（与契约抽象同步实施）

契约抽象和 TUI 独立包化一次到位，避免中间态（register 临时放在 voidx 包内）。

### 目标目录结构

```
仓库根/
├── src/voidx/              ← 核心层（不含 tui）
│   ├── agent/
│   ├── runtime/
│   ├── ui/
│   │   ├── output/         ← InteractionFrontend Protocol 定义处
│   │   ├── tools/          ← paste_clipboard_image 基础实现
│   │   ├── protocol/
│   │   └── gateway/
│   └── ...
├── tui/                    ← 独立包 voidx_tui（和 frontend/、desktop/ 平级）
│   ├── src/voidx_tui/
│   │   ├── __init__.py     ← register_default_frontend 注册
│   │   ├── app.py          ← PureTui 实现 InteractionFrontend
│   │   └── ...（18 个文件）
│   ├── tests/
│   │   └── ...（从 tests/test_ui/tui/ 迁入）
│   └── pyproject.toml      ← voidx_tui 包，dependencies=["voidx=={version}"]
├── frontend/               ← web 前端（已有）
└── desktop/                ← 桌面壳（已有）
```

### 包依赖关系

```
voidx_tui → voidx=={version} (install_requires)
voidx     → voidx_tui=={version} (默认 CLI 运行时依赖)
```

源码层仍保持无反向 import：`voidx` 不 import `voidx_tui`，只通过 `create_frontend()` 的注册表获取实现。包发布层为保证默认 CLI 可用，`voidx` wheel 依赖同版本 `voidx_tui`；`voidx_tui` 依赖同版本 `voidx`。构建顺序必须先构建 `voidx`，再构建 `voidx_tui`，本地 wheel 构建使用 `--no-deps` 避免互相拉取。

### Import 变更

| 原路径 | 新路径 | 范围 |
|--------|--------|------|
| `voidx.ui.tui.*` | `voidx_tui.*` | TUI 内部 31 处自引用 + 测试 9 处 |
| `sys.modules.get("voidx.ui.tui.app")` | `sys.modules.get("voidx_tui.app")` | clipboard_mixin.py |
| `from voidx.runtime.ui import PureTui` | `from voidx.runtime.ui import create_frontend` | run_loop.py |
| `PureTui` 类型标注 | `InteractionFrontend` | voidx_graph.py、tool_executor/helpers.py |

### 打包配置

- `pyproject.toml`（voidx）：`[tool.setuptools.packages.find]` 的 `where=["src"]` 自动不再发现 tui（已迁出）；`[project.dependencies]` 需新增同步版本 `voidx_tui=={version}`，保证默认 CLI 安装可运行
- `tui/pyproject.toml`（voidx_tui）：独立 `[build-system]` + `[project] dependencies=["voidx=={version}"]`，版本与 `src/voidx/__init__.py` 同步
- `scripts/package.py`：需与 TUI 独立包化方案一致，重构为主包与 TUI 包顺序构建，并覆盖 `wheel` / `sdist` / `all`
- `pyproject.toml` 的 `[tool.pytest.ini_options]`：`pythonpath` 加 `tui/src`；若测试迁入 `tui/tests`，`testpaths` 同步包含 `tests` 与 `tui/tests`

### 开发环境

```bash
pip install -e . -e ./tui   # 同一条命令安装 voidx 与 voidx_tui 两个 editable 包
```

开发态使用同一条 editable 安装命令，避免主包解析默认 `voidx_tui=={version}` 依赖时找不到本地前端包。运行时需要先导入前端包使其执行注册；默认 CLI 入口应在创建前端前显式导入 `voidx_tui`，或通过入口点发现机制加载默认前端，避免仅依赖用户手动 import。

## 实施顺序

1. **核心层契约**：定义 InteractionFrontend Protocol + 工厂注册制（runtime/ui.py）
2. **agent 层解耦**：类型标注改 Protocol，paste_clipboard_image 归核心层，删 hasattr 鸭子类型
3. **TUI 物理迁移**：src/voidx/ui/tui/ → tui/src/voidx_tui/，import 改名，包入口注册工厂
4. **测试迁移**：tests/test_ui/tui/ → tui/tests/，import 改名
5. **验证**：核心层除 `TYPE_CHECKING` / 文档外 `grep PureTui` 零命中；确认无 `hasattr(app, "ask_choice")` / `hasattr(app, "ask_text")` 残留；TUI 测试、相关 agent/slash 测试、全量测试绿；`scripts/package.py --check-only` 和双 wheel 构建通过
