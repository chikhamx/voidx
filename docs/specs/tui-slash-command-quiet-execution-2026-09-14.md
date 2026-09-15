# TUI 斜杠指令免回显与静默执行技术规格文档

- **文档名称**：TUI 斜杠指令免回显与静默执行规范
- **文件路径**：`docs/specs/tui-slash-command-quiet-execution-2026-09-14.md`
- **日期**：2026-09-14
- **状态**：方案已修订（Revised after Subagent Independent Review）
- **读者**：架构维护者、前端/终端核心开发者、实施代理 (Human + LLM)
- **涉及范围**：
  - `src/voidx/agent/application/agent_service.py`
  - `src/voidx/presentation/slash/`（包括 `registry.py`、`port.py`、`handler.py`、`commands/*.py`、`runtime.py`）
  - `src/voidx/presentation/command_catalog.py`
  - `tui/voidx_cli/`（`app.py`、`render_status.py`）
  - 相关测试套件：`src/tests/test_application/test_slash_echo.py`、`src/tests/test_slash/` 等

---

## 1. 背景与根因剖析

### 1.1 现状与用户体验痛点

用户在 TUI 界面中执行 `/model` 指令时，界面出现以下非预期现象：

```text
❯ /model
gemini/gemini-3.8-flash-tiered (gemini/gemini-3.8-flash-tiered) ✓ switched (local)
─────────────────────────────────────────────────────────────────────────────
```

在此流程中：
1. 用户在底部输入框键入 `/model` 并回车；
2. 界面弹出浮层选择框（Choice Overlay Menu），供用户通过上下方向键选择模型；
3. 用户选择完成后浮层消失；
4. TUI 主聊天流（Dock 树）中残留了一整轮伪对话 Turn：包含前缀气泡 `❯ /model`、成功提示 `✓ switched (local)` 以及一条底部分割横线。

**体验冲突与冗余根因**：
- **状态栏已具备即时视觉反馈**：TUI 底部状态栏面板（`render_status.py`）已经实时绑定并显示当前生效的 `model`、`effort`、`context_limit`、`policy`、`state`、`profile` 等字段。当用户选择完毕时，状态栏字段会自动改变。
- **对话上下文污染**：在主聊天窗口中插入无意义的配置切换气泡和横线，割裂了正常的人机交互与代码生成上下文，导致聊天历史混乱。
- **取消操作残留脏文本**：用户若在弹出选择框后按 `ESC` 取消，目前还会向主窗口写入一行 `[dim]Cancelled.[/dim]`，造成二次干扰。

### 1.2 底层调用链与双层输出机制

上述输出由两个独立机制层级叠加产生：

```
[用户输入 /model 并回车]
          │
          ▼
  1. 输入路由层 (AgentService.dispatch_input)
          │ ── 判定 user_input.startswith("/")
          │ ── 判定 is_quiet（当前仅依赖 consume_quiet_command，用户主动输入时为 False）
          │ ── 判定 self_displays（当前硬编码仅包含 /loop, /init, /continue）
          │ ── turn_started = not is_quiet and not self_displays (= True)
          │ ──【产生第 1 层输出】：调用 autonomous_router.start_turn(user_input)
          │       └─ Dock 创建 TurnNode，渲染 "[bold white]❯[/] /model"
          ▼
  2. 命令执行层 (SlashHandler.dispatch -> ModelCommandsMixin._model_switch)
          │ ── 弹出 Choice Overlay (_select_from_list)
          │ ── 用户选定 profile
          │ ── 同步配置与状态栏 (_sync_context_limit 更新 prompt_ui.status)
          │ ──【产生第 2 层输出】：显式调用 model_port.ui.print("... ✓ switched")
          │       └─ Dock.append_message 写入普通文本消息节点
          ▼
  3. Turn 结束与收尾 (AgentService.dispatch_input finally)
          │ ── if turn_started: autonomous_router.end_turn()
          │ ──【产生第 3 层视觉残留】：Dock 画出底部分割横线与间距
```

**结论**：要达成“非必要不输出到 TUI”的目标，必须同时对这两层进行拦截和规范：
1. **输入回显层**：彻底阻断状态配置/弹窗类指令的 `start_turn` 与 `end_turn` 生命周期。
2. **命令输出层**：移除已被状态栏视觉覆盖的成功提示及取消输出，遵循“成功静默，失败报错”。

