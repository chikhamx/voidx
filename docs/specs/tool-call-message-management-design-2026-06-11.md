# Tool Call Message Management — 技术设计文档

> **Status: Draft**

## Context

voidx 的 tool call message 管理目前存在三个问题：

1. **LLM context 和 UI display 耦合** — `sanitize_tool_message_content()` 同时影响发给模型的内容和用户看到的内容，但两者的需求不同。
2. **大结果直接截断，信息丢失** — 超过 4000 字符的工具输出被截断，LLM 看到残缺内容 + `[truncated]`，无法知道丢失了什么，也无法恢复。
3. **没有 per-tool 显示策略** — 所有工具的 UI 显示逻辑散落在各处 if/else 中，只有 `todo` 被硬编码抑制（`_suppressed_tool_ids`），没有可配置的显示/摘要/隐藏三级控制。

调研了 Claude Code、Codex CLI、OpenCode 的做法后，提炼出三个独立改进。

## Goals and Non-Goals

### Goals

- 解耦 LLM context 路径和 UI display 路径，各自独立控制
- 超大工具结果持久化到磁盘，LLM 收到 preview + 路径，可按需读取
- 引入 ToolDisplayPolicy，支持 show / summary / hidden 三级显示控制
- 每个工具可自定义结果摘要文案

### Non-Goals

- 不改变工具执行的权限模型
- 不改变 compaction 的现有逻辑（Layer 2+）
- 不引入 LLM 生成摘要（成本太高，用规则摘要即可）
- 不改变 TranscriptNodeRow 的持久化结构

## 核心原则：LLM Context 和 UI Display 完全解耦

**Display policy 只控制用户在终端看到什么，不影响 LLM 收到的内容。**

LLM 调用 `read`，LLM 就收到完整文件内容——无论 display policy 是 show、summary 还是 hidden。summary 模式只是让终端里少显示几行，hidden 模式只是让终端里什么都不显示，LLM 那边该看什么看什么。

这个原则适用于**有语义结果需要交还给 LLM 的工具**。但有一类 runtime-only / barrier 工具本来就不应该把结果放进对话上下文：它们只负责更新运行时状态、完成审批/同步屏障、采集用户选择或推进控制流。对这类工具，hidden 不只是 UI display policy，也可能配合 ToolMessage suppression：

- **UI hidden**：不创建 tool call 节点，不发射 `ToolStarted` / `ToolFinished` / `ToolResultAppended` 给普通 UI。
- **ToolMessage suppressed**：不把工具结果追加到 LLM messages；LLM 通过 runtime state、后续用户消息或控制流状态获知结果。
- **Failure visible**：如果 hidden/barrier 工具失败且需要用户知道，应通过 warning/error/status 事件显示失败，而不是把正常工具节点重新露出来。

| 场景 | LLM 收到 | 用户在终端看到 |
|------|---------|--------------|
| `read` + display=show | 完整文件内容 | 完整文件内容 |
| `read` + display=summary | 完整文件内容 | 前 3 行 + "… +153 more lines" |
| `read` + display=hidden | 完整文件内容 | 什么都不显示 |
| `grep` + display=summary | 完整匹配结果 | 前 5 行 + "… +195 more lines" |
| `todo` | 不生成 ToolMessage（runtime state 已保存） | 不显示重复工具节点 |
| `plan_checkpoint` / barrier tool | 通常不生成 ToolMessage，除非该 barrier 的结果是后续推理必须读取的语义数据 | 不显示工具节点；必要时显示独立 warning/status |

同理，Large Result Persistence 也只影响 LLM context 路径——超大结果写磁盘后 LLM 收到 preview + 路径，但 UI 可以选择显示 preview 或从磁盘读取完整内容。

## Architecture

### 总体数据流

```
Tool Execution
     │
     ▼
ToolResult (raw output + diff + metadata + summary)
     │
     ├──► LLM Context Path（不受 display policy 影响）
     │    maybe_persist_large_result()  ──► ToolMessage (preview + path) or ToolMessage (full)
     │    sanitize_tool_message_content()
     │
     └──► UI Display Path（受 display policy 控制）
     │    ToolDisplayPolicy.resolve()  ──► show / summary / hidden
     │    │
     │    ├── show    → 完整渲染 tool_call + tool_result
     │    ├── summary → 渲染 tool_call header + 摘要行
     │    └── hidden  → 不创建节点，不发射事件
     │
     └──► 注意：UI display 的 summary/hidden 默认不改变 LLM 收到的 ToolMessage 内容；
          runtime-only/barrier 工具可显式声明 suppress_tool_message=True
```

