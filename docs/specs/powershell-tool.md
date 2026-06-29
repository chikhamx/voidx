# PowerShell 工具 — 技术设计文档

## Context

voidx 当前只有一个 `bash` 工具（`src/voidx/tools/bash/`），在所有平台上都注册。在 Windows 上这会误导 LLM：

- LLM 看到 `bash` 工具，倾向于写 `ls`/`cat`/`grep`/`echo > file` 等 unix 命令，但实际执行环境是 PowerShell 或 cmd，语义不同（路径分隔符、引号、重定向语法、管道对象 vs 文本）。
- 路由提示（`bash/router.py`）全是 unix 命令名（`cat`/`grep`/`find`/`sed`），Windows 下基本不触发，LLM 不会被引导到专用工具。
- 危险命令拦截（`bash/safety.py` 的 `_BLOCKED`）全是 unix 语义（`sudo`/`chmod`/`chown`/`mkfs`/`dd if= of=/dev/`），对 Windows 上的 `Stop-Computer`/`Format-Volume` 等无覆盖。
- 进程终止用 `os.killpg`（`bash/safety.py:69`），Windows 没有进程组概念——不过现有代码已有 `hasattr(os, "killpg")` 分支，这部分可复用。
- 权限层 `permission/service.py` 封装的 `is_safe_bash_command`（底层 `permission/rules.py:is_safe_bash`）和 `bash_sandbox_denial`（底层 `permission/sandbox.py:check_sandbox_bash`）用 `shlex` 解析 unix 命令，PowerShell 语法（cmdlet、别名、`|` 管道对象）解析不了。

**决策**：在 Windows 下注册一个独立的 `powershell` 工具替代 `bash`，非 Windows 仍用 `bash`。两个工具互斥注册，LLM 在 Windows 下只看到 `powershell`，不会误写 bash 命令。同时提取 bash/powershell 的公共逻辑到 `shell/common.py`，避免重复。

## Goals and Non-Goals

### Goals

- 新增 `src/voidx/tools/shell/` 公共层：`RouteHint` dataclass、结果封装工厂函数、进程终止逻辑、git 路由提示——bash 和 powershell 共享。
- 新增 `src/voidx/tools/powershell/` 包，结构与 `bash/` 对称，语义全部 Windows 化。
- `PowerShellTool`（id=`"powershell"`）用 `powershell.exe`（系统自带 5.1）执行命令，正确处理 UTF-8 编码。
- Windows 语义的危险命令拦截：`Stop-Computer`/`Restart-Computer`/`Format-Volume`/`Remove-Item -Force` 关键路径、`iex`/`Invoke-Expression` 配合下载、`Start-Process -Verb RunAs` 提权等。
- 沙箱校验：检查 `>`/`Out-File`/`Set-Content`/`Remove-Item`/`Move-Item`/`Copy-Item` 的写目标是否越界 workspace，复用 `resolve_safe` 路径校验。
- 路由提示：`Get-Content`→read、`Select-String`→grep、`Get-ChildItem`/`dir`→glob、`git`→git tool 等。
- `registry.py` 按 `os.name == "nt"` 平台二选一注册。
- 适配所有硬编码 `"bash"` 工具名的耦合点（权限层、工作流、UI、runtime guards）。
- 测试覆盖：核心执行、危险命令拦截、沙箱校验、路由提示。

### Non-Goals

- 不改造现有 `bash` 工具的 unix 专属逻辑（`_BLOCKED`/`_shell_words`/路由提示），只改外部引用指向 `shell/common`。
- 不支持 PowerShell 7+（`pwsh.exe`）——一期只用系统自带的 `powershell.exe`，兼容性优先。
- 不实现 PowerShell 脚本签名策略管理（依赖系统执行策略）。
- 不做交互式 PowerShell 会话（与 bash 工具一致，`-NonInteractive`）。
- 一期不重构 `permission/` 层为平台无关接口——PowerShell 工具自带 `sandbox.py`，在工具内部做沙箱校验；二期可考虑统一。

## Architecture

### 逻辑分类：平台无关 vs 平台专属

