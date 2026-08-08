# 压缩总结独立模型与 reasoning 配置

> **Status: Done** — Archived on 2026-08-09.

## 状态

设计已确认，待实现。

- 目标：compaction summary 可独立配置 model/profile、reasoning 和单次调用超时
- 默认：未配置的项继承主 agent；零配置行为保持兼容
- 输入边界：`Long Summary` 只总结被移除的完整历史语义轮次，不读取当前保留轮次或其他运行时注入
- 范围：config、settings API、slash、summary 模型解析与重试、输入过滤、usage/metadata
- 非范围：前端 settings 面板、temperature/max_tokens 细调、单轮 pressure 收敛提示

## 背景与当前差距

整轮压缩由
`src/voidx/agent/adapters/langgraph/runtime/compaction_coordinator.py`
协调。当前 summary 调用直接绑定 `host.model` 和 `host.config.model`，因此不能单独选择模型或 reasoning。

这里的“summary 子 agent”是 compaction coordinator 内部的一次无工具 LLM 行为，**不是**注册到
`src/voidx/agent/application/agents.py` 的独立 agent。当前正式 agent 定义只有 `voidx`；实现不得为本功能虚构或注册隐藏 `compaction` agent。

当前还有一个与模型配置独立、但必须同时修复的输入边界问题：

1. coordinator 先对 `raw_semantic_messages(messages)` 做 head/tail 选择；
2. 但 `run_compaction_agent` 在 `_active_compaction_source_messages` 可用且预算允许时，会把完整 live source context 发送给 summary 模型；
3. 该 source context 可能包含 system/runtime context、保留的 tail、当前用户轮次及 synthetic guidance；
4. `CompactionService.build_prompt()` 会排除 step hint，但目前会把 guidance 作为 `[Guidance]` 写入 summary prompt。

因此现状不能保证 `Long Summary` 只来自被移除的历史区间。本设计把该边界冻结为实现不变量。

## 目标与非目标

### 目标

1. summary 可指定独立 `profile_name`（`provider/model`）。
2. summary 可指定独立 `reasoning_effort`。
3. `profile_name` 或 `reasoning_effort` 未配置时，各自继承主 agent 配置。
4. 可配置 summary 调用的 `timeout_seconds`，语义为 per-attempt。
5. 独立配置失败或调用失败时 fail-open：优先 summary 配置，失败后回退主 agent 原模型，最终做保留 previous summary 的 deterministic fallback merge。
6. 临时 summary 模型不写回 `host.model` / `host.config.model`。
7. `Long Summary` 只由既有 previous summary 与本次被移除的完整历史语义轮次更新。
8. usage、context frame、日志和 metadata 记录实际模型及实际 reasoning 来源。
9. 提供 workspace settings API 与 slash 入口。

### 非目标

1. 不改变完整轮次的 head/tail 选择算法。
2. 不实现单轮中途截断 summary。
3. 不配置 temperature、max_tokens 或独立 context window。
4. 不在本阶段实现前端 settings 面板。
5. 不修改 delegated child run 的通用模型配置；本文只覆盖 compaction summary 行为。
6. 不改变 `inline_compaction_enabled` 或长单轮 pressure hint 的触发逻辑。

## 配置契约

### 配置模型

在 `src/voidx/config/models.py` 新增：

```python
class CompactionConfig(BaseModel):
    profile_name: str = ""
    reasoning_effort: ReasoningEffort | None = None
    timeout_seconds: float = Field(
        default=60.0,
        ge=1.0,
        le=300.0,
        allow_inf_nan=False,
    )
```

settings key：

```json
{
  "compaction": {
    "profile_name": "",
    "reasoning_effort": null,
    "timeout_seconds": 60.0
  }
}
```

字段语义：

| 字段 | 未配置值 | 语义 |
|---|---|---|
| `profile_name` | `""` | 使用主 agent 的 provider/model |
| `reasoning_effort` | `null` | 继承 `host.config.model.reasoning_effort` |
| `timeout_seconds` | `60.0` | 单次 summary LLM attempt 的超时 |

`reasoning_effort=null` 是显式的继承状态，**不能**通过创建默认 `ModelConfig()` 间接得到 `xhigh`。
`ReasoningEffort.NONE` 是合法的显式覆盖，含义是关闭 reasoning；它与 `null`/inherit 不同。