---

## 2. 核心设计原则

1. **状态栏已有反馈即静默 (Status Bar Authority)**：凡是操作结果已在底部状态栏（或活动视图）持久化呈现的指令，成功时不得向主消息流输出确认信息。
2. **免回显 (No Turn Echo)**：纯配置变更、弹窗交互、会话切换类指令，不得在主聊天区生成 `❯ /command` 气泡和 `end_turn` 分割线。
3. **取消无痕 (Pristine Cancellation)**：用户通过 `ESC` 取消弹窗交互时，界面保持原样，不输出 `Cancelled.`，不留任何痕迹。
4. **成功静默，失败报错 (Fail-Loud)**：静默原则仅适用于成功与主动取消；发生异常、缺少 API Key、参数非法时，必须通过 `ui.error` 显式输出错误，绝不静默吞没错误。
5. **报表与长文本例外 (Explicit Reporting Exemption)**：用户显式发起的查询、报表、诊断类指令（如 `/help`、`/diff`、`/usage`、`/lsp doctor` 等），其主观目的就是查阅长文本，必须完整保留输入回显与内容输出。

---

## 3. 全量斜杠指令完整分类矩阵 (Registry 41 个命令全覆盖)

基于 `src/voidx/presentation/slash/registry.py` 中登记的全部 41 个斜杠指令，严格按上述原则完成无死角分类：

### 3.1 【类别 A：全静默与免回显 (Category A: Pure Silent & Turnless)】
> **判定规则**：纯状态/权限/配置修改，或弹出选择菜单；执行结果由状态栏或会话元数据直接体现；无长文本阅读需求。  
> **行为规范**：
> - **输入回显**：禁止调用 `start_turn`，不打印 `❯ /cmd`。
> - **命令执行**：成功时完全静默（无 `ui.print`）；取消时完全静默；失败时调用 `ui.error` 报错。
> - **收尾行为**：禁止调用 `end_turn`，不画分割线。

| 指令 | 参数形式 | 交互方式 | 状态栏/界面已有对应反馈 | 处置方案 |
| :--- | :--- | :--- | :--- | :--- |
| `/model` | 无参或 `switch <name>` | 弹窗选择或直接切换 | 状态栏 `model`、`reasoning_effort`、`context_limit` 立即更新 | **全静默**：去除 `✓ switched`，去除取消输出 |
| `/model reasoning` | 无参或带参 | 弹窗选择或直接设置 | 状态栏 `effort`（如 `xhigh`）更新 | **全静默**：去除 `Reasoning effort: ... ✓` |
| `/model ctx` | 无参或带参 | 弹窗选择或直接设置 | 状态栏 `usage` 区域上限值更新 | **全静默**：去除 `Context window: ... ✓` |
| `/plan` | 无参 | 直接模式切换 | 状态栏 `state` 区域显示 `plan` | **全静默**：去除 `Mode set to Plan` |
| `/unplan` | 无参 | 直接模式切换 | 状态栏 `state` 区域隐藏 `plan` | **全静默**：去除 `Mode set to Auto` |
| `/permission` | 无参或带预设名 | 弹窗选择或直接设置 | 状态栏 `policy` 区域更新权限标签 | **全静默**：去除 `Permission mode set to ...` |
| `/allow` | 带参数（如 `/allow bash`） | 直接授权单工具 | 运行时权限表更新 | **全静默**：无需打印提示 |
| `/deny` | 带参数（如 `/deny bash`） | 直接拦截单工具 | 运行时权限表更新 | **全静默**：无需打印提示 |
| `/debug` | 无参或 `on/off` | 切换调试模式 | 状态栏 `state` 区域显示/隐藏 `debug` 标签 | **全静默**：去除 `debug on/off` |
| `/log` | 无参或 `exchange/diagnostic` | 切换 LLM 调试日志 | 运行时全局开关更新 | **全静默**：去除 `log exchange ...` |
| `/chat` | **仅限无参** | 切换至 chat 模式 | 状态栏 `profile` 显示 `chat` | **全静默**：去除 `Mode set to chat` |
| `/coding` | **仅限无参** | 切换至 coding 模式 | 状态栏 `profile` 显示 `coding`（缺省隐藏） | **全静默**：去除 `Mode set to coding` |
| `/goal` | **仅限无参** | 切换至 goal 模式 | 状态栏 `profile` 显示 `goal` | **全静默**：去除 `Mode set to goal` |
| `/lang` | 无参或带语言代号 | 弹窗选择或直接设置 | 写入全局用户偏好 | **全静默**：去除 `Language: ... ✓` |
| `/tone` | 无参或带语气代号 | 弹窗选择或直接设置 | 写入全局用户偏好 | **全静默**：去除 `Tone: ... ✓` |
| `/code-ide` | **无参或选择** | 弹窗选择 IDE | 写入全局设置 | **全静默**：配置已持久化保存 |
| `/compact-model` | **无参** | 弹窗选择压缩模型 | 写入全局设置 | **全静默**：去除提示文本 |
| `/image` | **无参或 strip on/off** | 弹窗选择或直接开关 | 运行时设置生效 | **全静默**：去除 `image strip: ...` |
| `/agents use <name>` | 带目标名 | 切换智能体 Profile | 状态栏 `profile` 更新 | **全静默**：去除切换提示 |
| `/title` | 带参数或 `auto` | 设置当前会话标题 | 状态栏 `session_title` 立即更新 | **全静默**：去除 `Title set: ...` |
| `/compact` | 无参 | 手工触发上下文压缩 | 状态栏 `usage` 区域 Token 数即时下降 | **全静默**：去除 `Compacted context.` |
| `/paste` | 无参 | 从系统剪贴板粘贴图片 | 生成图片引用注入输入框 | **全静默**：失败时报错，成功时静默 |

