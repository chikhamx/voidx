# MCP Gateway Tool — 技术设计文档

> **Status: Done** — Archived on 2026-07-20.
> Date: 2026-07-14

## Context

voidx 当前 MCP 集成会在 MCP server 启动后发现真实工具，并把每个工具封装成 `mcp__{server}__{tool}_{hash}` 注册到 `ToolRegistry`。随后主循环从 registry 生成 `tool_defs`，再传给模型的 `bind_tools()`。

这个模式直接、类型约束强，但有三个代价：

1. **工具列表膨胀**：多个 MCP server 会把几十到上百个工具直接暴露给模型。
2. **模型侧 prefix cache 不稳定**：MCP catalog 一旦新增、删除、重连或过滤，`tool_defs` 变化会让 provider 侧工具 schema 前缀缓存失效。
3. **TUI 可读性差**：原始 MCP tool id 长且带 hash，需要额外压缩渲染。

Skill 工具提供了另一种模式：`skill list/load` 只把按需说明注入当前上下文，不动态改变 bound tools。本设计借鉴 skill 的 `list/load` 体验，但新增一个稳定的 MCP 执行入口。

## Goals

- 保持 bound tool 列表稳定，避免 MCP catalog 变化破坏 prefix cache。
- 用一个固定 `mcp` gateway tool 覆盖 MCP discovery、说明加载和实际执行。
- 支持按需加载 MCP server/tool 的描述、参数摘要和调用示例。
- 保留 MCP manager/client 的连接、调用、状态管理能力。
- 在执行前用真实 MCP input schema 校验参数，降低 gateway 模式下的参数漂移。
- 让 TUI/GUI 渲染真实 MCP 动作，而不是显示笼统的 `mcp(...)`。
- 保留现有 MCP 权限粒度，审批对象必须是具体 `server/tool`，不是笼统的 gateway。
- 让 MCP catalog 有明确的 source of truth，避免 direct wrapper、gateway load/call 和 UI 状态各自重复发现。

## Non-Goals

- 不在第一版继续把每个 MCP tool 暴露成 native function tool。
- 不要求完整替换 `websearch/webfetch`；高频内置路线可继续保留或逐步迁移。
- 不在 `load` 输出里塞完整原始 JSON Schema；优先输出压缩后的可读参数说明。
- 不实现跨进程 MCP catalog 持久缓存；第一版以内存 catalog 为准。
- 不在第一版实现完整语义搜索；`query` 只做轻量字符串匹配和排序。
- 不把 MCP tool 的 schema 校验结果当成安全边界。权限仍以 `server/tool` 和用户策略为准。

## Implemented Model

新增一个固定内置工具 `mcp`，始终注册在 `ToolRegistry` 中。它的 schema 稳定，不随 MCP server 或 tool 数量变化。

```json
{
  "op": "list | load | call",
  "server": "tavily",
  "tool": "tavily_search",
  "arguments": {"query": "..."},
  "query": "optional discovery query"
}
```

`arguments` 是内联 JSON object，不接受序列化后的 JSON 字符串：

- 对模型只提供一种调用形式，避免双重编码、转义错误和类型丢失。
- gateway 的 schema 固定为开放 object，不随 MCP catalog 变化，因此仍可保持 prefix cache 稳定。
- `mcp` gateway 不启用 provider strict mode；内部在调用前使用目标工具的真实 `inputSchema` 校验参数。
- 参数不是 object 时直接返回可修复错误；不提供字符串兼容路径。

模型使用流程：

1. 模型先从稳定的 `## Available MCP Servers` capability hints、用户显式 `#server` 引用，或 `mcp(op="list")` 的 server 摘要中判断相关能力。
2. 摘要不是工具文档；相关时必须调用 `mcp(op="load", server="tavily")`，也可用 `tool` 参数只加载一个已知工具。
3. `load` 返回类似 skill load 的当前 turn 上下文，包含真实工具名称、用途、参数摘要和调用示例。
4. 执行时调用 `mcp(op="call", server="tavily", tool="tavily_search", arguments={"query": "..."})`。
5. `mcp` 工具内部通过 `McpManager.call_tool(server, tool, arguments)` 调用真实 MCP server。

