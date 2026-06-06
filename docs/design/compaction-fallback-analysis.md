# Compaction Fallback 问题分析报告

> **日期**: 2026-06-06
> **状态**: 分析中
> **现象**: 用户触发 compaction 后显示 `✗ Compaction fallback summarized 404 messages`，LLM 生成的摘要失败，走了 fallback 路径

---

## 1. 现象描述

用户在正常对话过程中触发了上下文压缩（compaction），但 compaction agent 的 LLM 调用失败，最终走了 fallback 路径。用户看到的唯一信息是：

```
✗ Compaction fallback summarized 404 messages
```

其中 `404` 是被压缩的消息数量（head messages），不是 HTTP 状态码。

## 2. Compaction 流程梳理

### 2.1 触发条件

`_maybe_compact()` 在以下情况触发：

- **自动触发**: token 使用量 ≥ 90% 的 `context_limit`（`COMPACTION_THRESHOLD = 0.90`）
- **手动触发**: 用户执行 `/compact` 命令（`force=True`）

### 2.2 执行流程

```
_maybe_compact()
  ├─ 检查是否溢出 (is_overflow)
  ├─ 询问用户 (ask_compact, 可配置)
  ├─ 选择分割点 (select_details)
  │    ├─ head: 需要压缩的旧消息
  │    ├─ tail: 保留的近期消息
  │    └─ keep_from: 分割索引
  ├─ 运行 compaction agent (重试 1+2 次)
  │    └─ _run_compaction_agent()
  │         ├─ 构建 prompt (build_prompt, 截断至 60K 字符)
  │         ├─ 调用 stream_llm (无 tools)
  │         └─ 提取文本摘要
  └─ 失败则走 fallback
       ├─ CompactionService.fallback_summary()
       ├─ 替换消息列表为 tail
       └─ 持久化 compaction
```

### 2.3 关键代码位置

| 文件 | 行号 | 职责 |
|------|------|------|
| `src/voidx/agent/graph/compaction.py:29` | `GraphCompactionMixin` | compaction 入口和重试逻辑 |
| `src/voidx/agent/graph/compaction.py:229` | `_run_compaction_agent()` | 调用 LLM 生成摘要 |
| `src/voidx/llm/compaction.py:104` | `CompactionService` | token 预算、消息分割、prompt 构建 |
| `src/voidx/llm/compaction.py:274` | `fallback_summary()` | LLM 失败时的兜底摘要生成 |
| `src/voidx/llm/compaction.py:346` | `build_prompt()` | 构建 compaction prompt |

## 3. 失败原因分析

### 3.1 两种失败路径

compaction 走 fallback 有两种可能：

| 路径 | 条件 | `last_error` | 可诊断性 |
|------|------|-------------|---------|
| **A: LLM 调用抛异常** | `stream_llm()` 抛出异常，3 次重试都失败 | 有值 | 可诊断，但信息被吞 |
| **B: LLM 返回空内容** | 调用成功但 `extract_text()` 返回空字符串 → `summary` 为 `None` | `None` | 不可诊断 |

### 3.2 路径 A：LLM 调用异常

可能的异常原因：

1. **模型不支持纯文本补全**: compaction 不带 tools（`# Use a cheap/fast call for compaction — no tools`），而正常对话通过 `bind_tools()` 绑定了工具。某些 OpenAI 兼容 API 在不带 tools 时的行为可能不同（如使用 Responses API vs Chat Completions API 的路由差异）。

2. **prompt 过长**: 虽然有 60K 字符截断（`COMPACTION_PROMPT_CONTEXT_MAX_CHARS`），但 404 条消息的 prompt 仍然可能接近或超过模型的实际上下文限制。

3. **API 限流/超时**: 连续 3 次调用可能在短时间内触发 rate limit。

### 3.3 路径 B：LLM 返回空内容

`_run_compaction_agent()` 第 272-273 行：

```python
text = extract_text(assistant_msg)
return text if text else None
```

如果模型返回了内容但 `extract_text()` 无法提取（如返回了非标准格式的内容块），`summary` 就是 `None`，循环继续但 `last_error` 始终为 `None`。

### 3.4 当前模型的特殊性

用户使用 `poweronlabs/zai-org/GLM-5-FP8`，该 provider：

- 不在 `_PROVIDER_PROTOCOLS` 中 → 默认走 `openai` 协议
- 不在 `_DEFAULT_BASE_URLS` 中 → 依赖用户配置的 `base_url`
- 不在任何 reasoning kwargs 分支中 → 不传特殊参数