### 3.2 【类别 B：会话与画布重置类 (Category B: Canvas & Session Control - Turnless)】
> **判定规则**：直接重置工作区画布或变更会话生命周期的指令。无论是否带参数，**均不得在旧会话或新会话画布上残留发起指令的 Turn 节点**。  
> **行为规范**：
> - **输入回显**：禁止调用 `start_turn`。
> - **命令执行**：由指令自身刷新、清屏或重建 Dock 树。

| 指令 | 参数形式 | 行为说明 | 处置方案 |
| :--- | :--- | :--- | :--- |
| `/clear` | 无参 | 重置会话上下文并清空 Dock 历史 | **免回显**：不生成 `❯ /clear`，直接重置画布并显示启动横幅 |
| `/resume` | **任意形式（无参或带 session_id）** | 弹窗选择或直接加载历史会话 | **免回显**：禁止在当前画布或目标画布创建孤儿 Turn，全量恢复 Transcript Snapshot |
| `/session resume` | **任意形式（无参或带 session_id）** | 同 `/resume` | **免回显** |
| `/session new` | 可选带 profile | 创建全新空白会话并清屏 | **免回显**：直接初始化画布，不残留 `/session new` 气泡 |
| `/exit` / `/quit` | 无参 | 退出 TUI 进程 | **免回显**：直接退出应用循环 |
| `/continue` | 无参 | 触发模型继续上一轮生成 | **免回显**（保留在 `_SELF_DISPLAYING_COMMANDS` 中由续写任务接管） |
| `/rollback` | 无参 | 弹窗确认后回滚本轮代码修改 | **免回显**：弹窗确认前不建 Turn；回滚完成后仅在状态区刷新 |

### 3.3 【类别 C：内容查看与诊断排查类 (Category C: Reporting & Diagnostics - Echo & Full Output)】
> **判定规则**：指令的核心诉求是获取大段文本、表格、代码 Diff 或诊断报表，状态栏无法承载。  
> **行为规范**：
> - **输入回显**：正常调用 `start_turn`，保留 `❯ /cmd`，明确标记当前查阅内容的发起人意图。
> - **命令执行**：在主窗口打印结构化内容。
> - **收尾行为**：正常调用 `end_turn`，完成本轮查看。

