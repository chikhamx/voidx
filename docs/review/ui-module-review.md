# UI 模块 Code Review

> **日期**: 2026-06-07
> **范围**: `src/voidx/ui/` 全部子模块
> **结论**: NEEDS_CHANGE

---

## 模块概览

| 子模块 | 文件数 | 职责 |
|--------|--------|------|
| `tui/` | 12 | 终端 TUI — 输入解析、渲染、面板、状态管理 |
| `output/` | 16 | 输出渲染 — dock 树、流式、控制台、事件总线、diff |
| `gateway/` | 3 | WebSocket 网关 — Web/桌面前端通信 |
| `protocol/` | 6 | UI 协议定义 — 信封、命令、请求、快照 |
| `tools/` | 5 | UI 工具 — 剪贴板、文件选择器、IDE 集成 |
| 顶层 | 4 | 命令面板、前端接口、会话、转录 |

---

## 高优先级问题

### 1. Gateway 请求无超时，可永久挂起

- **文件**: `src/voidx/ui/gateway/session.py:73-86`
- **严重性**: 🔴 High

`GatewaySession.request()` 中 `await future` 没有超时保护。如果前端客户端不响应，调用方协程将永远阻塞。此外存在 TOCTOU 竞态：`_broadcast` 会移除发送失败的客户端，之后 `if not self._clients` 检查可能为 true，导致 future 永远不会 resolve。

```python
# 当前代码
await self._broadcast(request_envelope_json)
if not self._clients:
    return None  # future 永远不会 resolve
return await future  # 无超时
```

**建议**: 添加可配置超时，移除第二次客户端检查：

```python
try:
    return await asyncio.wait_for(future, timeout=60.0)
except asyncio.TimeoutError:
    self._pending_requests.pop(request_id, None)
    return None
```

---

### 2. Gateway Token 认证不安全

- **文件**: `src/voidx/ui/gateway/server.py:74-80`
- **严重性**: 🔴 High

两个问题：

1. `query.get("token") == [self._token]` 是列表比较而非字符串比较（`parse_qs` 返回 `list[str]`，碰巧能用），但 `?token=a&token=b` 会失败
2. 使用 `==` 做字符串比较，存在时序攻击风险

```python
# 当前代码
return query.get("token") == [self._token]
```

**建议**: 使用常量时间比较：

```python
import hmac

token_values = query.get("token", [])
if len(token_values) != 1:
    return False
return hmac.compare_digest(token_values[0], self._token)
```

---

### 3. WebSocket 消息无 JSON 解析保护

- **文件**: `src/voidx/ui/gateway/server.py:83-86`
- **严重性**: 🔴 High

`parse_protocol_envelope_json` 直接调用 `json.loads(message)` 无 try/except。畸形或非 JSON 消息会抛出未处理异常，导致 `_handle` 协程崩溃，客户端被断开且无错误提示。

```python
# 当前代码
def parse_protocol_envelope_json(message: str):
    import json
    return parse_protocol_envelope(json.loads(message))
```

**建议**: 包裹异常处理，返回错误响应或优雅关闭：

```python
def parse_protocol_envelope_json(message: str):
    import json
    try:
        return parse_protocol_envelope(json.loads(message))
    except (json.JSONDecodeError, ValueError) as exc:
        raise ProtocolParseError(str(exc)) from exc

# 在 _handle 中：
try:
    envelope = parse_protocol_envelope_json(message)
except ProtocolParseError:
    await websocket.close(code=4003, reason="invalid message format")
    return
```

---

## 中优先级问题

### 4. `discard_stream()` 存在死代码分支

- **文件**: `src/voidx/ui/output/dock/stream.py:38-52`
- **严重性**: 🟡 Medium

两个分支（`not self._stream_text.strip()` 和 `elif`）执行完全相同的逻辑（`_remove_node` + 重置 `_current_agent`），`not self._stream_text.strip()` 条件无实际区分效果。