正常对话能工作说明 API 连通性没问题，但 compaction 的调用方式（无 tools、SystemMessage 开头）可能触发了该 API 的边界行为。

## 4. 核心问题：错误信息被吞

### 4.1 问题描述

无论走哪条失败路径，用户最终只看到：

```
✗ Compaction fallback summarized 404 messages
```

具体原因完全丢失。原因有两层：

**第一层：`StatusFinished` 没有传递 `detail`**

```python
# compaction.py:144-150
await ui_events.emit(StatusFinished(
    status_id="compaction",
    label=f"Compaction fallback summarized {len(head_msgs)} messages",
    ok=False,
    remove=False,
    # ❌ 没有 detail 字段，last_error 信息丢失
))
```

之前的 `StatusUpdated`（包含 `detail=f"{err_detail}using extracted summary"`）被 `StatusFinished` 覆盖，用户看不到。

**第二层：路径 B 没有 `last_error`**

如果 LLM 返回空内容而非抛异常，`last_error` 为 `None`，即使加了 `detail` 也无法展示原因。

### 4.2 对比：正常对话的错误处理

正常对话的 LLM 调用失败时，错误信息会直接打印：

```python
# core.py:501-507
except Exception as e:
    if attempt < max_retries:
        ui.print(f"[dim]LLM error, retrying in {delay}s: {e}[/dim]")
    else:
        ui.error(f"LLM call failed after {max_retries + 1} attempts: {e}")
```

而 compaction 的错误信息只在 `StatusUpdated.detail` 中短暂存在，最终被 `StatusFinished` 覆盖。

## 5. 影响评估

| 方面 | 影响 |
|------|------|
| **功能** | Fallback 摘要质量远低于 LLM 生成的摘要，可能导致上下文丢失、后续对话质量下降 |
| **可诊断性** | 用户无法判断失败原因，无法自行排查 |
| **可修复性** | 由于信息缺失，开发者也无法远程诊断具体是路径 A 还是路径 B |

## 6. 优化建议

### 6.1 必须修复：暴露错误信息

在 `StatusFinished` 中加入 `detail` 字段，展示 `last_error`：

```python
# 修改 compaction.py:144-150
err_hint = f" ({last_error})" if last_error else " (LLM returned empty summary)"
await ui_events.emit(StatusFinished(
    status_id="compaction",
    label=f"Compaction fallback summarized {len(head_msgs)} messages",
    detail=err_hint,  # 新增
    ok=False,
    remove=False,
))
```

### 6.2 建议修复：区分失败路径

为路径 B（LLM 返回空内容）添加日志记录：

```python
# 在 _run_compaction_agent 中，当 text 为空时记录原始响应
text = extract_text(assistant_msg)
if not text:
    logger.warning(
        "Compaction agent returned empty text. "
        "Response type: %s, content types: %s",
        type(assistant_msg).__name__,
        [type(c).__name__ for c in assistant_msg.content] if isinstance(assistant_msg.content, list) else type(assistant_msg.content).__name__,
    )
return text if text else None
```

### 6.3 长期优化：compaction 独立模型配置

当前 compaction 使用主会话模型，但 compaction 的需求不同（不需要 tools、不需要 reasoning、需要稳定）。建议：

- 在 `ModelConfig` 或 `Config` 中增加 `compaction_model` 可选配置
- 默认回退到主模型，但允许用户指定更轻量/更稳定的模型用于 compaction
- 对不支持的 API（如无 tools 调用失败），自动降级到 fallback

### 6.4 可选优化：compaction 调用方式适配

对于 OpenAI 兼容 API，compaction 可以尝试：

- 先用无 tools 调用，如果失败则尝试绑定一个 dummy tool 再调用
- 或者在 `ChatOpenAI` 创建时显式禁用 Responses API（`use_responses_api=False`），避免路由差异

## 7. 总结

| 项目 | 结论 |
|------|------|
| **直接原因** | compaction agent 的 LLM 调用失败（异常或返回空），走了 fallback |
| **根本原因** | 无法确定——错误信息被 `StatusFinished` 吞掉，路径 B 甚至没有 `last_error` |
| **最可能的假设** | `poweronlabs/GLM-5-FP8` 在无 tools 的纯文本调用场景下行为异常（返回空或报错） |
| **最紧迫的修复** | 在 fallback 的 `StatusFinished` 中暴露 `last_error`，让下次失败时可诊断 |
