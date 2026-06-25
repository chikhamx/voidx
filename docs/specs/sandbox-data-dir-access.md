# Spec: 沙箱 DATA_DIR 可访问性与外部路径申请权限

> **Status: Draft** — 等待实现

## 背景与问题

当 bash 等工具返回超过 50000 字符的大输出时，`tool_result_storage.py` 会把内容持久化到：

```
~/.voidx/tool-results/<session_id>/<safe_id>.txt
```

随后 LLM 收到一条 `<persisted-output>` 消息，其中包含该文件路径（经 `sanitize_tool_message_content` 处理后以 `~` 形式呈现）。LLM 尝试用 `read` 工具读取该路径时被拦截，返回 `"Path traversal blocked"`。

### 根因

拦截发生在工具执行层，而非权限引擎层：

1. **权限引擎层面 read 是允许的**：`BASIC_RULES` 中 `read: * → allow`（`rules.py:29`），`sandbox_denial_reason`（`engine.py:55-88`）在 workspace-write 模式下只检查 `FILE_WRITE`/`FILE_FORMAT` 和 `bash`，不检查 `READ_TOOLS`。
2. **工具执行层独立做沙箱检查**：`read.py:151` 调用 `resolve_safe`（`base.py:15-34`），该方法只允许 workspace + `sandbox_extra_paths` 范围内的路径。`~/.voidx/tool-results` 不属于 workspace，默认 `sandbox_workspace_write` 为空列表（`models.py:100-103`），因此 `resolve_safe` 返回 `None`，read 工具直接返回 `"Path traversal blocked"`。

### 次要问题

- `sanitize_tool_message_content`（`tool_messages.py:30`）把绝对路径的 home 替换成 `~`，LLM 收到 `~\.voidx\...`。`resolve_safe` 用 `(ws / file_path).resolve()` 解析，对 `~` 开头的路径不会 expanduser，导致路径解析到 workspace 下不存在的位置。

## 目标

1. **系统生成的文件可被读取**：`DATA_DIR`（`~/.voidx`）下的文件，尤其是 `tool-results/`，read 工具应能直接读取，无需用户配置。
2. **外部路径默认申请权限而非直接拒绝**：当 read 工具遇到 workspace 和 extra_paths 之外的路径时，不应直接返回 `"Path traversal blocked"`，而应走权限申请流程（ask），让用户决定是否允许。

## 方案

### 方案 B：将 DATA_DIR 注入沙箱可访问路径

在构造 `ToolContext` 和 `PermissionContext` 时，自动把 `DATA_DIR` 加入 `sandbox_extra_paths` / `sandbox_workspace_write`。这样：

- read 工具的 `resolve_safe` 会把 `~/.voidx` 视为允许路径，`tool-results` 文件可读。
- write 类工具（file/write/replace）在 workspace-write 模式下也能写入 `~/.voidx`（与现有 `external_directory: {"~/.voidx/*": "allow"}` 示例一致）。

### 外部路径申请权限

修改 `resolve_safe` 的调用方（read 工具），当路径不在允许范围内时，不直接返回 None/拦截，而是通过 `ctx.interact()` 发起用户确认。用户允许后把路径加入 session 级 `sandbox_extra_paths`，后续不再重复询问。详见"详细设计"第 2 节。

## 详细设计

### 1. DATA_DIR 注入点

**文件**：`src/voidx/agent/graph/wiring.py` — `build_permission_service`

在构造 `PermissionService` 时，把 `DATA_DIR` 合并进 `sandbox_workspace_write`：

```python
from voidx.memory.store import DATA_DIR

def build_permission_service(config: Config, *, notifier):
    extra_paths = list(config.sandbox_workspace_write)
    data_dir = str(DATA_DIR.resolve())
    if data_dir not in extra_paths:
        extra_paths.append(data_dir)
    return PermissionService(
        ...
        sandbox_workspace_write=extra_paths,
        ...
    )
```

**文件**：`src/voidx/agent/graph/tool_executor/executor.py` — `make_context`

`ToolContext.sandbox_extra_paths` 已从 `host._permission.sandbox_workspace_write` 取值（`executor.py:119`），注入后自动生效，无需额外改动。

**文件**：`src/voidx/agent/runtime_context.py` — `ExecutionPolicy.from_config`

