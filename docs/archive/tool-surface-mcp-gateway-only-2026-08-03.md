# 工具面分层与 MCP Gateway-only

> **Status: Done** — Archived on 2026-08-04.

## 来源

2026-08-03 工具注册/暴露设计讨论结论。目标是把“可执行工具目录”和“当前 LLM 可见工具面”拆清楚，并彻底移除 legacy 直连式 MCP 工具（`mcp__server__tool_hash`）。本期 MCP 变更是有意的破坏性变更：不提供 legacy 权限键读取、迁移或兼容。

## 背景与问题

当前工具系统把多个概念混在同一层：

- `ToolRegistry` 既是可执行工具目录，又常被调用方当成当前可见工具集。
- `GoalTool` / `LoopTool` 在 `ToolRegistry._register_builtins()` 中与读写、搜索、shell 等普通工具一起注册，但它们实际是 lifecycle control tools，只应在特定 runtime profile / phase 可见。
- 主 runtime 在 `llm_turn.py` 内做 profile 过滤、tool policy、control protocol 注入和去重；subagent 之前走独立的 registry copy + blocklist 路径，容易漏掉同一套规则。
- MCP 已有稳定 `mcp` gateway，但代码里仍保留 legacy direct wrapper、`mcp_tool_id()`、`mcp__*` 清理和 UI 展示兼容，导致权限、展示和注册语义不统一。

这类混合设计的直接风险是：任何入口只要绕过某个局部过滤器，就可能把 `goal` / `loop` / legacy MCP 工具暴露给不该看到它们的 LLM turn；protocol injection 也可能绕过普通工具过滤。

## 目标

1. 明确分层：`ToolRegistry` 只表示 executable catalog；LLM 绑定工具必须通过统一 `ToolSurface` resolver 计算。
2. 统一 main runtime 与 subagent 的 visible tool resolution，避免局部 blocklist 分叉。
3. 将 `turn` / `goal` / `loop` 归类为 lifecycle tools，而不是普通基础工具。
4. MCP 只保留稳定 `mcp` gateway；彻底移除 `mcp__server__tool_hash` 直连式工具路径及其旧权限键。
5. 用测试矩阵固定各 runtime/profile/phase/child constraint 的最终可见工具面。

## 非目标

- 不重写工具执行模型：`ToolRegistry.execute_tool()` 仍负责按 tool id 执行实现。
- 不改变 MCP gateway 的用户接口：继续使用 `mcp(op="list" | "load" | "call", ...)`。
- 不改变 MCP client/server 连接、catalog、description generation 的行为。
- 不在本期重构 goal/loop runner 业务语义；只清理工具可见性边界。
- 不保留 legacy MCP wrapper、旧 tool id 或旧 permission key 的读取兼容。

## 术语

| 概念 | 含义 |
|---|---|
| ToolCatalog | 可执行工具实现目录，即当前 `ToolRegistry` 的核心职责。包含 builtin、runtime-injected、gateway 工具。 |
| ToolSurface | 某一次 LLM 调用实际绑定的 tool definitions。它是 catalog + profile + phase + policy + protocol + child constraints 的计算结果。 |
| Lifecycle tools | 控制 runtime 生命周期/协议的工具：`turn`、`goal`、`loop`。它们不是普通基础工具。 |
| Gateway tool | 单个稳定入口代表一组外部能力。MCP 的唯一 gateway tool 是 `mcp`。 |
| Legacy direct MCP | 旧的每个 MCP server tool 映射成独立 LLM tool 的路径：`mcp__server__tool_hash`。本设计直接移除。 |
| Tool-surface visibility | 工具是否出现在当前 LLM request 的 definitions 中；不等同于执行授权。 |
| Tool-call authorization | LLM 已产生 tool call 后，对当前 profile、policy、permission 和 runtime 状态进行的执行前校验。 |

## 当前入口