### 改进 A：Tool Display Policy

#### 数据模型

```python
# src/voidx/ui/output/display_policy.py

class ToolDisplayMode(str, Enum):
    SHOW = "show"        # 完整显示工具调用 + 结果
    SUMMARY = "summary"  # 显示工具调用 header + 结果摘要
    HIDDEN = "hidden"    # 不显示（不创建 OutputNode，不发射 UI 事件）

class ToolDisplayRule(BaseModel):
    tool_name: str
    mode: ToolDisplayMode = ToolDisplayMode.SHOW
    suppress_tool_message: bool = False  # True 时不把工具结果追加到 LLM messages
    summary_max_lines: int = 3       # summary 模式显示的最大行数
    auto_summary_lines: int = 50     # 结果超过此行数自动降级为 summary
    auto_summary_chars: int = 5000   # 结果超过此字符数自动降级为 summary

class ToolDisplayPolicy(BaseModel):
    rules: dict[str, ToolDisplayRule] = {}
    default_mode: ToolDisplayMode = ToolDisplayMode.SHOW
    default_summary_max_lines: int = 3
    default_auto_summary_lines: int = 50
    default_auto_summary_chars: int = 5000
```

#### Per-Tool 语义分析

不同工具的"结果"语义完全不同，不能一刀切。以下是每个工具在 LLM context 和 UI display 两条路径上的需求：

| 工具 | LLM context 需要什么 | UI display 默认策略 | 理由 |
|------|---------------------|--------------------|------|
| **read** | 完整文件内容 | SHOW | LLM 需要完整内容做判断，用户也想看完整内容 |
| **write** | 确认写入成功 + 文件路径 | SHOW（有 diff 时显示 diff） | LLM 不需要看到写入的完整内容（它刚生成的），用户想确认写了什么 |
| **edit** | 确认修改成功 + diff 统计 | SHOW（显示 diff） | LLM 不需要完整 diff（它刚生成的），用户想确认改了什么 |
| **bash** | exit code + output | SHOW + 自适应 summary | 语义取决于命令类型（见下文），小结果完整显示，大结果 preview |
| **grep** | 完整匹配结果 | SUMMARY | LLM 需要完整匹配做判断，用户只需摘要（匹配数 + 前几行） |
| **glob** | 完整文件列表 | SUMMARY | LLM 需要完整列表，用户只需摘要（文件数 + 前几个） |
| **repo_map** | 完整结构化 map | SUMMARY | LLM 需要完整 map，用户只需摘要 |
| **agent** | 子 agent 最终结果 | SHOW（结果预览） | 已有预览机制，保持现状 |
| **webfetch** | 抓取内容 | SHOW + 自适应 summary | LLM 需要完整内容，用户看 preview 即可 |
| **websearch** | 搜索结果 | SUMMARY | LLM 需要完整结果，用户只需摘要 |
| **todo** | 不生成 ToolMessage（已有 runtime state） | HIDDEN | 已通过 runtime state 管理，避免 UI 和 LLM context 都重复 |
| **task_status** | 子 agent / worker task 当前状态 | HIDDEN | 仍在工具注册和子 agent tracker 中使用；UI 不显示工具节点，但 LLM 需要收到状态结果 |
| **load_doc_template** | 文档模板内容 | HIDDEN | 模板内容只给 LLM 写文档用，用户不需要看到工具节点或 `Load_doc_templateing` 过程 |
| **lsp_*** | LSP 结果 | SUMMARY | LLM 需要完整结果，用户只需摘要 |
| **plan_checkpoint** | 通常不生成 ToolMessage | HIDDEN | barrier/审批控制工具，不应污染 UI 和普通对话上下文 |
| **clarify** | 通常不生成 ToolMessage；用户回答进入后续用户消息或 runtime state | HIDDEN | barrier/交互控制工具，不显示工具节点 |

#### Runtime-only / Barrier 工具

Barrier 工具用于改变执行状态，而不是向 LLM 提供一段新的语义材料。状态查询工具可以同样 UI hidden，但不一定 suppress ToolMessage：