## Tool Description Contract

固定 gateway tool 的注册描述承担 `list/load/call` 工作流指引；稳定 system prefix 另有一个只含配置态 auto server 的 capability-hint 区间。两者都不包含运行时 tool catalog。

`McpGatewayTool.description` 必须保持稳定，并明确区分摘要发现、工具文档加载和真实调用：

```text
Discover and use Model Context Protocol (MCP) servers through a stable gateway.

- `mcp(op="list")` returns semantic server summaries; it does not load tool documentation.
- When a server is relevant, use `mcp(op="load", server="...")` before calling it.
- `mcp(op="load")` may target a whole server or one tool and returns current-turn context.
- `mcp(op="call", ...)` executes a real MCP tool; pass arguments as a JSON object.
- Never invent server names, tool names, or parameters; list or load when uncertain.
```

这层和 skill 机制的关系：

- `skill load` 注入某个能力的操作说明。
- `mcp load` 注入某个 MCP server/tool 的操作说明。
- `mcp` tool description 只告诉模型 gateway 工作流，不列出真实 server/tool。

因为描述属于固定 bound tool schema，只要 `mcp` 工具 schema 不变，它不会因 MCP server 新增、删除或重连而抖动。相比 system prompt，这个位置更贴近工具调用决策，也避免在 runtime context 中重复维护一份 MCP 使用规则。

## Discovery Modes（auto / manual）

借鉴 skill 的选择机制（`SkillSelectionConfig.auto` → `available_skill_summaries()` → `## Available Skills` 段，见 `skills/service.py`、`llm/instruction.py`），MCP server 分两种发现模式：

- **auto**：`McpServerConfig.auto = true` 且未禁用的 server 出现在稳定提示词区间 `## Available MCP Servers`。auto server 会连接并进入 catalog，但不注册独立 `mcp__...` 模型工具。
- **manual**：不进入自动提示词区间；仍可通过 `mcp(op="list")` 或用户显式 `#server` 引用发现，并通过 gateway `load/call` 使用。现有 exposure 配置允许 manual server 保留 direct wrapper 行为。

**稳定性约束**：auto 段内容必须在 session 开始时可确定。MCP server 是后台异步连接、可断线重连（`McpManager.start_all()` 非阻塞），catalog 属于运行时易变数据；因此 auto 段只放配置态内容（server 名 + 配置描述/来源），**不放**连接状态、tool_count、运行时 instructions 或发现到的工具列表。该段按 `InstructionService` 会话冻结，避免 mid-session 改动 system 前缀。工具级细节统一走 `load`。

发现粒度为 per-server。auto/manual server 都支持用户显式 `#server` 引用；UI 实际插入 `$server` token，运行时将其替换为语义摘要。摘要作为该条用户消息的一部分保留在会话历史中。

实现上需要给 `McpServerConfig` 增加配置态字段：

| 字段 | 说明 |
| --- | --- |
| `auto` | 是否进入 `## Available MCP Servers` 固定提示词段 |
| `description` | 可选 server 描述，供 auto 段和 `mcp list` 使用 |
| `source` | 可选来源说明，例如 workspace/user/plugin/bundled |

这些字段只能来自配置或安装元数据，不能来自运行时 `tools/list` 结果。

## Tool Operations

### `list`

返回 MCP server 的语义摘要，控制输出长度。每个条目包含 server 名、配置态 description/source、状态、过滤后的工具数量，以及精确的 `mcp(op="load", server="...")` 指引；**不展示工具名或参数**。

摘要来源边界：

- 系统 Available 段、`mcp list`、`#server` 引用和 UI/TUI 候选只使用受控配置字段 `description/source`。
- MCP `initialize.instructions` 是不受控的运行时文本，可能包含工具名、参数或调用示例，不进入 discovery 摘要。
- 候选菜单缺少配置 description 时显示固定回退文本，不从 catalog 工具列表生成 `Tools: ...`。
- 完整工具说明只能由 `mcp load` 展开。

