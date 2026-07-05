# gateway/session.py 拆分实施计划

## Goal

将 971 行的 `src/voidx/ui/gateway/session.py` 拆为 `session/` 子包,方法层按领域拆进 `session/method/xxx.py`,外部引用零改动。

## Architecture

`session.py` 升级为 `session/` 包,`GatewaySession` 主体留在 `core.py`,40+ 个 `_method_*` handler 按领域搬进 `method/{terminal,diff,sessions,settings,integrations}.py`。每个 method 模块定义一个 mixin 类(如 `TerminalMethods`),`GatewaySession` 通过多继承组合:

```python
class GatewaySession(TerminalMethods, DiffMethods, SessionMethods, SettingsMethods, IntegrationMethods):
    ...
```

handler 仍是 `GatewaySession` 的方法(通过 mixin 注入),`self._xxx` 原样可用,无需暴露访问器、无需改签名。`_register_default_methods` 仍是 `GatewaySession` 的方法,注册 `self._method_xxx` 原样工作。`__init__.py` 重新导出公共 API,外部 import 路径不变。

## Tech Stack

- Python 3.12+,Pydantic v2
- 现有依赖:`MethodDispatch` / `UiEventItemAdapter` / `TerminalManager` / `DiffReviewSession` / `Settings`
- 测试:pytest + `tests/test_ui/gateway/` 下 8 个测试文件

## File Structure

| 文件 | 职责 |
|---|---|
| `src/voidx/ui/gateway/session/__init__.py` | 重新导出 `GatewaySession` / `GatewayEventConsumer` / `ProtocolClient` |
| `src/voidx/ui/gateway/session/core.py` | `ProtocolClient` + `GatewaySession` 主体(连接/广播/线程/snapshot/`_register_default_methods`) |
| `src/voidx/ui/gateway/session/consumer.py` | `GatewayEventConsumer` |
| `src/voidx/ui/gateway/session/method/__init__.py` | 空文件(标记为包) |
| `src/voidx/ui/gateway/session/method/terminal.py` | `TerminalMethods` mixin:terminal.* 4 个 handler + `_start_terminal_output_reader` |
| `src/voidx/ui/gateway/session/method/diff.py` | `DiffMethods` mixin:diff.* 4 个 handler |
| `src/voidx/ui/gateway/session/method/sessions.py` | `SessionMethods` mixin:session.* + commands.* 10 个 handler |
| `src/voidx/ui/gateway/session/method/settings.py` | `SettingsMethods` mixin:settings.get/update + integrations.get + `_desktop_settings_snapshot` + `_gateway_settings` + `_settings_for_scope` |
| `src/voidx/ui/gateway/session/method/integrations.py` | `IntegrationMethods` mixin:mcp.* / skills.* / lsp.* / tavily.* 14 个 handler + 辅助(`_mcp_server_summary` 等 9 个) |
| `src/voidx/ui/gateway/session.py` | **删除**(被包替代) |

## Tasks

### Task 1: 创建包骨架

- [ ] `file create` `src/voidx/ui/gateway/session/__init__.py`
- [ ] `file create` `src/voidx/ui/gateway/session/method/__init__.py`(空)
- [ ] 验证:`./python.sh -c "import voidx.ui.gateway.session; print('ok')"`(此时应报错,因为 core.py 还不存在)

### Task 2: 搬迁 core.py(GatewaySession 主体 + ProtocolClient)

