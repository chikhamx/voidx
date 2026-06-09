# Gemini Provider 集成设计

> **Status: Draft**

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

使用 `langchain-google-genai` 包的 `ChatGoogleGenerativeAI`：

```python
# pyproject.toml 新增可选依赖
[project.optional-dependencies]
gemini = ["langchain-google-genai>=2.1.0"]
```

在 `provider.py` 中延迟导入：

```python
def create_chat_model(api_key, config: ModelConfig) -> BaseChatModel:
    ...
    if protocol == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        ...
```

### 2. 协议注册

```python
_PROVIDER_PROTOCOLS["gemini"] = "gemini"

_DEFAULT_BASE_URLS[("gemini", "gemini")] = ""  # 使用 SDK 默认端点
```

### 3. Gemini Reasoning Kwargs

Gemini 的 thinking 模式通过 `thinkingConfig` 控制：

```python
def _gemini_reasoning_kwargs(config: ModelConfig) -> dict:
    effort = _normalized_effort(config.reasoning_effort)
    if effort in (None, "none"):
        return {}
    # Gemini 使用 thinkingBudget (token 数) 或动态模式
    if effort == "auto":
        return {"thinking": {"type": "adaptive"}}
    budget_map = {
        "minimal": 1_024,
        "low": 4_096,
        "medium": 8_192,
        "high": 16_384,
        "xhigh": 32_768,
        "max": 65_536,
    }
    budget = budget_map.get(effort, 8_192)
    return {"thinking": {"type": "enabled", "budget_tokens": budget}}
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

### 4. Gemini Thinking 提取

Gemini 的 thinking block 在 `AIMessageChunk` 中的位置取决于 `langchain-google-genai` 的实现。根据 LangChain 惯例，thinking 内容通常出现在 `additional_kwargs` 或 `content` 的特定 type block 中。

```python
def _extract_thinking_gemini(chunk: AIMessageChunk) -> str:
    parts: list[str] = []
    # 1. 尝试 content 中的 thinking block
    content_text = _extract_reasoning_blocks(chunk.content)
    if content_text:
        parts.append(content_text)
    # 2. 尝试 additional_kwargs
    extra = chunk.additional_kwargs
    if isinstance(extra, dict):
        for key in ("thinking", "thought"):
            text = _extract_reasoning_text(extra.get(key))
            if text:
                parts.append(text)
    return "".join(parts)
```

在 `extract_thinking()` 中添加分支：

```python
def extract_thinking(chunk: AIMessageChunk, protocol: str) -> str:
    if protocol == "anthropic":
        return _extract_thinking_anthropic(chunk)
    if protocol == "openai":
        return _extract_thinking_openai(chunk)
    if protocol == "gemini":
        return _extract_thinking_gemini(chunk)
    return _extract_thinking_anthropic(chunk) or _extract_thinking_openai(chunk)
```

### 5. Context Limit

```python
limits: dict[str, int] = {
    ...
    "gemini": 1_000_000,  # Gemini 2.5 Pro: 1M tokens
}
```

### 6. 模型工厂

```python
if protocol == "gemini":
    from langchain_google_genai import ChatGoogleGenerativeAI
    kwargs = dict(
        model=config.model,
        temperature=config.temperature,
        max_output_tokens=config.max_tokens,
    )
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url
    kwargs.update(_reasoning_kwargs(config, protocol))
    return ChatGoogleGenerativeAI(**kwargs)
```

注意：`ChatGoogleGenerativeAI` 使用 `max_output_tokens` 而非 `max_tokens`，且 `api_key` 参数名可能不同（`google_api_key`），需要根据实际 SDK 版本确认。

## 实现计划

### Step 1: 添加依赖

- `pyproject.toml` 添加 `langchain-google-genai>=2.1.0` 到可选依赖
- 验证 `ChatGoogleGenerativeAI` 的构造参数名

### Step 2: 注册协议

- `provider.py`: `_PROVIDER_PROTOCOLS` 添加 `"gemini": "gemini"`
- `provider.py`: `_DEFAULT_BASE_URLS` 添加 `("gemini", "gemini")` 条目

### Step 3: 实现 reasoning kwargs

- `provider.py`: 新增 `_gemini_reasoning_kwargs()`
- `provider.py`: `_reasoning_kwargs()` 添加 gemini 分支

### Step 4: 实现 thinking 提取

- `provider.py`: 新增 `_extract_thinking_gemini()`
- `provider.py`: `extract_thinking()` 添加 gemini 分支
- 需要实际测试确认 thinking block 在 chunk 中的位置

### Step 5: 实现模型工厂

- `provider.py`: `create_chat_model()` 替换 `NotImplementedError` 为 `ChatGoogleGenerativeAI` 构造

### Step 6: 注册 context limit

- `provider.py`: `get_context_limit()` 添加 `"gemini": 1_000_000`

### Step 7: 测试

- 新增 `tests/test_llm/test_gemini_provider.py`
- 测试协议解析、reasoning kwargs 映射、thinking 提取、context limit
- Mock `ChatGoogleGenerativeAI` 避免真实 API 调用

## 边界情况

| 场景 | 行为 |
|------|------|
| 用户未安装 `langchain-google-genai` | 延迟导入失败，抛出 `ImportError` 并提示安装 `voidx[gemini]` |
| Gemini API key 未配置 | `ChatGoogleGenerativeAI` 自身会抛出认证错误 |
| Gemini 模型不支持 thinking | `_gemini_reasoning_kwargs()` 返回空 dict，不影响正常调用 |
| 用户通过 OpenRouter 使用 Gemini | 走 `openai` 协议，不受此变更影响 |
| `max_tokens` vs `max_output_tokens` | 工厂函数中做参数名映射 |

## Non-goals

- 不实现 Gemini 特有的 Google Search grounding / URL context 等扩展功能
- 不实现 Gemini 的安全设置（safety settings）自定义
- 不修改 `catalog.py` 中的静态模型列表（Gemini 模型列表变化快，建议仅用动态获取）
- 不实现多模态（图片/视频）输入的特殊处理

## 验收标准

- [ ] `provider="gemini"` + `protocol="gemini"` 可成功创建 `ChatGoogleGenerativeAI` 实例
- [ ] `_gemini_reasoning_kwargs()` 正确映射 effort 到 Gemini thinking 参数
- [ ] `_extract_thinking_gemini()` 可从 Gemini chunk 中提取 thinking 文本
- [ ] `get_context_limit("gemini")` 返回 `1_000_000`
- [ ] 未安装 `langchain-google-genai` 时给出清晰的安装提示
- [ ] 通过 OpenRouter 使用 Gemini 的现有路径不受影响
- [ ] 新增测试覆盖以上所有场景
