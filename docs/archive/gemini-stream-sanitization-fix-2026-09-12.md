# Gemini 流式正文截断与分块空格丢失修复规范

> **Status: Done** — Archived on 2026-09-16.

- 日期：2026-09-12
- 状态：已实施并验收（2026-09-16，提交 4bfe4ee2；后端全套 7066 passed）
- 读者：维护者与实施代理
- 范围：`src/voidx/agent/adapters/langgraph/runtime/streaming.py` 及其测试

## 1. 结论与证据边界

当前实现存在两个独立缺陷：

1. 思考去重仅凭前缀关系删除整段正文，可同时损坏实时输出与最终消息。
2. DSML 提取器无条件去除首尾空白，逐块净化后拼接导致最终消息中的单词粘连。

以上已使用本地真实函数及合成 `AIMessageChunk` 复现，不依赖远端模型。原问题报告提到回复以 `"]` 等闭合符号开头，并出现 `TaskState()runs.`、`Sohost`。

**尚未核查原会话的 `messages.jsonl`、`transcript.jsonl`、截图对应原始 chunk 或 SDK 返回包。** 因此不能断言原始故障一定由这两个缺陷造成，也不能将模型在思考末尾草拟答案、SDK 的具体分片方式或 thinking signature 的影响写成已证实事实。

## 2. 当前调用链与根因

### 2.1 无门槛前缀去重

源码：

- `src/voidx/agent/adapters/langgraph/runtime/streaming.py`：`stream_llm`、`_stream_visible_content`、`_strip_duplicate_thinking_text`。
- `src/voidx/llm/thinking.py`：`extract_thinking` 及结构化思考块提取。

`stream_llm` 对每个 `raw_chunk` 单独提取 thinking，再将其和当前 content 交给 `_stream_visible_content`。这里的 thinking 是当前 chunk 的提取结果，不是跨 chunk 累计内容。结构化思考块虽被排除，独立 text 块仍会经过以下比较：

```python
if text == thinking or text.strip() == thinking.strip():
    return ""
if thinking.startswith(text) or text.startswith(thinking):
    return ""
```

最小复现输入（通过 `stream_llm(..., protocol="gemini")`）：

```python
[
    AIMessageChunk(content=[
        {"type": "thinking", "thinking": 'states["coding"] is the answer'},
        {"type": "text", "text": 'states["coding'},
    ]),
    AIMessageChunk(content=[{"type": "text", "text": '"] remains'}]),
]
```

当前实时正文和最终 `AIMessage.content` 均为 `'"] remains'`，预期为 `'states["coding"] remains'`。

触发条件是前缀关系，不是包含关系：`thinking = 'Let\'s output states["coding"]...'` 与 `text = 'states["coding'` 不满足上述比较，实际不会删除正文。仅在思考末尾出现草稿不足以解释截断。

### 2.2 DSML 清洗破坏分块边界

`_extract_dsml_tool_calls_from_text` 当前无条件执行：

```python
cleaned = _DSML_TOOL_CALLS_RE.sub("", normalized).strip()
```

`_sanitize_ai_content_for_replay` 对字符串列表项和 text 字典逐项清洗，再用 `"".join(text_parts)` 合并。只要合并后的 content 仍保留多个文本块，就可能丢失边界空格；不要求特定模型版本或元数据。

最小复现：分别发送四个含 text 字典的 chunk，文本依次为 `"TaskState() "`、`"runs. "`、`"So "`、`"host"`。

| 观测位置 | 当前结果 |
| --- | --- |
| 传给 renderer 的实时文本拼接 | `TaskState() runs. So host` |
| 返回的最终消息 | `TaskState()runs.Sohost` |

此根因证明最终消息及回放净化会损坏文本，不能单独证明实时终端显示也粘连。普通字符串消息同样会被去除首尾空白，只是不会产生块间粘连。

### 2.3 XML 已有无调用保护

`_extract_legacy_xml_tool_calls_from_text` 当前实现为：

```python
cleaned = _LEGACY_XML_TOOL_CALL_RE.sub("", text).strip() if calls else text
```

无工具调用时已经原样返回。实测 `"  spaced text  "` 经 XML 提取器后不变，经 DSML 提取器后变成 `"spaced text"`。本次不应重复修改 XML 实现。

## 3. 建议实施方案（待确认）

### 3.1 思考去重以正文保留为优先

1. 将 `protocol` 关键字参数贯通 `stream_llm → _stream_visible_content → _strip_duplicate_thinking_text`，覆盖字符串、列表字符串项和 text 字典三个分支；辅助函数默认值保持 `""` 以兼容已有调用。
2. 对 `gemini`、`anthropic` 跳过正文与 thinking 的文本去重，仍排除结构化思考块，仍正常调用 `renderer.feed_thinking`。
3. 对 `openai`、`deepseek` 及默认协议，仅保留现有的完全一致或去除首尾空白后完全一致的去重；纯空白 text 应原样保留，不因空白相等而删除。
4. 删除两个方向的前缀删除分支，不引入 32 字符阈值。较长前缀仍可能是合法正文，尤其 `text.startswith(thinking)` 时删除整段会连带吞掉后缀。