| 逻辑 | 当前位置 | 分类 | 处理方式 |
|------|---------|------|---------|
| `RouteHint` dataclass | `bash/core.py:16` | 平台无关 | 提取到 `shell/common.py` |
| 结果封装（blocked/sandbox/hint/timeout/success） | `bash/tool.py:35-127` | 平台无关 | 提取为 `shell/common.py` 工厂函数 |
| `_terminate_process`（已有 killpg/terminate 分支） | `bash/safety.py:65-88` | 平台无关 | 提取到 `shell/common.py` |
| git 路由提示 + `_git_subcommand` | `bash/hint/git.py` + `bash/core.py:90` | 平台无关 | 提取到 `shell/hint/git.py`（git 命令两平台语法一致） |
| `resolve_safe()` 路径校验 | `tools/base.py:15` | 平台无关 | 已共享，不用动 |
| `_shell_words`（`shlex` 解析） | `bash/core.py:22` | unix 专属 | bash 保留，powershell 重写 |
| `_has_shell_expansion`（`$()`/`` ` `` 检测） | `bash/core.py:38` | unix 专属 | bash 保留，powershell 重写 |
| `_strip_cd_prefix`（`cd dir && cmd`） | `bash/core.py:69` | unix 专属 | bash 保留，powershell 重写 |
| `_BLOCKED` 危险命令 | `bash/safety.py:15-32` | unix 专属 | bash 保留，powershell 重写 |
| `_normalize_command`（unix 转义剥离） | `bash/safety.py:35-43` | unix 专属 | bash 保留，powershell 重写 |
| 路由提示：`cat`/`head`/`tail`→read | `bash/hint/file.py:12-57` | unix 专属 | bash 保留，powershell 重写 |
| 路由提示：`echo >`/heredoc→write | `bash/hint/file.py:90-184` | unix 专属 | bash 保留，powershell 重写 |
| 路由提示：`find`→glob | `bash/hint/file.py:187-232` | unix 专属 | bash 保留，powershell 重写 |
| 路由提示：`grep`/`rg`→grep | `bash/hint/search.py:31-151` | unix 专属 | bash 保留，powershell 重写 |
| 路由提示：`sed`→replace | `bash/hint/search.py:158+` | unix 专属 | bash 保留，powershell 重写 |
| `is_safe_bash`（只读命令白名单） | `permission/rules.py:149-284` | unix 专属 | 不动，powershell 自带 `is_safe_powershell_command` |
| `check_sandbox_bash`（写目标提取） | `permission/sandbox.py:94-155` | unix 专属 | 不动，powershell 自带 `check_sandbox_powershell` |

### 模块边界

```
src/voidx/tools/
├── shell/                      # 新增：平台无关的公共逻辑
│   ├── __init__.py             # 导出 RouteHint, build_*_result, terminate_process
│   ├── common.py               # RouteHint, 结果封装工厂, terminate_process
│   └── hint/
│       ├── __init__.py
│       └── git.py              # git 路由提示 + _git_subcommand（两边复用）
├── bash/                       # 瘦身：只保留 unix 专属逻辑
│   ├── __init__.py             # 不变
│   ├── tool.py                 # execute() 改为调用 shell.common 工厂函数
│   ├── safety.py               # _BLOCKED(unix) + _normalize_command(unix)，_terminate_process 改为从 shell.common 导入
│   ├── router.py               # try_hint() 导入 shell.common.RouteHint + shell.hint.git
│   ├── core.py                 # _shell_words(shlex) + unix 解析原语（RouteHint 定义移走）
│   └── hint/
│       ├── file.py             # cat/head/tail/echo/find → read/write/glob（不变）
│       └── search.py           # grep/sed → grep/replace（不变）
├── powershell/                 # 新增：Windows 专属逻辑
│   ├── __init__.py             # 导出 PowerShellTool, PowerShellInput, RouteHint, try_hint
│   ├── tool.py                 # PowerShellTool — 执行 + 调用 shell.common 工厂函数
│   ├── safety.py               # _BLOCKED(powershell) + _normalize_command(powershell)
│   ├── sandbox.py              # check_sandbox_powershell + is_safe_powershell_command + _sandbox_denial
│   ├── router.py               # try_hint() 导入 shell.common.RouteHint + shell.hint.git
│   ├── core.py                 # PowerShell 语法解析原语（重新实现，非复制 bash）
│   └── hint/
│       ├── __init__.py
│       ├── file.py             # Get-Content/Set-Content/Out-File → read/write
│       └── search.py           # Select-String → grep, Get-ChildItem → glob
└── registry.py                 # os.name == "nt" 二选一注册
```

### 数据流

```
LLM 调用 powershell 工具 (command, timeout)
  │
  ▼