| 指令 | 输出内容与必要性 |
| :--- | :--- |
| `/help` | 列出全部可用指令与说明，主屏帮助必读 |
| `/diff` | Git 变更文件及高亮 Diff 补丁，必须全屏审阅代码改动 |
| `/usage` | 会话各轮输入/输出/缓存明细与总消耗报告，状态栏仅能显示简短计数 |
| `/permissions` | 打印当前会话的所有放行/拒绝规则明细 |
| `/model list` | 查阅当前所有可用服务商及其模型目录列表 |
| `/model test <target>` | 实际连网测试指定模型的可用性与连通耗时/错误 |
| `/lsp` / `/lsp status` / `/lsp doctor` / `/lsp servers` | LSP 进程 PID、健康检查、报错诊断与安装指引 |
| `/skills` / `/skills list` / `/skills show <name>` / `/skills paths` | 技能列表、元数据与技能脚本正文查阅 |
| `/mcp` / `/mcp list` / `/mcp tools <name>` / `/mcp test <name>` | MCP 服务列表、工具接口清单及连通性测试报告 |
| `/upgrade check` / `/upgrade status` / `/upgrade now` | 版本更新检查、升级日志与执行进度输出 |
| `/code-ide status` | 查看当前配置及系统检测到的各 IDE 路径状态 |
| `/session` / `/session list` / `/list` | 查看已存会话列表 |
| `/session del [scope]` | 交互式展示待删除会话统计、清理报告与确认 |
| `/tavily` / `/tavily show` / `/bocha` / `/bocha show` | 查看当前搜索 API Key 的掩码配置与状态 |
| `/agents` / `/agents list` | 查看可用智能体 Profile 清单及诊断状态 |

### 3.4 【类别 D：业务任务启动类 (Category D: Autonomous Turn - Turn Initiated)】
> **判定规则**：附带用户 Prompt 或触发后台自主循环/代码编写轮次。  
> **行为规范**：进入完整 Agent 运行时轮次（`start_turn` -> 执行 -> `end_turn`）。

| 指令 | 行为说明 |
| :--- | :--- |
| `/chat <prompt>` / `/coding <prompt>` | 带有提示词时，作为对话首条消息启动 Coding/Chat 轮次 |
| `/goal <objective> --accept <cond>` | 启动后台目标驱动自治循环 |
| `/loop <interval> <prompt>` | 启动后台定时巡检/循环任务 |
| `/init [force]` | 启动基于模板生成 `AGENTS.md` 的代码生成轮次 |
| `/guide <guidance>` | 向上一步正在运行的 Agent 注入实时修正指引 |

---

## 4. 技术架构方案与健全性设计

### 4.1 `AgentService.dispatch_input` 健全路由与状态闭环

在 `src/voidx/agent/application/agent_service.py` 中：

```python
def is_turnless_command(user_input: str) -> bool:
    """Determine whether a slash command should skip turn creation in dock.
    
    Turnless commands do not create '❯ /cmd' turn headers or completion dividers.
    """
    cleaned = user_input.strip()
    if not cleaned.startswith("/"):
        return False
    parts = cleaned.split(maxsplit=1)
    cmd = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""

    # 1. 固有自渲染或接管 Turn 循环的指令
    if cmd in {"/loop", "/init", "/continue"}:
        return True

    # 2. 画布与会话重置类指令 (Category B)
    if cmd in {"/clear", "/exit", "/quit", "/rollback"}:
        return True
    if cmd == "/resume":  # 无论有无参数，恢复会话均不得在当前或新画布建立孤儿 Turn
        return True
    if cmd == "/session":
        sub = args.split(maxsplit=1)[0].lower() if args else ""
        if sub in {"new", "resume"}:
            return True

    # 3. 状态与配置修改类指令 (Category A)
    if cmd in {"/plan", "/unplan", "/debug", "/log", "/compact", "/paste"}:
        return True
    if cmd in {"/allow", "/deny"}:
        return True
    if cmd == "/title":
        return True
    if cmd == "/permission":
        # /permission (无参选模式) 或 /permission <mode_name> 为免回显；
        # /permissions (查看规则) 属于 Category C，由 Registry 精确路由
        return True
    if cmd in {"/lang", "/tone", "/compact-model", "/image"}:
        return True
    if cmd == "/code-ide" and args != "status":
        return True
    if cmd == "/model":
        # /model list 和 /model test 需要查看报告，其余 switch/reasoning/ctx 等全免回显
        sub = args.split(maxsplit=1)[0].lower() if args else ""
        if sub in {"list", "test"}:
            return False
        return True
    if cmd in {"/chat", "/coding", "/goal"} and not args:
        # 仅切换模式（无 prompt 参数）
        return True
    if cmd == "/agents" and args.startswith("use "):
        return True

    return False
```