- `todo`：更新任务状态；现有行为是既不在 UI 重复显示，也不创建 ToolMessage。
- `task_status` 不属于 suppress ToolMessage 的 barrier：它仍由 `TaskStatusTool` 注册，并读取子 agent 运行期间写入的 `TaskTracker` 状态。默认 UI hidden，但 ToolMessage 必须保留给 LLM。
- `load_doc_template` 同样不 suppress ToolMessage：它是 LLM 的模板加载工具，默认 UI hidden，但模板内容必须进入 ToolMessage。
- `plan_checkpoint`：审批或 checkpoint barrier；正常路径不显示工具节点，结果由控制流消费。
- `clarify`：用户交互 barrier；用户回答应作为用户输入或 runtime state 进入上下文，而不是作为工具结果回灌。

这类工具需要显式建模为：

```python
class ToolDisplayRule(BaseModel):
    tool_name: str
    mode: ToolDisplayMode = ToolDisplayMode.SHOW
    suppress_tool_message: bool = False  # runtime-only/barrier 工具设为 True
    summary_max_lines: int = 3
    auto_summary_lines: int = 50
    auto_summary_chars: int = 5000
```

`mode=HIDDEN` 只表示 UI 不显示；`suppress_tool_message=True` 才表示不把结果追加到 LLM messages。大多数普通工具即使 hidden，也仍然返回 ToolMessage；runtime-only/barrier 工具两者都关闭。

#### bash 的特殊性

bash 是最复杂的工具，因为它的 output 语义取决于命令类型，但执行前无法判断：

| bash 场景 | LLM 需要什么 | 用户想看什么 |
|-----------|-------------|-------------|
| `git status` | 完整输出 | 完整输出 |
| `npm test` | exit code + 失败测试输出 | 完整输出 |
| `ls -la` | 完整输出 | 完整输出 |
| `python train.py`（1000行日志） | 末尾几行 + exit code | 末尾几行 + 可展开 |
| `curl api/endpoint`（50K JSON） | JSON 内容 | 摘要 + 可展开 |

bash 无法在执行前判断 output 语义，所以只能**按大小自适应**：
- 小结果（≤50 行 / ≤10K 字符）：完整显示
- 大结果：UI 显示 summary（头尾几行 + exit code），LLM 收到 preview + 持久化路径
- 失败（exit code ≠ 0）：无论大小都完整显示，因为错误信息通常关键

#### 默认策略

```python
DEFAULT_DISPLAY_RULES: dict[str, ToolDisplayRule] = {
    # ── Hidden：runtime-only / barrier / 状态工具 ──
    "todo": ToolDisplayRule(tool_name="todo", mode=ToolDisplayMode.HIDDEN, suppress_tool_message=True),
    "task_status": ToolDisplayRule(tool_name="task_status", mode=ToolDisplayMode.HIDDEN),
    "load_doc_template": ToolDisplayRule(tool_name="load_doc_template", mode=ToolDisplayMode.HIDDEN),
    "plan_checkpoint": ToolDisplayRule(tool_name="plan_checkpoint", mode=ToolDisplayMode.HIDDEN, suppress_tool_message=True),
    "clarify": ToolDisplayRule(tool_name="clarify", mode=ToolDisplayMode.HIDDEN, suppress_tool_message=True),

    # ── Summary：搜索/查询类，用户只需摘要 ──
    "grep": ToolDisplayRule(
        tool_name="grep",
        mode=ToolDisplayMode.SUMMARY,
        summary_max_lines=5,
    ),
    "glob": ToolDisplayRule(
        tool_name="glob",
        mode=ToolDisplayMode.SUMMARY,
        summary_max_lines=5,
    ),
    "repo_map": ToolDisplayRule(
        tool_name="repo_map",
        mode=ToolDisplayMode.SUMMARY,
        summary_max_lines=5,
    ),
    "websearch": ToolDisplayRule(
        tool_name="websearch",
        mode=ToolDisplayMode.SUMMARY,
        summary_max_lines=5,
    ),
    "lsp_diagnostics": ToolDisplayRule(
        tool_name="lsp_diagnostics",
        mode=ToolDisplayMode.SUMMARY,
        summary_max_lines=5,
    ),
    "lsp_symbols": ToolDisplayRule(
        tool_name="lsp_symbols",
        mode=ToolDisplayMode.SUMMARY,
        summary_max_lines=5,
    ),
    "lsp_references": ToolDisplayRule(
        tool_name="lsp_references",
        mode=ToolDisplayMode.SUMMARY,
        summary_max_lines=5,
    ),
    "lsp_definition": ToolDisplayRule(
        tool_name="lsp_definition",
        mode=ToolDisplayMode.SUMMARY,
        summary_max_lines=5,
    ),

    # ── Show + 自适应：内容类，小结果完整显示，大结果自动 summary ──
    "bash": ToolDisplayRule(
        tool_name="bash",
        mode=ToolDisplayMode.SHOW,
        auto_summary_lines=50,
        auto_summary_chars=10000,
    ),
    "read": ToolDisplayRule(
        tool_name="read",
        mode=ToolDisplayMode.SHOW,
        auto_summary_lines=100,
    ),
    "webfetch": ToolDisplayRule(
        tool_name="webfetch",
        mode=ToolDisplayMode.SHOW,
        auto_summary_lines=50,
        auto_summary_chars=10000,
    ),

    # ── Show：文件操作类，用户想确认变更 ──
    "write": ToolDisplayRule(tool_name="write", mode=ToolDisplayMode.SHOW),
    "edit": ToolDisplayRule(tool_name="edit", mode=ToolDisplayMode.SHOW),
    "apply_patch": ToolDisplayRule(tool_name="apply_patch", mode=ToolDisplayMode.SHOW),
    "agent": ToolDisplayRule(tool_name="agent", mode=ToolDisplayMode.SHOW),
}
```