PowerShellTool.execute(args, ctx)
  │
  ├─ 1. _check_command(command)          # safety.py: 危险命令正则拦截
  ├─ 2. _sandbox_denial(command, ctx)    # sandbox.py: read-only 判定 + 写目标越界检查
  ├─ 3. try_hint(command)                # router.py: 路由提示，命中则不执行
  ├─ 4. asyncio.create_subprocess_exec(  # 执行 powershell.exe
  │      "powershell.exe", "-NoProfile", "-NonInteractive",
  │      "-OutputFormat", "Text",
  │      "-Command", "$OutputEncoding=[Console]::OutputEncoding=[Text.Encoding]::UTF8; <command>"
  │    )
  ├─ 5. asyncio.wait_for(proc.communicate(), timeout)
  └─ 6. 解码 stdout/stderr (UTF-8) → shell.common.build_success_result()
```

### 平台注册

`src/voidx/tools/registry.py` 的 `_register_builtins` 改为按平台二选一：

```python
import os

def _register_builtins(self) -> None:
    for cls in [
        FileReadTool, FileTool, WriteTool, FileReplaceTool,
        GitTool,
        GlobTool, GrepTool,
        LspTool,
        ClarifyTool, PlanCheckpointTool, WorkflowTool, CompactContextTool, LoadDocTemplateTool,
    ]:
        instance = cls()
        self.register(instance.id, instance, instance.description, instance.parameters_schema())
    # Shell 工具按平台二选一
    if os.name == "nt":
        from voidx.tools.powershell import PowerShellTool
        shell_cls = PowerShellTool
    else:
        from voidx.tools.bash import BashTool
        shell_cls = BashTool
    shell_instance = shell_cls()
    self.register(shell_instance.id, shell_instance, shell_instance.description, shell_instance.parameters_schema())
    # ... 其余依赖注入工具不变
```

### 工具名适配影响面

`"bash"` 工具名硬编码在多个层。PowerShell 注册为 `"powershell"` 后，这些点需要适配：

| 文件 | 当前代码 | 适配方式 |
|------|---------|---------|
| `permission/rules.py:46` | `Rule(permission="bash", pattern="*", action="ask")` | 追加 `Rule(permission="powershell", pattern="*", action="ask")` |
| `permission/rules.py:80` | `if name == "bash":` in `tool_call_from_pattern` | 追加 `elif name == "powershell":` 分支，args 同样取 `{"command": pattern}` |
| `permission/rules.py:111` | `if tool == "bash":` in `build_pattern` | 追加 `or tool == "powershell"` |
| `permission/rules.py:375` | `if tool == "bash":` in `capability_for_tool` | 追加 `or tool == "powershell"`，调用 `is_safe_powershell_command`，复用 `BASH_READ`/`BASH_WRITE` 能力枚举（不新增 `POWERSHELL_*` 枚举，使 `engine.py` 第 63/97/120/132/143 行的权限判断自动生效） |
| `permission/engine.py:80` | `if classified.name == "bash":` → `check_sandbox_bash` | 追加 `elif classified.name == "powershell":` → `check_sandbox_powershell` |
| `workflow/nodes.py:175,230,322,370` | `tools=[...,"bash",...]` | 追加 `"powershell"` 到各节点 tools 列表 |
| `agent/graph/runtime_guards.py:15` | `REPETITIVE_TOOL_EXEMPTIONS = frozenset({"bash",...})` | 追加 `"powershell"` |
| `agent/graph/runtime_guards.py:308` | `if tool_name == "bash":` | 改为 `if tool_name in ("bash", "powershell"):` |
| `workflow/auto_advance.py:80` | `elif tool_name == "bash":` | 改为 `elif tool_name in ("bash", "powershell"):`。**注意**：第 81 行调用的 `_check_bash_result(metadata, active_names)` 函数名含 "bash"，若 powershell 也走此函数需重命名为 `_check_shell_result` 并更新 docstring（当前 docstring 为 "Detect failed_implementation from bash test failures"），避免名不副实 |
| `ui/output/display_policy.py:127` | `"bash": ToolDisplayRule(...)` | 追加 `"powershell": ToolDisplayRule(...)` 同规则 |
| `ui/output/dock/nodes.py:153,361,394` | `if tool_name == "bash":` / `"bash": "Bash"` | 追加 powershell 分支 |
| `ui/output/events/consumers.py:554,575` | `"bash": "Running"` / `elif tool_name == "bash":` | 追加 powershell 分支 |
| `ui/output/capture.py:76` | `elif tool_name == "bash":` | 追加 powershell 分支 |
| `ui/output/console/app.py:44` | `"bash": "running"` | 追加 `"powershell": "running"` |
| `ui/output/console/formatting.py:92` | `if tool_name == "bash":` | 追加 powershell 分支 |
| `permission/rules.py:91-107` | `repair_tool_name` 的 `tool_map`（含 `"shell": "bash"` 别名） | 追加 `"PowerShell": "powershell"` 一条即可。`repair_tool_name` 第 107 行已有 `tool_map.get(tool.lower(), tool)` 兜底，`"powershell"`/`"POWERSHELL"` 等大小写变体会被 lowercase fallback 处理，无需枚举。与现有 `"Bash": "bash"` 保持对称 |

**策略**：权限层追加 `"powershell"` 规则和分支；工作流节点 tools 列表追加 `"powershell"`；UI 层和 runtime guards 改为集合判断 `{"bash", "powershell"}`。bash 的行为完全不变。

## Data Model

### PowerShellInput

```
PowerShellInput
├── command: str    (PowerShell 命令或脚本块)
└── timeout: int    (默认 120 秒)
```

与 `BashInput` 字段一致，便于未来统一抽象。

### RouteHint（提取到 shell/common.py）

```python
# shell/common.py
@dataclass
class RouteHint:
    tool_id: str       # "read" | "git" | "file" | "write" | "replace" | "glob" | "grep"
    ui_label: str      # UI 显示标签，如 "→ read"
    llm_hint: str      # 引导 LLM 用专用工具的文案
