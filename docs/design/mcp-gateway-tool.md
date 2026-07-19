# MCP Gateway Tool — 技术设计文档

> **Status: Design**
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

## Proposed Model

新增一个固定内置工具 `mcp`，始终注册在 `ToolRegistry` 中。它的 schema 稳定，不随 MCP server 或 tool 数量变化。

```json
{
  "op": "list | load | call",
  "server": "tavily",
  "tool": "tavily_search",
  "arguments": "{\"query\": \"...\"}",
  "query": "optional discovery query"
}
```

`arguments` 是 JSON object **字符串**，不是内联对象：

- 内置工具会带 `strict: true` 且 schema 强制 `additionalProperties: false`（见 `tools/registry.py`、`tools/base.py`），开放对象会破坏 strict；字符串 schema 完全稳定，也避免在 strict 逻辑里给 `mcp` 开特例。
- 跨 provider 行为统一（Anthropic 无 strict 概念，字符串两边一致）。
- gateway 内部解析后再走 input schema 校验；解析失败和 schema 校验失败是两类不同的可修复错误。
- 防御性解析：调用方直接传 dict 时容错接受，不强制要求字符串。

模型使用流程：

1. 需要 MCP 能力时，调用 `mcp(op="list")` 查看可用 server/tool bundle。
2. 需要具体说明时，调用 `mcp(op="load", server="tavily")` 或 `mcp(op="load", server="tavily", tool="tavily_search")`。
3. `load` 返回类似 skill load 的当前 turn 上下文，包含工具列表、用途、参数摘要和示例。
4. 执行时调用 `mcp(op="call", server="tavily", tool="tavily_search", arguments="{\"query\": \"...\"}")`。
5. `mcp` 工具内部通过 `McpManager.call_tool(server, tool, arguments)` 调真实 MCP server。

## Tool Description Contract

固定 gateway tool 的注册描述需要承担 MCP 工作流指引，不再额外新增 system/runtime section。这样模型在查看 bound tool schema 时就能学到 `list/load/call` 用法，同时避免 prompt 里再重复一层 MCP 指引。

`McpGatewayTool.description` 应包含短规则，但不包含 MCP catalog。Catalog 只能通过 `mcp list/load` 进入当前 turn。

建议工具描述包含：

```text
Discover, load, and call Model Context Protocol (MCP) tools through a stable gateway.

- Use `mcp(op="list")` to discover available MCP servers and tool bundles.
- Use `mcp(op="load", server="...")` before calling an unfamiliar MCP server or tool.
- Use `mcp(op="call", server="...", tool="...", arguments="{...}")` to execute a real MCP tool.
- Do not invent MCP server or tool names. If uncertain, list or load first.
- Treat `mcp load` output as current-turn context and follow its parameter examples.
```

这层和 skill 机制的关系：

- `skill load` 注入某个能力的操作说明。
- `mcp load` 注入某个 MCP server/tool 的操作说明。
- `mcp` tool description 只告诉模型 gateway 工作流，不列出真实 server/tool。

因为描述属于固定 bound tool schema，只要 `mcp` 工具 schema 不变，它不会因 MCP server 新增、删除或重连而抖动。相比 system prompt，这个位置更贴近工具调用决策，也避免在 runtime context 中重复维护一份 MCP 使用规则。

## Discovery Modes（auto / manual）

借鉴 skill 的选择机制（`SkillSelectionConfig.auto` → `available_skill_summaries()` → `## Available Skills` 段，见 `skills/service.py`、`llm/instruction.py`），MCP server 分两种发现模式：

- **auto**：`McpServerConfig.auto = true` 的 server 出现在固定提示词区间（`## Available MCP Servers`，对齐 skills 段），模型无需 `list` 即可直接 `load`/`call`。
- **manual**：其余 server 只能通过 `mcp list` 发现，或由用户显式点名后 `load`。

**稳定性约束**：auto 段内容必须在 session 开始时可确定。MCP server 是后台异步连接、可断线重连（`McpManager.start_all()` 非阻塞），catalog 属于运行时易变数据；因此 auto 段只放配置态内容（server 名 + 配置描述/来源），**不放**连接状态、tool_count 或发现到的工具列表。否则连接完成后 mid-session 更新该段会改动 system 前缀，重新引入本设计要消除的 prefix cache 失效。工具级细节仍走 `load`。

第一版粒度为 per-server；per-tool auto 和 `@server` 显式引用（对齐 skill 的 `EXPLICIT_REF_RE`）留作后续。

实现上需要给 `McpServerConfig` 增加配置态字段：

