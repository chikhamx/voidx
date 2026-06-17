# Compaction Fallback 问题分析报告

> **Status: Done**
> **日期**: 2026-06-07
> **现象**: 用户触发 compaction 后显示 `✗ Compaction fallback summarized 404 messages`，LLM 摘要未生成，系统走了 fallback 路径

---

## 1. 现象描述

用户在正常对话过程中触发上下文压缩（compaction），但 compaction agent 没有产出 LLM 生成的摘要，最终使用 `CompactionService.fallback_summary()` 生成提取式摘要。用户最终只看到：

```text
✗ Compaction fallback summarized 404 messages
```

其中 `404` 是被压缩的消息数量（head messages），不是 HTTP 状态码。

## 2. Compaction 流程梳理

### 2.1 触发条件

`_maybe_compact()` 在以下情况触发：

- **自动触发**: token 使用量 >= 90% 的 `context_limit`（`COMPACTION_THRESHOLD = 0.90`）
- **手动触发**: 用户执行 `/compact` 命令（`force=True`）

### 2.2 执行流程

```text
_maybe_compact()
  ├─ 检查是否溢出 (is_overflow)
  ├─ 询问用户 (ask_compact, 可配置)
  ├─ 选择分割点 (select_details)
  │    ├─ head: 需要压缩的旧消息
  │    ├─ tail: 保留的近期消息
  │    └─ keep_from: 分割索引
  ├─ 运行 compaction agent (初始调用 + 2 次重试)
  │    └─ _run_compaction_agent()
  │         ├─ 构建 prompt (build_prompt, 历史片段截断至 60K 字符)
  │         ├─ 构造 SystemMessage + HumanMessage
  │         ├─ 调用 stream_llm (无 tools)
  │         └─ 提取文本摘要
  └─ 失败则走 fallback
       ├─ CompactionService.fallback_summary()
       ├─ 替换消息列表为 tail
       ├─ 持久化 runtime summary 并删除已压缩消息
       └─ 发出 StatusFinished
```

### 2.3 关键代码位置

| 文件 | 位置 | 职责 |
|------|------|------|
| `src/voidx/agent/graph/compaction.py:33` | `GraphCompactionMixin` | compaction 入口、重试、fallback 和 UI 状态 |
| `src/voidx/agent/graph/compaction.py:243` | `_run_compaction_agent()` | 构造 compaction 消息并调用 LLM |
| `src/voidx/llm/compaction.py:104` | `CompactionService` | token 预算、消息分割、prompt 构建 |
| `src/voidx/llm/compaction.py:273` | `fallback_summary()` | LLM 摘要失败时生成提取式摘要 |
| `src/voidx/llm/compaction.py:346` | `build_prompt()` | 构建 compaction prompt |

## 3. 已确认原因与失败路径

### 3.1 直接原因（已修复 ✅）

`_run_compaction_agent()` 使用 `HumanMessage` 构造 prompt 消息，但原代码未导入 `HumanMessage`，导致调用 `stream_llm()` 前抛出 `NameError: name 'HumanMessage' is not defined`，重试耗尽后进入 fallback。

**修复**: 在 `src/voidx/agent/graph/compaction.py` 顶部补齐 `HumanMessage` 导入：

```python
from langchain_core.messages import HumanMessage, SystemMessage
```

### 3.2 fallback 的三类路径

| 路径 | 条件 | `last_error` | 可诊断性 |
|------|------|-------------|---------|
| **A: compaction agent 执行异常** | `_run_compaction_agent()` 内任意步骤抛异常 | 有值 | 可诊断 |
| **B: agent 正常返回但无摘要** | `_run_compaction_agent()` 返回 `None` | `None` | 需显式标记 |
| **C: 无可用模型** | `self.model is None`，直接返回 `None` | `None` | 需显式标记 |

### 3.3 待观察项

修复本地缺陷后，以下模型/API 行为仍需观察：

1. compaction 不绑定 tools，部分 OpenAI-compatible API 在无 tools 调用时行为可能不同。
2. 历史片段限制在 60K 字符，若 provider 上下文窗口小于配置值仍可能出错。
3. 连续 3 次调用可能遇到限流或超时。

## 4. 核心问题：错误信息被最终状态覆盖（已修复 ✅）

### 4.1 原问题

fallback 的 `StatusFinished` 原来没有传递 `detail`，导致用户只看到 `Compaction fallback summarized ...`，无法判断失败原因。

### 4.2 修复

fallback 时构造统一的 `failure_detail` 并同时传给 `StatusUpdated.detail` 和 `StatusFinished.detail`：

```python
if last_error:
    failure_detail = f"{type(last_error).__name__}: {last_error}"
elif returned_no_summary:
    failure_detail = "compaction agent returned no summary"
else:
    failure_detail = "compaction agent did not produce a summary"
```

```python
await ui_events.emit(StatusFinished(
    status_id="compaction",
    label=f"Compaction fallback summarized {len(head_msgs)} messages",
    detail=f"{failure_detail}; using extracted summary",
    ok=False,
    remove=False,
))
```

## 5. 影响评估

| 方面 | 影响 |
|------|------|
| **功能** | fallback 摘要质量低于 LLM 结构化摘要，可能丢失约束、决策、错误信息和下一步 |
| **可诊断性** | 修复后可通过 `StatusFinished.detail` 区分异常、空摘要、无模型三种路径 |
| **测试覆盖** | 新增回归测试覆盖真实消息构造路径和 fallback detail 传递 |

## 6. 修复清单

| 项目 | 状态 |
|------|------|
| 补齐 `HumanMessage` 导入 | ✅ 已完成 |
| fallback `StatusFinished` 传递 `failure_detail` | ✅ 已完成 |
| 空摘要路径增加 `logger.warning` 诊断日志 | ✅ 已完成 |
| 新增 `test_run_compaction_agent_builds_messages_and_extracts_text` | ✅ 已完成 |
| 新增 `test_maybe_compact_fallback_finished_event_includes_failure_detail` | ✅ 已完成 |

## 7. 后续优化

- 增加可选 `compaction_model` 配置，允许为 compaction 指定更稳定、便宜的模型。
- 在 provider 适配层明确区分主对话和 compaction 调用的参数策略。

## 8. 验证

```bash
.venv/bin/python -m pytest tests/test_compaction.py -q
```