### 有效配置解析

有效 summary 配置按字段合并，而不是把 profile 和 reasoning 绑定成一个开关：

```text
effective provider/model =
  compaction profile       if profile_name != "" and profile resolves
  main agent provider/model otherwise

effective reasoning =
  compaction.reasoning_effort        if not null
  host.config.model.reasoning_effort otherwise
```

示例：

| profile | summary reasoning | 有效行为 |
|---|---|---|
| 未配置 | 未配置 | 直接使用主模型实例及主 reasoning |
| 未配置 | `low` | 主 provider/model + 临时 `low` reasoning 模型 |
| `provider/model` | 未配置 | 独立 profile + 主 agent reasoning |
| `provider/model` | `none` | 独立 profile + 显式关闭 reasoning |
| profile 无效 | `medium` | warning；主 provider/model + 临时 `medium` reasoning 模型 |

从 profile 构造 `ModelConfig` 时：

- `provider` / `model` 来自 profile；
- `base_url` / `protocol` 使用 profile 值，否则通过 settings 的 provider resolver 补齐；
- `reasoning_effort` 使用上面的有效值；
- `temperature` / `max_tokens` 继续使用当前 summary 构造约定，不新增用户配置；
- `context_window` 保持 `None`，本阶段仍使用现有 compaction 预算。

### Settings 作用域与 API

`compaction` 固定为 workspace-only：

- `src/voidx/config/settings.py` 的 `WORKSPACE_ONLY_KEYS` 加入 `compaction`；
- 只写工作区 `.voidx/settings.json`，不声明全局覆盖语义；
- `src/voidx/config/__init__.py` 导出 `CompactionConfig`；
- 在 `src/voidx/config/settings_permissions.py` 或独立、职责明确的 settings mixin 中提供：

```python
def get_compaction_config(self) -> CompactionConfig: ...
def set_compaction_config(self, config: CompactionConfig) -> Path: ...
```

要求：

- 非 dict、非法枚举、非法 timeout 或其他校验失败时，记录 warning 并返回 `CompactionConfig()`；
- `settings is None` 时等同默认配置；
- settings 的已知 key/清理逻辑必须保留 `compaction`；
- 序列化保留 `reasoning_effort: null`，不能写成默认 `xhigh`。

## 运行时模型解析与隔离

### 解析结果

coordinator 内部使用类型化结果：

```text
ResolvedCompactionModel = {
  model,                         # BaseChatModel
  model_config,                  # 本次调用的完整 ModelConfig
  model_source,                  # "main" | "profile"
  profile_name,                  # 空表示 main
  reasoning_source,              # "main" | "compaction"
  effective_reasoning_effort,
  is_exact_main_instance,        # 是否直接复用 host.model
}
```

模型可用性必须在解析后判断，禁止保留下面的早退：

```python
if host.model is None:
    return None
```

合法场景包括：主模型实例为空，但独立 compaction profile 具备 API key 且可成功构造临时模型。

### Primary 与 fallback

解析生成有序 stage：

1. **summary stage**
   - 若 profile 有效，使用 profile 模型；
   - 若只覆盖 reasoning，使用主 provider/model 与主凭据构造临时模型；
   - 若无任何覆盖，直接复用 `host.model`。
2. **main fallback stage**
   - 仅当 summary stage 不是主 agent 的原模型实例，且 `host.model` 可用时存在；
   - 使用 `host.model` 和 `host.config.model`，包括主 agent 原 reasoning；
   - 不沿用失败的 summary reasoning 覆盖。
3. 无可用 LLM stage 时直接进入 deterministic fallback merge。

profile 不存在、无 API key或构造失败时：

- 记录 warning；
- 若 reasoning 有独立覆盖且主凭据可用，仍可构造“主模型 + summary reasoning”的 summary stage；
- 否则直接使用主模型 stage；
- 解析异常不得中断 compaction 主路径。

### 状态隔离

硬约束：

1. 临时模型只存在于 coordinator 的局部解析结果中；
2. 禁止写回 `host.model`、`host.config.model`、当前 profile 或 UI 主模型状态；
3. summary 完成后，下一次主 agent LLM 调用仍使用原主模型实例；
4. fallback main stage 必须复用主 agent 原配置，不能继承独立 summary override；
5. 并发 session 之间不得通过模块级 mutable state 或 host 临时字段交换 resolved model。