**建议**: 合并为单个 `if self._stream_node:` 块。如果意图是空文本时保留节点，需添加注释说明。

---

### 5. `_DockProxy` 白名单静默吞掉方法调用

- **文件**: `src/voidx/ui/output/dock/state.py:19-53`
- **严重性**: 🟡 Medium

`_DockProxy.__getattr__` 对未在白名单中的方法返回 `lambda *args, **kwargs: None`。新增 `BottomInputDock` 方法但忘记更新白名单时，调用变为静默 no-op，极难调试。白名单与 `BottomInputDock` 实际接口完全脱钩。

**建议**: 至少在静默丢弃时记录 warning 日志；更好的方案是从 `BottomInputDock` 的公共方法动态生成白名单。

---

### 6. `PureTui.__getattr__/__setattr__` 代理脆弱

- **文件**: `src/voidx/ui/tui/app.py:87-106`
- **严重性**: 🟡 Medium

两个问题：

1. 属性名拼写错误会静默创建实例属性而非路由到 state dataclass，破坏状态分组不变量
2. `__setattr__` 中 `except AttributeError: pass` 会吞掉 state 对象不存在时的错误

```python
# 当前代码
def __setattr__(self, name: str, value: Any) -> None:
    mapping = STATE_FIELD_MAP.get(name)
    if mapping is not None:
        state_attr, field_name = mapping
        try:
            state = object.__getattribute__(self, state_attr)
        except AttributeError:
            pass  # ← 静默吞错
        else:
            setattr(state, field_name, value)
            return
    object.__setattr__(self, name, value)
```

**建议**: `except AttributeError` 中不要 pass，要么 raise 要么记录警告。考虑添加 `__dir__` 覆盖使代理可发现。

---

### 7. `emit_direct` 发射即忘任务无错误处理

- **文件**: `src/voidx/ui/output/events/__init__.py:109-123`
- **严重性**: 🟡 Medium

`emit_direct` 对 awaitable 消费者结果使用 `asyncio.create_task(result)` 但不保留引用也不处理异常。消费者抛出异常时被事件循环静默吞掉。

```python
# 当前代码
if inspect.isawaitable(result):
    asyncio.create_task(result)  # 无错误处理，无引用保留
```

**建议**: 添加 done callback 记录异常：

```python
if inspect.isawaitable(result):
    task = asyncio.create_task(result)
    task.add_done_callback(_log_task_exception)
```

---

### 8. `_browse_unix` 未处理信号，终端可能留在 raw 模式

- **文件**: `src/voidx/ui/output/browse.py:104-159`
- **严重性**: 🟡 Medium

`_browse_unix` 调用 `tty.setraw(fd)` 并在 `finally` 中恢复，但进程收到 SIGTSTP (Ctrl+Z) 或 SIGWINCH 时，`finally` 不会执行，终端留在 raw 模式。

**建议**: 注册 SIGTSTP/SIGCONT 信号处理器，在挂起前恢复终端、恢复后重新设置 raw 模式。

---

### 9. 剪贴板 AppleScript 存在 shell 注入风险

- **文件**: `src/voidx/ui/tools/clipboard_image.py:120-140`
- **严重性**: 🟡 Medium

两个问题：

1. AppleScript 中 `do shell script "echo $VOIDX_CLIP_OUT"` — 如果 `output_path` 包含 shell 元字符，存在注入风险
2. `env={**os.environ, ...}` 继承完整父环境，包括 API keys 等敏感变量

**建议**: 校验 `output_path` 拒绝含 shell 元字符的路径；考虑使用最小化 env 字典。

---

## 低优先级问题

### 10. 协议版本号未校验

- **文件**: `src/voidx/ui/protocol/envelope.py:15-16`
- **严重性**: 🟢 Low

`PROTOCOL_VERSION = 1` 已定义但从未在入站消息中校验。客户端发送 `v: 2` 会被静默接受，无法向前兼容。

**建议**: 在 `parse_protocol_envelope` 或 gateway `_handle` 中添加版本校验，不匹配时拒绝或警告。

