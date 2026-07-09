> **Status: Done** — 实施计划已全部执行完毕，代码与测试均已落地。

# PowerShell 工具 — 实施计划

> 基于 `docs/specs/powershell-tool.md`（已 review 修正）。本文档是可执行的逐步实施计划。

## Goal

在 Windows 下注册独立的 `powershell` 工具替代 `bash`，提取公共逻辑到 `shell/common.py`，适配所有硬编码 `"bash"` 耦合点。

## Architecture

新建 `src/voidx/tools/shell/` 公共层（RouteHint、结果封装工厂、terminate_process、git hint），`bash/` 瘦身为只保留 unix 专属逻辑，新增 `powershell/` 包结构与 `bash/` 对称。`registry.py` 按 `os.name == "nt"` 平台二选一注册。权限层复用 `BASH_READ`/`BASH_WRITE` 枚举，追加 `powershell` 规则和分支。

## Tech Stack

- Python 3.11+，Pydantic（Input model），asyncio（subprocess 执行）
- `powershell.exe`（Windows 自带 5.1），`-NoProfile -NonInteractive -OutputFormat Text -Command`
- pytest（测试），`pytest.mark.skipif(os.name == "nt")`（bash 测试平台标记）

## File Structure

### 新增文件

| 文件 | 职责 |
|------|------|
| `src/voidx/tools/shell/__init__.py` | 导出 RouteHint, build_*_result, terminate_process |
| `src/voidx/tools/shell/common.py` | RouteHint dataclass、结果封装工厂函数、terminate_process |
| `src/voidx/tools/shell/hint/__init__.py` | 包初始化 |
| `src/voidx/tools/shell/hint/git.py` | git 路由提示 + `_git_subcommand`（从 bash 提取，两边复用） |
| `src/voidx/tools/powershell/__init__.py` | 导出 PowerShellTool, PowerShellInput, RouteHint, try_hint |
| `src/voidx/tools/powershell/tool.py` | PowerShellTool — 执行 + 调用 shell.common 工厂函数 |
| `src/voidx/tools/powershell/safety.py` | `_BLOCKED`(powershell) + `_normalize_command`(powershell) + `_check_command` |
| `src/voidx/tools/powershell/sandbox.py` | `check_sandbox_powershell` + `is_safe_powershell_command` + `_sandbox_denial` |
| `src/voidx/tools/powershell/router.py` | `try_hint()` 导入 shell.common.RouteHint + shell.hint.git |
| `src/voidx/tools/powershell/core.py` | PowerShell 语法解析原语（`_shell_words`/`_has_shell_expansion`/`_strip_cd_prefix`） |
| `src/voidx/tools/powershell/hint/__init__.py` | 包初始化 |
| `src/voidx/tools/powershell/hint/file.py` | Get-Content/Set-Content/Out-File → read/write |
| `src/voidx/tools/powershell/hint/search.py` | Select-String → grep, Get-ChildItem → glob |
| `tests/test_tools/test_powershell_tool.py` | PowerShell 核心执行、危险命令拦截、沙箱、路由提示测试 |

### 修改文件