#### 解析逻辑

```python
def resolve_display_mode(
    self,
    tool_name: str,
    result_output: str,
    result_ok: bool = True,       # 工具是否执行成功
) -> tuple[ToolDisplayMode, int]:
    """返回 (effective_mode, summary_max_lines)"""
    rule = self.rules.get(tool_name)

    if rule is None:
        mode = self.default_mode
        summary_lines = self.default_summary_max_lines
        auto_lines = self.default_auto_summary_lines
        auto_chars = self.default_auto_summary_chars
    else:
        mode = rule.mode
        summary_lines = rule.summary_max_lines
        auto_lines = rule.auto_summary_lines
        auto_chars = rule.auto_summary_chars

    # 失败的工具调用：无论策略如何，都完整显示
    # 因为错误信息通常关键，用户和 LLM 都需要看到
    if not result_ok:
        return ToolDisplayMode.SHOW, summary_lines

    # SHOW 模式下，结果过大时自动降级为 SUMMARY
    if mode == ToolDisplayMode.SHOW:
        output_lines = result_output.count("\n") + 1
        if output_lines > auto_lines or len(result_output) > auto_chars:
            mode = ToolDisplayMode.SUMMARY

    return mode, summary_lines
```

#### 影响点

**1. `tool_executor.py` — 事件发射前查询策略**

```python
# execute_one() 中，发射 ToolStarted 之前
policy = host._display_policy  # 从 host 获取
mode, summary_lines = policy.resolve_display_mode(tid, result.output)

if mode == ToolDisplayMode.HIDDEN:
    # 不发射 ToolStarted / ToolFinished / ToolResultAppended
    # 但仍然执行工具；是否返回 ToolMessage 由 suppress_tool_message 单独决定
    pass
else:
    # 发射事件，携带 display_mode
    await host._ui.events.request(ToolStarted(
        tool_call_id=tool_event_id,
        tool_name=tid,
        label=gerund,
        args=_fmt_args(targs),
        raw_args=targs,
        display_mode=mode,           # 新增字段
        summary_max_lines=summary_lines,  # 新增字段
    ))
```

ToolMessage 构造也必须查询同一条 rule，但不能只看 display mode：

```python
rule = policy.rule_for(tid)
message = None if rule.suppress_tool_message else ToolMessage(
    content=sanitize_tool_message_content(result.output, workspace=ctx.workspace),
    tool_call_id=cid,
)
```

这保留现有 `todo` 语义：`todo` 不只是 UI 上不重复显示，也不会再进入 tool call 上下文。后续新增 barrier 工具必须显式选择 `suppress_tool_message=True`，避免误把普通 hidden 工具的语义结果从 LLM context 中删掉。

**2. `events/schema.py` — 事件增加显示模式字段**

