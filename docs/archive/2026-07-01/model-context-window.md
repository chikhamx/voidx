> **Status: Done** — 实现已完成并提交（commit 6351817），245 个测试通过。

# ModelConfig.context_window — 技术设计文档

## Context

voidx 当前的上下文窗口（context window）大小是**按 provider 写死**在 `src/voidx/llm/provider.py:587` 的 `get_context_limit()` 函数里的一张静态表，用户无法覆盖。

这带来两个问题：
1. 同一个 provider 下不同模型的实际上下文窗口可能差异很大（例如 OpenAI 的 gpt-4o 是 128K，gpt-4.1 是 1M，但表里 openai 统一记为 1,050,000）。
2. 自定义 provider / 自建端点无法表达自己的上下文窗口，只能用默认的 128,000，导致压缩（compaction）触发时机不准——要么过早压缩浪费 token，要么过晚压缩被 provider 拒绝超长请求。

本设计加一个可选的 `context_window` 字段到 `ModelConfig`，并新增 `/model ctx` 子命令让用户通过选项框（128k/256k/384k/512k/1M）动态设置并**持久化**覆盖默认值。字段为 `None` 时保持原有 provider 查表行为，完全向后兼容。

### 持久化现状（重要背景）

调研发现：`Profile`（`src/voidx/config/models.py:9`）只存 `name/provider/model/api_key/base_url/protocol`，**不存** `max_tokens`/`reasoning_effort`/`context_window`。`save_profile`（`settings.py:215`）也只持久化这几个字段到 SQLite。`_model_reasoning` 改完 `reasoning_effort` 后**不调 `save_profile`**——这些 ModelConfig 数值字段目前根本不持久化，每次重启回默认值。

因此 `context_window` 的持久化不能走 `Profile`/`save_profile` 路径，而要走 `_set_setting(key, value)`——它往配置文件写一个顶层键值对（JSON），`build_config` 启动时读取。这和 `parallel_subagents`、`ask_compact` 等设置的持久化方式一致。

## Goals and Non-Goals

### Goals

- `ModelConfig` 新增 `context_window: int | None` 字段，默认 `None`。
- `get_context_limit()` 能感知该字段：传入有效值（>0）时直接返回，否则走原 provider 查表。
- 所有 3 个调用点（`wiring.build_compaction_service`、`run_loop`、`slash/model._sync_context_limit`）统一传入 `config.model.context_window`。
- 现有 `get_context_limit("gemini")` 这类只传 provider 的调用方式保持兼容。
- 新增 `/model ctx` 子命令：弹出选项框（128k/256k/384k/512k/1M + Auto），用户选择后写入 `config.model.context_window` 并通过 `_set_setting("context_window", value)` 持久化到配置文件。
- `build_config` 启动时从配置文件读 `context_window` 键，注入 `ModelConfig`。
- 现有测试不破坏，新增覆盖优先级测试和 `/model ctx` 流程测试。

### Non-Goals

- **不**改 `build_config` 对其他 `ModelConfig` 数值字段（`max_tokens`/`reasoning_effort`/`temperature`）的加载路径——只给 `context_window` 开持久化，因为本次有明确的 `/model ctx` 交互需求；其他字段是否持久化是独立决策。
- **不**做按模型名（model id）自动推断上下文窗口的逻辑（那需要维护一张模型→窗口的映射表，超出本次范围）。
- **不**把 `context_window` 存进 `Profile`/SQLite——它走配置文件顶层键（`_set_setting`），与 `parallel_subagents` 等设置一致，避免给 `Profile` 加字段引发迁移。

## Architecture

### 数据流

