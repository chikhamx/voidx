---
name: bash-auto-route-git
display_name: Bash Auto-Route to Git Tool
description: 让 bash/powershell 工具遇到 git 命令时自动 route 到 git 工具执行，而非仅返回提示
doc_type: tech-design
audience: human+llm
---

# Bash Auto-Route to Git Tool — 技术设计文档

## TL;DR

当前 bash 工具遇到 `git` 命令时，通过 `try_hint()` 识别后只返回一个文本提示（"Prefer git tool with args=..."），**不执行**，需要 LLM 再次调用 git 工具。本设计让 bash 工具在 hint 命中后，通过 `ToolContext` 上的 registry 引用直接调用目标工具的 `execute()`，一步完成执行，能力不变。

## Context

### 当前行为

bash 工具执行流程（`src/voidx/tools/bash/tool.py:40-57`）：

1. `_check_command()` — 危险模式检查
2. `_sandbox_denial()` — sandbox 权限检查
3. `try_hint(inp.command)` — 路由提示检测
4. 命中 hint → `build_hint_result()` → **返回提示，不执行**
5. 未命中 → 正常执行 shell 命令

powershell 工具完全相同的流程（`src/voidx/tools/powershell/tool.py:56-58`）。

### 问题

- **多余的一轮交互**：LLM 写 `bash "git status"` → 得到"请用 git 工具" → 再调 `git args="status"` → 得到结果
- **LLM 可能忽略提示**：hint 是建议而非强制，LLM 可能放弃使用结构化工具
- **用户体验割裂**：同一个 git 命令，走 bash 和走 git 工具得到完全不同的结果格式

### 为什么只改 git

`try_hint()` 覆盖 7 种路由：`git`、`cat/head/tail`→read、`echo >`→write/manage、`find`→glob、`grep`→grep、`sed`→replace。

本设计**只改 git 路由**，原因：
- git 工具的参数模型最简单（`path` + `args` 两个字符串字段），转换零歧义
- git 是最高频的 bash 路由目标（开发工作流核心）
- 其他路由（read/write/grep/glob）的参数转换涉及复杂解析（正则、路径、include/exclude），风险高
- 渐进式推进：先验证 git auto-route，再考虑扩展

## Design

### 核心变更：RouteHint 携带结构化参数

当前 `RouteHint`（`src/voidx/tools/shell/common.py:32-36`）：

```python
@dataclass
class RouteHint:
    tool_id: _HintableTool
    ui_label: str
    llm_hint: str
```

新增 `tool_args` 字段：

```python
@dataclass
class RouteHint:
    tool_id: _HintableTool
    ui_label: str
    llm_hint: str
    tool_args: dict | None = None  # None = hint-only（旧行为）；dict = auto-route
```

- `tool_args=None`：保持旧行为，返回提示不执行
- `tool_args={...}`：bash/powershell 工具用这些参数直接调用目标工具

### git hint 填充 tool_args

`_hint_git()`（`src/voidx/tools/shell/hint/git.py:67-78`）已经解析出 `path` 和 `git_args`，当前只拼进文本。改为同时填充 `tool_args`：

```python
def _hint_git(stripped: str, words: list[str]) -> RouteHint | None:
    path, git_args = _git_tool_args(words)
    if not git_args:
        return None
    tool_args = {"args": git_args}
    if path:
        tool_args["path"] = path
    if path:
        llm_hint = f"Prefer git tool with path={path!r}, args={git_args!r} for structured output."
    else:
        llm_hint = f"Prefer git tool with args={git_args!r} for structured output."
    return RouteHint(
        tool_id="git", ui_label="→ git",
        llm_hint=llm_hint,
        tool_args=tool_args,
    )
```

### 全局选项处理与降级策略

`_git_tool_args()` 对全局选项的处理（`src/voidx/tools/shell/hint/git.py:17-54`）：

| 全局选项 | 当前行为 | auto-route 影响 |
|----------|---------|----------------|
| `-C <path>` | 提取 path，不进 git_args | ✅ 正确，path 填入 tool_args["path"] |
| `-c key=value` | 跳过，不进 git_args | ⚠️ 静默丢失配置 |
| `--git-dir`/`--work-tree` | 返回 None，不 hint | ✅ 安全，走原始 shell |
| `--no-pager` 等 flag | 跳过，不进 git_args | ✅ 无影响，git 工具默认不分页 |

**`-c` 降级策略**：当检测到 `-c` 全局选项时，`_hint_git()` 返回 `tool_args=None`（hint-only 模式），不 auto-route。这样 LLM 仍能看到提示并手动调用 git 工具补上配置。实现方式：在 `_git_tool_args()` 返回前检查是否遇到过 `-c`，若遇到则标记 `has_config=True`，`_hint_git()` 据此设置 `tool_args=None`。

### ToolContext 注入 registry

在 `ToolContext`（`src/voidx/tools/base.py:79`）新增字段：

```python
tool_registry: Any | None = Field(default=None, exclude=True)
```

使用 `exclude=True` 避免 pydantic 序列化时尝试深度遍历 registry（与 `mcp_manager`、`lsp_manager` 等字段一致）。

### 三处 ToolContext 构建点注入

| 位置 | 文件 | 注入源 |
|------|------|--------|
| 主 executor | `src/voidx/agent/graph/tool_executor/executor.py:108` | `host.tools` |
| subagent | `src/voidx/agent/graph/subagent.py:141` | 需传入 registry |
| slash loop | `src/voidx/agent/loop/slash.py:98` | `host.tools` |

主 executor 改动（`make_context()` 内）：

```python
def make_context() -> ToolContext:
    return ToolContext(
        ...
        tool_registry=host.tools,  # 新增
    )
```

