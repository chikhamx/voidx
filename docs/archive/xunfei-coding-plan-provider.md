> **Status: Done**

# Xunfei Astron Coding Plan Provider — 技术设计文档

## Context

讯飞星辰 MaaS 平台推出的 Astron Coding Plan（星火编码计划）是一个面向 AI 编程场景的 API 套餐服务。它使用 **标准的 OpenAI 兼容协议**（Chat Completions API），统一模型 ID `astron-code-latest`，底层模型可通过后台切换（支持 GLM-5、DeepSeek-V4、Qwen3.5-397B、Kimi-K2.6、MiniMax-M2.5 等国产旗舰模型）。

定价非常低（¥3.9 起/月，按请求次数而非 token 计费），且完全兼容 OpenAI 生态工具。将其作为内置供应商接入 voidx，可以让用户直接通过 profile 配置使用，无需手动配置自定义供应商。

## Goals and Non-Goals

### Goals

- 用户可在 profile 中将 provider 设为 `xunfei-coding-plan`，model 设为 `astron-code-latest`，配置 api_key 即可使用
- 走 `openai` 协议（标准 `ChatOpenAI`），作为 OpenAI 兼容代理接入
- 提供合理的默认 base_url
- 供应商名出现在 `/model` 等 slash 命令的候选列表中

### Non-Goals

- 不支持通过 Anthropic 协议接入（Coding Plan 虽也提供 Anthropic 协议 endpoint，但 voidx 目前没有通用的 Anthropic 兼容 provider 抽象，且 Coding Plan 的核心是 OpenAI 协议）
- 不处理 Coding Plan 套餐订阅/购买流程（用户在讯飞控制台自行完成）
- 不添加讯飞原生 Spark API（WebSocket 协议）的支持——那是另一个供应商

## Architecture

Coding Plan 是标准的 OpenAI Chat Completions API 代理，接入方式与 OpenRouter 类似：

```
用户 profile
  └─ provider = "xunfei-coding-plan"
     └─ _PROVIDER_PROTOCOLS lookup  →  "openai"
        └─ ChatOpenAI
           ├─ base_url = "https://maas-coding-api.cn-huabei-1.xf-yun.com/v2"
           ├─ model = "astron-code-latest"
           └─ api_key = 用户从讯飞控制台获取的 Coding Plan Key
```

选用 `openai` 协议的考量：
- Coding Plan 的 Routing 层 /v2 端点是纯 OpenAI 兼容，响应格式与 OpenAI 一致
- 不需要 `DeepSeekChatOpenAI` 的 provider 特定 thinking 映射——Coding Plan 后台决定底层模型，thinking 走标准 `reasoning` effort 格式
- 自动获得 `_strip_stainless_headers()` 清理，有利于第三方代理兼容性
- `_openai_reasoning_kwargs` 对非 `openai`/`openrouter` 供应商走通用路径：当模型名匹配推理模型前缀时注入 `extra_body.reasoning.effort`

## Data Model

无新增数据模型。仅在三处字典中添加条目：

```
_PROVIDER_PROTOCOLS
  └── "xunfei-coding-plan" → "openai"

_DEFAULT_BASE_URLS
  └── ("xunfei-coding-plan", "openai") → "https://maas-coding-api.cn-huabei-1.xf-yun.com/v2"

STATIC_MODELS
  └── "xunfei-coding-plan" → ["astron-code-latest"]
```

## API Contract

### 对外表现

```yaml
# config/profile.yaml 示例
provider: xunfei-coding-plan
model: astron-code-latest
api_key: YOUR_CODING_PLAN_API_KEY
# base_url 可选，默认使用内置值
```

### 内部函数变更

| 函数/字典 | 变更类型 | 说明 |
|-----------|---------|------|
| `_PROVIDER_PROTOCOLS` | 新增条目 | `"xunfei-coding-plan": "openai"` |
| `_DEFAULT_BASE_URLS` | 新增条目 | 指向 Coding Plan v2 端点 |
| `get_context_limit()` | 新增条目 | 92160（文档标注值） |
| `STATIC_MODELS` | 新增条目 | `["astron-code-latest"]` |
| `_STATIC_PROVIDERS` | 新增条目 | 显示在 slash 命令列表中 |

### Reasoning 策略

`_openai_reasoning_kwargs` 对 `xunfei-coding-plan` 的处理：
- 供应商不属于 `openai` / `openrouter`，走第三方通用分支
- 如果模型名匹配推理前缀（`gpt-5`/`o1`/`o3`/`o4`）→ 注入 `{"extra_body": {"reasoning": {"effort": ...}}}`
- 否则（`astron-code-latest` 不匹配）→ 不注入 reasoning 参数，由后端模型自行决定

无需修改 `_openai_reasoning_kwargs`，现有逻辑已覆盖。

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| API Key 无效/过期 | OpenAI 标准 401 返回，voidx 透传错误信息提示用户检查 Coding Plan 套餐状态 |
| 套餐流量耗尽 | 服务端返回 429/403，voidx 透传提示用户升级套餐或等待额度重置 |
| 底层模型切换未生效 | 控制台切换后 1-3 分钟生效，期间请求可能返回模型不存在错误——用户重试即可 |
| 网络超时 | 继承 `httpx.AsyncClient(timeout=15.0)` 默认行为，可升级 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 供应商名 `xunfei-coding-plan` | `xunfei`、`astron`、`xunfei-astron` | 与讯飞未来可能的普通 Spark API 区分；明确是 Coding Plan 套餐；符合现有供应商命名风格 |
| 协议走 `openai` | 走 `deepseek` 协议使用 `DeepSeekChatOpenAI` | Coding Plan 是纯 OpenAI 兼容代理，没有 provider 特定的 thinking 格式；用 `openai` 协议更简洁，自动获得 stainless header 清理和标准 reasoning effort 格式 |
| 不添加 reasoning 分支 | 添加专用分支 | 底层模型可变，静态映射无法覆盖所有场景；fallback 逻辑已够用 |
| base_url 用 `/v2` | 去掉 `/v2` 或将 `/v2` 改为 `/v1` | Coding Plan 明确使用 `/v2` 路径（非标准 OpenAI `/v1`），必须保留 |

## Open Questions

- [ ] Coding Plan 的上下文窗口 92160 是否对所有底层模型一致？文档标注 92160，但某些模型（如 GLM-5）原生支持 1M+——实际使用时 Coding Plan 网关可能截断。
