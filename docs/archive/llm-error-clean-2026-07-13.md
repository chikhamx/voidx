# LLM 报错信息提取与 JSON 格式优化

> **Status: Done** — Archived on 2026-07-14.

## 来源

在 LLM 调用失败或触发 Rate Limit 时，返回的异常信息中包含原始的 JSON/Dict 字符串，例如：
`Error code: 402 - {'error': {'type': 'rate_limit_error', 'message': '每日额度超限: 当前 $50.813...'}}`
这在 UI/终端中显示不够友好，需要提取出具体的 `message` 字段进行展示。

## 目标

- 提取 LLM 异常信息中的核心错误提示（如 `message` 字段）。
- 避免在 UI/终端中直接显示 JSON/Dict 格式的原始报错。
- 保持原有的错误代码前缀（如 `Error code: 402`）。

## 设计方案

1. 在 `src/voidx/agent/graph/core/helpers.py` 中实现 `_clean_error_message(exc: Exception) -> str` 辅助函数：
   - 尝试解析异常字符串中的 JSON/Dict 部分。
   - 提取 `error.message` 或 `message` 字段。
   - 拼接前缀（如 `Error code: 402`）与提取出的错误消息。
   - 如果解析失败，则回退到原始的异常字符串。

2. 在以下位置应用该辅助函数：
   - `src/voidx/agent/graph/core/llm.py` 中的重试和失败日志输出。
   - `src/voidx/agent/graph/subagent.py` 中的重试日志输出。

3. 编写单元测试验证各种异常格式的提取效果。