```python
class ToolStarted(UiEventBase):
    kind: Literal["tool.started"] = "tool.started"
    tool_call_id: str
    label: str
    args: str = ""
    tool_name: str = ""
    raw_args: dict[str, Any] = Field(default_factory=dict)
    display_mode: ToolDisplayMode = ToolDisplayMode.SHOW      # 新增
    summary_max_lines: int = 3                                 # 新增

class ToolResultAppended(UiEventBase):
    kind: Literal["tool_result.appended"] = "tool_result.appended"
    tool_call_id: str = ""
    text: str
    collapsed: bool = False
    display_mode: ToolDisplayMode = ToolDisplayMode.SHOW      # 新增
    summary_max_lines: int = 3                                 # 新增
```

**3. `events/consumers.py` — 用策略替代 `_suppressed_tool_ids`**

```python
class DockEventConsumer:
    def __init__(self, target: BottomInputDock):
        self._dock = target
        self._tool_nodes: dict[str, OutputNode] = {}
        self._hidden_tool_ids: set[str] = set()   # 替代 _suppressed_tool_ids
        self._agent_nodes: dict[int, OutputNode] = {}

    def handle(self, event: UiEvent) -> Any:
        match event:
            case ToolStarted() as e:
                if e.display_mode == ToolDisplayMode.HIDDEN:
                    self._hidden_tool_ids.add(e.tool_call_id)
                    return None
                # ... 正常创建节点

            case ToolFinished() as e:
                if e.tool_call_id in self._hidden_tool_ids:
                    return None
                # ... 正常完成节点

            case ToolResultAppended() as e:
                if e.tool_call_id in self._hidden_tool_ids:
                    return None
                if e.display_mode == ToolDisplayMode.SUMMARY:
                    # 截断结果到 summary_max_lines
                    lines = e.text.splitlines()
                    if len(lines) > e.summary_max_lines:
                        truncated = "\n".join(lines[:e.summary_max_lines])
                        omitted = len(lines) - e.summary_max_lines
                        text = f"{truncated}\n[dim]… +{omitted} more lines[/dim]"
                    else:
                        text = e.text
                    return self._dock.append_tool_result(text, ...)
                # ... 正常显示
```

**4. `dock/nodes.py` — summary 模式截断结果**

`append_tool_result()` 已有 `collapsed` 参数，summary 模式在传入前截断文本即可，不需要修改 `append_tool_result()` 本身。

**5. `console/app.py` — Console 路径也应用策略**

VoidConsole 的 `tool_call()` / `tool_done()` / `tool_result()` 也需要查询 display policy，hidden 模式下跳过打印。

### 改进 B：Large Result Persistence

#### 数据模型

```python
# src/voidx/agent/tool_result_storage.py

TOOL_RESULT_PERSIST_THRESHOLD = 50_000   # 50K 字符触发持久化
TOOL_RESULT_PREVIEW_CHARS = 2_000        # preview 2KB
PREVIEW_HEAD_FRACTION = 0.7              # 70% head, 30% tail

class PersistedResult(BaseModel):
    """持久化结果的信息，用于构建 LLM context 和 UI preview。"""
    original_size: int
    file_path: str
    preview: str
```

#### 核心逻辑

```python
def maybe_persist_tool_result(
    content: str,
    tool_use_id: str,
    tool_name: str,
    *,
    threshold: int = TOOL_RESULT_PERSIST_THRESHOLD,
    preview_chars: int = TOOL_RESULT_PREVIEW_CHARS,
) -> str:
    """如果内容超过阈值，持久化到磁盘并返回 preview + 路径。

    返回值：
    - 未超阈值：原样返回 content
    - 超过阈值：返回 <persisted-output> 包裹的 preview + 路径
    """
    if len(content) <= threshold:
        return content

    # Read 工具豁免：避免循环依赖（Read 自己控制输出大小）
    if tool_name == "read":
        return content

    file_path = _persist_to_disk(content, tool_use_id)
    preview = _make_preview(content, preview_chars)

    return (
        f"<persisted-output>\n"
        f"Output too large ({len(content)} chars). Saved to: {file_path}\n"
        f"Preview:\n{preview}\n"
        f"</persisted-output>"
    )


def _persist_to_disk(content: str, tool_use_id: str) -> str:
    """写入 ~/.voidx/tool-results/{session_id}/{tool_use_id}.txt"""
    safe_id = "".join(c for c in tool_use_id if c.isalnum() or c in "-_")
    dir_path = Path.home() / ".voidx" / "tool-results" / _current_session_id()
    dir_path.mkdir(parents=True, exist_ok=True)
    file_path = dir_path / f"{safe_id}.txt"
    file_path.write_text(content, encoding="utf-8", errors="replace")
    return str(file_path)


def _make_preview(content: str, limit: int) -> str:
    """生成 head + tail preview。"""
    if len(content) <= limit:
        return content
    head_n = int(limit * PREVIEW_HEAD_FRACTION)
    tail_n = limit - head_n
    return content[:head_n] + "\n…\n" + content[-tail_n:]
```