在 `AgentService.dispatch_input` 中构建**异常防双发、状态严闭环**的分发流：

```python
        if user_input.startswith("/"):
            if user_input in ("/exit", "/quit"):
                return False, "\n[dim]bye.[/dim]"
            is_quiet = self._slash_dispatcher.consume_quiet_command(user_input)
            if is_quiet:
                self._slash_dispatcher.hide_command_output()
            is_turnless = is_quiet or is_turnless_command(user_input)
            turn_started = not is_turnless
            if turn_started:
                self._autonomous_router.start_turn(user_input)
            try:
                dispatched = await self._slash_dispatcher.dispatch_slash(user_input)
            except asyncio.CancelledError:
                if turn_started:
                    self._autonomous_router.cancel_turn()
                raise
            except Exception as exc:
                if turn_started:
                    self._autonomous_router.fail_turn(str(exc) or "command failed")
                # 注意：免回显指令发生异常时，直接向上 re-raise，由外层 run_loop.py
                # 统一捕获并在 UI 渲染，严禁在此处额外 publish_message 导致重复报错！
                raise
            finally:
                if is_quiet:
                    self._slash_dispatcher.hide_command_output()

            if not dispatched:
                self._autonomous_router.publish_message(
                    f"[dim]Unknown command: {user_input}  — type [cyan]/help[/cyan] to see available commands[/dim]"
                )
            if turn_started:
                self._autonomous_router.end_turn()
            # 关键保障：斜杠指令处理完毕后必须立即返回，严禁穿透到底层普通聊天/Coding 流程！
            return True, None
```

### 4.2 状态栏联动协议契约 (`PromptUi.invalidate`)

#### (1) 接口协议补齐
在 `src/voidx/presentation/slash/port.py` 的 `PromptUi` 协议中补齐 `invalidate`：
```python
class PromptUi(Protocol):
    status: object
    async def ask_choice(self, prompt: str, choices: list[object], **kwargs: object) -> object: ...
    async def ask_text(self, prompt: str, **kwargs: object) -> str | None: ...
    def invalidate(self) -> None: ...
```

在 `src/voidx/agent/adapters/slash_host.py` 的 `_PromptUi` 适配器中增加代理：
```python
class _PromptUi:
    def __init__(self, execution): self._execution = execution
    @property
    def status(self): return self._execution.presentation_ui._status_frontend.status
    async def ask_choice(self, prompt, choices, **kwargs): return await self._execution.presentation_ui.ask_choice(prompt, choices, **kwargs)
    async def ask_text(self, prompt, **kwargs): return await self._execution.presentation_ui.ask_text(prompt, **kwargs)
    def invalidate(self) -> None:
        target = getattr(self._execution.presentation_ui, "_interaction_frontend", None)
        if target is not None and hasattr(target, "invalidate"):
            target.invalidate()
```

#### (2) 防御性调用与 TUI 自身刷新保障
在各命令 Mixin 中（如 `ModelCommandsMixin._sync_context_limit`）调用时增加安全守护：
```python
app = self.model_port.prompt_ui
if app is not None:
    app.status.provider = ...
    app.status.model = ...
    invalidate = getattr(app, "invalidate", None)
    if callable(invalidate):
        invalidate()
```
同时，TUI 运行主循环（`PureTui._consume`，`app.py:2126`）在每个 `submit_task` 结束后本身就会执行 `self.invalidate()`。因此即便在未显式调用 `invalidate()` 的边界场景下，状态栏也 100% 能够在命令执行结束时完成重绘刷新。

### 4.3 命令输出降噪改造清单

1. **`ModelCommandsMixin` (`src/voidx/presentation/slash/commands/model.py`)**：
   - 移除 `_do_switch` 的 `ui.print(f"... ✓ switched ({scope_label})")`。
   - 移除 `_model_reasoning` 的 `ui.print("Reasoning effort: ... ✓")`。
   - 移除 `_model_ctx` 的 `ui.print("Context window: ... ✓")`。
   - 移除 `_pick_or_act` 中的 `ui.print("[dim]Cancelled.[/dim]")`。