`extra_write_paths` 从 `config.sandbox_workspace_write` 取值（`runtime_context.py:58`）。由于 DATA_DIR 注入发生在 `build_permission_service`（PermissionService 层）而非 config 层，`ExecutionPolicy.from_config` 不会自动拿到注入的 DATA_DIR。需要在 `from_config` 中同样注入，或在构造 `RuntimeEnvelope` 时从 PermissionService 取值。推荐在 `from_config` 中注入以保持单一来源：

```python
@classmethod
def from_config(cls, config: Config) -> "ExecutionPolicy":
    from voidx.memory.store import DATA_DIR
    extra = list(config.sandbox_workspace_write)
    data_dir = str(DATA_DIR.resolve())
    if data_dir not in extra:
        extra.append(data_dir)
    return cls(
        sandbox_mode=config.sandbox_mode.value,
        approval_policy=config.approval_policy.value,
        extra_write_paths=extra,
    )
```

### 2. read 工具外部路径申请权限

**当前行为**（`read.py:151-153`）：
```python
path = resolve_safe(ctx.workspace, inp.file_path, ctx.sandbox_extra_paths)
if path is None:
    return ToolResult(output=f"Path traversal blocked: {inp.file_path}", ...)
```

**目标行为**：路径不在允许范围内时，不直接拦截，而是通过 `ctx.interact()` 发起用户确认。用户允许后，将该路径加入 session 级 `sandbox_extra_paths`，后续同一 session 内读取同一路径或其子目录不再询问。

由于 `resolve_safe` 是同步纯函数且被多个工具共用，不宜在其中嵌入交互逻辑。交互逻辑放在 read 工具的 `execute` 方法中。

**关键约束**：`ToolContext.sandbox_extra_paths` 是 Pydantic list 字段，构造时会被深拷贝（`base.py:94-106` 注释说明 Pydantic v2 深拷贝破坏引用共享）。因此 read 工具无法直接往 `ctx.sandbox_extra_paths` 追加路径并期望反映回 `PermissionService`。需要通过回调把路径写回 `host._permission.sandbox_workspace_write`（该 list 是原始引用，不会被深拷贝隔离）。

**`_try_resolve_external` 辅助函数**（read.py 内部）：对 `~` 开头或绝对路径做 `expanduser().resolve()`，返回解析后的 Path 或 None。仅用于判断路径是否指向一个真实存在的文件，不做沙箱判定。

**方案**：read 工具在 `resolve_safe` 返回 None 时，调用 `_try_resolve_external` 检查路径是否指向 workspace 外的合法文件。若是，通过 `ctx.interact()` 发起用户确认（与 clarify 工具一致，`clarify.py:59`）；用户允许后，调用 `ctx.add_extra_path()` 把路径所在目录写回 `PermissionService.sandbox_workspace_write`，然后读取文件；若路径无法解析或不存在，仍返回原有的 `"Path traversal blocked"` 错误。

```python
path = resolve_safe(ctx.workspace, inp.file_path, ctx.sandbox_extra_paths)
if path is None:
    # 不再直接拦截，而是申请权限
    resolved = _try_resolve_external(inp.file_path)
    if resolved and resolved.exists() and resolved.is_file():
        if not ctx.interact:
            return ToolResult(output=f"Path traversal blocked: {inp.file_path}", metadata={"error": True})
        response = await ctx.interact(UserInteraction(
            prompt=f"读取 workspace 外的文件: {inp.file_path}",
            options=[("允许", "allow", "本次允许读取该文件"), ("拒绝", "deny", "不读取该文件")],
        ))
        if response.cancelled or response.value == "deny":
            return ToolResult(output=f"Read denied by user: {inp.file_path}", metadata={"error": True})
        # 用户允许 → 把路径所在目录加入 session 级 extra_paths，后续不再询问
        if ctx.add_extra_path:
            ctx.add_extra_path(str(resolved.parent))
        path = resolved
    else:
        return ToolResult(output=f"Path traversal blocked: {inp.file_path}", metadata={"error": True})
```

### 2.1 ToolContext 新增 add_extra_path 回调

**文件**：`src/voidx/tools/base.py` — `ToolContext`

仿照 `interact` 回调（`base.py:90`），新增 `add_extra_path` 回调字段：

```python
AddExtraPathCallback = Callable[[str], None]

class ToolContext(BaseModel):
    ...
    interact: UserInteractionCallback | None = Field(default=None, exclude=True)
    add_extra_path: AddExtraPathCallback | None = Field(default=None, exclude=True)
```