如果传入 `query`，`list` 可按 server 名、配置 description/source、工具名或工具描述做轻量匹配；匹配工具名只影响筛选，不改变输出边界，结果仍只显示 server 摘要。

`list` 读取 `McpCatalog` 内存快照，不临时请求所有 MCP server。连接状态和 `tool_count` 只出现在 tool result 中，不回写稳定 system prompt。

`tool_count` 统计配置过滤后的可用工具。对尚未连接或 catalog 尚未就绪的 server，数量可为 `0`，并返回 `connecting/error/disconnected/unknown` 状态。

### Explicit `#server` Reference

Web/TUI 引用菜单同时展示 auto/manual server，但候选描述只使用配置 `description`；缺失时显示固定回退文本。用户选择后发送 `$server` token，运行时注入包含以下字段的语义摘要：

- server 名和当前状态；
- 配置 description；
- 可选 `serverInfo` 实现名称/版本；
- `mcp(op="load", server="...")` 指引。

引用摘要不包含 runtime instructions 或工具列表。server 处于 connecting、disconnected、error 或 unknown 状态时仍保留摘要与状态，避免静默吞掉用户引用；实际 `load/call` 仍按连接状态返回可修复错误。

### `load`

返回当前 turn 可读上下文，不改变 bound tool 列表。

输出使用独立 marker，便于历史消息 stripping：

```text
VOIDX_MCP_TOOL_CONTEXT
Scope: current-turn

## MCP Server: tavily
Status: connected

Tools:
- tavily_search: Search the web.
  Required: query
  Optional: max_results, search_depth
  Example:
    mcp(op="call", server="tavily", tool="tavily_search", arguments={"query": "..."})
- tavily_extract: Extract page content from URLs.
  Required: urls
  Example:
    mcp(op="call", server="tavily", tool="tavily_extract", arguments={"urls": ["https://..."]})
```

`load` 输出应压缩 schema：

- 只展示 required fields、常用 optional fields、字段描述和 examples。
- 大 schema 超限时只展示前 N 个字段，并提示可加载具体 tool。
- 加载具体 `tool` 时可以展示更完整参数摘要。

第一版建议强制按 server 或具体 tool 加载，不提供“加载所有 connected MCP server”的默认路径。若后续允许全量加载，必须有硬上限和截断提示，避免把 tool catalog 膨胀问题从 `tool_defs` 转移到 tool result。

`load` 返回应同时包含结构化 metadata，至少包括 `server`、`tool_names`、`schema_hash`、`truncated`。文本输出给模型读，metadata 给 UI、日志和测试使用，避免未来从渲染文本反解析。

### `call`

执行真实 MCP tool。

执行前校验（按顺序）：

- server 存在且 connected。
- tool 存在且未被配置过滤。
- `arguments` 是 JSON object，不接受序列化后的 JSON 字符串。
- arguments 能通过 MCP tool input schema 校验。
- 权限服务允许 `mcp:{server}:{tool}` 或对应策略。

权限检查需要发生在真实 MCP `tools/call` 之前，并且审批展示应使用解析后的 arguments。即使 `McpGatewayTool.execute()` 内部也做防御性检查，graph 级 authorization 仍需要能把 gateway call 分类成具体 MCP capability，否则 on-failure approval、session allow/deny 和 UI pending request 都只能看到 `mcp`。

校验失败时返回结构化、可修复错误，并区分 object 类型错误和目标工具 schema 错误：

```text
MCP call failed: arguments must be a JSON object.
Run mcp(op="load", server="tavily", tool="tavily_search") for parameter details.
```

```text
MCP call failed: invalid arguments for tavily/tavily_search.
Missing required field: query.
Run mcp(op="load", server="tavily", tool="tavily_search") for parameter details.
```

## Architecture

### Components

