# gateway/session.py 拆分为 session/ 包 + method/ 子模块 — 技术设计文档

## Context

`src/voidx/ui/gateway/session.py` 当前是 971 行单文件,`GatewaySession` 类塞了 40+ 个 `_method_*` 处理器,涵盖 terminal / diff / session / settings / mcp / skills / lsp / tavily 八个领域。其中 `_method_settings_update` 单方法 210 行,是全文件最大的复杂度热点。

本设计将 `session.py` 升级为 `session/` 子包,方法层按领域拆进 `method/xxx.py` 子模块(而非扁平的 `xxx_methods.py`),通过 `__init__.py` 重新导出公共 API,外部引用零改动。

## Goals and Non-Goals

### Goals

- `session.py` 单文件消失,逻辑全部内聚进 `session/` 子包
- 方法层按领域拆进 `session/method/xxx.py`,每个模块导出 `register(session)` 函数
- 外部引用 `from voidx.ui.gateway.session import GatewaySession` 等零改动
- `_method_settings_update` 210 行巨型方法随 `method/settings.py` 独立,便于后续内聚

### Non-Goals

- 不重构 `GatewaySession` 的核心连接/广播/线程管理逻辑(只搬位置,不改行为)
- 不拆分 `adapter.py` / `diff_review.py` / `terminal.py` / `server.py` 等同级文件
- 不修改测试文件(除非 import 路径因包化而失效)

## Architecture

### 目标目录结构

```
src/voidx/ui/gateway/session/
├── __init__.py              # 重新导出 GatewaySession / GatewayEventConsumer / ProtocolClient
├── core.py                  # GatewaySession 主体 + ProtocolClient
├── consumer.py              # GatewayEventConsumer
└── method/
    ├── __init__.py          # 空或聚合 re-export(可选)
    ├── terminal.py          # terminal.* 方法 + output reader
    ├── diff.py              # diff.review / diff.decide / diff.apply / diff.generate
    ├── sessions.py          # session.* CRUD + submit/cancel + commands.*
    ├── settings.py          # settings.get / settings.update / integrations.get + _desktop_settings_snapshot
    └── integrations.py      # mcp.* / skills.* / lsp.* / tavily.* + 相关辅助函数
```

### 模块职责

| 文件 | 行数(估) | 内容 |
|---|---|---|
| `session/__init__.py` | ~10 | `from .core import GatewaySession, ProtocolClient`; `from .consumer import GatewayEventConsumer` |
| `session/core.py` | ~280 | `ProtocolClient` + `GatewaySession` 主体:连接/广播/线程管理/snapshot 编码/`_register_default_methods`(只做注册,调用各 method 模块的 `register`) |
| `session/consumer.py` | ~15 | `GatewayEventConsumer`(已独立,直接搬出) |
| `session/method/terminal.py` | ~60 | `_method_terminal_create/input/resize/close` + `_start_terminal_output_reader`,导出 `register(session)` |
| `session/method/diff.py` | ~50 | `_method_diff_review_start/decide/apply` + `_method_diff_generate`,导出 `register(session)` |
| `session/method/sessions.py` | ~90 | `_method_session_create/fork/delete/rename/switch/list/submit/cancel` + `_method_commands_list/run`,导出 `register(session)` |
| `session/method/settings.py` | ~230 | `_method_settings_get/update`(210 行巨型方法)+ `_method_integrations_get` + `_desktop_settings_snapshot` + `_settings_for_scope` + `_gateway_settings`,导出 `register(session)` |
| `session/method/integrations.py` | ~180 | `_method_mcp_*` / `_method_skills_*` / `_method_lsp_*` / `_method_tavily_*` + 辅助(`_mcp_server_summary` / `_mcp_tool_summaries` / `_require_mcp_server` / `_tavily_summary` / `_skill_service` / `_skill_summaries` / `_skill_detail` / `_new_lsp_manager` / `_lsp_status_list`),导出 `register(session)` |

### 注册模式

每个 `method/xxx.py` 模块导出一个 `register(session: GatewaySession) -> None` 函数,在 `core.py` 的 `_register_default_methods` 里依次调用:

