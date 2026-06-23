# 代码重复与冗余扫描报告

> **Status: Done** — 仅第 11 组（`_DSML_MARKER_RE` 常量）已合并，其余标记为无需改动。
>
> 扫描日期：2026-06-11
> 扫描范围：`src/voidx/` 全量 Python 源码
> 重点关注：重复代码、冗余代码

---

## 🔴 高优先级 — 实质性重复（建议合并）

### 1. `_message_text` — 3 处几乎相同的实现

| 文件 | 行号 | 函数签名 |
|---|---|---|
| `llm/compaction.py` | 430 | `_message_text(msg: object) -> str` |
| `agent/graph/convergence.py` | 156 | `_message_text(message: BaseMessage) -> str` |
| `agent/graph/streaming.py` | 399 | `extract_text(msg) -> str` |

**逻辑**：从消息对象提取文本内容，处理 `str` / `list[dict]` 两种格式。

**差异**：
- `extract_text` 用 `"".join`，另外两个用 `"\n".join` + 过滤空 part
- `llm/compaction.py` 版本额外处理 `isinstance(item, str)` 的情况
- `convergence.py` 版本对 `AIMessage | ToolMessage` 有特殊分支（实际与 fallback 相同）

**建议**：提取到 `llm/message_markers.py` 或新建 `agent/message_utils.py`，统一调用。

> **决定**：无需改动。语义差异（join 策略、类型处理）是有意的，合并风险大于收益。

---

### 2. `latest_user_text` — 2 处几乎相同的实现

| 文件 | 行号 | 函数签名 |
|---|---|---|
| `agent/graph/topology.py` | 52 | `latest_user_text(messages: list[BaseMessage]) -> str` |
| `agent/graph/convergence.py` | 105 | `_latest_user_text(messages: list[BaseMessage]) -> str` |

**逻辑**：反向遍历消息，找第一个非 step-hint、非 guidance 的 HumanMessage，提取文本。

**差异**：
- `topology.py` 版本内联了文本提取逻辑
- `convergence.py` 版本调用了 `_message_text(message).strip()`

**建议**：统一到 `topology.py` 的公开函数，`convergence.py` 直接调用。

> **决定**：无需改动。内联 vs 调用差异不影响维护，提取收益低。

---

### 3. `_dedupe` — 2 处完全相同的实现

| 文件 | 行号 | 函数签名 |
|---|---|---|
| `llm/compaction.py` | 452 | `_dedupe(items: list[str]) -> list[str]` |
| `agent/graph/convergence.py` | 145 | `_dedupe(items: list[str]) -> list[str]` |

**逻辑**：去重 + 过滤空值，完全一致。

**建议**：提取到公共 utils（如 `voidx/utils.py`）。

> **决定**：无需改动。两处语义不同（strip+过滤空值 vs 不 strip 不过滤），统一需改行为。

---

### 4. `_is_empty_content` — 2 处几乎相同的实现

| 文件 | 行号 | 函数签名 |
|---|---|---|
| `agent/graph/todo_state.py` | 164 | `_is_empty_content(content: object) -> bool` |
| `agent/graph/streaming.py` | 282 | `_is_empty_content(content: object) -> bool` |

**差异**：
- `todo_state.py`：`content.strip() == ""`（对空白字符串也视为空）
- `streaming.py`：`content == ""`（仅空字符串）

**建议**：提取到 `agent/graph/` 下的公共模块，统一为 `strip()` 版本。

> **决定**：无需改动。`strip()` 差异影响 streaming 行为，合并可能引入回归。

---

### 5. `_dump_pending_approval` — 3 处实现

| 文件 | 行号 | 返回类型 | 用途 |
|---|---|---|---|
| `memory/runtime_state.py` | 219 | `str` (JSON) | 持久化序列化 |
| `agent/graph/tool_executor.py` | 636 | `dict \| None` | 运行时状态传递 |
| `agent/graph/turn_runner.py` | 432 | `dict \| None` | 运行时状态传递 |

**差异**：
- `runtime_state.py` 版本序列化为 JSON 字符串，用于 SQLite 存储
- `tool_executor.py` 和 `turn_runner.py` 版本几乎完全相同（都返回 `dict | None`），仅类型注解略有不同

**建议**：`tool_executor.py` 和 `turn_runner.py` 的版本合并为一个，放到 `agent/task_state.py` 或 `memory/runtime_state.py`。

> **决定**：无需改动。跨模块、类型差异大（JSON str vs dict），强行合并易引入序列化 bug。

---

### 6. `_active_workflow_names` — 3 处实现

| 文件 | 行号 | 参数类型 |
|---|---|---|
| `agent/graph/core.py` | 154 | `group: list[WorkflowRunState \| dict]` |
| `agent/graph/permissions.py` | 146 | `value: object` |
| `agent/graph/tool_executor.py` | 658 | `value: object` |

**逻辑**：从 workflow runs 中提取活跃的 workflow 名称。

**差异**：
- `core.py` 版本接受强类型 list
- 另外两个接受 `object` 并自行解析 dict/values

**建议**：提取到 `workflow/runtime.py`，统一为接受 `object` 的版本。