```

bash 和 powershell 的 `router.py` 都从 `shell.common` 导入，不各自定义。

### 结果封装工厂（shell/common.py）

```python
# shell/common.py — 提取自 bash/tool.py 的重复模式

def build_blocked_result(command: str, reason: str) -> ToolResult: ...
def build_sandbox_result(command: str, reason: str) -> ToolResult: ...
def build_hint_result(command: str, hint: RouteHint, tool_label: str) -> ToolResult: ...
def build_timeout_result(command: str, timeout: int) -> ToolResult: ...
def build_success_result(command: str, stdout: str, stderr: str, exit_code: int) -> ToolResult: ...
```

bash 和 powershell 的 `tool.py` 调用同一组工厂函数，结果结构完全一致。

### _BLOCKED 危险命令模式（powershell/safety.py）

```
_BLOCKED (PowerShell 语义)
├── Stop-Computer / Restart-Computer / Shutdown-Computer
├── Format-Volume
├── Remove-Item -Force (针对 C:\, C:\Windows, 注册表根)
├── Set-ExecutionPolicy Unrestricted/Bypass
├── Invoke-Expression (iex) 配合 Invoke-WebRequest/Invoke-RestMethod 下载
├── Start-Process -Verb RunAs (提权)
├── New-Service / Remove-Service
├── Set-ItemProperty (注册表 HKLM:\ 关键路径)
├── cmd /c (嵌套 cmd 绕过检查)
└── curl/wget | iex (下载执行)
```

## API Contract

### PowerShellTool

- **id**: `"powershell"`
- **description**: `"Execute a PowerShell command in the workspace directory on Windows. Returns stdout, stderr, and exit code."`
- **parameters**: `model_to_json_schema(PowerShellInput)` → `{"command": str, "timeout": int}`
- **execute(args, ctx) -> ToolResult**:
  - **成功**: `output` = JSON `{"ok": true, "exit_code": 0, "stdout": "...", "stderr": "..."}`，`display` = stdout + stderr，`metadata` = `{"command", "exit_code", "ok"}`
  - **危险命令拦截**: `metadata.blocked=True`，`display` = 拦截原因
  - **沙箱拒绝**: `metadata.blocked=True`，`display` = 拒绝原因
  - **路由提示**: `metadata.skipped=True`，`metadata.route_hint={tool_id, command}`，`next_step_hint` = 引导文案
  - **超时**: `metadata.timeout=True`，`display` = 超时提示，进程已终止
  - **非零退出**: `metadata.error=True`

### 进程执行细节

```python
proc = await asyncio.create_subprocess_exec(
    "powershell.exe",
    "-NoProfile",              # 不加载用户 profile，避免副作用
    "-NonInteractive",         # 不提示交互
    "-OutputFormat", "Text",   # 纯文本输出，不用 CLIXML
    "-Command",
    f"$OutputEncoding=[Console]::OutputEncoding=[Text.Encoding]::UTF8; {command}",
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    cwd=ctx.workspace,
)
```

**与 bash 的差异**：bash 用 `create_subprocess_shell`（通过 shell 执行），PowerShell 用 `create_subprocess_exec`（直接启动 `powershell.exe`，`-Command` 参数本身即"shell"）。不传 `start_new_session`（Windows 无进程组概念，该参数无意义）。

**编码处理**：PowerShell 5.1 默认输出编码可能是 GBK/UTF-16。通过在命令前注入 `$OutputEncoding=[Console]::OutputEncoding=[Text.Encoding]::UTF8` 强制 UTF-8，再用 `stdout.decode("utf-8", errors="replace")` 解码。

**进程终止**（提取到 `shell/common.py`，bash 和 powershell 共享）：

```python
# shell/common.py
async def terminate_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGTERM)   # Unix: 进程组
        else:
            proc.terminate()                       # Windows: TerminateProcess
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=2)
        return
    except asyncio.TimeoutError:
        pass
    with suppress(ProcessLookupError):
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    with suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=2)
```

### 沙箱校验 (powershell/sandbox.py)

```python
def check_sandbox_powershell(command: str, workspace: str, extra_paths: list[str]) -> str | None:
    """检查 PowerShell 命令的写目标是否在 workspace 内。

    覆盖：
    - 重定向: >, >>, >$null
    - Out-File, Set-Content, Add-Content, Tee-Object 的 -FilePath
    - Remove-Item, Move-Item, Copy-Item 的目标路径
    - New-Item -ItemType File 的 -Path

    返回 None 表示安全，或拒绝原因字符串。
    """
