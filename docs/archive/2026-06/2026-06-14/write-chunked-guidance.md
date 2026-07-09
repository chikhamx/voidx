> **Status: Done**

# 引导 LLM 分步写入大文件 — 技术设计文档

## Context

voidx 的 `write` 工具要求 LLM 在一次 tool_call 的 `content` 参数中输出完整文件内容。当文件较大时：

1. **生成耗时长** — LLM 需要流式输出大量 token，用户长时间看到内容逐字流出
2. **可能被截断** — 输出 token 受 `max_tokens` 约束，大文件可能写不完整，导致文件残缺
3. **重试代价高** — 截断后需要重新生成整个文件

`edit` 工具天然支持增量修改，但 LLM 在新建大文件时不会主动采用"先写骨架再 edit"的策略。

## Goals and Non-Goals

### Goals

- 引导 LLM 对大文件采用"先 write 骨架 → 再 edit 增量"的写入策略
- 减少 LLM 单次输出的 token 量，降低截断风险
- 改善用户等待体验（每轮快速返回而非长时间等待）

### Non-Goals

- 不改变 `write` 工具的执行机制（不增加 append/chunk 模式）
- 不改变 tool schema 结构（仅修改 description 文本）
- 不改变 compaction / sanitize 逻辑
- 不在 system prompt 中重复引导（工具描述本身就是 LLM 看到的引导）

## Architecture

三层引导，全部集中在 `src/voidx/tools/file_ops.py`，无跨模块改动。

```
Layer 1: 工具描述引导
  FileWriteTool.description → 告知 LLM 大文件应分步写入
  ↓ LLM 决策
  小文件 → write 一次性写入（行为不变）
  大文件 → write 骨架 + edit 增量

Layer 2: 参数描述引导
  FileWriteInput.content Field description → 给出 ~150 行的单次写入建议

Layer 3: 执行后反馈闭环
  FileWriteTool.execute → 超过 200 行时在 ToolResult.output 追加事后提醒
  ↓ 回传给 LLM（通过 sanitize_tool_message_content）
  LLM 在后续写入中调整策略
```

Layer 3 不是当前写入的截断恢复机制：如果 `content` 参数在模型输出阶段已经被截断，
`write` 工具不会成功执行，也就无法追加提醒。真正降低当前截断风险的是 Layer 1/2 的
预先引导；Layer 3 只作为后续写入的校正信号。

## Data Model

无新增数据模型。仅修改现有字段的描述文本和返回值格式。

## API Contract

### FileWriteTool.description

```
Write content to a file. Creates parent directories. Overwrites existing files.
For files around 150 lines or larger, write a skeleton first (imports, class/function signatures,
docstrings, and unique placeholders), then use edit to replace placeholders with implementation
blocks incrementally. This avoids output truncation and reduces wait time.
```

### FileWriteInput.content Field description

```
Content to write. Keep under ~150 lines for best results; for larger files write a skeleton with
unique placeholders and use edit to replace them incrementally.
```

### FileWriteTool.execute 返回值

当 `content` 行数超过 200 行时，追加提醒文本。200 行是明显过大的事后提醒阈值，
~150 行仍是工具描述里的主动建议阈值。

```python
output = f"File written: {inp.file_path} ({size} bytes)"
line_count = inp.content.count("\n") + 1
if line_count > 200:
    output += (
        f"\nNote: This file is large ({line_count} lines). "
        "For future writes of similar size, consider writing a skeleton first "
        "and using edit to add content incrementally."
    )
```

- **200 行以内的文件**: 行为与改动前完全一致，无任何额外输出
- **超过 200 行的文件**: 文件仍会正常写入；提醒只影响返回给 LLM 的 `output`

## Error Handling

无新增错误场景。所有改动都是描述文本和可选的输出追加，不影响工具执行逻辑。

| 失败场景 | 处理策略 |
|---------|---------|
| LLM 忽略引导仍一次性写入大文件 | Layer 3 执行后提醒形成反馈闭环，下次写入时 LLM 会调整 |
| 大文件 tool_call 参数在模型输出阶段被截断 | 工具无法执行；Layer 1/2 的预先引导负责降低此风险，Layer 3 不承诺恢复 |
| LLM 过度拆分（小文件也分步写） | Layer 2 的 ~150 行建议给出明确阈值，避免过度拆分 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 仅修改描述文本，不改工具机制 | 增加分块写入参数 / append 模式 | edit 已覆盖增量修改，无需重复；改描述零破坏性 |
| content 参数建议 ~150 行 | 无建议 / 更低阈值 | 4K-8K max_tokens 下可靠输出的范围，留有余量 |
| 执行后提醒阈值 200 行 | 与建议值相同（150 行） | 避免对"略超建议"的文件频繁提醒，只在明显过大时触发 |
| 不改 system prompt | 在 system prompt 中加写入策略 | 工具描述已注入 LLM 上下文，重复引导浪费 token |
| 固定阈值，不按 max_tokens 动态调整 | 动态阈值 | 固定值简单可靠，动态调整增加复杂度且收益有限 |