## Summary 输入与 `Long Summary` 边界

### 权威区间

本次 summary 的唯一新增历史来源是 coordinator 选择出的 `selection.head`，即压缩成功后从 live context 移除的完整旧轮次。

```text
live messages
  -> raw semantic normalization
  -> whole-turn selection
  -> selection.head                 # 唯一允许的新历史区间
  -> compaction_summary_messages()  # 删除控制/运行时注入
  -> summary LLM or fallback_summary_with_previous
  -> host._pending_summary / host._compaction_summary
  -> RuntimeContextBuilder(summary=...)
  -> ## Long Summary
```

允许输入：

1. `previous_summary`：已经锚定、更早且已被移除的历史总结；
2. `selection.head` 内真实 top-level user turn；
3. 上述轮次产生的 assistant messages 与 tool results。

禁止输入：

1. `SystemMessage`，包括 Base System、Workflow Runtime、Runtime State、Project Instructions、Session Time 和已经渲染的 `Long Summary`；
2. standalone 或 turn-overlay 形式的 `VOIDX_RUNTIME_CONTEXT`；
3. `STEP_HINT_MARKER` 消息，包括 convergence hint 和 context pressure hint；
4. `GUIDANCE_MARKER` 消息，包括用户运行中 guidance、repair prompt、guard/system guidance 和 child-run notice；
5. `VOIDX_COMPACTION_GUIDE`、legacy goal/workflow overlay、无 marker 的 legacy `Continue if you have next steps` continuation message 或其他 synthetic control guide；
6. `semantic_tail`、当前保留轮次和 tail anchor 之后的任何消息；
7. coordinator 的完整 active source context（`_active_compaction_source_messages` / `_current_messages`）。

这意味着 `Long Summary` 是**历史语义记忆**，不是当前 runtime prompt 的摘要，也不是所有曾注入 LLM 的文本副本。

### 过滤位置

新增单一 helper（名称可按模块职责微调）：

```python
def compaction_summary_messages(
    selected_head: list[BaseMessage],
) -> list[BaseMessage]: ...
```

职责：

- 输入必须已经是 `selection.head`，helper 不自行扩大区间；
- 复用 `raw_semantic_messages` 的 overlay stripping；
- 额外排除 step hint、guidance 和所有已知 control guide；
- 保留 eligible user/assistant/tool 消息的原始顺序和完整轮次关系；
- 返回新 list，不修改 LangGraph state。

coordinator 必须把过滤后的同一份 `summary_head` 同时用于：

- summary LLM 输入；
- `fallback_summary_with_previous(summary_head, previous_summary)`，确定性保留旧 summary 并追加本次 filtered head 的提取结果；
- compaction context frame 的输入消息；
- summary 输入 message count/审计 metadata。

`removed_messages`、tail anchor 和持久化删除边界仍由原始 `selection.head` 决定，不能因过滤 synthetic 消息而改变压缩边界。

### Deterministic fallback 合并

LLM stages 全部失败时，不能用 `fallback_summary(summary_head)` 直接覆盖
`previous_summary`，否则更早已经从 persistence/live state 删除的历史会永久丢失。

新增单一 deterministic helper：

```python
def fallback_summary_with_previous(
    summary_head: list[BaseMessage],
    previous_summary: str | None,
) -> str: ...
```

语义：

- 先从 filtered `summary_head` 生成 bounded extracted summary；
- previous summary 非空时，将其作为“既有历史”的最高优先级输入；
- 合并结果必须有明确字符/token 上限；若 previous summary 本身超预算，按其结构化 section 从尾部/低优先级项确定性裁剪，禁止整段丢失；随后再保留新提取的 decisions/changes/failures/next steps；
- 不重新读取 system/runtime/tail/current turn；
- LLM fallback 与 deterministic fallback 最终都写入同一个 `_compaction_summary`。

### 禁止 full-context 快路径

删除 `run_compaction_agent` 中“完整 source context 能放下就全部发送”的分支。summary attempt 只能从：

```text
filtered summary_head + compaction request(previous_summary)
```

构建输入。预算不足时只能在 `summary_head` 内按完整历史轮次裁剪/降级，不能把 tail 或 runtime prefix 加回来。