```
配置文件 / 运行时设置
        │
        ▼
ModelConfig.context_window (int | None)
        │
        ▼
get_context_limit(provider, protocol, context_window)
        │
        ├─ context_window 有效 (>0) → 直接返回 context_window
        │
        └─ context_window 为 None  → 走原 provider 查表（向后兼容）
        │
        ▼
CompactionService.context_limit
        │
        ├─ usable_window() = context_limit - COMPACTION_BUFFER - output_token_max
        ├─ soft_threshold() = min(context_limit * 0.75, usable_window)
        └─ 触发 compaction 当 used >= context_limit * 0.90
```
┌─────────────────────────────────────────────────────────┐
│  设置路径                                                │
│                                                          │
│  /model ctx  ──► 选项框 (128k/256k/384k/512k/1M/Auto)   │
│       │                                                  │
│       ▼                                                  │
│  config.model.context_window = <值或 None>               │
│       │                                                  │
│       ├─► _set_setting("context_window", value)  ──► 配置文件 (持久化)
│       │
│       └─► _sync_context_limit()  ──► 立即生效
│
│  启动路径                                                │
│                                                          │
│  build_config()  ──► 读配置文件 context_window 键        │
│       │                                                  │
│       ▼                                                  │
│  ModelConfig(context_window=<值或 None>)                 │
└─────────────────────────────────────────────────────────┘
```

### `/model ctx` 交互流程

仿照 `_model_reasoning`（`slash/model.py:318`）的模式：

1. **分发**：`_dispatch_model`（`handler.py:132`）加 `elif args == "ctx" or args.startswith("ctx ")` 分支，调 `self._model_ctx(target)`。
2. **选项框**：用 `_select_from_list(app, "Context window", choices)` 弹出箭头选择。选项：
   - `Auto`（对应 `None`，走 provider 查表）
   - `128k` → 128_000
   - `256k` → 256_000
   - `384k` → 384_000
   - `512k` → 512_000
   - `1M`   → 1_000_000
3. **直接传值**：`/model ctx 256k` 可跳过选项框直接设置（与 `/model reasoning high` 一致）。
4. **生效**：写 `config.model.context_window`，调 `_sync_context_limit()` 立即刷新状态栏和压缩阈值。
5. **持久化**：调 `self.host.settings._set_setting("context_window", value)` 写配置文件。选 `Auto` 时用 `_pop_setting("context_window")` 移除键。
6. **Usage 提示**：`_list_models` 的 Usage 行（`model.py:193`）更新为 `Usage: /model list|new|reasoning|ctx|test|del|switch|<name>`。

### 调用点改动

| 文件 | 行 | 改动前 | 改动后 |
|------|----|--------|--------|
| `src/voidx/llm/provider.py` | 587 | `def get_context_limit(provider, protocol="")` | `def get_context_limit(provider, protocol="", context_window=None)` |
| `src/voidx/llm/service.py` | 24 | `def get_context_limit(provider_name, protocol="")` | `def get_context_limit(provider_name, protocol="", context_window=None)` |
| `src/voidx/agent/graph/wiring.py` | 77 | `get_context_limit(config.model.provider)` | `get_context_limit(config.model.provider, config.model.protocol or "", config.model.context_window)` |
| `src/voidx/agent/graph/run_loop.py` | 172 | `get_context_limit(self.config.model.provider)` | `get_context_limit(self.config.model.provider, self.config.model.protocol or "", self.config.model.context_window)` |
| `src/voidx/agent/slash/model.py` | 415 | `get_context_limit(self.host.config.model.provider)` | `get_context_limit(self.host.config.model.provider, self.host.config.model.protocol or "", self.host.config.model.context_window)` |
| `src/voidx/agent/slash/handler.py` | 132 | （无 ctx 分支） | `elif args == "ctx" or args.startswith("ctx "): target = args.removeprefix("ctx").strip(); await self._model_ctx(target)` |
| `src/voidx/agent/slash/model.py` | 新增 | （无） | 新增 `async def _model_ctx(self, target: str)` 方法：选项框/直接传值 → 写 `config.model.context_window` → `_sync_context_limit()` → `_set_setting`/`_pop_setting` 持久化 |
| `src/voidx/agent/slash/model.py` | 193 | `Usage: /model list\|new\|reasoning\|test\|del\|switch\|<name>` | `Usage: /model list\|new\|reasoning\|ctx\|test\|del\|switch\|<name>` |
| `src/voidx/config/settings.py` | 315 | `cfg = ModelConfig(provider=provider, model=model, base_url=base_url)` | `cfg = ModelConfig(provider=provider, model=model, base_url=base_url, context_window=self._effective_data().get("context_window"))` |

## Data Model

```
ModelConfig (src/voidx/config/models.py:24)
├── provider: str = "anthropic"
├── model: str = "claude-sonnet-4-6"
├── base_url: str | None = None
├── protocol: str | None = None
├── temperature: float = Field(default=0.3, ge=0.0, le=2.0)
├── max_tokens: int = Field(default=8192, ge=1, le=128000)
├── reasoning_effort: str | None = Field(default="xhigh")
└── context_window: int | None = Field(           # ← 新增
        default=None,
        ge=1,
        description="Override context window size in tokens. None = auto-detect by provider.",
    )