取舍：宽松前缀去重停止后，兼容层的部分 reasoning 回显可能可见；相比静默丢失正文，优先保留内容。协议级跳过也会保留 Gemini/Anthropic 兼容层可能产生的完全相等回显。若后续需处理这些情况，应基于实际 provider 的回显证据独立设计，不继续扩大文本猜测规则。

### 3.2 无工具调用时保留原文

`_extract_dsml_tool_calls_from_text` 完成现有解析后，若 `calls` 为空，直接返回 `(text, [])`，不返回归一化文本、不执行 `.strip()` 或标签替换。

有真实调用时保留现有标签移除、`.strip()`、boilerplate 清理和调用结构；XML 实现保持不变，仅补回归覆盖。

本次不承诺“命中工具调用时仅清理标签邻近换行”：该要求与现有整体 `.strip()` 行为不同，属于额外变更，不与无调用空白修复混合。返回原文也可能改变无有效调用的 DSML 标签处理，必须验证 malformed 检测及重放行为，不能只验证普通句子。

### 3.3 不变量与禁止变更

- 保留 DeepSeek 历史回放中 reasoning/thinking 块的既有逻辑，不改 `extract_thinking`。
- 保留真实 DSML/XML 调用的名称、参数、ID 形式和清理结果。
- 保留 malformed 工具调用识别与元数据，不能让畸形调用作为普通正文泄露。
- 无调用纯文本的字符流应完整保留，包括首尾空白、纯空白块、换行和缩进；不能用 `" ".join(...)` 臆造空格。
- 不改 SDK、provider、renderer、持久化格式或无关用户文件；不扩展为跨块工具标签解析重构。

## 4. 文件与测试要求

生产文件：`src/voidx/agent/adapters/langgraph/runtime/streaming.py`。

测试文件：`src/tests/test_agent/adapters/langgraph/runtime/test_stream_llm_sanitization.py`。优先使用已有 `FakeRenderer`；新增合成流模型可直接在该测试文件中定义，避免无关辅助模块变更。

按 TDD 分两项实施：先写测试并确认因目标缺陷失败，再做最小实现。已经满足的兼容性断言应保持绿色，不制造失败。

### 4.1 正文保留

- Gemini、Anthropic 同 chunk 含独立 thinking/text 块：短前缀、长前缀、反方向前缀及完全一致时均保留正文；思考只进入 thinking 通道。
- 经 `stream_llm` 复现 §2.1，分别断言实时 renderer 文本和最终消息完整，以覆盖参数贯通而非只测底层函数。
- OpenAI、DeepSeek、默认协议：双向非相等前缀（含超过 32 字符的前缀）不删除；完全一致及非空的去空白一致仍去重。
- 纯空白 text 保留；覆盖 `_stream_visible_content` 的字符串、列表字符串项和 text 字典分支。
- 保留现有 `test_stream_llm_hides_duplicated_reasoning_content` 回归。

### 4.2 空白保留

- DSML/XML 提取器在无调用时，对 `"  spaced text  "`、空字符串、纯空格、换行和缩进原样返回。
- `_sanitize_ai_content_for_replay` 覆盖单字符串、字符串列表、text 字典列表及含独立空白块的输入。
- 经 `stream_llm` 发送 §2.2 四个 chunk：实时文本和最终消息都严格等于 `"TaskState() runs. So host"`。
- 有真实 DSML/XML 调用时维持现有解析和净化结果；覆盖无有效调用的标签与 malformed 响应，确认不绕过已有保护。
- 验证 DeepSeek 回放的 reasoning 保留及其他协议的思考块净化不变。

## 5. 验证命令与验收

从仓库根目录执行，每项实现先运行对应新增测试（用 `-k` 筛选），再运行目标文件：

```bash
./test.py --backend -- src/tests/test_agent/adapters/langgraph/runtime/test_stream_llm_sanitization.py
```

完成两项后运行相关回归：

```bash
./test.py --backend -- src/tests/test_agent/adapters/langgraph/runtime/test_stream_llm_sanitization.py src/tests/test_llm/test_gemini_provider.py src/tests/test_llm/test_streaming_sanitize.py
```

最后执行后端全套：

```bash
./test.py --backend
```

验收条件：

- 新增缺陷测试先 RED 后 GREEN，相关及全后端测试无回归；如受环境阻塞，应如实记录，不声明全套通过。
- 两个合成流复现均修正，实时输出和最终消息分别断言；兼容性断言保持成立。
- 不以合成输入测试代替原始会话因果证明。若需确认原始截图，另行获取并核查原始 chunk 与显示/持久化链路。
- 完成实际实现与最终验证之前，不归档本规范。

### 已有证据（修复前）

2026-09-12，macOS arm64，本仓库根目录：

- 通过 `./python.py` 执行合成 `AIMessageChunk` 的 `stream_llm` 调用，得到 §2 的截断、最终消息粘连和 XML/DSML 空白差异。
- 上述三个文件的组合回归命令结果为 **70 passed**。这是旧测试基线，不能证明新增场景通过，也不是全后端验证。
- 本次规范校正未修改生产或测试代码。