**文件**：`src/voidx/agent/graph/tool_executor/executor.py` — `make_context`

注入回调，把路径写入 `host._permission.sandbox_workspace_write`：

```python
def make_context() -> ToolContext:
    def _add_extra_path(path: str) -> None:
        if path not in host._permission.sandbox_workspace_write:
            host._permission.sandbox_workspace_write.append(path)

    return ToolContext(
        ...
        interact=_make_interact_callback(getattr(host, "_app", None)),
        add_extra_path=_add_extra_path,
    )
```

由于 `make_context` 每次重建 ctx 时都从 `host._permission.sandbox_workspace_write` 取值（`executor.py:119`），追加的路径会在后续工具调用中自动生效。

### 3. 路径解析修正

**文件**：`src/voidx/tools/base.py` — `resolve_safe`

当前 `resolve_safe` 用 `(ws / file_path).resolve()` 解析相对路径，对 `~` 开头的路径不 expanduser。修正为先判断是否以 `~` 开头：

```python
def resolve_safe(workspace, file_path, extra_paths=None):
    ws = Path(workspace).resolve()
    raw = Path(file_path)
    if raw.expanduser().is_absolute() or file_path.startswith("~"):
        resolved = raw.expanduser().resolve()
    else:
        resolved = (ws / raw).resolve()
    ...
```

## 影响范围

| 文件 | 改动 |
|------|------|
| `src/voidx/agent/graph/wiring.py` | `build_permission_service` 注入 DATA_DIR 到 `sandbox_workspace_write` |
| `src/voidx/agent/runtime_context.py` | `ExecutionPolicy.from_config` 同步注入 DATA_DIR |
| `src/voidx/tools/base.py` | `resolve_safe` 修正 `~` 路径解析；`ToolContext` 新增 `add_extra_path` 回调字段 |
| `src/voidx/tools/file_ops/read.py` | 外部路径走 `ctx.interact()` 申请权限，用户允许后通过 `ctx.add_extra_path()` 写回 session 级 extra_paths |
| `src/voidx/agent/graph/tool_executor/executor.py` | `make_context` 注入 `add_extra_path` 回调 |

## 测试计划

1. **DATA_DIR 可读**：构造一个 `~/.voidx/tool-results/<sid>/test.txt` 文件，read 工具应成功读取，无需用户确认。
2. **`~` 路径解析**：read 工具传入 `~/.voidx/tool-results/...` 路径，应正确 expanduser 并读取。
3. **外部路径申请权限**：read 工具传入 workspace 外的路径（非 DATA_DIR），应触发 `ctx.interact()` 用户确认，而非直接返回 `"Path traversal blocked"`。
4. **用户允许后路径加入 session extra_paths**：用户允许读取外部路径后，`host._permission.sandbox_workspace_write` 应包含该路径所在目录；同一 session 内再次读取同目录下文件不再询问。
5. **用户拒绝后不读取**：用户在交互中选择"拒绝"，read 工具应返回 `"Read denied by user"` 错误。
6. **workspace 内路径不受影响**：read 工具读取 workspace 内文件，行为不变，不触发交互。
7. **write 工具 DATA_DIR 可写**：file/write 工具写入 `~/.voidx/` 下文件，在 workspace-write 模式下应成功。
8. **`resolve_safe` 回归**：read/file/write/replace/git/lsp 工具读取 workspace 内相对路径，行为不变。

## 风险

- **安全面扩大**：将 `~/.voidx` 加入可写路径，意味着 LLM 可写入 sessions、store 等数据。但这些路径本就是 voidx 自身管理的运行时数据，风险可控。
- **外部路径授权持久性**：用户允许的外部路径会留在 session 级 `sandbox_workspace_write` 中直到 session 结束。这是预期行为（避免重复询问），但意味着用户授权一次后 LLM 可读取该目录下任意文件。通过加入路径所在目录而非文件本身来平衡便利性与安全。
- **`resolve_safe` 改动影响面广**：该方法被 read/file/write/replace/git/lsp 共用。`~` 解析修正对所有工具生效，需确保不破坏现有 path traversal 防护。
- **`add_extra_path` 回调缺失场景**：在无 host 的环境（如子 agent 或测试中），`ctx.add_extra_path` 可能为 None，read 工具需 fallback 为直接拦截。