| 文件 | 当前职责 | 问题 |
|---|---|---|
| `src/voidx/tools/registry.py` | 注册 builtins、动态工具、生成 `tools_for_llm()` | registry 同时承担 catalog 与 visible surface；`goal` / `loop` 作为 builtins 易误解；`mcp__*` 仍有非 strict 特判。 |
| `src/voidx/agent/infrastructure/langgraph/runtime/llm_turn.py` | main LLM turn 装配工具：profile 过滤、tool policy、control protocol、去重 | visible tool 逻辑内联，subagent 不能自然复用。 |
| `src/voidx/agent/infrastructure/langgraph/runtime/subagent.py` | child agent copy parent registry、注入 `message`、按 blocklist 过滤 | 子 agent 自有过滤路径，且 `can_delegate` 会改变 blocklist 结果。 |
| `src/voidx/agent/infrastructure/langgraph/runtime/control_protocol.py` | 注入 `turn` / `goal` / `loop` control tool definitions | 语义正确，但需要由统一 surface resolver 集中装配，并携带 phase 语义。 |
| `src/voidx/mcp/manager.py` | MCP server lifecycle、catalog、deny filter | 仍导入 `mcp_tool_id()` 并写入 legacy permission key。 |
| `src/voidx/mcp/gateway.py` | 稳定 MCP gateway tool，id=`mcp` | 应成为唯一 LLM-visible MCP path。 |
| `src/voidx/mcp/tool.py` | legacy direct MCP wrapper | 本期删除，不保留兼容模块。 |
| `src/voidx/ui/output/tool_display.py` | 工具调用展示 | 仍有 `mcp__...` display compatibility，需删除。 |

## 目标设计

### 1. ToolRegistry 降级为 catalog

`ToolRegistry` 保留：

- `register()` / `get()` / `get_def()` / `ids()` / `execute_tool()`；
- `filtered_copy()` / `filter_tools()`，用于执行权限闭集或 child registry 实例裁剪；
- `tools_for_llm()` 本期改名为 `serialize_definitions()`，它只是 catalog serialization helper：序列化所有已注册 tool definitions，不做 LLM visibility 过滤；runtime 不得把它的结果直接当作最终 visible surface。

`ToolRegistry` 不再表达：

- 当前 profile 或 phase 哪些工具可见；
- lifecycle tool 是否可见；
- child agent 是否允许 delegate；
- MCP legacy direct exposure。

`ToolRegistry.execute_tool()` 不是授权边界。任何来自 LLM 的 tool call 仍必须先经过当前 tool policy / permission flow；不可见工具不能因为仍存在于 catalog 或历史消息中而获得执行权限。

### 2. 新增 ToolSurface resolver

新增文件：`src/voidx/agent/infrastructure/langgraph/runtime/tool_surface.py`。

建议接口：

```python
class ToolNamePolicy(Protocol):
    """Resolver 依赖的最小 policy 接口；不绑定任何具体 ToolView 实现。"""

    def allows(self, tool_name: str) -> bool: ...


@dataclass(frozen=True)
class ToolSurfaceContext:
    runtime_profile: RuntimeProfile | None
    goal_phase: str | None = None
    loop_phase: str | None = None
    tool_policy: ToolNamePolicy | None = None
    turn_context: TurnExecutionContext | None = None
    child_agent: bool = False
    lsp_manager: object | None = None
    model_protocol: str | None = None


@dataclass(frozen=True)
class ToolSurface:
    definitions: list[dict[str, Any]]
    dropped: dict[str, str]  # tool_id -> 被哪条规则过滤（用于 debug log）


def resolve_tool_surface(
    registry: ToolRegistry,
    context: ToolSurfaceContext,
) -> ToolSurface:
    ...
```

`ToolSurface.dropped` 是可选诊断：调用方可以把过滤理由写入 debug log 以便排查“模型为什么没看到某工具”，不要求每个调用点消费；不影响热路径行为。

约束：

- `goal_phase` / `loop_phase` 必须分别来自当前 runtime turn context 的 `goal_phase` / `loop_phase` 字段；不得把两者折叠成单一 `phase`，也不得仅凭 profile protocol 推断完整可见性。resolver 只读取与当前 protocol 对应的 phase 字段，另一字段视为无关输入。
- child constraints 是 resolver 的固定规则，不引入配置开关：child surface 永不暴露 `agent`、`clarify`、`checkpoint`。若未来开放 child delegation，必须单独设计并更新矩阵，不得由现有 `AgentDef.can_delegate` 隐式放开。
- resolver 不接收独立的 `control_protocol` 或 `turn_control_enabled` 输入；它内部通过 `resolve_control_protocol(runtime_profile)` 派生 protocol，并以 `protocol.protocol_id == "turn"`、当前非 child/goal/loop phase 推导是否注入 `turn`。这避免 profile、protocol 与布尔多个输入表达同一件事而产生不一致组合。未来若重新引入 turn-control 开关，也必须仍以 profile-derived protocol 为唯一事实来源。
- resolver 输出的是绑定 definitions，不改变 registry 的 executable catalog。