```

复用 `voidx.tools.base.resolve_safe(workspace, target, extra_paths)` 做路径校验，与 bash 沙箱一致。

`_sandbox_denial(command, ctx)` 是 `tool.py` 调用的入口，按 `ctx.sandbox_mode` 分流（与 `bash/safety.py:_sandbox_denial` 对称）：

```python
def _sandbox_denial(command: str, ctx: ToolContext) -> str | None:
    if ctx.sandbox_mode == "danger-full-access":
        return None
    if ctx.sandbox_mode == "read-only":
        if is_safe_powershell_command(command):  # sandbox.py: 只读命令判定
            return None
        return f"SANDBOX READ-ONLY: 'powershell' is not allowed.\n  command: {command.strip()[:120]}"
    return check_sandbox_powershell(command, ctx.workspace, ctx.sandbox_extra_paths)
```

`is_safe_powershell_command(command)` 判定只读命令（`Get-Content`/`Get-ChildItem`/`Select-String`/`Write-Output` 无重定向/`git status` 等），对应 bash 侧的 `permission/service.py:is_safe_bash_command`（底层 `permission/rules.py:is_safe_bash`）。一期在 `powershell/sandbox.py` 内实现，不改动 `permission/` 层。

**权限引擎层的适配**：`permission/engine.py:80` 的 `sandbox_denial_reason` 里有 `if classified.name == "bash":` 分支，调用 `check_sandbox_bash`。追加 `elif classified.name == "powershell":` 分支调用 `check_sandbox_powershell`，使 `workspace-write` 模式下权限引擎也能对 PowerShell 命令做沙箱校验。

### 路由提示 (powershell/router.py)

```python
def try_hint(command: str) -> RouteHint | None:
    """PowerShell 命令路由提示。

    覆盖别名和 cmdlet：
    - Get-Content / cat / type → read
    - Select-String / sls → grep
    - Get-ChildItem / dir / ls / gci → glob
    - git → git tool（复用 shell/hint/git.py）
    - Set-Content / Out-File → write
    """