若 eligible head 经过预算处理后为空，summary LLM attempt 失败并进入 main stage / deterministic fallback；不得改为总结当前 live context。

### 注入到主 agent

summary 成功或 deterministic fallback 后：

- 写入 `host._pending_summary` 与 `host._compaction_summary`；
- `prepare_with_stream` 继续通过 `RuntimeContextBuilder(summary=summary)` 渲染唯一的 `## Long Summary` section；
- `RuntimeContextBuilder` 不自行抓取消息或重新总结其他 section；
- 后续再次压缩时，只从 `_compaction_summary` 读取 previous summary，不从已渲染的 runtime `SystemMessage` 回读。

## 调用与重试

### Callback 边界

保留现有两参数 override seam：

```python
Callable[[list, str | None], Awaitable[str | None]]
```

避免仅为传 model/reasoning 扩大
`src/voidx/agent/adapters/langgraph/graph_compaction.py` 与
`src/voidx/agent/adapters/langgraph/execution.py` 的 callback 参数形状。

生产路径的模型解析、stage 编排和 attempt metadata 内聚在 coordinator：

- `compact_for_live_state(..., run_compaction_agent=None)` 走 coordinator 的内建模型解析与两阶段重试；
- coordinator-private `_run_compaction_attempt(summary_head, previous_summary, resolved, timeout)` 执行一次 resolved model 调用；
- `run_compaction_agent` 参数仅保留给测试或外部 custom override，非 `None` 时走兼容的单 stage、最多 3 次 attempt；
- `GraphCompactionAdapter` 和 `LangGraphExecution` 的生产接线需停止无条件传入 `self._run_compaction_agent` wrapper，否则 `run_compaction_agent` 永远非 `None`，会绕过内建模型解析；
- wrapper 方法可以保留向后兼容，但只在明确 custom override 路径调用。

### 两阶段重试

`COMPACTION_MAX_RETRIES = 2`，因此每个 stage 最多 3 次：

```text
Stage A: summary stage      <= 3 attempts
Stage B: main fallback      <= 3 attempts，仅当 Stage A 不是原主模型且主模型可用
All fail: fallback_summary_with_previous(summary_head, previous_summary)
```

| 场景 | Stage A | Stage B | 最终 |
|---|---|---|---|
| 零配置 | main 原实例 ×≤3 | 无 | fallback merge |
| 仅 reasoning override | main model + override ×≤3 | main 原实例 ×≤3 | fallback merge |
| 有效 profile，reasoning inherit | profile + main reasoning ×≤3 | main 原实例 ×≤3 | fallback merge |
| 有效 profile + reasoning override | profile + override ×≤3 | main 原实例 ×≤3 | fallback merge |
| profile 无效、无 reasoning override | main 原实例 ×≤3 | 无 | fallback merge |
| profile 可用、主模型不可用 | profile ×≤3 | 无 | fallback merge |
| 无 LLM 可用 | 无 | 无 | 直接 fallback merge |

attempt 失败定义：

- 超过 `timeout_seconds`；
- 抛异常；
- 返回 `None` 或空文本。

规则：

- timeout 是 per-attempt，不是跨 stage 总预算；
- Stage A 全部失败后才进入 Stage B；
- Stage A 成功后不得调用主模型；
- 最坏等待上界约为 `6 × timeout_seconds`，不含本地预处理；
- prompt、token estimation、protocol、usage 和 output token estimation 均使用该 attempt 的 `resolved.model_config`，不能继续硬编码主模型配置。

## Slash 入口

新增：

```text
/compact-model
/compact-model <profile>
/compact-model clear
/compact-model timeout <seconds>
/compact-model reasoning
/compact-model reasoning <none|low|medium|high|xhigh|max>
/compact-model reasoning inherit
```

行为：

| 命令 | 效果 |
|---|---|
| `/compact-model` | 显示 stored/effective profile、stored/effective reasoning、reasoning source 和 timeout |
| `/compact-model <profile>` | 校验并设置 profile，不修改 reasoning |
| `/compact-model clear` | 仅清空 profile，恢复主 provider/model；保留 reasoning override |
| `/compact-model timeout <sec>` | 更新 per-attempt timeout |
| `/compact-model reasoning` | 显示 stored value、effective value 与 main/compaction source |
| `/compact-model reasoning <effort>` | 设置显式 summary reasoning；`none` 表示关闭 reasoning |
| `/compact-model reasoning inherit` | 写入 `null`，恢复继承主 agent reasoning |