Resolver 负责按固定顺序处理：

1. 从 catalog 读取 `serialize_definitions()` 的 base tool definitions。
2. 应用固定 execution-only 过滤：`git`、`lsp_format` 是 catalog 中可执行但永不进入 LLM surface 的内部工具；即使 policy 允许，resolver 也必须丢弃它们。未来若某 phase 需要暴露其中任一工具，必须显式移除该 execution-only 标记并更新矩阵。
3. 应用 profile + phase lifecycle visibility，映射关系固定为：
   - coding/普通 profile：不允许 `goal` / `loop`；`turn` 仅在 resolver 从 profile 派生的 `protocol.protocol_id == "turn"` 时注入。
   - loop profile + `loop_phase="idle"`：暴露 `loop`（intake 语义），不允许 `goal` / `turn`。
   - loop profile + `loop_phase="work"`：暴露 `loop`（decision 语义），不允许 `goal` / `turn`。
   - goal profile + `goal_phase="idle"`/`"intake"`：暴露 `goal`，不允许 `loop` / `turn`。
   - goal profile + `goal_phase="evaluator"`：暴露 `goal` 及 goal phase policy 提供的 verification tools，不允许 `loop` / `turn`。
   - goal profile + `goal_phase="work"`：暴露 phase policy 允许且非 execution-only 的执行工具，不暴露 `goal` / `loop` / `turn`。
   - phase 字段缺失或取值未知：采用最小可见集合（不暴露任何 lifecycle tool），并由测试显式覆盖该行为。
4. 应用 child constraints：
   - child 永不暴露 `agent` / `clarify` / `checkpoint`；
   - child registry 可保留 `message`，并由 resolver 暴露；
   - child 使用 coding profile visibility，不暴露 `goal` / `loop` / `turn`；
   - `message` 是否存在由 child injection 决定，但 child surface 的其余规则不依赖 parent 的最终 definitions。
5. 应用 runtime `tool_policy.allows(tool_name)`。
6. 若 profile-derived `protocol.protocol_id == "turn"` 且当前 context 允许（非 child、非 goal/loop phase），追加 `protocol.tool_definitions()`。
7. 再次应用 `tool_policy`，确保 protocol-injected tools 也受闭集约束。
8. 按 tool id 去重：protocol-injected definition 覆盖同名 catalog definition（lifecycle tool 的 schema/description 以 control protocol 为唯一权威来源），其余同名冲突按首次出现顺序保留。
9. 应用 provider-specific cleanup：`filter_unavailable_lsp_tools()`、`strip_gemini_unsupported_schema_keys()` 可在 resolver 内或 resolver 后集中调用，但 main/subagent 必须走同一 helper。

可见性和执行授权必须保持以下不变量：

- surface 未暴露的 tool call 不能绕过当前 policy/permission flow 执行；
- protocol-injected tool 与 catalog tool 使用同一套最终 policy 检查；
- `ToolRegistry.execute_tool()` 只负责 dispatch，不负责判断当前 LLM 是否有权调用；
- 历史消息中的 `goal` / `loop` / `turn` / legacy MCP tool call 不能恢复其可见性或授权；
- provider cleanup 的结果必须同时适用于 main runtime 和 subagent。

### 3. Lifecycle tools 语义

`goal` / `loop` 继续作为 executable tools 注册在 `ToolRegistry`，因为 runner/phase 需要执行实现；但 registry 中的它们只承担执行实例职责，不再作为 LLM definition 的来源。

`turn` / `goal` / `loop` 的最终 LLM definition（schema、description）统一由 control protocol 提供，三者统一称为 lifecycle tools：

| Tool | 执行实例来源 | LLM definition 来源 | 可见条件 |
|---|---|---|---|
| `turn` | control protocol | control protocol | resolver 从 profile 派生的 `protocol.protocol_id == "turn"`、当前不是 child/loop/goal lifecycle phase 且 policy 允许。 |
| `goal` | registry builtin | goal protocol | `goal_phase` 为 idle/intake/evaluator 且 policy 允许；work phase 不暴露。 |
| `loop` | registry builtin | loop protocol | loop profile 下 `loop_phase` 为 idle/work 且 policy 允许；goal/coding 不暴露。 |