2. **`ModeCommandsMixin` (`src/voidx/presentation/slash/commands/mode.py`)**：
   - 移除 `_set_interaction_mode` 的 `ui.print(f"[dim]Mode set to ...[/dim]")`。
   - 移除 `_debug` 的 `ui.print(f"[dim]debug {state}[/dim]")`。
   - 移除 `_image` 的 `ui.print(f"[dim]image strip: {state}[/dim]")`。
3. **`PermissionCommandsMixin` (`src/voidx/presentation/slash/commands/permission.py`)**：
   - 移除 `_permission_mode` 的 `ui.print(f"[dim]Permission mode set to ...[/dim]")`。
4. **`ProfileCommandsMixin` (`src/voidx/presentation/slash/commands/profile.py`)**：
   - 移除 `_apply_language` 的 `Language: ... ✓`。
   - 移除 `_apply_tone` 的 `Tone: ... ✓`。
   - 移除 `_pick_or_reset` 取消时的 `Cancelled.`。
5. **`SessionCommandsMixin` (`src/voidx/presentation/slash/commands/session.py`)**：
   - 移除 `_switch_profile` 切换空会话时的 `Mode set to <profile>`。
   - 移除 `_set_title` 设置完成后的 `Title set: ...`。
6. **`Handler` 顶层命令 (`src/voidx/presentation/slash/handler.py`)**：
   - 移除 `_cmd_compact` 的 `Compacted context.`。

---

## 5. 多端兼容性评估

- **Web Gateway (`src/voidx/presentation/gateway/`)**：  
  Web UI 与后端通信基于 `UiSubmitCommand` 和独立的 `ui_events` 协议流。Web 端前端面板本身不解析控制台 print 输出，静默化不会影响 Web 端的状态传输。
- **桌面端 (Desktop / Tauri 2)**：  
  桌面端的斜杠命令通过 `command_catalog.py` 分发，多数配置项标记为 `open-ui`（打开原生设置弹窗）。本改动针对终端 TUI 流程，桌面端行为不受破坏。
- **Headless 模式 (`run_headless`)**：  
  自动化测试与 CI 管道在无终端环境下运行，测试用例直接断言状态数据（如 `settings` 或 `model_config`），控制台静默使日志更纯净。

---

## 6. 测试套件迁移与验收标准

### 6.1 现有测试迁移指南
在 `src/tests/test_application/test_slash_echo.py` 中，原有以下用例：
- `test_slash_command_completes_turn`:
  ```python
  # 原用例使用 /model switch 验证普通指令会回显并结束 Turn：
  await service.dispatch_input("/model switch deepseek/deepseek-v4-flash --local")
  assert dock.echoed == ["/model switch deepseek/deepseek-v4-flash --local"]
  assert events.completed == 1
  ```
- `test_cancelled_slash_command_cancels_turn`:
  同样使用了 `/model switch` 测试取消。

**迁移方案**：
根据分类矩阵，`/model switch` 已被重构为**类别 A（免回显）**，不再产生 `dock.echoed`。  
迁移措施：
1. 将上述两个用例中用于验证“常规指令创建并完成 Turn”的命令替换为**类别 C（报表类指令）**，例如 `/help` 或 `/permissions`：
   ```python
   await service.dispatch_input("/help")
   assert dock.echoed == ["/help"]
   assert events.completed == 1
   ```
2. 在 `test_slash_echo.py` 中新增针对类别 A 的专项测试用例：
   ```python
   @pytest.mark.parametrize("command", [
       "/model",
       "/model switch deepseek/deepseek-v4-flash --local",
       "/plan",
       "/unplan",
       "/permission safe",
       "/debug on",
   ])
   async def test_silent_commands_skip_turn_lifecycle(command: str) -> None:
       dock = FakeDock()
       service, events = _service(dock)
       service._slash_dispatcher.bind_frontend(FakeApp())
       await service.dispatch_input(command)
       assert dock.echoed == []
       assert events.completed == 0
   ```
3. 在 `tests/test_slash/test_slash_model.py` 等测试的 Mock 对象（如 `FakeChoiceApp`、`StubPromptUi`）中补齐 `def invalidate(self) -> None: pass`。

### 6.2 验收验证命令
- 针对性回归：`./test.py --backend -- src/tests/test_application/test_slash_echo.py -v`
- 模型命令测试：`./test.py --backend -- src/tests/test_slash/test_slash_model.py -v`
- 全量后端验证：`./test.py --backend`（确保 0 failures）