| 字段 | 说明 |
| --- | --- |
| `auto` | 是否进入 `## Available MCP Servers` 固定提示词段 |
| `description` | 可选 server 描述，供 auto 段和 `mcp list` 使用 |
| `source` | 可选来源说明，例如 workspace/user/plugin/bundled |

这些字段只能来自配置或安装元数据，不能来自运行时 `tools/list` 结果。

## Tool Operations

### `list`

返回 MCP server 摘要，控制输出长度。

字段建议：

| 字段 | 说明 |
| --- | --- |
| `server` | MCP server name |
| `status` | connected / connecting / error / disabled |
| `tool_count` | 可用工具数量 |
| `summary` | server 描述或来源 |
| `examples` | 可选，1-2 个代表工具名 |

如果传入 `query`，`list` 可以做简单过滤，用于查找相关 MCP server 或 tool。

`list` 应优先读取 `McpCatalog` 的内存快照，不应每次临时请求所有 MCP server。输出可以展示连接状态和 `tool_count`，但这些字段只出现在 tool result 中，不能回写到 system/runtime prompt。`query` 的第一版匹配范围建议限定为 server name、server description、tool name、tool description。

`tool_count` 必须统计配置过滤后的可用工具。对尚未连接或 catalog 尚未就绪的 server，`tool_count` 可为 `0` 或 omitted，并返回 `status=connecting/error/disabled`。

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
    mcp(op="call", server="tavily", tool="tavily_search", arguments="{\"query\": \"...\"}")
- tavily_extract: Extract page content from URLs.
  Required: urls
  Example:
    mcp(op="call", server="tavily", tool="tavily_extract", arguments="{\"urls\": [\"https://...\"]}")
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
- `arguments` 是合法 JSON object 字符串（容错接受已解析的 dict）。
- 解析后的 arguments 能通过 MCP tool input schema 校验。
- 权限服务允许 `mcp:{server}:{tool}` 或对应策略。

权限检查需要发生在真实 MCP `tools/call` 之前，并且审批展示应使用解析后的 arguments。即使 `McpGatewayTool.execute()` 内部也做防御性检查，graph 级 authorization 仍需要能把 gateway call 分类成具体 MCP capability，否则 on-failure approval、session allow/deny 和 UI pending request 都只能看到 `mcp`。

校验失败时返回结构化、可修复错误。JSON 语法错误和 schema 校验错误分开报，便于模型自我修正：

