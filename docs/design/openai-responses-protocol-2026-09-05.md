---
name: openai-responses-protocol
display_name: OpenAI Responses API 协议接入
description: 新增可选 responses 协议，复用 LangChain，保留本地会话管理和完整工具调用历史。
doc_type: tech-design
audience: human+llm
status: draft
---

# OpenAI Responses API 协议接入

## 结论与审批范围

新增 `protocol: responses`，供 OpenAI 官方和实现相同协议的中转显式选择；**不修改已有 provider 的默认协议，不自动探测或降级协议**。底层复用 `ChatOpenAI` 的 Responses 支持，voidx 继续管理本地消息历史和工具执行。

本文件是待审批设计，不是已实现说明。用户已确认目标是 OpenAI Responses API，并要求先出文档；以下协议命名、无状态策略和首版边界仍需设计审批。

关键改动不是替换 URL，而是分离**给用户展示的内容**与**下一轮需要回传的结构化消息**。现有流式管线会移除推理块，直接开启 SDK 开关不足以满足多轮工具调用。

### 首版包含

- 显式 Responses 协议选择、配置保存和恢复，支持自定义 API Base URL。
- 普通文本与现有图片输入、流式输出、voidx 函数工具调用及结果回传。
- 推理强度设置、可用时展示推理摘要、结构化输出、token 用量与 prompt cache key。
- 同一 provider、模型和端点下的多轮会话、会话恢复及本地上下文压缩回归。

### 首版不包含

- 新增名为 `responses` 的供应商；这是协议，不是 provider。
- 服务端会话链：`previous_response_id`、Conversations API、后台任务、WebSocket 传输。
- OpenAI 托管工具，如 web/file search、code interpreter、computer use；仍使用 voidx 工具系统。
- OAuth、第三方代理部署、自动协议探测、HTTP 失败后切换 Chat Completions。
- 跨 provider、模型或端点复用加密推理的兼容承诺；切换这些连接信息时应使用新会话。跨协议的可见文本和标准工具历史需能安全转换，不透传 Responses 专属块。

## 1. 当前实现与已核实证据

调查基于 2026-09-05 的当前工作树，包含用户未提交的推理参数和 prompt-cache 修改。

| 位置 | 当前行为及设计影响 |
|---|---|
| `src/voidx/llm/domain/model.py`、`src/voidx/llm/domain/provider.py` | `ModelConfig.protocol` 是可选字符串，显式配置优先于 provider 默认；不需要为新协议新增配置字段。 |
| `src/voidx/llm/adapters/langchain_model_factory.py` | 仅显式分发 anthropic/openai/deepseek/gemini；未知协议抛出 `ValueError`。`_reasoning_kwargs` 先取 provider hook，不能让其向 Responses 注入旧协议的请求字段。 |
| `src/voidx/llm/providers/openai.py` | 使用 `ReasoningPreservingChatOpenAI` 保留 Chat Completions 风格推理 delta；这不是 Responses 事件转换器。 |
| `src/voidx/agent/adapters/langgraph/runtime/streaming.py` | `stream_llm` 当前将过滤后的内容写入累计 chunk；结束时再次清理内容且未传 protocol。`_sanitize_ai_content_for_replay` 移除 reasoning，并合并文本块，可能丢失 item 元信息和顺序。 |
| `src/voidx/llm/thinking.py` | 已能从 `reasoning.summary` 提取文本；应复用并补 Responses 流式测试，不另写解析器。 |
| `src/voidx/agent/adapters/langgraph/runtime/turn_runner.py`、`src/voidx/agent/adapters/persistence/message_rows.py` | assistant 的 list content 以 structured JSON 保存并恢复，工具调用单独保存；适合保留 `responses/v1` 内容。不能依赖尚未持久化的 response metadata 来恢复协议所需数据。 |
| `src/voidx/agent/adapters/persistence/context_frame_repository.py` | 上下文帧保存 content、tool_calls、tool_call_id；保留在 content 内的原生项可随帧保存。 |
| `src/voidx/llm/cache_key.py` | 当前只给 `openai` 绑定 prompt cache key，需纳入 `responses`。该文件目前属于用户未提交工作。 |
| `frontend/src/ui/providers.ts`、`src/voidx/presentation/slash/commands/model.py` | Web 协议选项和终端自定义 provider 协议选项均未包含 responses；终端选择已有 provider 时目前不询问协议。 |
| `src/voidx/llm/adapters/http_model_discovery.py`、`src/voidx/llm/application/model_catalog.py` | 除 Anthropic/Gemini 外走 `/models`；静态回退按 provider 或协议名查找，需为 responses 复用 OpenAI 的静态列表，而不是复制供应商目录。 |