```

**别名映射**（PowerShell 内置别名）：

```
cat, type     → Get-Content
dir, ls, gci  → Get-ChildItem
sls           → Select-String
echo, write   → Write-Output
del, erase    → Remove-Item
```

### powershell/core.py — 重新实现，非复制 bash

`powershell/core.py` 提供 PowerShell 语法解析原语，**不是**复制 bash 的 `shlex` 逻辑：

- `_shell_words(command)`: 解析 PowerShell 命令为 token 列表。PowerShell 的 `-Param` 语法、`|` 管道对象、`'...'`/`"..."` 引号规则与 posix shell 不同，不能用 `shlex`。
- `_has_shell_expansion(command)`: 检测 PowerShell 变量/子表达式（`$var`/`$(...)`/`@( )`），语义与 bash 的 `$()`/`` ` `` 不同。
- `_strip_cd_prefix(command)`: 处理 `Set-Location dir; cmd`（PowerShell 5.1 没有 `&&`，用 `;` 或 `if ($?) {}`）。

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| `powershell.exe` 不存在（极端情况） | `subprocess.FileNotFoundError`，返回 `ToolResult` 带 `error=True`，提示用户检查系统 |
| 命令超时 | `shell.common.terminate_process` 终止进程，返回 `timeout=True` |
| 命令被危险模式拦截 | 不执行，返回 `blocked=True` + 拦截原因 |
| 写目标越界 workspace | 不执行，返回 `blocked=True` + 拒绝原因 |
| 路由提示命中 | 不执行，返回 `skipped=True` + `route_hint`，引导 LLM 用专用工具 |
| 输出包含非 UTF-8 字节 | `errors="replace"` 解码，不抛异常 |
| 命令语法错误 | PowerShell 返回非零退出码 + stderr，正常返回给 LLM |
| 交互式命令（Read-Host 等） | `-NonInteractive` 下会立即报错，stderr 返回，LLM 可看到提示 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 新建独立 `powershell/` 包，与 `bash/` 平行 | 复用 bash 工具，按平台换执行后端（id 仍叫 `"bash"`） | 工具名仍叫 `bash` 会持续误导 LLM；独立包可让描述、参数、路由提示全部 Windows 化 |
| 提取公共逻辑到 `shell/common.py` | 各自复制 | RouteHint、结果封装、进程终止、git 路由提示是平台无关的，复制会导致维护时两处改 |
| 只用 `powershell.exe`（5.1） | 优先 `pwsh.exe`（7+）回退 5.1 | 系统自带，兼容性最好，不要求用户额外安装；7+ 可二期支持 |
| 平台二选一注册，互斥 | 两个都注册让 LLM 选 | 两个都注册仍会误导，LLM 可能混用 |
| `RouteHint` 提取到 `shell/common.py` 共享 | powershell 复制定义 | 避免重复定义；bash 和 powershell 的路由提示返回结构必须一致 |
| `powershell/core.py` 重新实现 PowerShell 解析 | 复制 bash 的 `shlex` 逻辑 | PowerShell 语法不是 posix shell，`shlex` 解析不了 cmdlet 的 `-Param` 和 `|` 管道对象 |
| 复用 `resolve_safe` 做沙箱路径校验 | 重新实现 | 路径校验逻辑平台无关，复用避免重复 |
| 路由提示覆盖 PowerShell 别名 | 只覆盖 cmdlet 全名 | LLM 常用别名（`dir`/`cat`/`ls`），不覆盖别名提示不触发 |
| 命令前注入 UTF-8 编码设置 | 用 `-Encoding` 参数或后处理解码 | PowerShell 5.1 的输出编码问题最稳妥的解法是运行时设置 `[Console]::OutputEncoding` |
| 一期 PowerShell 自带 `sandbox.py`，不重构 permission 层 | 把 `is_safe_bash`/`check_sandbox_bash` 重构为平台无关接口 | 一期范围控制；工具内部沙箱校验已覆盖安全需求；二期可统一 |
| 权限引擎 `engine.py` 追加 powershell 分支 | 不改权限引擎 | `workspace-write` 模式下权限引擎需要对 PowerShell 命令做沙箱校验，否则只靠工具内部不够 |
| 复用 `BASH_READ`/`BASH_WRITE` 枚举，不新增 `POWERSHELL_*` | 新增 `POWERSHELL_READ`/`POWERSHELL_WRITE` 枚举 | `engine.py` 的 `sandbox_denial_reason`（63/97 行）、`mode_overlay_denial_reason`（97 行）、`resolve_approval`（132/143 行）均基于 `capability` 判断，复用枚举使这些路径自动生效。**注意**：`strategy_action_for_tool`（120-123 行）对非只读命令走 `evaluate(classified.name, ...)` 路径——对 powershell 写命令会 `evaluate("powershell", ...)`，故 `BASIC_RULES` 必须有 `Rule(permission="powershell", ...)` 才能返回 `ask`（否则 fallback 到默认 `ask`，结果碰巧一致但语义上不应依赖默认值）。只读命令（`BASH_READ`）在第 120 行即 `return "allow"`，不依赖 `BASIC_RULES`。 |