```

约束：`ge=1`（正整数），`None` 表示自动推断。不设上界——某些模型支持 1M+，硬上限会误伤。

## API Contract

### `get_context_limit`

- **Signature**: `get_context_limit(provider: str, protocol: str = "", context_window: int | None = None) -> int`
- **行为**:
  - 若 `context_window` 不为 `None` 且 `> 0`，直接返回 `context_window`。
  - 否则走原有 provider 查表逻辑（`limits` dict → protocol 回退 → 128,000 默认）。
- **向后兼容**: 现有 `get_context_limit("gemini")`、`get_context_limit(provider, protocol)` 调用全部不受影响（新参数有默认值 `None`）。

### 配置示例（配置文件）

```json
{
  "model": {
    "provider": "openai",
    "model": "gpt-4o",
    "context_window": 128000
  }
}
```

> 注：本次 `build_config` 已支持从配置文件读取 `context_window` 键并注入 `ModelConfig`（见调用点改动表 settings.py:315）。其他 `ModelConfig` 数值字段（`max_tokens`/`temperature`/`reasoning_effort`）的配置文件加载仍留作后续，是否对称支持是独立决策。

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| `context_window` 设为 0 或负数 | Pydantic `ge=1` 约束在加载时拒绝；运行时直接赋值 0 会被 `get_context_limit` 当作"无效"走回退（`> 0` 判断） |
| `context_window` 远超模型实际支持 | 不做校验——用户自负。后果是压缩不及时、请求被 provider 拒绝超长。状态栏会显示该值，用户可观察到 |
| `context_window` 设得过小 | 频繁触发 compaction，浪费 token 和延迟。同样不做校验，由用户调整 |
| 运行时改了 `context_window` 但没调 `_sync_context_limit` | 状态栏/压缩逻辑用旧值。`/model` 命令切换模型时已调 `_sync_context_limit`，会自动刷新 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 用 `int \| None`，`None` 走回退 | 用 `0` 表示"自动" | `None` 语义更清晰，避免 0 与"无效输入"混淆；Pydantic 里 `ge=1` 也能挡住 0 |
| 不设 `le` 上界 | 设 `le=2_000_000` | 模型上下文窗口在持续增长，硬上限会很快过时；用户自负 |
| `get_context_limit` 加可选参数而非改签名 | 改成接收 `ModelConfig` 整体 | 加可选参数保持向后兼容，现有 `get_context_limit("gemini")` 调用不破坏 |
| 调用点顺带传入 `protocol` | 维持现状只传 provider | 现有 3 个调用点都只传 provider，导致自定义 provider 拿不到 protocol 回退（`get_context_limit` 在 provider 不在表里时回退到 protocol 判断，见 `provider.py:606-607`）。本次改动顺带传入 `config.model.protocol or ""`，修复了这个遗漏；属于行为改进而非破坏性变更 |
| `/model ctx` 用独立子命令而非塞进 `_model_new` 流程 | 在 `_model_new` 加一个提示步骤 | 独立子命令更轻量，用户随时可改不用重走整个配置流程；与 `/model reasoning` 模式一致 |
| 持久化走 `_set_setting` 而非 `Profile`/SQLite | 给 `Profile` 加 `context_window` 字段 | `Profile` 目前只存连接相关字段（provider/model/key/base_url/protocol），加数值字段会引发 schema 迁移；`_set_setting` 是现有的顶层键持久化机制，`parallel_subagents` 等设置都用它 |
| 只给 `context_window` 开 `build_config` 读取 | 对称支持所有 `ModelConfig` 数值字段 | 本次有明确的 `/model ctx` 交互需求；其他字段（`max_tokens` 等）是否持久化是独立决策，避免范围蔓延 |
| 选项框含 `Auto` 选项 | 只给固定档位 | 用户需要能恢复"按 provider 自动推断"的默认行为；`Auto` 对应 `None` + `_pop_setting` 移除键 |

## Open Questions

- [x] 是否需要在 `/model` 交互流程里加一个可选的"设置上下文窗口"步骤？→ **已纳入本次**：`/model ctx` 子命令。
- [ ] `build_config` 是否应该统一支持从配置文件加载所有 `ModelConfig` 数值字段（`max_tokens`/`temperature`/`reasoning_effort`）？本次只给 `context_window` 开了持久化，其他字段是否对称支持是独立决策。
- [ ] 选项框的选项列表（128k/256k/384k/512k/1M）是否需要可配置？当前硬编码在 `_model_ctx` 里，未来可考虑从配置读。