本机通过 `./python.py` 检查得到：`langchain-openai 1.3.5`、`langchain-core 1.4.9`、`openai 2.46.0`。前者已经包含 `use_responses_api`、`output_version`、`store`、`include`、`reasoning` 和 `use_previous_response_id` 字段。

以假 key、无网络请求构造 SDK payload 的探针已确认：`store=False` 时加密 reasoning、function_call、function_call_output 顺序保留；content 与 `tool_calls` 同时包含同一个调用不会重复序列化；`max_tokens` 转为 `max_output_tokens`，prompt cache key 保留，不产生 previous_response_id。**这只证明当前安装版本的载荷构造能力，不代表 voidx 已接入或真实端点已通过联调。**

## 2. 方案选择

| 方案 | 优点 | 代价 | 决策 |
|---|---|---|---|
| 独立 `responses` 协议 + LangChain 适配 | 沿用 BaseChatModel、工具绑定和配置体系；兼容开关清楚 | 要补齐消息保留与回归测试 | 推荐 |
| 在 `openai` 下自动切换接口 | 少一个配置选项 | 依赖 SDK/模型推断，旧中转行为容易变化 | 不采用 |
| 直接实现 OpenAI SDK 或 HTTP/SSE 适配器 | 完全控制 wire format | 重复 LangChain 的消息、工具、流式和用量转换 | 首版不采用 |

这里的“默认行为不变”是指不修改原 provider 的配置解析和 SDK 选项；不强行将旧 `openai` 路径设为 `use_responses_api=False`。当前 SDK 自身可能根据模型或请求参数选择接口，这属于既有行为，不在本次顺带重定义。

## 3. 目标架构

```text
Profile / ModelConfig(protocol=responses)
  → create_chat_model
  → Responses 专用构造参数 + ChatOpenAI
  → POST <base_url>/responses
  → LangChain AIMessageChunk（responses/v1）
      ├─ 可见文本 / reasoning summary → renderer
      └─ 完整结构化内容 → AIMessage → 本地持久化
                                 → ToolMessage
                                 → 下一轮 Responses input
```

### 3.1 配置与入口

沿用 provider、model、base_url、protocol 四个字段，不增加 SQLite schema、provider catalog 条目或新的凭证格式。以下仅表示目标配置值，不是新增 YAML 文件格式：

| 字段 | 官方示例 | 兼容中转示例 |
|---|---|---|
| provider | `openai` | `my-relay` |
| model | `gpt-5.5` | 端点实际支持的模型 ID |
| protocol | `responses` | `responses` |
| base_url | `https://api.openai.com/v1` | `https://relay.example.com/v1` |

Base URL 是 API 根地址，**不填写 `/responses` 后缀**。官方 provider 可沿用目录中的默认地址；未知自定义 provider 使用 Responses 时应要求显式 base_url，避免意外请求官方端点。

Web 增加协议选项。终端 `/model new` 的自定义 provider 增加 responses；已有 provider 也应提供协议覆盖选择，默认“保持当前/使用供应商默认”，不因新增一次交互改变最终配置。相关交互测试需同步更新。模型列表发现仍调用 `/models`，失败时复用已有 OpenAI 静态列表并允许手工输入模型名，不将静态建议视为端点能力证明。

### 3.2 模型构造与推理设置

新增 `src/voidx/llm/providers/responses.py`，负责 Responses 专有参数与必要的薄适配；不在此复制 provider 元数据或实现 SSE。模型工厂负责协议分发与通用连接配置。