| Component | Responsibility |
| --- | --- |
| `McpGatewayTool` | 固定 bound tool，处理 `list/load/call` |
| `McpManager` | 继续管理 MCP client 生命周期、状态、真实调用 |
| `McpCatalog` | 从 connected clients 读取、过滤并缓存 server/tool definitions，作为 gateway 的 source of truth |
| `McpContextRenderer` | 把 MCP definitions 渲染成 skill-like current-turn context |
| `McpArgumentValidator` | 校验 `call.arguments` 为 object，并用目标工具 input schema 校验 |
| `McpSchemaSummarizer` | 把 JSON Schema 压缩成 required/optional/type/enum/nested path 摘要 |
| `McpPermissionResolver` | 从 gateway args 生成 `mcp:{server}:{tool}` 权限资源和审批摘要 |
| `McpAutoRenderer` | 把 auto server 渲染成固定提示词区间（仅配置态内容，见 Discovery Modes） |
| `McpGatewayTool.description` | 提供固定工具描述，教模型使用 gateway 工作流 |
| UI display helpers | 把 gateway call 渲染成 `Tavily Search("query")` |

### Data Flow

```text
LLM
  -> mcp(op=list/load/call)
    -> McpGatewayTool
      -> McpCatalog / McpContextRenderer
      -> PermissionService
      -> McpManager.call_tool()
        -> McpClient
          -> real MCP server
```

`ToolRegistry.tools_for_llm()` 始终包含稳定的 `mcp` gateway。auto server 不注册独立 wrapper；manual server 是否额外直接暴露由现有 `mcp.exposure` 配置控制。真实 MCP catalog 不进入 gateway schema，而是作为 tool result 或内部 runtime state 使用。

### Catalog and Refresh Semantics

`McpCatalog` 应从 `McpManager` 接收 discovery 结果，而不是自己拥有 MCP client 生命周期。第一版可以在 server 连接成功后写入内存 catalog，并在 `mcp list/load/call` 时读取同一份快照。

需要避免三类状态分裂：

- direct MCP wrapper 使用一份 tool defs，gateway 使用另一份 tool defs。
- `mcp list` 显示某个 tool 可用，但 `mcp call` 因过滤或 stale catalog 找不到。
- UI integrations 状态显示的 `tool_count` 与 gateway 可调用工具数量不一致。

建议 `McpManager` 暴露 filtered tool definitions，例如：

```text
McpManager.catalog_snapshot() -> list[McpServerCatalogEntry]
McpManager.tool_def(server, tool) -> McpToolDef | None
```

现有 MCP `tools/list` 结果可能分页。当前第一版可先保持单页实现，但接口命名和测试应为 pagination 留出口；如果 client 后续支持 cursor，应由 `McpCatalog` 聚合完整工具列表后再过滤。

当 server reconnect 或收到 tools-list-changed 类通知时，应清除对应 server 的 catalog 并重新 discover。重新 discover 只能改变 `mcp list/load` 结果和 direct 模式下的 dynamic registration，不能改变 gateway tool schema。

### Argument Validation

`McpArgumentValidator` 区分两类失败：

1. `arguments` 不是 object，例如 array/string/null。
2. object 不满足 MCP tool `inputSchema`。

schema 校验第一版不要只检查 required 字段。至少要保留：

- required fields
- primitive type
- enum / const
- array item type
- nested object path
- additionalProperties

如果遇到暂不支持的 JSON Schema 关键字（例如复杂 `oneOf` / `anyOf` / `$ref` 图），validator 应返回“保守通过 + metadata warning”或“可修复错误”，但不能静默丢弃关键约束。建议优先使用成熟 JSON Schema validator；自写 validator 只适合第一版 smoke coverage，不适合长期维护。

## Prefix Cache Behavior

目标是让 provider 侧请求前缀尽量稳定：