> **决定**：无需改动。参数类型差异大，调用方多，投入产出比低。

---

### 7. `_shell_words` — 2 处几乎相同的实现

| 文件 | 行号 | 返回类型 |
|---|---|---|
| `permission/rules.py` | 148 | `list[str] \| None` |
| `permission/sandbox.py` | 158 | `list[str]` |

**逻辑**：使用 `shlex.shlex` 分词，完全一致。

**差异**：
- `rules.py`：解析失败返回 `None`
- `sandbox.py`：解析失败 fallback 到 `command.split()`

**建议**：提取到 `permission/` 下的公共模块（如 `permission/shell.py`），统一 fallback 策略。

> **决定**：无需改动。安全相关代码，错误处理差异是有意的，合并可能引入权限绕过。

---

### 8. `_git_subcommand` + `_GIT_GLOBAL_OPTIONS_WITH_VALUE` — 2 处几乎相同的实现

| 文件 | 行号 | 函数签名 |
|---|---|---|
| `permission/rules.py` | 293-314 | `_git_subcommand(args) -> tuple[str, list[str]]` |
| `permission/sandbox.py` | 277-298 | `_git_subcommand(args) -> str` |

**常量** `_GIT_GLOBAL_OPTIONS_WITH_VALUE` 完全相同。

**差异**：
- `rules.py` 返回 `(subcommand, remaining_args)` 元组
- `sandbox.py` 仅返回 subcommand 字符串

**建议**：合并到 `permission/shell.py`，统一返回 tuple，sandbox 版本取 `[0]` 即可。

> **决定**：无需改动。安全相关代码，返回类型差异服务于不同调用方需求。

---

### 9. `_program_and_args` vs `_program` + `_program_args`

| 文件 | 行号 | 函数 |
|---|---|---|
| `permission/rules.py` | 262 | `_program_and_args(words) -> tuple[str, list[str]]` |
| `permission/sandbox.py` | 232-249 | `_program(words) -> str` + `_program_args(words) -> list[str]` |

**逻辑**：跳过环境变量赋值，找到程序名和参数。功能等价。

**建议**：统一为一套实现（tuple 版本），sandbox 版本解构即可。

> **决定**：无需改动。同上，安全代码不宜轻易合并。

---

## 🟡 中优先级 — 结构性冗余（Mixin 代理层）

### 10. Mixin 代理模式 — 6 对文件

| Mixin 文件（代理） | 实际实现文件 |
|---|---|
| `agent/graph/compaction.py` | `agent/graph/compaction_coordinator.py` |
| `agent/graph/tool_execution.py` | `agent/graph/tool_executor.py` |
| `agent/graph/session_mixin.py` | `agent/graph/session_runtime.py` |
| `agent/graph/turn_mixin.py` | `agent/graph/turn_runner.py` |
| `agent/graph/title_mixin.py` | `agent/graph/session_runtime.py`（间接） |
| `agent/graph/transcript_mixin.py` | `agent/graph/session_runtime.py`（间接） |

**模式**：每个 mixin 都有相同的 `_xxx_for(host)` 懒初始化模式，方法全部是单行代理调用。

**评估**：这是有意为之的架构模式（mixin 做接口适配 + 懒初始化，coordinator/runner 做实际逻辑），**不算 bug**。但增加了文件数量和维护成本。

**建议**：暂不处理。如果未来迁移到组合而非继承，可考虑简化。

---

## 🟢 低优先级 — 小规模重复

### 11. `_DSML_MARKER_RE` 常量 — 2 处定义

| 文件 | 行号 |
|---|---|
| `agent/graph/todo_state.py` | 14 |
| `agent/graph/streaming.py` | 22 |

**建议**：提取到 `agent/graph/` 公共常量模块。

> **决定**：✅ 已完成。常量统一到 `agent/todo_state.py`，`streaming.py` 改为 import。

---

### 12. `VoidConsole` vs `TreeAwareConsole` — 方法签名重复

| 文件 | 类 |
|---|---|
| `ui/output/console/app.py` | `VoidConsole` |
| `ui/output/console/app.py` | `TreeAwareConsole` |

重复方法：`tool_call`, `tool_done`, `tool_result`, `diff`, `print`, `warn`, `error`, `sep`。

**建议**：用基类或 Protocol 统一接口。

> **决定**：无需改动。低优先级，接口重复不影响运行。

---

## 汇总

| 严重程度 | 数量 | 主要类型 | 建议动作 |
|---|---|---|---|
| 🔴 高 | 9 组 | 跨文件函数重复 | 合并到公共模块 |
| 🟡 中 | 1 组 | 架构模式冗余 | 暂不处理 |
| 🟢 低 | 2 组 | 常量/接口重复 | 低优先级统一 |

### 推荐处理顺序

1. **permission 模块**（第 7-9 组）：3 组重复在同一子模块内，合并风险最低
2. **agent/graph 消息工具函数**（第 1-4 组）：4 组重复，提取到公共模块
3. **_dump_pending_approval / _active_workflow_names**（第 5-6 组）：跨模块重复，需注意调用方
