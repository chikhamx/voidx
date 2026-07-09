# Gemini Provider 集成设计

> **Status: Done**

## 问题

`create_chat_model()` 在 `protocol == "gemini"` 时直接抛出 `NotImplementedError`，用户无法使用 Google Gemini 原生 API。当前 Gemini 模型只能通过 OpenAI 兼容协议（如 OpenRouter）间接访问，无法使用 Gemini 特有的功能（如 Google Search grounding、原生 thinking block）。

## 目标

1. 支持 `provider: "gemini"` + `protocol: "gemini"` 的原生 API 调用
2. 支持 Gemini 的 thinking/reasoning 参数映射
3. 支持 Gemini thinking block 的流式提取
4. 在 `get_context_limit()` 中注册 Gemini 的上下文窗口
5. 保持与现有 provider 一致的接口和错误处理风格

## 当前架构

### 关键文件

| 文件 | 职责 |
|------|------|
| `src/voidx/llm/provider.py` | 协议解析、模型工厂、reasoning kwargs、thinking 提取、context limit |
| `src/voidx/config/settings.py` | `ModelConfig` 定义（provider, protocol, model, api_key 等） |
| `src/voidx/llm/catalog.py` | 静态模型列表和动态模型获取 |

### 协议解析

```python
_PROVIDER_PROTOCOLS: dict[str, str] = {
    "anthropic": "anthropic",
    "deepseek": "anthropic",
    "openai": "openai",
    # ... 共 11 个 provider，映射到 anthropic / openai 两个协议
}
```

### 模型工厂

```python
def create_chat_model(api_key, config: ModelConfig) -> BaseChatModel:
    protocol = resolve_protocol(config)
    if protocol == "anthropic":
        return ChatAnthropic(**kwargs)
    if protocol == "openai":
        return ChatOpenAI(**kwargs)
    if protocol == "gemini":
        raise NotImplementedError(...)  # ← 当前卡点
```

### Reasoning Kwargs 模式

每个协议有独立的 reasoning kwargs 函数：

| 协议 | 函数 | 参数风格 |
|------|------|----------|
| anthropic | `_anthropic_reasoning_kwargs()` | `thinking.type` + `budget_tokens` / `effort` |
| openai | `_openai_reasoning_kwargs()` | `reasoning_effort` 字符串 |
| mimo | `_mimo_reasoning_kwargs()` | `thinking.type` enabled/disabled |
| doubao | `_doubao_reasoning_kwargs()` | `extra_body.thinking.type` |

### Thinking 提取

```python
def extract_thinking(chunk: AIMessageChunk, protocol: str) -> str:
    if protocol == "anthropic":
        return _extract_thinking_anthropic(chunk)
    if protocol == "openai":
        return _extract_thinking_openai(chunk)
    return _extract_thinking_anthropic(chunk) or _extract_thinking_openai(chunk)
```

## 设计

### 1. 依赖引入

使用 `langchain-google-genai` 包的 `ChatGoogleGenerativeAI`（≥4.0.0，4.0 重写了底层实现，支持 Gemini Developer API 和 Vertex AI）：

```python
# pyproject.toml 新增可选依赖
[project.optional-dependencies]
gemini = ["langchain-google-genai>=4.0.0"]
```

在 `provider.py` 中延迟导入，导入失败时给出安装提示：

```python
if protocol == "gemini":
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
    except ImportError:
        raise ImportError(
            "langchain-google-genai is required for Gemini protocol. "
            "Install with: pip install voidx[gemini]"
        )
```

### 2. 协议注册

```python
_PROVIDER_PROTOCOLS["gemini"] = "gemini"
```

注意：不在 `_DEFAULT_BASE_URLS` 中注册 `("gemini", "gemini")`。Gemini SDK 使用自己的端点发现机制，无需自定义 base_url。`_DEFAULT_BASE_URLS.get(("gemini", "gemini"))` 返回 None，工厂函数中 `if base_url:` 判断为 False，不会传入无效的空字符串。

### 3. Gemini Reasoning Kwargs

`ChatGoogleGenerativeAI` 的 thinking 参数按模型代际分为两种：

| 模型代际 | 参数 | 值 |
|----------|------|-----|
| Gemini 3+ | `thinking_level` | `"minimal"`, `"low"`, `"medium"`, `"high"` |
| Gemini 2.5 | `thinking_budget` | `0`（关闭）, `-1`（动态）, 正整数（token 上限） |

此外，要看到 thinking 内容需要设置 `include_thoughts=True`。