- bound tools 固定，不随 MCP catalog 变化。
- system prompt 只包含按会话冻结的 auto server 配置摘要，不包含运行时 catalog、状态、instructions 或工具名。
- 固定 `mcp` tool description 承载 gateway 工作流，不随 MCP catalog 变化。
- `mcp load` 的大段说明只出现在当前 turn 的 tool result 中，历史中可被 marker stripping 压缩。
- `#server` 摘要作为用户显式引用的一部分保留在用户消息历史中，但不包含工具级文档。
- 新增/删除 MCP server 不改变 gateway schema；只改变后续会话的 auto 摘要以及当前运行时的 `mcp list/load` 结果。
- auto server 段只包含 session-start 可确定的配置态内容，不包含连接状态、tool_count、tool names、runtime instructions 或 schema hash。

这比动态注册 MCP native tools 更适合长会话和大量 MCP server。

## Permissions

权限模型建议引入 MCP 资源名：

```text
mcp:{server}:{tool}
```

示例：

- `mcp:tavily:tavily_search`
- `mcp:github:create_issue`
- `mcp:*:*`

权限 prompt 展示应使用真实动作：

```text
MCP: Tavily Search
Arguments: query="..."
```

第一版可沿用现有 MCP wildcard 策略，但执行层应统一从 gateway call 中提取 `server/tool/arguments`，不要只审批 `mcp` 这个固定工具名，否则权限粒度会过粗。

需要补齐的权限行为：

- `classify_tool_call()` 识别 `name="mcp"` 且 `op="call"`，capability 为 `MCP_TOOLS`。
- `build_pattern()` 对 gateway call 返回 `mcp:{server}:{tool}`，而不是 `*`。
- session allow/deny 同时支持 `mcp:{server}:{tool}`、`mcp:{server}:*`、`mcp:*:*`。
- 配置里的 denied tools 需要映射到 gateway 资源名，不能只 `deny_silent(mcp__...hash)`。
- `mcp op=list/load` 是 discovery/read-only，默认可 allow；`mcp op=call` 默认 ask，除非 permission mode 或 session rule 放行。

如果保留 direct/gateway hybrid 模式，direct tool id 和 gateway resource 必须能互相映射，避免用户允许 direct `mcp__github__create_issue_*` 后 gateway `mcp:github:create_issue` 仍反复询问。

## UI Rendering

TUI/GUI 不应显示 `Mcp("call")`。对 `mcp(op="call", server, tool, arguments)`：

- 标题显示为 `{Server} {Tool}("display value")`。
- display value 复用现有工具摘要逻辑：优先 `query/url/urls/path/pattern/name/text`；arguments 非 object 时降级为 `MCP Call("server/tool")`。
- `urls` 列表显示为 `first +N more`。
- `op=list/load` 可显示为 `MCP List()`、`MCP Load("tavily")`。
- UI 事件中的 `raw_args` 保留 gateway 原始参数；display helper 负责解析 `raw_args.arguments`，不要要求执行层改写 tool call args。
- `ToolResult.title` / `summary` 可以提供更准确的完成态标签，但开始态标题必须只依赖原始 tool call args。

示例：

```text
● Tavily Search("goal mode mechanism")
● Tavily Extract("https://example.com +2 more")
● MCP Load("github")
```

## Error Handling

| 场景 | 行为 |
| --- | --- |
| MCP manager unavailable | 返回可读错误，提示配置 MCP server |
| server connecting | 返回状态并提示稍后重试 |
| server disconnected | 尝试 reconnect；失败则返回连接错误 |
| unknown server/tool | 返回可用候选或提示 `mcp list` |
| arguments 非 object | 返回类型错误和 object 调用示例，提示 `load` 获取参数细节 |
| arguments schema mismatch | 返回 schema 校验错误和 `load` 提示 |
| permission denied | 返回具体 `mcp:{server}:{tool}` 被拒绝，不执行真实 MCP call |
| stale catalog | 返回可修复错误并提示重新 `mcp list/load`；自动 refresh 尚未实现 |
| unsupported schema feature | 返回 validator warning，提示加载具体 tool 查看参数细节 |
| MCP tool error | 保留 MCP error 内容，标记 metadata.error |
| large result | 走现有 display policy summary / truncation |