| SDK 设置 | 目标值/规则 |
|---|---|
| `use_responses_api` | `True`，不依赖 SDK 自动推断 |
| `output_version` | `"responses/v1"`，协议所需原生项保存在 content 中 |
| `store` | `False` |
| `use_previous_response_id` | `False`；不发送 conversation 或 previous_response_id |
| `include` | `["reasoning.encrypted_content"]`，显式兼容支持该 include 的实现 |
| `reasoning` | 对已识别的 OpenAI 推理模型使用 `{effort: 映射结果, summary: "auto"}`；有效 effort 为 none 时不请求摘要；未知/非推理模型不强加此字段 |
| token 上限 | 复用 config.max_tokens，由 SDK 转换为 max_output_tokens |
| temperature | 推理启用时不发送；非推理模型沿用配置。避免把 Chat Completions 的 temperature=1.0 特例盲目迁移到 Responses |
| headers / timeout | 沿用自定义中转头清理与现有超时政策，覆盖 reasoning 字典形式及 effort=none 情形 |

Responses 请求形状优先于 provider 专属 hook，禁止携带 `enable_thinking`、Anthropic thinking 或不相容的 extra_body。复用 `providers/common.py` 的强度映射；如需共享模型识别规则，应提取公共 helper，不让 provider 模块互相依赖，不复制维护能力表。

`create_resolver_model` 必须重算 Responses reasoning，并清除原模型残留的 `reasoning` 字段；目前仅清除 reasoning_effort 等字段并不足够。无法关闭推理的已知模型使用现有能力映射允许的最低强度，不承诺所有模型的 none 都真正关闭推理。resolver 保持同一 Responses 协议及无状态设置。

### 3.3 无状态会话与原生项保留

- 本地历史仍是唯一上下文来源；每次发送本地编译/压缩后的消息，不引用服务端响应链。
- Responses 分支累计未经展示过滤的 chunk；文本和摘要只从展示副本中提取。旧协议保持原有输出约定。
- 最终 AIMessage 保留 reasoning 的 id、summary、encrypted_content，function_call 的 id、call_id、name、arguments，以及文本块的关联元信息。**不得将 list content 压成字符串、重排原生项或修改不透明密文。**
- 标准 `AIMessage.tool_calls` 仍是 voidx 执行工具的入口，`ToolMessage.tool_call_id` 对应 Responses `call_id`，不是 `fc_*` item id。不能因为同一调用同时存在于 content 和 tool_calls 而执行两次。
- `_sanitize_messages_for_replay` 对 Responses 保留原生内容和次序，继续修复缺失工具结果的邻接关系；最终合并阶段也必须使用相同协议政策，不能提前丢弃 reasoning-only 消息。
- 无 encrypted_content 的 reasoning 不能假设可由远端 ID 恢复；当前 SDK 在 store=false 的序列化路径会略过此类 reasoning。不伪造密文、不为恢复它而开启 store。
- 非 Responses 目标的出站消息需移除 Responses 原生 reasoning/function_call 等协议项，并保留可见文本和标准 tool_calls，不能修改已保存的历史原对象。
- 同一端点的普通续聊不丢 reasoning；压缩时可按原政策丢弃完整旧轮次，但保留的工具调用链不能只剩输出或半个推理/调用组合。

加密块是协议状态，不是可展示推理内容。只展示 API 返回的 summary 文本，不解码或展示 encrypted_content，不将其放入子代理结果摘要。需检查 `subagent.py` 当前直接切片 content 的 preview 路径，改用已有文本提取 helper。历史展示、会话导出和诊断日志也要区分用户可见文本与协议数据；本地原始会话/上下文帧中保留密文是预期行为。`store=false` 不构成对官方或中转日志、数据保留政策的零留存保证。

### 3.4 持久化、用量和结构化输出

现有 structured content 存储应能承载 Responses 原生项。先用测试证明保存 → 加载 → 重新构造请求没有数据丢失；仅有 response_metadata 留在内存不能作为恢复依据。预计无 schema 迁移；如果测试暴露必须持久化的新字段，先更新设计，不静默扩张存储格式。