subagent 改动：`run_subagent()` 已通过 `parent_tools` 参数接收 registry（赋值给局部变量 `agent_tools`，line 92）。ctx 构建处（line 141）直接注入 `tool_registry=agent_tools`，**无需新增参数**。

slash loop 改动：`_make_slash_context()` 函数从 host 获取 registry。

### bash/powershell 工具执行 auto-route

bash 工具（`src/voidx/tools/bash/tool.py:55-57`）：

```python
hint = try_hint(inp.command)
if hint is not None:
    if hint.tool_args is not None and ctx.tool_registry is not None:
        return await ctx.tool_registry.execute_tool(hint.tool_id, hint.tool_args, ctx)
    return build_hint_result(inp.command, hint, "Bash")
```

powershell 工具（`src/voidx/tools/powershell/tool.py:56-58`）同步改动。

**降级策略**：以下任一条件满足时，回退到旧行为（返回 hint）：
- `tool_args` 为 None（非 git 路由，或 git 命令含 `-c` 配置）
- `ctx.tool_registry` 为 None（MCP server 等无 registry 场景）
- `ctx.tool_registry.get("git")` 为 None（subagent 的 filtered registry 排除了 git 工具）

### 权限与安全保持不变

auto-route 调用 `registry.execute_tool("git", tool_args, ctx)`，这和 LLM 直接调用 git 工具走的是**完全相同的代码路径**：

1. `ToolRegistry.execute_tool()` → `GitTool.execute()`
2. GitTool 内部的 `git_policy_for_args()` 权限检查
3. 破坏性命令拦截（`_DENIED_SUBCOMMANDS`、`_DENIED_SUBCOMMAND_FLAGS`）
4. 外部仓库检测、运行时访问计划验证
5. 结构化输出解析

**不绕过任何安全检查**。唯一区别是调用来源从 LLM tool_call 变为 bash 工具内部转发。

### UI 显示

auto-route 返回的 `ToolResult` 来自 git 工具，其 `title`/`display`/`metadata` 格式与 LLM 直接调用 git 工具完全一致。UI 层无需改动。

bash 工具的 `metadata.command` 仍记录原始 bash 命令（如 `git status --porcelain`），但结果来自 git 工具。可在 metadata 中增加 `routed_from: "bash"` 标记，便于调试。

## File Structure

| 文件 | 改动 |
|------|------|
| `src/voidx/tools/shell/common.py` | `RouteHint` 新增 `tool_args` 字段 |
| `src/voidx/tools/shell/hint/git.py` | `_git_tool_args()` 返回 `has_config` 标记；`_hint_git()` 填充 `tool_args`，遇 `-c` 时设为 None |
| `src/voidx/tools/base.py` | `ToolContext` 新增 `tool_registry` 字段 |
| `src/voidx/tools/bash/tool.py` | hint 命中且有 `tool_args` 时 auto-route |
| `src/voidx/tools/powershell/tool.py` | 同步 bash 的 auto-route 逻辑 |
| `src/voidx/agent/graph/tool_executor/executor.py` | `make_context()` 注入 `host.tools` |
| `src/voidx/agent/graph/subagent.py` | ctx 构建注入 registry |
| `src/voidx/agent/loop/slash.py` | ctx 构建注入 registry |

## Tests

| 测试 | 命令 | 预期 |
|------|------|------|
| bash git auto-route 结构化输出 | `bash "git status --porcelain"` | 返回 git 工具的结构化 JSON |
| bash git auto-route 带路径 | `bash "git -C /path status"` | git 工具以 path 执行 |
| bash git 破坏性命令拦截 | `bash "git reset --hard"` | git 工具拒绝，返回 command_denied |
| bash 非 git 命令不受影响 | `bash "echo hello"` | 正常执行 shell |
| bash hint 无 registry 时降级 | ctx 无 `tool_registry` | 返回旧式 hint 提示 |
| powershell git auto-route | `powershell "git log --oneline -5"` | 返回 git 工具结构化 JSON |
| RouteHint tool_args 默认 None | 其他 hint 函数 | `tool_args=None`，保持旧行为 |
| bash git 含 -c 配置时降级 | `bash "git -c core.x=v status"` | 返回旧式 hint 提示，不 auto-route |
| bash git --git-dir 不 hint | `bash "git --git-dir=x status"` | 走原始 shell 执行 |
| filtered registry 降级 | subagent 的 registry 排除 git | 返回旧式 hint 提示 |
| try_hint 异常安全 | hint 解析抛异常 | `try_hint` 返回 None，走原始 shell |

测试命令：

```bash
./test.py --backend -- src/tests/test_tools/bash/test_router_git.py -v
./test.py --backend -- src/tests/test_tools/test_git_tool_structured.py -v
./test.py --backend -- src/tests/test_tools/test_git_tool_destructive.py -v
```

## Risks

1. **registry 循环引用**：bash 工具通过 registry 调用 git 工具，git 工具不会反过来调用 bash，无循环风险
2. **ToolContext 序列化**：`tool_registry` 用 `exclude=True`，与 `mcp_manager` 等字段处理方式一致
3. **subagent 无需签名变更**：`run_subagent()` 已通过 `parent_tools` 参数接收 registry（赋值给 `agent_tools`），ctx 构建处直接注入即可
4. **MCP server 场景**：`src/voidx/mcp_servers/web.py` 构建 `ToolContext(workspace=".")` 时无 registry，auto-route 会降级为 hint，行为正确
5. **渐进式扩展**：本设计只改 git，其他 hint（read/write/grep/glob）保持 hint-only，未来可逐步扩展