```python
def _gemini_reasoning_kwargs(config: ModelConfig) -> dict:
    effort = _normalized_effort(config.reasoning_effort)
    if effort in (None, "none"):
        return {}
    kwargs: dict = {"include_thoughts": True}

    # 根据模型名判断代际
    model_lower = config.model.lower()
    if _is_gemini3_plus(model_lower):
        # Gemini 3+: thinking_level
        level_map = {
            "minimal": "minimal",
            "low": "low",
            "medium": "medium",
            "high": "high",
            "xhigh": "high",
            "max": "high",
        }
        kwargs["thinking_level"] = level_map.get(effort, "medium")
    else:
        # Gemini 2.5: thinking_budget
        budget_map = {
            "minimal": 1_024,
            "low": 4_096,
            "medium": 8_192,
            "high": 16_384,
            "xhigh": 32_768,
            "max": 65_536,
        }
        kwargs["thinking_budget"] = budget_map.get(effort, 8_192)

    return kwargs


def _is_gemini3_plus(model: str) -> bool:
    """判断是否为 Gemini 3+ 模型（使用 thinking_level 而非 thinking_budget）。"""
    return any(model.startswith(p) for p in ("gemini-3", "gemini-4"))
```

在 `_reasoning_kwargs()` 中添加分支：

```python
def _reasoning_kwargs(config: ModelConfig, protocol: str) -> dict:
    if protocol == "anthropic":
        ...
    if protocol == "openai":
        ...
    if protocol == "gemini":
        return _gemini_reasoning_kwargs(config)
    return {}
```

注意：`_normalized_effort()` 当前不识别 `"auto"` 值，会 fallback 到 `"medium"`。Gemini 2.5 的动态 thinking（`thinking_budget=-1`）暂不映射，因为用户可以通过设置 `reasoning_effort: "medium"` 获得等效行为。如需支持，需在 `_normalized_effort()` 中新增 `"auto"` 识别。

### 4. Gemini Thinking 提取

根据 `langchain-google-genai` 源码（`chat_models.py`），Gemini 的 thinking block 在 `AIMessageChunk.content` 中以两种格式出现：

**v0 格式**（`output_version` 默认）：
```python
{"type": "thinking", "thinking": "思考内容文本", "signature": "..."}
```

**v1 格式**（`output_version="v1"`）：
```python
{"type": "reasoning", "reasoning": "思考内容文本", "extras": {"signature": "..."}}
```

这两种 type 都已在 `_THINKING_BLOCK_TYPES` 中注册（`"thinking"` 和 `"reasoning"`），且 `_extract_reasoning_blocks()` 会遍历 `content` 列表提取 `type` 匹配的 block。因此：

**现有通用提取逻辑已覆盖 Gemini 的 thinking block，无需编写专门的 `_extract_thinking_gemini()` 函数。**

只需在 `extract_thinking()` 中将 gemini 协议路由到通用 fallback 路径：

```python
def extract_thinking(chunk: AIMessageChunk, protocol: str) -> str:
    if protocol == "anthropic":
        return _extract_thinking_anthropic(chunk)
    if protocol == "openai":
        return _extract_thinking_openai(chunk)
    if protocol == "gemini":
        # Gemini thinking blocks (type="thinking"/"reasoning") 已被
        # _extract_reasoning_blocks 覆盖，走通用提取路径
        return _extract_thinking_anthropic(chunk) or _extract_thinking_openai(chunk)
    return _extract_thinking_anthropic(chunk) or _extract_thinking_openai(chunk)
```

### 5. Context Limit

```python
limits: dict[str, int] = {
    ...
    "gemini": 1_000_000,  # Gemini 2.5 Pro/Flash: 1M tokens
}
```

注：Gemini 1.5 Pro 为 2M，但当前其他 provider 也不按模型细分，取主流值即可。

### 6. 模型工厂

```python
if protocol == "gemini":
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
    except ImportError:
        raise ImportError(
            "langchain-google-genai is required for Gemini protocol. "
            "Install with: pip install voidx[gemini]"
        )
    kwargs = dict(
        model=config.model,
        temperature=config.temperature,
        max_tokens=config.max_tokens,  # ChatGoogleGenerativeAI 接受 max_tokens
    )
    if api_key:
        kwargs["api_key"] = api_key  # 参数名为 api_key（非 google_api_key）
    if base_url:
        kwargs["base_url"] = base_url
    kwargs.update(_reasoning_kwargs(config, protocol))
    return ChatGoogleGenerativeAI(**kwargs)
```

参数名说明（基于 `langchain-google-genai` 4.x 文档和源码验证）：