- `bind_prompt_cache_key` 增加 responses；沿用 session/provider/model/scope 的键生成政策，不重写现有用户修改。主代理、子代理、压缩调用都应经过现有绑定入口。
- `extract_token_usage` 优先消费 SDK 标准 usage_metadata，验证 input/output/total、cache_read 和 reasoning；output_tokens 已含 reasoning 时不得重复累加。
- 结构化输出继续走 `ainvoke_structured` 的 function_calling 路径，验证实际请求和解析结果，而不是仅断言模型对象创建成功。
- 主代理、子代理和压缩仍复用模型工厂；禁止只让主对话可用而遗漏辅助模型请求。
- API 错误、超时、取消和断流沿用现有生命周期；不将失败/不完整工具调用提交执行，不因错误自动改用另一个协议。

### 3.5 依赖政策

`pyproject.toml` 当前声明 `langchain-openai>=0.3.0`，不足以作为本设计所需全部行为的已验证下限。建议首版使用保守下限 `>=1.3.5`，因为当前已检查该版本的无状态 replay 实现；这不是声称 Responses 最早在此版本引入。

1.3.5 的元数据要求 `langchain-core>=1.4.9,<2.0.0`、`openai>=2.45.0,<3.0.0`，Python 要求与项目的 >=3.11 相容。实现验收必须运行受影响后端回归并记录实际解析出的依赖版本；若要降低下限，必须在候选下限环境运行同一测试矩阵，不能凭 SDK 字段存在就降低。不要修改 desktop 打包产物或当前运行环境的安装目录来冒充依赖声明更新。

## 4. 实现落点与测试映射

下表是执行边界，不是授权立即修改实现代码。已有文件以真实路径列出；标记“新增”的文件在设计阶段不创建。

| 工作项 | 实现位置 | 验收测试位置 |
|---|---|---|
| 协议工厂、无状态参数、resolver | `src/voidx/llm/providers/responses.py`（新增）、`src/voidx/llm/adapters/langchain_model_factory.py`；共享 helper 必要时在 `src/voidx/llm/providers/common.py`、`src/voidx/llm/providers/openai.py` 调整 | `src/tests/test_llm/test_responses_provider.py`（新增）、`src/tests/test_llm/test_llm_provider.py`、`src/tests/test_llm/providers/test_reasoning_effort.py` |
| 流式与多轮 replay | `src/voidx/agent/adapters/langgraph/runtime/streaming.py`、`src/voidx/llm/thinking.py` | `src/tests/test_llm/test_streaming_sanitize.py`、`src/tests/test_agent/adapters/langgraph/runtime/test_stream_llm_sanitization.py`、`src/tests/test_agent/adapters/langgraph/runtime/test_responses_streaming.py`（新增） |
| 持久化与辅助调用 | 先验证现有 `turn_runner.py`、`subagent.py`、`compaction_coordinator.py`（均在 `src/voidx/agent/adapters/langgraph/runtime/`）；只改失败测试证明的兼容缺口 | 同目录对应的 `src/tests/test_agent/adapters/langgraph/runtime/test_session_persistence.py`、`test_session_context_frames.py` 及新 Responses 集成测试 |
| 缓存、用量、结构化输出 | `src/voidx/llm/cache_key.py`；按测试需要调整 `src/voidx/llm/usage.py`、`src/voidx/llm/structured.py` | `src/tests/test_llm/test_prompt_cache_key.py`、`test_llm_usage.py`、`test_structured.py`、新 provider 测试 |
| 配置入口与目录回退 | `src/voidx/presentation/slash/commands/model.py`、`frontend/src/ui/providers.ts`、`src/voidx/llm/application/model_catalog.py`；现有 config 存取逻辑优先不改 | `src/tests/test_slash/test_slash_model.py`、`frontend/test/ui/providers.test.ts`、`src/tests/test_config/test_config.py`、`src/tests/test_llm/test_llm_catalog_fetchers.py` |
| 依赖声明 | `pyproject.toml` | 下列回归命令与依赖版本检查 |

### 必须覆盖的验收场景