| 文件 | 改动 |
|------|------|
| `src/voidx/tools/bash/tool.py` | execute() 改为调用 shell.common 工厂函数 |
| `src/voidx/tools/bash/safety.py` | `_terminate_process` 改为从 shell.common 导入 |
| `src/voidx/tools/bash/router.py` | RouteHint 改为从 shell.common 导入 |
| `src/voidx/tools/bash/core.py` | RouteHint 定义移走，改为从 shell.common 导入（re-export 保持向后兼容） |
| `src/voidx/tools/bash/hint/git.py` | 改为从 shell.hint.git 导入 |
| `src/voidx/tools/registry.py` | `_register_builtins` 按 `os.name == "nt"` 二选一注册 |
| `src/voidx/permission/rules.py` | 追加 powershell 规则、分类分支、capability 分支、repair_tool_name 别名 |
| `src/voidx/permission/engine.py` | 追加 powershell 沙箱分支 |
| `src/voidx/workflow/nodes.py` | 各节点 tools 列表追加 `"powershell"` |
| `src/voidx/agent/graph/runtime_guards.py` | 去重豁免 + 结果指纹追加 powershell |
| `src/voidx/workflow/auto_advance.py` | 结果检测追加 powershell，`_check_bash_result` 重命名为 `_check_shell_result` |
| `src/voidx/ui/output/display_policy.py` | 追加 powershell display rule |
| `src/voidx/ui/output/dock/nodes.py` | 追加 powershell 分支 |
| `src/voidx/ui/output/events/consumers.py` | 追加 powershell 分支 |
| `src/voidx/ui/output/capture.py` | 追加 powershell 分支 |
| `src/voidx/ui/output/console/app.py` | 追加 `"powershell": "running"` |
| `src/voidx/ui/output/console/formatting.py` | 追加 powershell 分支 |
| `tests/test_tools/test_bash_tool.py` | 追加 `skipif(os.name == "nt")` 标记 |
| `tests/test_agent/test_call_llm_tools.py` | 按平台断言 bash/powershell |

## Tasks

### 阶段一：提取公共层 + bash 瘦身

- [ ] **T1.1** 创建 `src/voidx/tools/shell/__init__.py`，导出 RouteHint, build_blocked_result, build_sandbox_result, build_hint_result, build_timeout_result, build_success_result, terminate_process
- [ ] **T1.2** 创建 `src/voidx/tools/shell/common.py`：
  - 从 `bash/core.py:15-19` 移入 `RouteHint` dataclass（含 `_HintableTool` Literal 类型）
  - 从 `bash/tool.py:35-127` 提取 5 个结果封装工厂函数（build_blocked_result/build_sandbox_result/build_hint_result/build_timeout_result/build_success_result）
  - 从 `bash/safety.py:65-88` 移入 `_terminate_process`，重命名为 `terminate_process`（公开）
- [ ] **T1.3** 创建 `src/voidx/tools/shell/hint/__init__.py`（空包初始化）
- [ ] **T1.4** 创建 `src/voidx/tools/shell/hint/git.py`：从 `bash/core.py:85-107` 移入 `_git_subcommand` + `_GIT_GLOBAL_OPTIONS_WITH_VALUE`，从 `bash/hint/git.py` 移入 `_hint_git`，改为从 `shell.common` 导入 RouteHint
- [ ] **T1.5** 修改 `src/voidx/tools/bash/core.py`：删除 RouteHint 定义和 `_git_subcommand`/`_GIT_GLOBAL_OPTIONS_WITH_VALUE`，改为从 `shell.common` 和 `shell.hint.git` 导入并 re-export（保持 `bash/__init__.py` 的 `from voidx.tools.bash.router import RouteHint` 仍可用）
- [ ] **T1.6** 修改 `src/voidx/tools/bash/safety.py`：删除 `_terminate_process` 函数体，改为 `from voidx.tools.shell.common import terminate_process as _terminate_process`
- [ ] **T1.7** 修改 `src/voidx/tools/bash/tool.py`：execute() 中的结果构造改为调用 `shell.common` 工厂函数
- [ ] **T1.8** 修改 `src/voidx/tools/bash/hint/git.py`：改为从 `shell.hint.git` 导入 `_hint_git`，从 `shell.common` 导入 RouteHint
- [ ] **T1.9** 修改 `src/voidx/tools/bash/router.py`：RouteHint 导入改为从 `shell.common`

**验证**：`.\python.ps1 -m pytest tests/test_tools/test_bash_tool.py tests/test_tools/test_bash_router.py -v` 全部通过（回归验证，bash 行为不变）

### 阶段二：PowerShell 核心执行 + 危险命令拦截