---

### 11. `StreamingRenderer` 异常时提交而非丢弃

- **文件**: `src/voidx/ui/output/console/streaming.py:53-61`
- **严重性**: 🟢 Low

`__aexit__` 无论是否异常都调用 `self.done()`，可能在错误时提交不应持久化的部分流式状态。

**建议**: 检查 `exc_type is not None`，异常时调用 `self.discard()` 而非 `self.done()`。

---

### 12. 剪贴板 mixin 使用 `sys.modules` 查找注入函数

- **文件**: `src/voidx/ui/tui/clipboard_mixin.py:70-83`
- **严重性**: 🟢 Low

通过 `sys.modules.get("voidx.ui.tui.app")` 查找猴子补丁函数，模块未导入或属性被删除时静默回退，测试注入不可靠。

**建议**: 使用显式依赖注入（构造函数参数或 Protocol）替代 `sys.modules` 查找。

---

## Bug

### `FileCandidate.size` 始终为 0

- **文件**: `src/voidx/ui/tools/file_picker.py:111`

`list_file_candidates` 对文件条目未调用 `entry.stat().st_size`，`size` 始终为 0。附件面板永远显示 "0B"。

```python
# 当前代码
candidates.append(FileCandidate(
    rel_path=rel_path,
    kind="image" if is_image_file(rel_path) else "file",
    size=0,  # ← 应为 entry.stat().st_size
))
```

**建议**: 添加 `size=entry.stat().st_size`，包裹 `try/except OSError`。

---

## 测试覆盖分析

| 子模块 | 状态 | 测试文件 | 缺失覆盖 |
|--------|------|----------|----------|
| gateway | ✅ 部分 | `test_ui_gateway.py` | auth、畸形输入、请求超时 |
| protocol | ✅ 良好 | `test_ui_frontend_protocol.py` | 版本校验 |
| events | ✅ 良好 | `test_ui_events.py` | `emit_direct` 错误处理路径 |
| dock | ✅ 间接 | 通过 events 测试 | 节点 mixin 单元测试 |
| diff | ✅ 良好 | `test_ui_diff.py` | — |
| session | ✅ 良好 | `test_ui_session_changes.py` | — |
| tui | ✅ 良好 | `test_pure_tui.py` | — |
| browse | ❌ 无 | — | 全部 |
| clipboard_image | ❌ 无 | — | 全部 |
| clipboard_text | ❌ 无 | — | 全部 |
| code_ide | ❌ 无 | — | 全部 |
| file_picker | ❌ 无 | — | 全部 |

---

## 建议修复优先级

1. **#1** Gateway 请求超时 — 生产环境可挂起
2. **#2** Token 认证安全 — 时序攻击向量
3. **#3** JSON 解析保护 — 客户端可崩掉服务端连接
4. **#5** Dock 代理白名单 — 静默吞 bug
5. **#7** emit_direct 错误处理 — 异常丢失
6. **FileCandidate.size bug** — 用户可见的 UI 错误
7. **#6** PureTui 代理脆弱性
8. **#8** browse 信号处理
9. **#9** 剪贴板注入风险
10. 其余 Low 项

---

## 架构评价

**优点**:
- 模块分层清晰：protocol → gateway → events → dock → tui，职责边界明确
- 事件总线模式（`UiEventBus`）设计良好，单消费者串行化避免了大量并发问题
- Protocol 层使用 Pydantic discriminated union，类型安全且可导出 JSON Schema
- TUI 状态分组到 dataclass（`InputState`、`RenderState` 等）比散落实例变量更可维护

**可改进**:
- `BottomInputDock` 通过 6 个 Mixin 组合，继承链较深，新增功能需判断应加在哪个 Mixin
- `_DockProxy` 全局单例 + ContextVar 模式增加了隐式耦合
- TUI 的 `__getattr__/__setattr__` 代理虽然减少了样板代码，但牺牲了 IDE 补全和静态分析能力