1. **配置与请求**：官方及自定义端点显式走 `/responses`；base_url 不被错误覆盖；旧 provider 默认不变；自定义端点缺失地址有明确错误；配置保存/切换/重启后保留协议。
2. **真实 SDK 边界的本地模拟**：使用 httpx mock transport 返回合法 Responses JSON/SSE，调用真实 ChatOpenAI，断言请求路径、payload 和解析结果；不只 mock `AIMessageChunk`。
3. **完整工具闭环**：reasoning → 两个并行 function_call → 两个结果 → 再一次工具调用 → 最终文本。确认 call_id 配对、每个工具只执行一次、次序与密文不丢失，第二轮不依赖服务端存储。
4. **流式细节**：文本/摘要分片、参数分片、仅 reasoning/仅工具、末尾 usage、异常/取消/不完整参数。正文不出现摘要重复文本、密文或工具 JSON。
5. **恢复与压缩**：经过实际会话保存/加载及 context frame 后重构请求；本地压缩不拆散保留的调用链；非 Responses 协议收到安全转换后的普通历史。
6. **旁路调用**：主代理、子代理、压缩、resolver 都能构造 Responses 请求；resolver 不继承主模型高强度 reasoning；结构化返回可解析为目标 Pydantic 模型。
7. **内容与用量**：已有图片输入不退化；token/cache/reasoning 统计正确；cache key 绑定到请求顶层；子代理 preview 与历史展示不暴露原生密文。

### 验证命令

工作目录为仓库根目录，统一使用项目测试入口。新增文件仅在实现阶段对应 RED 测试创建后运行。

```bash
# 先对每个工作项完成 RED → 最小实现 → GREEN
./test.py --backend -- src/tests/test_llm/test_responses_provider.py src/tests/test_agent/adapters/langgraph/runtime/test_responses_streaming.py -v
./test.py --backend -- src/tests/test_llm/test_streaming_sanitize.py src/tests/test_agent/adapters/langgraph/runtime/test_stream_llm_sanitization.py -v
./test.py --backend -- src/tests/test_slash/test_slash_model.py src/tests/test_config -v
./test.py --frontend -- test/ui/providers.test.ts

# 再验证相关整体行为与架构约束
./test.py --backend -- src/tests/test_llm src/tests/test_agent/adapters/langgraph/runtime src/tests/test_architecture src/tests/test_contracts -v
./test.py --frontend

# 记录测试实际使用的依赖版本；不安装或改写当前环境
./python.py -c 'from importlib.metadata import version; print({p: version(p) for p in ("langchain-openai", "langchain-core", "openai")})'
```

预期：新协议的定向测试全部通过，原协议、配置、运行时及契约回归无新增失败。当前工作树已有用户修改，实施前记录基线；既有失败单列，不得回滚无关工作来消除失败。

真实端点联调另行使用用户提供的测试连接，执行“普通问答 → 工具调用 → 连续追问 → 恢复会话”，记录 provider/模型、SDK 版本与结果，不能把 API key 或原始响应密文写入仓库。在未完成该步骤时，只能报告“本地协议测试通过”，不得宣称 OpenAI 官方或某个中转端到端可用。

## 5. 约束与审批后的下一步

- 保留当前工作树全部无关修改，特别是已修改的 openai/common、运行时缓存绑定和相关测试；本设计阶段只创建本文档。
- 不为首版重构整个 provider 架构、重写事件总线或复制 token/推理强度规则，不增加 provider 目录的 `responses` 假供应商。
- 不手改 `frontend/src/rpc/protocol.d.ts`、desktop 资源包或运行时 site-packages；本设计不新增 UI RPC 字段。
- 不用 `store=true` 或服务端 response ID 绕过消息丢失问题，不在工具报错后跨协议重发请求。
- 文档获批后进入实施计划与 TDD；功能通过最终验证后才按项目文档规则迁移/归档，不在设计阶段标记完成。

## 参考

- [OpenAI Responses API](https://developers.openai.com/api/docs/guides/migrate-to-responses)
- [OpenAI reasoning：推理项回传、摘要与无状态会话](https://developers.openai.com/api/docs/guides/reasoning)
- 本机 `langchain_openai.chat_models.base` 的 `_construct_responses_api_payload`、`_construct_responses_api_input` 与 `_convert_responses_chunk_to_generation_chunk`（1.3.5）。

官方当前文档说明，无状态模式会默认返回 encrypted_content，旧的 include 值仍受支持；因此本设计的显式 include 是兼容意图，不把它描述为所有版本的唯一取回方式。文档也建议连续工具调用回传最近用户消息以来的 reasoning、function_call 和 function_call_output 项。第三方是否完整实现这些行为，仍需逐端点验证。