#### 集成位置

在 `tool_executor.py` 的 `execute_one()` 中，`sanitize_tool_message_content()` 之前调用：

```python
# 当前：
return _ExecutedTool(
    message=ToolMessage(
        content=sanitize_tool_message_content(result.output, workspace=ctx.workspace),
        tool_call_id=cid,
    ),
    ...
)

# 改为：
rule = policy.rule_for(tid)
if rule.suppress_tool_message:
    message = None
else:
    sanitized = maybe_persist_tool_result(
        result.output,
        tool_use_id=cid,
        tool_name=tid,
    )
    sanitized = sanitize_tool_message_content(sanitized, workspace=ctx.workspace)

    message = ToolMessage(content=sanitized, tool_call_id=cid)

return _ExecutedTool(message=message, ...)
```

注意：`maybe_persist_tool_result()` 在 `sanitize_tool_message_content()` **之前**运行，因为：
1. 持久化需要原始完整内容
2. sanitize 处理路径替换和脱敏，应在 preview 生成后执行

#### 清理策略

| 时机 | 行为 |
|------|------|
| Session 结束时 | 删除 `~/.voidx/tool-results/{session_id}/` 下所有文件 |
| Session resume 时 | 保留，LLM 可能引用旧路径 |
| 用户 `/clear` 时 | 删除当前 session 的持久化文件 |
| 配置项 `tool_result_persist_ttl` | 默认 `session`（随 session 清理），设为 `0` 则永不删除 |

```python
# src/voidx/agent/tool_result_storage.py

def cleanup_session_results(session_id: str) -> None:
    """删除指定 session 的所有持久化工具结果。"""
    dir_path = Path.home() / ".voidx" / "tool-results" / session_id
    if dir_path.exists():
        shutil.rmtree(dir_path, ignore_errors=True)
```

在 `session_runtime.py` 的 session 关闭流程中调用 `cleanup_session_results()`。

### 改进 C：Per-Tool Summary

#### 数据模型

在 `ToolResult` 中增加 `summary` 字段：

```python
# src/voidx/tools/base.py

class ToolResult(BaseModel):
    title: str = ""
    output: str
    summary: str = ""       # 新增：工具自定义的摘要文案
    metadata: dict = {}
    diff: str | None = None
```

#### 各工具的摘要生成

| 工具 | 摘要示例 | 生成逻辑 |
|------|---------|---------|
| `grep` | `"Found 15 matches in 8 files"` | 从 output 统计匹配行数和文件数 |
| `glob` | `"Found 23 files matching *.py"` | 从 output 统计文件数 |
| `bash` | `"Exit 0, 12 lines output"` | 从 metadata 取 exit_code + 统计行数 |
| `read` | `"Read 156 lines from src/main.py"` | 从 args 取 file_path + 统计行数 |
| `write` | `"Wrote 42 lines to src/main.py"` | 从 args 取 file_path + 统计行数 |
| `edit` | `"Updated src/main.py (+5/-3 lines)"` | 从 diff 统计 |
| `agent` | `"Implementer completed (3 steps, 12.5s)"` | 从 metadata 取 |
| `webfetch` | `"Fetched 2400 chars from https://..."` | 从 output 统计 |
| `websearch` | `"Found 8 results for 'query'"` | 从 output 统计 |
| `todo` | `"Updated: 2/5 done"` | 从 metadata 取 |
| `repo_map` | `"Mapped 45 symbols in 12 files"` | 从 output 统计 |

#### 摘要生成位置

每个工具的 `execute()` 方法在返回 `ToolResult` 时填充 `summary` 字段。这是最自然的位置——工具最了解自己的输出语义。