权威来源不变量：

- lifecycle tool 的 definition 只有一个权威来源：control protocol；resolver 在注入 protocol definition 时按 tool id 覆盖 catalog 中的同名 definition，而不是依赖首次出现顺序。
- `GoalProtocol.tool_definitions()` 已能构造 `goal` definition；`LoopProtocol.tool_definitions()` 同理提供 `loop` definition；resolver 不得再从 catalog 读取 `goal` / `loop` 的 schema/description。
- 若未来把 `GoalTool` / `LoopTool` 从 registry builtins 中移出，resolver 与 surface 测试不应受任何影响。

### 4. MCP Gateway-only

MCP 目标状态：

- LLM-visible MCP 工具永远只有 `mcp`。
- `McpManager` 只维护 server clients、catalog、statuses、generated descriptions。
- MCP deny filter 只写 gateway-style permission key：
  - `mcp@pattern:mcp:{server}:{tool}`
- `mcp_tool_id()`、`McpToolWrapper`、`mcp__*` 清理、`mcp__*` display parsing 全部删除。
- `ToolRegistry.serialize_definitions()`（原 `tools_for_llm()`）只对 `id == "mcp"` 保持 non-strict schema；不再识别 `startswith("mcp__")`。
- 不读取、不迁移、不清理旧的 `mcp__*` grants/denies；旧 key 在本期视为废弃数据，新的 permission decision 不再生成或匹配它们。
- `mcp(op="call")` 的 server/tool 资源授权继续由 permission classifier 解析为 `mcp:{server}:{tool}`，并使用 gateway-style permission key。

## 实施计划

### 阶段 1：引入 ToolSurface resolver

修改：

- 新增 `src/voidx/agent/infrastructure/langgraph/runtime/tool_surface.py`
- 修改 `src/voidx/agent/infrastructure/langgraph/runtime/llm_turn.py`
- 修改 `src/voidx/agent/infrastructure/langgraph/runtime/subagent.py`
- 修改 `src/voidx/tools/registry.py`：`tools_for_llm()` 改名为 `serialize_definitions()`，并更新全部调用方与测试 fake
- 必要时修改 `src/voidx/agent/infrastructure/langgraph/runtime/control_protocol.py`，提供 phase-aware protocol definitions
- 更新 `src/tests/test_domain/test_tool_visibility.py`
- 更新 `src/tests/test_infrastructure/runtime/test_prepare_workflow.py`
- 新增或更新 resolver 专项测试，覆盖 policy 二次过滤和 provider cleanup 一致性

实施步骤：

1. 定义 `ToolSurfaceContext`，从现有 `current_thread_execution_state()` / `TurnExecutionContext` 传入 profile、`goal_phase`、`loop_phase`、policy 和 child 标记；resolver 内部通过 `resolve_control_protocol(runtime_profile)` 派生 protocol。
2. 将 `llm_turn.py` 的 profile filtering、tool name extraction、control injection、dedupe 和 cleanup 迁移到 resolver。
3. 将 `subagent.py` 的 registry copy/blocklist/tool serialization 改为调用同一个 resolver；保留 `message` injection，但不再由 `AgentDef.can_delegate` 隐式开放 `agent`。
4. 将 `ToolRegistry.tools_for_llm()` 改名为 `serialize_definitions()` 并更新全部调用方与测试 fake；同时把现有 `_HIDDEN_FROM_LLM` 过滤从 registry 移到 resolver 的固定 execution-only 规则，`serialize_definitions()` 不再承担 visibility 判断。
5. 将 phase 解析收敛为单个 helper（如 `lifecycle_phase(ctx) -> tuple[protocol, phase] | None`），resolver 主体与测试共用，不重复映射逻辑。
6. 在 tool call authorization 入口确认不可见工具仍被 policy/permission 拒绝，不改变 `execute_tool()` 的 dispatch 责任。

验收：