- [ ] **T2.1** 创建 `src/voidx/tools/powershell/core.py`：PowerShell 语法解析原语
  - `_shell_words(command)`: 解析 PowerShell token（`-Param`、`|` 管道、`'...'`/`"..."` 引号），不用 shlex
  - `_has_shell_expansion(command)`: 检测 `$var`/`$(...)`/`@(...)`
  - `_strip_cd_prefix(command)`: 处理 `Set-Location dir; cmd`（PowerShell 5.1 无 `&&`）
- [ ] **T2.2** 创建 `src/voidx/tools/powershell/safety.py`：
  - `_BLOCKED`: Windows 语义危险命令（Stop-Computer/Restart-Computer/Format-Volume/Remove-Item -Force 关键路径/Set-ExecutionPolicy Unrestricted/Invoke-Expression 配合下载/Start-Process -Verb RunAs/New-Service/Remove-Service/Set-ItemProperty HKLM:/cmd /c/curl|wget|iex）
  - `_normalize_command(command)`: PowerShell 转义剥离
  - `_check_command(command) -> str | None`: 危险命令拦截
- [ ] **T2.3** 创建 `src/voidx/tools/powershell/tool.py`：
  - `PowerShellInput(BaseModel)`: command: str, timeout: int = 120
  - `PowerShellTool(BaseTool)`: id="powershell"，execute() 调用 `_check_command` → `_sandbox_denial`（阶段三接入，先占位返回 None）→ `try_hint`（阶段四接入，先占位返回 None）→ `asyncio.create_subprocess_exec("powershell.exe", ...)` → `shell.common.build_success_result()`
  - 进程执行：`powershell.exe -NoProfile -NonInteractive -OutputFormat Text -Command "$OutputEncoding=[Console]::OutputEncoding=[Text.Encoding]::UTF8; <command>"`
  - 编码：`stdout.decode("utf-8", errors="replace")`
  - 超时：调用 `shell.common.terminate_process`
- [ ] **T2.4** 创建 `src/voidx/tools/powershell/__init__.py`：导出 PowerShellTool, PowerShellInput
- [ ] **T2.5** 修改 `src/voidx/tools/registry.py`：`_register_builtins` 中 Shell 工具按 `os.name == "nt"` 二选一（移除 BashTool 从顶部 cls 列表，改为平台分支注册）
- [ ] **T2.6** 创建 `tests/test_tools/test_powershell_tool.py`：
  - `test_powershell_echo`: `Write-Output hello` 执行成功
  - `test_powershell_blocks_stop_computer`: `Stop-Computer` 被拦截
  - `test_powershell_blocks_format_volume`: `Format-Volume` 被拦截
  - `test_powershell_blocks_iex_download`: `iex (irm url)` 被拦截
  - 标记 `@pytest.mark.skipif(os.name != "nt", reason="PowerShell only on Windows")`

**验证**：`.\python.ps1 -m pytest tests/test_tools/test_powershell_tool.py -v`（Windows 上）通过

### 阶段三：沙箱校验

- [ ] **T3.1** 创建 `src/voidx/tools/powershell/sandbox.py`：
  - `is_safe_powershell_command(command) -> bool`: 只读命令判定（Get-Content/Get-ChildItem/Select-String/Write-Output 无重定向/git status 等）
  - `check_sandbox_powershell(command, workspace, extra_paths) -> str | None`: 写目标越界检查（`>`/`>>`/`Out-File`/`Set-Content`/`Add-Content`/`Tee-Object` 的 -FilePath，`Remove-Item`/`Move-Item`/`Copy-Item` 目标，`New-Item -ItemType File` 的 -Path），复用 `resolve_safe`
  - `_sandbox_denial(command, ctx) -> str | None`: 按 `ctx.sandbox_mode` 分流