```python
# 示例：grep 工具
class GrepTool(BaseTool):
    async def execute(self, args, ctx):
        # ... 执行搜索
        matches = output.count("\n") + 1 if output else 0
        files = len(set(line.split(":")[0] for line in output.splitlines() if ":" in line))
        return ToolResult(
            output=output,
            summary=f"Found {matches} matches in {files} files",
        )
```

#### UI 使用 summary

summary 在两个地方使用：

1. **`ToolFinished` 事件** — `detail` 字段使用 summary，替代当前的空字符串
2. **`OutputNode.collapse_summary`** — 折叠时显示 summary 而非截断 header

```python
# tool_executor.py
await host._ui.events.emit(ToolFinished(
    tool_call_id=tool_event_id,
    label=_title(tid),
    elapsed=elapsed,
    ok=ok,
    detail=result.summary,   # 使用工具摘要
))
```

```python
# tree.py — OutputNode.collapse_summary
@property
def collapse_summary(self) -> str:
    if self.node_type == "tool_call":
        # 优先使用 payload 中的 summary
        summary = self.payload.get("summary")
        if summary:
            return summary
        return self.header
```

## Data Model

### 新增文件

```
src/voidx/ui/output/display_policy.py    — ToolDisplayPolicy + ToolDisplayRule + 默认策略
src/voidx/agent/tool_result_storage.py   — maybe_persist_tool_result + 清理逻辑
```

### 修改文件

```
src/voidx/tools/base.py                  — ToolResult 增加 summary 字段
src/voidx/ui/output/events/schema.py     — ToolStarted/ToolResultAppended 增加 display_mode 字段
src/voidx/ui/output/events/consumers.py  — 用 display_mode 替代 _suppressed_tool_ids
src/voidx/agent/graph/tool_executor.py   — 查询 display policy + 调用 maybe_persist
src/voidx/ui/output/dock/nodes.py        — summary 模式截断 + collapse_summary 使用 summary
src/voidx/ui/output/console/app.py       — Console 路径应用 display policy
src/voidx/ui/output/tree.py              — collapse_summary 优先使用 summary
src/voidx/agent/graph/session_runtime.py — session 结束时清理持久化文件
```

### 新增配置

```yaml
# voidx.yaml 或 profile 中
tool_display:
  default_mode: show
  rules:
    grep:
      mode: summary
      summary_max_lines: 10
    todo:
      mode: hidden
      suppress_tool_message: true
    plan_checkpoint:
      mode: hidden
      suppress_tool_message: true

tool_result:
  persist_threshold: 50000    # 字符数，0 = 禁用持久化
  preview_chars: 2000
  persist_ttl: session        # session | forever
```

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| 磁盘写入失败（权限/空间） | 降级为直接截断，不阻塞工具执行 |
| 持久化文件被外部删除 | LLM read 时返回文件不存在，不影响其他功能 |
| Display policy 配置错误 | 忽略错误规则，回退到默认策略 |
| summary 为空 | 回退到现有行为（截断 header） |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| LLM context 和 UI display 解耦 | 统一处理 | 两者需求不同：LLM 需要精简但完整的信息，UI 需要美观但可交互的展示 |
| 超大结果持久化到磁盘 | 直接截断 / LLM 生成摘要 | 持久化零 API 成本，preview 提供足够上下文，LLM 可按需读取 |
| Session 结束时清理持久化文件 | 永不删除 / TTL 过期 | 最自然——session 结束后 LLM 不可能再引用，实现简单 |
| 规则摘要而非 LLM 生成摘要 | LLM 生成摘要 | 零成本、零延迟、确定性，对大多数工具够用 |
| Per-tool summary 在工具内部生成 | 在 executor 层统一生成 | 工具最了解自己的输出语义，生成最准确的摘要 |
| Display policy 作为 Pydantic 模型 | 纯 dict 配置 | 类型安全、可验证、可序列化 |

## Open Questions

- [ ] 持久化文件路径是否需要脱敏（workspace 路径替换）？目前 sanitize 在 persist 之后运行，preview 中可能包含真实路径
- [ ] summary 模式下，用户能否通过 browse 模式展开查看完整结果？还是需要单独的"展开"交互？
- [ ] 子 agent 的工具调用是否继承父 agent 的 display policy？还是子 agent 有自己的策略？
- [ ] MCP 工具的 display mode 默认值应该是什么？SHOW 还是 SUMMARY？