## Open Questions

- [ ] PowerShell 的 `&` 调用操作符（`& "C:\path\to\script.ps1" arg1`）是否需要沙箱特殊处理？一期先按普通命令处理。
- [ ] PowerShell 脚本块（`{ ... }`）和 here-string（`@" ... "@`）的沙箱解析复杂度较高，一期是否只做单行命令的沙箱校验，多行/脚本块直接走 `danger-full-access`？倾向是。
- [ ] `powershell.exe` 路径解析：`create_subprocess_exec("powershell.exe", ...)` 依赖 `PATH` 中含 `System32`。正常环境无问题，但若用户 PATH 被改过可能找不到。实施时考虑用 `shutil.which("powershell")` 或完整路径 `os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")` 作为 fallback。
- [ ] 现有测试 `tests/test_tools/test_bash_tool.py` 需在 Windows 下跳过（追加 `pytest.mark.skipif(os.name == "nt", ...)` 标记，当前尚未标记），Windows 下新增 `test_powershell_tool.py`。bash 测试逻辑与 unix 语义强绑定，在 Windows 上跑无意义。
- [x] `tests/test_agent/test_call_llm_tools.py:235`（`assert "bash" in tool_names`）改为按平台断言：`assert ("bash" if os.name != "nt" else "powershell") in tool_names`，不跳过整个测试（保留 Windows 下的工具注册覆盖）。

## 实施阶段

### 阶段一：提取公共层 + bash 瘦身
- 新增 `shell/__init__.py`、`shell/common.py`（RouteHint、结果封装工厂、terminate_process）
- 新增 `shell/hint/__init__.py`、`shell/hint/git.py`（从 `bash/hint/git.py` + `bash/core.py:_git_subcommand` 提取）
- `bash/tool.py` 改为调用 `shell.common` 工厂函数
- `bash/safety.py` 的 `_terminate_process` 改为从 `shell.common` 导入
- `bash/router.py` 和 `bash/core.py` 的 `RouteHint` 改为从 `shell.common` 导入
- `bash/hint/git.py` 改为从 `shell.hint.git` 导入
- 测试：现有 bash 测试全部通过（回归验证）

### 阶段二：PowerShell 核心执行 + 危险命令拦截
- `powershell/__init__.py`、`powershell/tool.py`、`powershell/safety.py`、`powershell/core.py`
- `registry.py` 平台注册改造
- 测试：`tests/test_tools/test_powershell_tool.py`（核心执行、危险命令拦截）

### 阶段三：沙箱校验
- `powershell/sandbox.py`（check_sandbox_powershell + is_safe_powershell_command + _sandbox_denial）
- `tool.py` 接入 `_sandbox_denial`
- `permission/engine.py` 追加 powershell 沙箱分支
- `permission/rules.py` 追加 powershell 规则和分类分支
- 测试：沙箱越界写拦截

### 阶段四：路由提示
- `powershell/router.py`、`powershell/hint/file.py`、`powershell/hint/search.py`
- 测试：路由提示命中

### 阶段五：工具名适配
- `workflow/nodes.py`：各节点 tools 列表追加 `"powershell"`
- `agent/graph/runtime_guards.py`：去重豁免 + 结果指纹追加 powershell
- `workflow/auto_advance.py`：结果检测追加 powershell
- `ui/output/*`：display_policy、dock、events、capture、console 追加 powershell
- 测试：`tests/test_agent/test_call_llm_tools.py` 按平台适配

### 阶段六：测试适配 + Windows CI
- 现有 bash 相关测试按平台适配
- Windows CI 验证