- [ ] `file create` `src/voidx/ui/gateway/session/core.py`
- [ ] 从 `session.py` 第 1-282 行搬入:imports + `RuntimeStateProvider` + `ProtocolClient` + `GatewaySession` 的 `__init__` / properties / connect / disconnect / set_command_handler / set_thread_id_provider / handle_command / request / handle_response / broadcast_event / broadcast_snapshot / dispatch_request / register_thread / unregister_thread / list_threads / switch_thread / _encode_snapshot / _build_workspace_snapshot / _active_thread_snapshot / _next_seq / _broadcast
- [ ] `GatewaySession` 改为多继承组合各 mixin:`class GatewaySession(TerminalMethods, DiffMethods, SessionMethods, SettingsMethods, IntegrationMethods)`(从 method 模块 import)
- [ ] `_register_default_methods` 保持原样,注册 `self._method_xxx`(mixin 方法直接可用)
- [ ] **不暴露任何访问器**——mixin 内 `self._xxx` 原样工作
- [ ] 验证:`./python.sh -c "from voidx.ui.gateway.session.core import GatewaySession; print('ok')"`(应报错,method 模块未建)

### Task 3: 搬迁 consumer.py

- [ ] `file create` `src/voidx/ui/gateway/session/consumer.py`
- [ ] 从 `session.py` 第 962-971 行搬入 `GatewayEventConsumer`
- [ ] 验证:`./python.sh -c "from voidx.ui.gateway.session.consumer import GatewayEventConsumer; print('ok')"`

### Task 4: 搬迁 method/terminal.py

- [ ] `file create` `src/voidx/ui/gateway/session/method/terminal.py`
- [ ] 定义 `class TerminalMethods:`(无基类,纯 mixin)
- [ ] 原样搬入 `_method_terminal_create/input/resize/close` + `_start_terminal_output_reader`(保持 `self` 签名,不改函数体)
- [ ] 验证:`./python.sh -c "from voidx.ui.gateway.session.method.terminal import TerminalMethods; print('ok')"`

### Task 5: 搬迁 method/diff.py

- [ ] `file create` `src/voidx/ui/gateway/session/method/diff.py`
- [ ] 定义 `class DiffMethods:`
- [ ] 原样搬入 `_method_diff_review_start/decide/apply` + `_method_diff_generate`
- [ ] 验证:`./python.sh -c "from voidx.ui.gateway.session.method.diff import DiffMethods; print('ok')"`

### Task 6: 搬迁 method/sessions.py

- [ ] `file create` `src/voidx/ui/gateway/session/method/sessions.py`
- [ ] 定义 `class SessionMethods:`
- [ ] 原样搬入 `_method_session_create/fork/delete/rename/switch/list/submit/cancel` + `_method_commands_list/run`
- [ ] 验证:`./python.sh -c "from voidx.ui.gateway.session.method.sessions import SessionMethods; print('ok')"`

### Task 7: 搬迁 method/settings.py

- [ ] `file create` `src/voidx/ui/gateway/session/method/settings.py`
- [ ] 定义 `class SettingsMethods:`
- [ ] 原样搬入 `_method_settings_get/update`(210 行,含 `[redacted]` 密钥处理,逐字节保留)+ `_method_integrations_get` + `_desktop_settings_snapshot` + `_gateway_settings` + `_settings_for_scope`(保持 `@staticmethod`)
- [ ] 验证:`./python.sh -c "from voidx.ui.gateway.session.method.settings import SettingsMethods; print('ok')"`

### Task 8: 搬迁 method/integrations.py

- [ ] `file create` `src/voidx/ui/gateway/session/method/integrations.py`
- [ ] 定义 `class IntegrationMethods:`
- [ ] 原样搬入 `_method_mcp_list/test/tools/restart/set_disabled/delete` + `_method_skills_list/show/set_enabled/set_auto` + `_method_lsp_status/doctor/restart` + `_method_tavily_set/delete`
- [ ] 原样搬入辅助:`_mcp_server_summary` / `_mcp_tool_summaries` / `_require_mcp_server` / `_tavily_summary` / `_skill_service` / `_skill_summaries` / `_skill_detail` / `_new_lsp_manager` / `_lsp_status_list`
- [ ] 验证:`./python.sh -c "from voidx.ui.gateway.session.method.integrations import IntegrationMethods; print('ok')"`

### Task 9: 填充 __init__.py 重新导出

