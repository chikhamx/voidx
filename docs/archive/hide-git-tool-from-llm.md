# 设计：隐藏 git 工具的 LLM schema，bash 路由到 git 工具

> **Status: Done** — Archived on 2026-07-25.

## 背景与目标

当前 `git` 工具同时暴露给 LLM（schema）和内部路由（bash hint）。LLM 既可以直接调 `git` 工具，也可以写 `git ...` bash 命令被路由到 git 工具。这导致 LLM 在两个入口间犹豫，且 git 工具的 schema 占用工具表。

**目标**：对 LLM 隐藏 `git` 工具 schema，所有 git 操作统一从 bash 进入，由 bash router 路由到 git 工具内部逻辑；路由覆盖不了的场景降级为 bash 直接执行。

## 现状分析

### 路由机制

bash 工具执行 git 命令的路径（`bash/tool.py:61-66`）：

```
try_hint(command)  →  RouteHint(tool_id="git", tool_args=...)
maybe_route_hint   →  ctx.tool_registry.get("git")  →  execute_tool("git", ...)
```

`maybe_route_hint`（`shell/common.py:57-71`）只依赖 `ctx.tool_registry.get("git")`（实例层），**不依赖 `tools_for_llm()`（schema 层）**。`ctx.tool_registry` 指向完整的 `host.tools`，不是 filtered 副本。所以"注册实例但不暴露 schema"在架构上天然可分离。

### 三类场景

**第一类：可路由到 git 工具（正常路径）**

bash router 拦截条件全部通过 + `_hint_git` 生成有效 `tool_args`：
- 无 `;` `&&` `|` `$` `` ` `` 未引用 glob
- 无 `--git-dir=` `--work-tree=` `--namespace` `--exec-path`
- 无 `-c` 配置

例：`git status`、`git log -5`、`git diff --cached`、`git add src/foo.py`、`git -C /path status`

**结果**：路由到 git 工具，结构化输出 + 完整安全策略。✅ 无变化。

**第二类：`git -c` 配置命令（hint 不路由）**

`_hint_git` 第 73 行：`tool_args = {"args": git_args} if not has_config else None`。

触发条件：`git -c key=val <subcommand>`（`has_config=True`）。

当前行为（`bash/tool.py:62-66`）：`tool_args=None` → `maybe_route_hint` 返回 None → `build_hint_result` 返回"命令未执行，建议用 git 工具"。

**为什么 hint 故意不路由**：git 工具的 policy 本身就拒绝所有 `-c` 命令。`git_policy.py:137-142` 的 `_split_global_options` 对 `-c` 调用 `_global_config_error`：
- key 在 `DANGEROUS_CONFIG_PREFIXES`（`alias.`/`core.editor`/`core.hookspath` 等）→ "dangerous global config" → deny
- key 安全 → "global config is not registered" → deny

即**无论 key 是否危险，git 工具都 deny `-c` 命令**。这是设计上的安全约束——`-c` 可注入危险配置（如 `core.hookspath` 指向恶意 hook）。

**隐藏 git 工具后的问题**：LLM 看不到 git 工具，无法响应 hint，会反复重试 bash → 每次都被 `build_hint_result` 拦截 → 死循环。

**处理原则**：路由到 git 工具也会被 deny，路由没有额外价值 → **不路由，直接走 bash**。bash 侧的安全网仍生效（`shell_sandbox_precheck` 的 `defer` + 审批机制）。

**第三类：router 不拦截，直接走 bash（降级路径）**

bash router 拦截条件触发（`;` `&&` `|` `$` glob 等），`try_hint` 返回 `None`，或 `_hint_git` 第 71-72 行 `git_args=""` 时返回 `None`（`--git-dir=` `--work-tree=` 等场景）。

例：`git add . && git commit -m "msg"`、`git log | head`、`git --git-dir=/foo status`

**当前行为**：直接走 bash 子进程执行。安全网为 `shell_sandbox_precheck` + `check_sandbox_bash`：
- `check_sandbox_bash`（`sandbox.py:69-130`）：只拦截 `git push`（workspace 外写入）和重定向/破坏性文件操作
- `shell_sandbox_precheck`（`shell_policy.py:330-354`）：git 不在 `READ_COMMANDS` → `_bash_policy` 返回 `allowed=False` → 返回 `("defer", "unknown shell command")`
- `bash/tool.py:73-86`：`defer` 时 `shell_blocked` 非 None → `build_blocked_result`

**关键发现**：当前 git 命令走 bash 在非 full-access 模式下**已经被 block**（`defer` → blocked）。只有 `danger-full-access` 模式或 `has_approved_tool_risk` 审批后才放行。

## 方案

### 改动 1：`tools_for_llm` 加隐藏集合

**文件**：`src/voidx/tools/registry.py`

在 `ToolRegistry` 增加 `_HIDDEN_FROM_LLM` 集合，`tools_for_llm()` 过滤掉：

```python
_HIDDEN_FROM_LLM: frozenset[str] = frozenset()