未知 profile、非法 effort 或非法 timeout 必须报错且不改 settings。

与 `/compact` 的关系：

- `/compact`：触发一次压缩；
- `/compact-model`：配置生成 summary 的模型行为。

## 可观测性

每个 summary attempt/context frame 至少记录：

```text
summary_model_provider
summary_model_name
summary_model_source             # main | profile
summary_profile_name
summary_reasoning_effort          # 实际 effective value
summary_reasoning_source          # main | compaction
summary_timeout_seconds
summary_stage                     # summary | main_fallback
summary_attempt                   # 该 stage 内 1..3
summary_fallback_used             # 是否进入 main_fallback
summary_timed_out
summary_input_message_count       # filtered summary_head
summary_removed_message_count     # 原始 selection.head
summary_input_scope="removed_history_only"
```

要求：

- usage 使用实际 attempt 的 provider/model cache key；
- context frame 的 provider/model/messages 与实际 summary 调用一致；
- stage 切换和 reasoning source 在 TUI/日志可见；
- metadata 不得把 profile attempt 记成主模型；
- 不记录 API key 或凭据。

## 涉及文件

| 文件 | 责任 |
|---|---|
| `src/voidx/config/models.py` | `CompactionConfig` 与 nullable reasoning |
| `src/voidx/config/__init__.py` | 导出配置类型 |
| `src/voidx/config/settings.py` | workspace-only key 与 settings mixin 接线 |
| `src/voidx/config/settings_permissions.py` 或新 compaction settings mixin | get/set API |
| `src/voidx/agent/adapters/langgraph/runtime/compaction_coordinator.py` | 输入过滤调用、模型解析、stage/retry、timeout、usage/metadata、移除 full-context 快路径 |
| `src/voidx/llm/compaction/service.py` | summary 输入过滤/helper 或 prompt/fallback 的统一入口 |
| `src/voidx/agent/adapters/langgraph/graph_compaction.py` | 保持两参数 override；生产路径不得误走 custom override |
| `src/voidx/agent/adapters/langgraph/execution.py` | adapter 接线与兼容 wrapper |
| `src/voidx/presentation/slash/registry.py` | 注册 `/compact-model` |
| `src/voidx/presentation/slash/commands/compact_model.py` | profile/reasoning/timeout 命令 |
| `src/voidx/presentation/slash/handler.py` | mixin 接入（若当前 registry 模式需要） |
| `src/tests/test_config/test_compaction_config.py` | 配置与 settings 契约 |
| `src/tests/test_llm/compaction/test_compaction_summary_input.py` | 历史区间和注入过滤 |
| `src/tests/test_infrastructure/runtime/test_compaction_summary_model.py` | 解析、stage、timeout、usage、隔离 |
| `src/tests/test_slash/test_slash_compact_model.py` | slash 行为 |

## 实现顺序

1. 新增 `CompactionConfig`、settings API、workspace-only key 和配置测试。
2. 新增 `compaction_summary_messages(selection.head)` 及 sentinel 过滤测试。
3. 删除 full active source context summary 路径，确保 LLM/fallback/context frame 共用 filtered head。
4. 实现 effective model/reasoning 解析及状态隔离测试。
5. 实现 summary/main 两阶段重试与 per-attempt timeout。
6. 修正 usage、context frame、metadata 和状态文案。
7. 实现 `/compact-model` profile/reasoning/timeout 命令。
8. 运行 focused tests，再运行完整 backend 回归。

## 验收标准