```python
def _register_default_methods(self) -> None:
    from voidx.ui.gateway.session.method import terminal, diff, sessions, settings, integrations
    terminal.register(self)
    diff.register(self)
    sessions.register(self)
    settings.register(self)
    integrations.register(self)
```

函数内 import 避免循环(method 模块需访问 session 实例)。

### 状态访问

handler 原本直接用 `self._xxx` 访问 session 私有字段。拆分后 handler 不再是 session 的方法,需通过 session 实例的属性访问器。

`core.py` 暴露以下内部访问器(`@property`,不加下划线前缀,供 method 模块使用):

| 访问器 | 对应原私有字段 | 使用方 |
|---|---|---|
| `methods` | `self.methods` | 所有 method 模块(注册用) |
| `terminals` | `self._terminals` | terminal.py |
| `diff_reviews` | `self._diff_reviews` | diff.py |
| `settings` | `self._settings` / `self._settings_provider()` | settings.py / integrations.py |
| `workspace` | `self._workspace` | settings.py / integrations.py |
| `command_handler` | `self._command_handler` | sessions.py |
| `thread_id_provider` | `self._thread_id_provider` | sessions.py |
| `clients` | 已有 property | broadcast 用 |

> 注:具体字段名以 `core.py` 实际代码为准,拆分时逐一核对,漏一个就崩。

## API Contract

### 外部引用(零改动)

```python
from voidx.ui.gateway.session import GatewaySession       # 仍有效,指向 session/__init__.py
from voidx.ui.gateway.session import GatewayEventConsumer  # 仍有效
from voidx.ui.gateway.session import ProtocolClient        # 仍有效
```

### method 模块统一接口

每个 `method/xxx.py` 模块定义一个 mixin 类(如 `TerminalMethods`),handler 作为 mixin 方法原样保留(`self` 签名不变,函数体不改)。`GatewaySession` 通过多继承组合:

```python
# session/method/terminal.py
class TerminalMethods:
    async def _method_terminal_create(self, params: dict) -> dict: ...
    async def _method_terminal_input(self, params: dict) -> dict: ...
    # ...

# session/core.py
class GatewaySession(TerminalMethods, DiffMethods, SessionMethods, SettingsMethods, IntegrationMethods):
    def _register_default_methods(self) -> None:
        m = self.methods
        m.register("terminal.start", self._method_terminal_create)  # 原样
        # ...
```

> handler 逐字节搬迁,`self._xxx` 原样可用,无需暴露访问器、无需改签名。测试只通过 JSON-RPC dispatch 调用方法,不直接 import `_method_xxx`(已 grep 确认),mixin 方案对测试透明。

## Error Handling

| 失败场景 | 处理策略 |
|---|---|
| MRO 冲突 | 各 mixin handler 名 `_method_xxx` 各不相同,辅助方法名唯一,无冲突(已核对) |
| mixin 访问 `self._xxx` | 运行时 `self` 是 `GatewaySession` 实例,有全部字段;静态检查警告可忽略 |
| handler 行为偏移 | 不改 handler 函数体和签名,只改位置;用现有测试覆盖 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|---|---|---|
| 用 `method/xxx.py` 子目录 | 扁平 `xxx_methods.py` | 用户明确要求子模块结构;领域内聚更清晰 |
| mixin 类组合(handler 原样搬迁) | 独立函数 + session 参数 + lambda 绑定 | mixin 方案 handler 逐字节搬迁零改动,无需暴露访问器,无需改签名;独立函数方案需 40 个 lambda + 12 个访问器,人为复杂度高 |
| `__init__.py` 重新导出 | 改所有外部 import 路径 | 零改动外部引用,迁移成本低 |

## Resolved Questions

- handler 拆分后测试是否受影响——已 grep 确认:测试只通过 JSON-RPC dispatch 调用方法,不直接 import `_method_xxx`,mixin 方案对测试透明。
- `_method_settings_update` 210 行是否在本拆分中一并内聚——本设计选择**仅搬迁位置,不改行为**,内聚留待后续。

## Open Questions

- (无)