- [ ] **T3.2** 修改 `src/voidx/tools/powershell/tool.py`：接入 `_sandbox_denial`（替换阶段二的占位）
- [ ] **T3.3** 修改 `src/voidx/permission/engine.py:80`：追加 `elif classified.name == "powershell":` → `check_sandbox_powershell`
- [ ] **T3.4** 修改 `src/voidx/permission/rules.py`：
  - 第 46 行后追加 `Rule(permission="powershell", pattern="*", action="ask")`
  - 第 80 行 `tool_call_from_pattern` 追加 `elif name == "powershell":` 分支
  - 第 111 行 `build_pattern` 追加 `or tool == "powershell"`
  - 第 375 行 `capability_for_tool` 追加 `or tool == "powershell"`，调用 `is_safe_powershell_command`
  - 第 99 行 `repair_tool_name` 的 tool_map 追加 `"PowerShell": "powershell"`
- [ ] **T3.5** 在 `tests/test_tools/test_powershell_tool.py` 追加沙箱测试：
  - `test_powershell_blocks_workspace_escape`: `Out-File` 写 workspace 外被拦截
  - `test_powershell_blocks_remove_item_outside`: `Remove-Item` workspace 外被拦截
  - `test_powershell_readonly_allowed_in_readonly_mode`: `Get-Content` 在 read-only 模式下允许

**验证**：`.\python.ps1 -m pytest tests/test_tools/test_powershell_tool.py tests/test_tools/test_bash_tool.py -v` 通过

### 阶段四：路由提示

- [ ] **T4.1** 创建 `src/voidx/tools/powershell/hint/__init__.py`（包初始化）
- [ ] **T4.2** 创建 `src/voidx/tools/powershell/hint/file.py`：
  - `_hint_get_content(words)`: Get-Content/cat/type → read
  - `_hint_set_content(stripped, words)`: Set-Content/Out-File → write
- [ ] **T4.3** 创建 `src/voidx/tools/powershell/hint/search.py`：
  - `_hint_select_string(words)`: Select-String/sls → grep
  - `_hint_get_child_item(words)`: Get-ChildItem/dir/ls/gci → glob
- [ ] **T4.4** 创建 `src/voidx/tools/powershell/router.py`：
  - `try_hint(command) -> RouteHint | None`: 调用 `_strip_cd_prefix` → `_has_shell_expansion` 检查 → 分发到 hint 函数
  - git 命令复用 `shell.hint.git._hint_git`
  - 别名映射：cat/type→Get-Content, dir/ls/gci→Get-ChildItem, sls→Select-String, echo/write→Write-Output, del/erase→Remove-Item
- [ ] **T4.5** 修改 `src/voidx/tools/powershell/tool.py`：接入 `try_hint`（替换阶段二的占位）
- [ ] **T4.6** 修改 `src/voidx/tools/powershell/__init__.py`：追加导出 RouteHint, try_hint
- [ ] **T4.7** 在 `tests/test_tools/test_powershell_tool.py` 追加路由提示测试：
  - `test_powershell_route_hint_git`: `git status` → route_hint tool_id="git"
  - `test_powershell_route_hint_get_content`: `Get-Content file.py` → route_hint tool_id="read"
  - `test_powershell_route_hint_select_string`: `Select-String -Pattern "foo" *.py` → route_hint tool_id="grep"
  - `test_powershell_route_hint_out_file`: `Out-File -FilePath out.txt` → route_hint tool_id="write"

**验证**：`.\python.ps1 -m pytest tests/test_tools/test_powershell_tool.py -v` 通过

### 阶段五：工具名适配

- [ ] **T5.1** 修改 `src/voidx/workflow/nodes.py`：第 175/230/322/370 行各节点 tools 列表追加 `"powershell"`
- [ ] **T5.2** 修改 `src/voidx/agent/graph/runtime_guards.py`：
  - 第 15 行 `REPETITIVE_TOOL_EXEMPTIONS` 追加 `"powershell"`
  - 第 308 行 `if tool_name == "bash":` 改为 `if tool_name in ("bash", "powershell"):`