- `llm_turn.py` 不再定义 profile filtering / tool name extraction / dedupe helpers。
- main runtime 和 subagent 都调用同一个 surface resolver。
- 代码库不再存在 `tools_for_llm` 命名；`serialize_definitions()` 仅被 resolver 或非 runtime 调用方使用。
- child 最终工具面包含 `message`（启用 child message 时），不包含 `agent` / `clarify` / `checkpoint` / `goal` / `loop` / `turn`。
- protocol injection 后仍受 policy 闭集约束，且 lifecycle definition 以 protocol 为准覆盖 catalog 同名 definition。
- main/subagent 的 LSP 与 Gemini cleanup 结果一致。
- 不可见的历史 tool call 不会绕过授权流执行。

### 阶段 2：移除 legacy direct MCP

修改：

- 删除 `src/voidx/mcp/tool.py`，不保留空兼容模块。
- 修改 `src/voidx/mcp/manager.py`：移除 `mcp_tool_id` import、`unregister_prefix("mcp__")`、legacy deny 写入；只保留 gateway-style deny。
- 修改 `src/voidx/mcp/__init__.py`：移除 `McpToolWrapper` export 和说明。
- 修改 `src/voidx/tools/registry.py`：移除 `t.id.startswith("mcp__")` 非 strict 分支；如无其他使用，删除 `unregister_prefix()`。
- 修改 `src/voidx/ui/output/tool_display.py`：删除 `mcp__...` 正则和展示兼容。
- 更新 `src/tests/test_mcp/test_mcp.py`、`src/tests/test_mcp/test_exposure.py`、`src/tests/test_mcp/test_gateway_permissions.py`、`src/tests/test_mcp/test_manager_catalog.py`、`src/tests/test_application/test_tool_filters_gemini.py`、`src/tests/test_infrastructure/runtime/test_graph_authorization.py` 中 legacy `mcp__*` 断言或 fixtures。

明确的破坏性行为：

- 删除 `mcp__*` wrapper、tool id、display parser 和 manager 清理逻辑。
- 新代码不读取、不迁移、不清理旧 `mcp__*` grants/denies；它们不再参与授权匹配。
- 新权限写入只允许 `mcp@pattern:mcp:{server}:{tool}`。
- 外部 MCP 调用必须通过 `mcp` gateway；旧的独立 tool call 不再受支持。

验收：

- 代码库 runtime 路径不再引用 `mcp_tool_id` / `McpToolWrapper`。
- LLM tool definitions 不会出现任何 `mcp__*`，且 MCP visible path 只有 `mcp`。
- MCP deny filter 只产生 `mcp@pattern:mcp:{server}:{tool}`。
- gateway permission tests 覆盖 allow、specific deny、wildcard deny 和 manager catalog deny。
- 旧 permission key 不会被新代码读取或生成。

### 阶段 3：测试矩阵固化

新增/更新断言：

| 场景 | 应出现 | 不应出现 |
|---|---|---|
| 普通 main profile（protocol 非 turn） | `agent` | `goal`、`loop`、`turn` |
| coding main（turn protocol） | `agent`、`turn`（policy 允许时） | `goal`、`loop` |
| loop idle（`loop_phase="idle"`） | `loop` | `goal`、`turn`、`agent` |
| loop work（`loop_phase="work"`） | `loop` | `goal`、`turn`、`agent` |
| goal idle/intake（`goal_phase="idle"`/`"intake"`） | `goal` | `loop`、`turn`、`agent` |
| goal evaluator（`goal_phase="evaluator"`） | `goal`、phase-approved verification tools | `loop`、`turn`、`agent` |
| goal work（`goal_phase="work"`） | phase policy 允许且非 execution-only 的执行工具 | `goal`、`loop`、`turn`、`agent`、`git`、`lsp_format` |
| phase 缺失或未知取值 | 非 lifecycle 工具（最小可见集合） | `goal`、`loop`、`turn` |
| subagent | `message`（启用时） | `agent`、`clarify`、`checkpoint`、`goal`、`loop`、`turn` |
| execution-only tools | 无 | `git`、`lsp_format`（即使 policy 允许） |
| protocol injection + closed policy | policy 允许的 injected tools | policy 拒绝的 injected tools |
| MCP gateway | `mcp` | `mcp__*`、任何独立 MCP server tool id |
| MCP permission | gateway-style resource key | `mcp__*` permission key |

## 验证命令

聚焦验证：