## Implementation Status

已实现：

1. 固定 `mcp` gateway 及 `list/load/call`。
2. 统一 `McpCatalog`，由 manager 连接发现后写入，gateway/UI 读取快照。
3. object-only arguments、真实 MCP input schema 校验和具体 `mcp:{server}:{tool}` 权限资源。
4. MCP gateway 的 TUI/GUI 动作渲染。
5. `auto/description/source` 配置、稳定 Available MCP Servers 段及 `/mcp auto|manual`。
6. Web/TUI `#server` 候选与显式语义摘要引用。
7. auto server gateway-only：继续连接并进入 catalog，但不注册独立 `mcp__...` 工具。
8. `mcp.exposure` 对 manual server 的 direct/gateway/hybrid 兼容行为。
9. `mcp load` current-turn marker 及历史 tool result 压缩。

尚未实现：过期 catalog 的自动 refresh。当前 stale catalog 返回可修复错误，refresh 作为独立增强处理。

## Testing

### Unit Tests

- `mcp list` 返回 server 语义摘要、状态、tool_count 和精确 load 指令，不展示工具名。
- `mcp list` 查询仍能按 server/config/tool 名称和描述匹配。
- `mcp load server` 返回 marker、工具描述、参数摘要和 examples。
- `mcp load server/tool` 返回单工具详细参数摘要。
- `mcp call` 对 valid arguments 调用 `McpManager.call_tool()`。
- `mcp call` 拒绝非 object arguments，并对 enum、array item、nested object、additionalProperties 做真实 schema 校验。
- `mcp op=list/load` 默认 read-only allow，`mcp op=call` 按具体 MCP 权限资源审批。
- auto server 段只含会话冻结的配置态内容，manual server 不进入该段。
- auto server 不注册独立模型工具，但 catalog 仍可供 gateway load/call。
- auto/manual `#server` 都只注入语义摘要；未连接状态仍保留摘要和状态。
- list、引用和候选菜单不泄露 runtime instructions 或 catalog 工具名。
- 历史 stripping 能把 `mcp load` 的 `VOIDX_MCP_TOOL_CONTEXT` tool result 压缩为摘要。
- `McpGatewayTool.description` 固定且不包含 server/tool catalog。
- `McpCatalog` 只返回配置过滤后的 tool defs。
- `load` metadata 包含 `server`、`tool_names`、`schema_hash`、`truncated`。

### Integration Tests

- 主 LLM call 的 `tool_defs` 在 MCP server 新增/删除后仍保持稳定。
- 主 LLM call 的 stable system prefix 不受 MCP catalog 变化影响。
- direct 模式与 gateway 模式都能调用同一个 fake MCP tool。
- 子代理继承固定 `mcp` tool，但不继承膨胀后的 direct MCP tools。
- auto server 出现在固定提示词区间，manual server 不出现。
- TUI 渲染 `mcp call` 为真实动作名称。
- UI pending permission prompt 展示真实 MCP 动作和解析后的 arguments。
- gateway 模式下配置 deny 的 MCP tool 无法通过 `mcp call` 绕过。

### Regression Metrics

- prefix cache read/write tokens。
- MCP tool call success rate。
- invalid argument retry rate（拆分为 object type error 和 schema validation error 两类计数）。
- tool_defs token count。
- `mcp list/load/call` latency，拆分 catalog cache hit 和 server refresh。
- permission prompt frequency，拆分 direct/gateway/hybrid。

## Follow-up Work

- 实现 stale catalog 自动 refresh，并覆盖 reconnect / tools-list-changed / pagination。
- 评估 per-tool auto、批量 load 和语义搜索是否有足够收益。
- 持续观测 prefix cache、gateway latency、权限 prompt 频率和参数校验失败率。
- 根据实际使用情况决定 manual server 的默认 exposure 策略，以及高频 native tools 是否迁移到 gateway。