- [ ] `write` `src/voidx/ui/gateway/session/__init__.py`:
  ```python
  from voidx.ui.gateway.session.core import GatewaySession, ProtocolClient
  from voidx.ui.gateway.session.consumer import GatewayEventConsumer
  __all__ = ["GatewaySession", "GatewayEventConsumer", "ProtocolClient"]
  ```
- [ ] 验证:`./python.sh -c "from voidx.ui.gateway.session import GatewaySession, GatewayEventConsumer, ProtocolClient; print('ok')"`

### Task 10: 删除原 session.py + 清理缓存

- [ ] `file delete` `src/voidx/ui/gateway/session.py`
- [ ] `bash` `rm -rf src/voidx/ui/gateway/__pycache__`
- [ ] 验证:`./python.sh -c "import voidx.ui.gateway.session; print(voidx.ui.gateway.session.__file__)"`(应显示 `session/__init__.py`)

### Task 11: 跑 gateway 测试

- [ ] `bash` `./python.sh -m pytest tests/test_ui/gateway/ -v`
- [ ] 预期:全部通过(与拆分前一致)

### Task 12: 跑全量测试回归

- [ ] `bash` `./python.sh -m pytest tests/ -v`
- [ ] 预期:无新增失败

## Tests

| Task | 验证命令 | 预期结果 |
|---|---|---|
| 1 | `./python.sh -c "import voidx.ui.gateway.session"` | ImportError(core.py 未建) |
| 2 | `./python.sh -c "from voidx.ui.gateway.session.core import GatewaySession"` | ImportError(method 模块未建) |
| 3 | `./python.sh -c "from voidx.ui.gateway.session.consumer import GatewayEventConsumer"` | `ok` |
| 4-8 | 各 `./python.sh -c "from voidx.ui.gateway.session.method import xxx"` | `ok` |
| 9 | `./python.sh -c "from voidx.ui.gateway.session import GatewaySession"` | `ok` |
| 10 | `./python.sh -c "import voidx.ui.gateway.session; print(session.__file__)"` | `session/__init__.py` |
| 11 | `./python.sh -m pytest tests/test_ui/gateway/ -v` | 全绿 |
| 12 | `./python.sh -m pytest tests/ -v` | 无新增失败 |

## Risks

1. **`_method_settings_update` 210 行含密钥处理**——搬迁时必须逐字节保留,不改逻辑。`[redacted]` 标记是安全脱敏,不能误删。
2. **mixin 间共享辅助方法**——`_gateway_settings` 在 `SettingsMethods` 里,`IntegrationMethods` 的 handler 调用 `self._gateway_settings()` 时,因 `GatewaySession` 同时继承两个 mixin,`self` 上有该方法,无需跨模块 import。前提:所有 mixin 都是 `GatewaySession` 的基类,通过 `self` 访问。
3. **MRO 顺序**——`GatewaySession(TerminalMethods, DiffMethods, SessionMethods, SettingsMethods, IntegrationMethods)`,各 mixin 无方法名冲突(已核对:handler 名字 `_method_xxx` 各不相同,辅助方法 `_gateway_settings` 等也唯一)。MRO 线性化无歧义。
4. **`__pycache__` 残留**——`session.py` 删除后,Python 可能仍加载缓存的 `session.pyc`,导致 import 指向旧模块。Task 10 必须清理 `__pycache__`。
5. **mixin 的类型注解**——mixin 内 `self._workspace` 等访问,静态检查器(mypy/pyright)会警告 `TerminalMethods` 无 `_workspace` 属性。可加 `if TYPE_CHECKING: from voidx.ui.gateway.session.core import GatewaySession` 并注解 `self: GatewaySession`,或忽略(运行时无影响)。本计划选择忽略,保持搬迁零改动。
6. **`_settings_for_scope` 是 `@staticmethod`**——原样搬进 `SettingsMethods`,保持 `@staticmethod` 装饰器。`_method_tavily_set/delete` 调用 `GatewaySession._settings_for_scope(...)` 或 `self._settings_for_scope(...)` 原样工作。