```bash
./test.py --backend -- \
  src/tests/test_domain/test_tool_visibility.py \
  src/tests/test_infrastructure/runtime/test_prepare_workflow.py \
  src/tests/test_mcp/test_mcp.py \
  src/tests/test_mcp/test_exposure.py \
  src/tests/test_mcp/test_gateway_permissions.py \
  src/tests/test_mcp/test_manager_catalog.py \
  src/tests/test_tools/test_tool_registry.py \
  src/tests/test_tools/bash/test_auto_route_git.py \
  src/tests/test_mcp/test_gateway_registration.py
```

补充验证：

```bash
./test.py --backend -- \
  src/tests/test_tools/test_loop_registry.py \
  src/tests/test_application/test_tool_filters_gemini.py \
  src/tests/test_infrastructure/runtime/test_graph_authorization.py
```

若改动触及 runtime wiring、tool authorization 或 subagent wiring，再跑：

```bash
./test.py --backend -- src/tests/test_infrastructure/runtime
```

代码库静态检查：

```bash
./python.py -m compileall -q \
  src/voidx/agent/infrastructure/langgraph/runtime \
  src/voidx/mcp \
  src/voidx/tools \
  src/voidx/ui/output
```

并确认以下搜索无命中：

```bash
grep -R "mcp_tool_id\|McpToolWrapper" src/voidx
grep -R "tools_for_llm" src/voidx src/tests
```

## 风险与权衡

- **短期 registry 仍包含 lifecycle implementations**：为了避免扩大执行层改动，`GoalTool` / `LoopTool` 可暂时保留在 builtins；清晰性由 `ToolSurface` 命名和测试矩阵保证。
- **`serialize_definitions()` 改名与 execution-only 过滤迁移触及全部调用方与测试 fake**：机械重命名之外还包含 visibility 职责从 registry 移到 resolver；review 时需区分纯改名 diff 与行为 diff，并用矩阵确认 `git` / `lsp_format` 不会因 policy 允许而进入 LLM surface。
- **MCP legacy path 直接删除**：这是有意破坏兼容。所有外部 MCP 调用必须走 `mcp` gateway；旧 tool id 和旧 display 解析不再工作。
- **MCP 历史 permission key 直接废弃**：本期不读取、不迁移、不清理 `mcp__*` grants/denies。用户需要重新产生 gateway-style permission decision。
- **phase-aware visibility 增加 context 依赖**：resolver 必须分别接收真实的 `goal_phase` / `loop_phase`，而不能通过 profile protocol 猜测或折叠成单一 `phase`；缺失或未知取值时采用最小可见集合，并由测试显式覆盖。
- **lifecycle definition 双来源风险**：`GoalTool` / `LoopTool` 仍在 registry，同时 protocol 也生成同名 definition；resolver 必须以 protocol definition 覆盖 catalog 同名 definition，禁止依赖首次出现顺序，否则 schema/description 来源不确定。
- **surface 与 execution authorization 分层**：新增 resolver 不应被误用为执行授权；tool call 必须继续走现有 policy/permission flow。

## 定义完成

- main runtime 和 subagent visible tools 均由 `tool_surface.py` 统一计算。
- resolver 使用 profile、`goal_phase`、`loop_phase`、policy、profile-derived protocol 和 child constraints，且 protocol-injected tools 也经过 policy 过滤。
- `serialize_definitions()` 只序列化 catalog；`git` / `lsp_format` 等 execution-only 工具由 resolver 固定过滤，任何 profile/phase/policy 组合都不会暴露它们。
- lifecycle tool 的 LLM definition 唯一权威来源为 control protocol，resolver 按 tool id 覆盖 catalog 同名 definition。
- child 不会因为 registry copy 或 `AgentDef.can_delegate` 隐式暴露 `agent`、`clarify`、`checkpoint`；child 不暴露 lifecycle tools。
- `goal` / `loop` 不再因为 registry copy 出现在 coding/subagent 工具面。
- 代码库无 runtime 使用 `McpToolWrapper` / `mcp_tool_id()`，且不保留 `src/voidx/mcp/tool.py`。
- `ToolRegistry.serialize_definitions()` 不再包含 `mcp__*` 特判，且代码库无 `tools_for_llm` 残留命名。
- MCP LLM-visible path 只有 `mcp` gateway。
- 新 MCP 权限只使用 gateway-style key；旧 `mcp__*` key 不读取、不迁移、不清理。
- 不可见 tool call 不会绕过当前授权流执行。
- 聚焦测试、补充测试和必要的 runtime 测试通过。