- [ ] **T5.3** 修改 `src/voidx/workflow/auto_advance.py`：
  - 第 80 行 `elif tool_name == "bash":` 改为 `elif tool_name in ("bash", "powershell"):`
  - `_check_bash_result` 重命名为 `_check_shell_result`，更新 docstring 和所有调用点
- [ ] **T5.4** 修改 `src/voidx/ui/output/display_policy.py:127`：追加 `"powershell": ToolDisplayRule(...)` 同 bash 规则
- [ ] **T5.5** 修改 `src/voidx/ui/output/dock/nodes.py`：第 153/361/394 行追加 powershell 分支
- [ ] **T5.6** 修改 `src/voidx/ui/output/events/consumers.py`：第 554/575 行追加 powershell 分支
- [ ] **T5.7** 修改 `src/voidx/ui/output/capture.py:76`：追加 powershell 分支
- [ ] **T5.8** 修改 `src/voidx/ui/output/console/app.py:44`：追加 `"powershell": "running"`
- [ ] **T5.9** 修改 `src/voidx/ui/output/console/formatting.py:92`：追加 powershell 分支
- [ ] **T5.10** 修改 `tests/test_agent/test_call_llm_tools.py:235`：改为 `assert ("bash" if os.name != "nt" else "powershell") in tool_names`

**验证**：`.\python.ps1 -m pytest tests/test_agent/test_call_llm_tools.py tests/test_workflow/ -v` 通过

### 阶段六：测试适配 + 回归

- [ ] **T6.1** 修改 `tests/test_tools/test_bash_tool.py`：类级或关键测试追加 `@pytest.mark.skipif(os.name == "nt", reason="bash tests are unix-only")`
- [ ] **T6.2** 运行全量测试回归：`.\python.ps1 -m pytest tests/ -v`
- [ ] **T6.3** 运行前端测试回归（如有 UI 层改动影响）：`cd frontend && npm test`

**验证**：全量测试通过，bash 行为在非 Windows 上完全不变

## Risks

1. **`bash/core.py` re-export 兼容性**：RouteHint 移到 `shell/common.py` 后，`bash/__init__.py` 通过 `from voidx.tools.bash.router import RouteHint` 导出，而 router 从 `shell.common` 导入——需确保 re-export 链不断。T1.5 中 `bash/core.py` 保留 `from voidx.tools.shell.common import RouteHint` 的 re-export。
2. **PowerShell 语法解析复杂度**：`_shell_words` 不能用 shlex，需手写 PowerShell token 解析。一期可做简化版（处理常见 cmdlet + 引号 + 管道），复杂脚本块走 `danger-full-access`（Open Questions 已记录）。
3. **`_check_bash_result` 重命名影响面**：重命名为 `_check_shell_result` 需更新所有调用点（auto_advance.py 内部），grep 确认无外部引用。
4. **Windows CI 缺失**：PowerShell 测试只能在 Windows 上跑，非 Windows CI 会 skip。需确保 `skipif` 标记正确，不会在 Linux CI 上报错。
5. **`strategy_action_for_tool` 权限路径**：powershell 写命令走 `evaluate("powershell", ...)`，依赖 `BASIC_RULES` 有 `Rule(permission="powershell", ...)`（T3.4 已覆盖）。若遗漏会导致 fallback 到默认 `ask`——结果碰巧一致但不应依赖默认值。
6. **`powershell.exe` 路径**：依赖 PATH 含 System32，极端环境可能找不到（Open Questions 已记录，一期用 `shutil.which` fallback）。

## Verification

每个阶段完成后运行对应验证命令。全部完成后运行：

```powershell
# Windows 全量验证
.\python.ps1 -m pytest tests/ -v

# 前端测试（UI 层改动）
cd frontend && npm test
```

非 Windows 环境验证 bash 回归：

```bash
./python.sh -m pytest tests/test_tools/test_bash_tool.py tests/test_tools/test_bash_router.py -v
```