| 参数 | 说明 |
|------|------|
| `api_key` | 4.x 统一使用 `api_key`，同时兼容 `GOOGLE_API_KEY` / `GEMINI_API_KEY` 环境变量 |
| `max_tokens` | 4.x 接受 `max_tokens`（内部映射为 `max_output_tokens`） |
| `temperature` | 直接传递 |
| `model` | 模型名，如 `"gemini-2.5-flash"` |
| `thinking_budget` | Gemini 2.5 的 thinking token 上限 |
| `thinking_level` | Gemini 3+ 的 thinking 级别 |
| `include_thoughts` | 设为 `True` 以在响应中包含 thinking 内容 |

⚠️ **已知问题**：`langchain-google-genai` 4.1.1 存在 `max_output_tokens` 在构造函数中设置但不生效的 bug（[issue #1454](https://github.com/langchain-ai/langchain-google/issues/1454)），需在 `.invoke()` 中传入才有效。实现时需关注此问题的修复进展，必要时在工厂函数中添加 workaround。

## 实现计划

### Step 1: 添加依赖

- `pyproject.toml` 添加 `langchain-google-genai>=4.0.0` 到可选依赖
- 验证 `ChatGoogleGenerativeAI` 的构造参数名（✅ 已验证，见上方参数表）

### Step 2: 注册协议

- `provider.py`: `_PROVIDER_PROTOCOLS` 添加 `"gemini": "gemini"`
- 不在 `_DEFAULT_BASE_URLS` 中注册（Gemini SDK 自行管理端点）

### Step 3: 实现 reasoning kwargs

- `provider.py`: 新增 `_is_gemini3_plus()` 和 `_gemini_reasoning_kwargs()`
- `provider.py`: `_reasoning_kwargs()` 添加 gemini 分支

### Step 4: 实现 thinking 提取

- `provider.py`: `extract_thinking()` 添加 gemini 分支，路由到通用 fallback 路径
- 无需新增专门的提取函数（✅ 已验证，Gemini thinking block 格式被现有通用逻辑覆盖）

### Step 5: 实现模型工厂

- `provider.py`: `create_chat_model()` 替换 `NotImplementedError` 为 `ChatGoogleGenerativeAI` 构造
- 包含 `try/except ImportError` 安装提示

### Step 6: 注册 context limit

- `provider.py`: `get_context_limit()` 添加 `"gemini": 1_000_000`

### Step 7: 测试

- 新增 `tests/test_llm/test_gemini_provider.py`
- 测试协议解析、reasoning kwargs 映射（2.5 和 3+ 两种路径）、thinking 提取、context limit
- Mock `ChatGoogleGenerativeAI` 避免真实 API 调用

## 边界情况

| 场景 | 行为 |
|------|------|
| 用户未安装 `langchain-google-genai` | 延迟导入失败，抛出 `ImportError` 并提示安装 `voidx[gemini]` |
| Gemini API key 未配置 | `ChatGoogleGenerativeAI` 自身会抛出认证错误（检查 `GOOGLE_API_KEY` / `GEMINI_API_KEY` 环境变量） |
| Gemini 模型不支持 thinking | `_gemini_reasoning_kwargs()` 返回空 dict，不影响正常调用 |
| 用户通过 OpenRouter 使用 Gemini | 走 `openai` 协议，不受此变更影响 |
| `max_tokens` 在构造函数中不生效 | 已知 4.1.1 bug，关注上游修复；暂不添加 workaround |
| Gemini 3+ 模型误用 `thinking_budget` | `_is_gemini3_plus()` 按模型名前缀判断，3+ 模型使用 `thinking_level` |

## Non-goals

- 不实现 Gemini 特有的 Google Search grounding / URL context 等扩展功能
- 不实现 Gemini 的安全设置（safety settings）自定义
- 不修改 `catalog.py` 中的静态模型列表（Gemini 模型列表变化快，建议仅用动态获取）
- 不实现多模态（图片/视频）输入的特殊处理
- 不在 `_normalized_effort()` 中新增 `"auto"` 识别（可后续迭代）

## 验收标准

- [ ] `provider="gemini"` + `protocol="gemini"` 可成功创建 `ChatGoogleGenerativeAI` 实例
- [ ] `_gemini_reasoning_kwargs()` 正确映射 effort 到 Gemini 2.5 `thinking_budget` 和 3+ `thinking_level`
- [ ] `extract_thinking()` 可从 Gemini chunk 中提取 thinking 文本（通过通用 fallback 路径）
- [ ] `get_context_limit("gemini")` 返回 `1_000_000`
- [ ] 未安装 `langchain-google-genai` 时给出清晰的安装提示
- [ ] 通过 OpenRouter 使用 Gemini 的现有路径不受影响
- [ ] 新增测试覆盖以上所有场景