1. **默认兼容**：零配置时直接使用主模型实例及主 reasoning，单 stage 最多 3 次。
2. **独立 profile**：有效 profile 成功时不调用主模型，不修改 `host.model`。
3. **reasoning 继承**：`reasoning_effort=null` 时，无论是否使用独立 profile，都使用主 agent 的实际 reasoning 配置。
4. **reasoning 覆盖**：可显式设置全部 `ReasoningEffort` 值；`none` 与 `inherit` 可区分。
5. **两阶段回退**：独立 profile 或 reasoning stage 失败 3 次后，回退主 agent 原模型/原 reasoning，再失败才用 deterministic fallback。
6. **可用性门闩**：`host.model is None` 但独立 profile 可用时仍能生成 summary。
7. **状态隔离**：summary 前后 `host.model` / `host.config.model` 对象及值不变。
8. **历史区间**：summary LLM、fallback 和 compaction context frame 只读取 filtered `selection.head` 与 previous summary。
9. **禁止注入**：system/runtime context、pressure/step hint、guidance、inline guide、tail/current turn sentinel 均不出现在 summary 输入。
10. **Long Summary 注入**：最终 summary 仅通过 `RuntimeContextBuilder(summary=...)` 出现在主 agent 的单个 `## Long Summary` section。
11. **超时**：每个 attempt 独立超时并进入下一 attempt/stage，不挂死。
12. **可观测性**：日志/metadata/usage 可还原实际 model、reasoning、source、stage、attempt 和输入 scope。
13. **slash**：可查看、设置、清空 profile，可设置 timeout，可设置 effort 或恢复 inherit。

## 测试与验证命令

### 配置、输入边界与 runtime

```bash
./test.py --backend -- \
  src/tests/test_config/test_compaction_config.py \
  src/tests/test_llm/compaction/test_compaction_summary_input.py \
  src/tests/test_infrastructure/runtime/test_compaction_summary_model.py \
  src/tests/test_slash/test_slash_compact_model.py -v
```

输入边界测试必须构造不同 sentinel，并断言 summary attempt 收不到：

- system/runtime sentinel；
- rendered previous `Long Summary` sentinel；
- soft/hard pressure hint sentinel；
- generic step hint sentinel；
- guidance/repair/child notice sentinel；
- inline compaction guide 与 legacy continuation sentinel；
- semantic tail 和当前请求 sentinel。

同时断言：eligible historical user/assistant/tool sentinel 与 `previous_summary` 可见。

### 现有 compaction 回归

```bash
./test.py --backend -- \
  src/tests/test_llm/compaction/test_compaction_retry.py \
  src/tests/test_infrastructure/runtime/test_call_llm_compaction.py \
  src/tests/test_infrastructure/runtime/test_compaction_flow.py -v
```

### 完整 backend

```bash
./test.py --backend
```

## 风险与残差

1. 独立模型 context window 可能与现有主模型预算不一致；本阶段只能在 filtered historical head 内裁剪，失败后回退。
2. 两阶段全部打满时，等待上界约为 `6 × timeout_seconds`。
3. 排除运行中 guidance 是有意的输入边界；其已产生的 durable outcome 仍可从 historical assistant/tool messages 进入 summary。
4. deterministic fallback 的质量低于 LLM summary，但必须遵守同一 historical-only 输入边界。
5. 本阶段不在 `/usage` 中新增独立 summary 分组，但底层 usage 记录必须使用实际模型。

## 决策记录

| 决策 | 选择 | 原因 |
|---|---|---|
| summary 身份 | coordinator 内部行为，不注册隐藏 agent | 与当前 agent architecture 一致 |
| profile 默认 | 空值继承主 provider/model | 零配置兼容 |
| reasoning 默认 | `null` 显式继承主 agent | 避免错误落入 `ModelConfig` 默认 `xhigh` |
| `none` 语义 | 显式关闭 reasoning | 与 inherit 状态可区分 |
| settings 作用域 | workspace-only | 对齐现有 workspace 行为 |
| retry | summary stage ≤3，再 main fallback ≤3 | fail-open 且优先使用用户配置 |
| fallback reasoning | 主 agent 原 reasoning | fallback 必须还原主模型完整配置 |
| callback | 保留两参数 override seam | 避免无必要扩大 adapter 契约 |
| Long Summary 来源 | previous summary + filtered removed head | 只表示已移除历史轮次 |
| guidance/pressure/runtime 注入 | 全部排除 | 它们是控制上下文，不是历史语义轮次 |
| full source context 快路径 | 删除 | 会跨过 head/tail 边界并污染 summary |
| 状态隔离 | 临时模型不写回 host | 防止影响后续主对话 |

## 开放问题

无实现前必须再次决策的开放问题。

可选后续：

1. 前端 settings 增加 compaction profile/reasoning 控件；
2. 为 summary 单独配置 max_tokens/context window；
3. `/usage` 增加 summary 模型分组；
4. 按独立模型 context window 重算 filtered head 预算。