```text
MCP call failed: arguments is not valid JSON for tavily/tavily_search.
Expect a JSON object string, e.g. "{\"query\": \"...\"}".
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
| `McpArgumentValidator` | 解析 `call.arguments` JSON 字符串，并用 input schema 校验 |
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

`ToolRegistry.tools_for_llm()` 只看到稳定的 `mcp` tool。真实 MCP catalog 不进入 `tool_defs`，而是作为 tool result 或内部 runtime state 使用。

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

`McpArgumentValidator` 至少要区分三类失败：

1. `arguments` 不是 JSON object 字符串，或解析失败。
2. 解析后不是 object，例如 array/string/null。
3. object 不满足 MCP tool `inputSchema`。

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
- system/runtime context 不包含 MCP catalog，也不需要额外 MCP 指引。
- 固定 `mcp` tool description 承载 gateway 工作流，不随 MCP catalog 变化。
- `mcp load` 的大段说明只出现在当前 turn 的 tool result 中，历史中可被 marker stripping 压缩。
- 新增/删除 MCP server 不改变 `bind_tools()` schema，只改变 `mcp list/load` 返回内容。
- auto server 段只包含 session-start 可确定的配置态内容，不包含连接状态、tool_count、tool names 或 schema hash。

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
- display value 复用现有工具摘要逻辑：优先 `query/url/urls/path/pattern/name/text`，需先解析 arguments JSON 字符串；parse 失败时降级为 `MCP Call("server/tool")`。
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
| invalid arguments JSON | 返回 JSON 语法错误和格式示例，提示 `load` 获取参数细节 |
| arguments schema mismatch | 返回 schema 校验错误和 `load` 提示 |
| permission denied | 返回具体 `mcp:{server}:{tool}` 被拒绝，不执行真实 MCP call |
| stale catalog | 尝试针对该 server refresh 一次；仍失败则提示重新 `mcp list/load` |
| unsupported schema feature | 返回 validator warning，提示加载具体 tool 查看参数细节 |
| MCP tool error | 保留 MCP error 内容，标记 metadata.error |
| large result | 走现有 display policy summary / truncation |

## Migration Plan

1. 新增 `McpGatewayTool`，注册为内置工具 `mcp`。
2. 提取 `McpCatalog`，让 `McpManager` 能提供 server/tool defs 给 gateway。
3. 在 `McpGatewayTool.description` 中写清 `list/load/call` 工作流。
4. 实现 `list/load`，先不改变现有 direct MCP tool 注册。
5. 增加 gateway 权限分类：`mcp op=call` → `mcp:{server}:{tool}`，`list/load` read-only。
6. 实现 `call`，复用 `McpManager.call_tool()` 和 `format_mcp_call_result()`。
7. 加入参数 JSON 解析、schema 校验和 stale catalog refresh。
8. 更新 TUI/GUI 渲染，使 gateway call 显示为真实 MCP 动作。
9. 增加 `McpServerConfig.auto` 和 `McpAutoRenderer`，输出配置态 auto server 段。
10. 增加配置开关：
   - `mcp.exposure = "gateway"`：只暴露固定 `mcp` tool。
   - `mcp.exposure = "direct"`：保留当前每工具注册模式。
   - `mcp.exposure = "hybrid"`：固定 gateway + 少量显式 allowlist direct tools。
11. 默认先保持 `direct` 或实验配置启用 `gateway`，观察 cache hit、调用成功率和用户反馈。

## Testing

### Unit Tests

- `mcp list` 返回 server 状态、tool_count、过滤后的工具摘要。
- `mcp load server` 返回 marker、工具描述、参数摘要和 examples。
- `mcp load server/tool` 返回单工具详细参数摘要。
- `mcp call` 对 valid arguments 调用 `McpManager.call_tool()`。
- `mcp call` 对非法 JSON arguments 字符串返回可修复的格式错误。
- `mcp call` 容错接受 dict 形式的 arguments。
- `mcp call` 对 missing required field 返回可修复错误。
- `mcp call` 对 enum、array item、nested object、additionalProperties 做 schema 校验。
- `mcp call` 遇到 stale catalog 时 refresh 一次，再决定失败或执行。
- `mcp op=list/load` 默认 read-only allow，`mcp op=call` 默认按 MCP 权限 ask。
- auto server 段只含配置态内容，不随连接状态或工具发现结果变化。
- 权限分类能按 `mcp:{server}:{tool}` 做 allow/ask/deny。
- direct `mcp__...hash` 与 gateway `mcp:{server}:{tool}` session rule 能等价或可迁移。
- 历史 stripping 能把 `VOIDX_MCP_TOOL_CONTEXT` 压缩为摘要。
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
- invalid argument retry rate（拆分为 JSON parse error 和 schema validation error 两类计数）。
- tool_defs token count。
- `mcp list/load/call` latency，拆分 catalog cache hit 和 server refresh。
- permission prompt frequency，拆分 direct/gateway/hybrid。

## Open Questions

- 默认 exposure 应直接切 `gateway`，还是先提供实验开关？
- auto server 段后续是否需要 per-tool 粒度，以及 `@server` 显式引用（对齐 skill 的 `EXPLICIT_REF_RE`）？
- `load` 是否允许一次加载所有 connected MCP server，还是强制按 server 加载？
- 参数 schema 压缩是否需要保留 enum、array item 类型和 nested object path？
- 第一版 schema 校验使用现有依赖还是新增 JSON Schema validator？
- MCP `tools/list` pagination 和 tools-list-changed 通知是否在第一版一起补齐？
- `websearch/webfetch` 是否继续作为高频 native tools，还是迁移到 gateway-backed wrappers？
- 是否需要 `mcp(op="search", query="...")` 作为 `list` 的语义别名，方便模型发现工具？

## Recommended First Version

第一版建议做保守实现：

1. 新增固定 `mcp` tool，支持 `list/load/call`。
2. 在 `mcp` 工具描述里教模型先 `list/load` 再 `call`。
3. `list/load` 使用统一 `McpCatalog`，并加 current-turn marker stripping。
4. `call` 先实现 JSON 解析、基础 JSON Schema 校验和 `mcp:{server}:{tool}` 权限分类。
5. 保留现有 direct MCP 注册，但加配置 `mcp.exposure`。
6. 默认仍可先用现状，开发者开启 `gateway` 测试。
7. 在 gateway 模式下禁用 direct `mcp__*` 注册，只保留固定 `mcp`。
8. 收集 cache hit、权限 prompt 频率和调用错误数据后，再决定是否默认切换。

这个路径风险低：不会一次性删除现有 MCP 能力，同时能验证 gateway 是否真的改善 cache 和工具列表膨胀问题。