def tools_for_llm(self) -> list[dict]:
    result = []
    for t in self._tools.values():
        if t.id in self._HIDDEN_FROM_LLM:
            continue
        ...
```

注册 git 工具时标记隐藏（或直接在 `_register_builtins` 后设置 `_HIDDEN_FROM_LLM = frozenset({"git"})`）。

git 工具仍注册到 `_instances`，`get("git")` / `execute_tool("git", ...)` 正常工作，bash 路由不受影响。

### 改动 2：`build_hint_result` 降级为 bash 执行（解决第二类卡死）

**文件**：`src/voidx/tools/bash/tool.py`

当前 `tool_args is None` 时直接返回 hint 阻止执行。改为：不再阻止，降级为 bash 执行。

```python
hint = try_hint(inp.command)
if hint is not None:
    routed = await maybe_route_hint(inp.command, hint, ctx, "bash")
    if routed is not None:
        return routed
    # tool_args is None（如 git -c）：路由到 git 工具也会被 policy deny，
    # 路由无额外价值 → 降级为 bash 执行，由 bash 侧安全网兜底
```

即：移除 `return build_hint_result(...)`，让控制流继续走到下方的 `shell_sandbox_precheck` + 子进程执行。

**影响**：
- `git -c key=val status` 会走 bash 子进程执行（经 `defer` + 审批），不再卡死
- 失去结构化输出，但 `git -c` 本就被 git 工具 deny，结构化输出本就拿不到
- bash 侧 `shell_sandbox_precheck` 的 `defer` 机制仍生效，非 full-access 模式下需审批

### 改动 3：bash 工具 description 更新

**文件**：`src/voidx/tools/bash/tool.py`

description 提示 git 操作走 bash 即可（LLM 不需要知道内部路由）：

```
Execute a Bash command; working directory is the workspace root.
Returns stdout, stderr, and exit code. Git commands are supported directly.
```

### 改动 4：不做

第三类（复合命令走 bash）当前在非 full-access 模式下已被 `defer` block。经评估：
- `git log | head` 这类只读复合命令被 block 是可接受的——LLM 应拆分为单条命令（`git log` 路由到 git 工具，`head` 不需要）
- 在 `shell_policy.py` 中识别只读 git 子命令放行（原选项 B）改动面较大，收益有限，不做

## 覆盖度与可接受性评估

| 场景 | 当前行为 | 改动后行为 | 可接受 |
|---|---|---|---|
| `git status` 等单条命令 | 路由到 git 工具，结构化输出 | 同上（无变化） | ✅ |
| `git -c key=val status` | hint 阻止，提示用 git 工具（但 git 工具也 deny） | 降级为 bash 执行（经 defer + 审批） | ✅ 路由无价值，bash 是唯一可执行路径 |
| `git --git-dir=/foo status` | router 不拦截，走 bash（被 defer block） | 同上（无变化） | ✅ 罕见场景 |
| `git add . && git commit` | router 不拦截，走 bash（被 defer block） | 同上（无变化） | ✅ 需审批，合理 |
| `git log \| head` | router 不拦截，走 bash（被 defer block） | 同上（无变化） | ✅ 保持现状，LLM 应拆分命令 |

## 风险

1. **结构化输出丢失**：第二类场景（`git -c`）从 hint 阻止改为 bash 文本输出。但 `git -c` 本就被 git 工具 deny，结构化输出本就拿不到，无实际损失。
2. **安全策略**：第二类降级到 bash 执行，但 `git -c` 走 git 工具也是 deny，两条路的安全语义一致（都不允许未经审批执行）。bash 侧 `shell_sandbox_precheck` 的 `defer` + 审批机制仍生效。
3. **复合命令审批频繁**：第三类在非 full-access 模式下需审批。LLM 应拆分命令，这是合理的安全边界。

## 测试策略

- `test_tools/bash/test_auto_route_git.py`：补"git 不在 LLM schema 但路由仍工作"用例
- `test_tools/bash/test_router_git.py`：补 `git -c` 降级执行用例（不再返回 hint）
- `test_tools/registry`：补 `tools_for_llm` 过滤 git 的用例
- 现有 git 工具测试保持不变（实例仍注册，行为不变）
