# GLM 模型切换后 image_url 报错分析报告

Date: 2026-07-04

## 问题现象

用户先使用 gpt-5.5 模型（支持视觉），在对话中粘贴/上传了图片。随后通过 `/model` 切换到智谱 GLM 模型（如 glm-5.1），此后每一轮对话都反复报 400 错误：

```
Error code: 400 - {'error': {'message': 'Failed to deserialize the JSON body into the target type: messages[1]: unknown variant image_url, expected text at line 1 column 52586', 'type': 'invalid_request_error', 'param': None, 'code': 'invalid_request_error'}}
```

错误持续存在，无法通过正常对话恢复。

## 根因

### 1. 历史消息中残留 image_url 多模态内容

用户在 gpt-5.5 下发送图片时，`build_user_message_payload`（`src/voidx/agent/attachments.py:68`）会将图片编码为 base64 data URL，构造结构化 content：

```python
# attachments.py:147-149
if image_parts:
    content = [{"type": "text", "text": text_content}, *image_parts]
    content_format = "structured"
```

其中每个 `image_part` 的结构为（`attachments.py:251-256`）：

```python
{
    "type": "image_url",
    "image_url": {"url": f"data:{mime_type};base64,{encoded}"}
}
```

这条 HumanMessage 被持久化到会话历史中。切换模型不会修改历史消息。

### 2. GLM 的 OpenAI 兼容端点不支持 image_url

智谱 GLM 系列（glm-5.1、glm-5、glm-4.7、glm-4.7-flash 等）的 OpenAI 兼容端点 **只接受 `text` 类型的 content**，不支持 `image_url` 变体。只有带 V 后缀的视觉模型（glm-4.5v、glm-4.6v）才支持 `image_url`。

> 参考：智谱官方文档中 GLM-4.5V/4.6V 的示例使用 `image_url` 类型，而 GLM 纯文本模型的 API schema 中 content type 枚举仅包含 `text`。

### 3. 消息发送前缺少 HumanMessage 多模态内容适配

`_sanitize_messages_for_replay`（`src/voidx/agent/graph/streaming.py:120`）是消息发送前的唯一清洗入口，但它 **只处理 AIMessage 的内容**（剥离 thinking/reasoning 块），完全没有处理 HumanMessage 中的 `image_url` 块：

```python
# streaming.py:128-136 — 只处理 AIMessage
for message in messages:
    if isinstance(message, AIMessage):
        content = _sanitize_ai_content_for_replay(message.content, protocol=protocol)
        ...
    sanitized.append(message)  # HumanMessage 原样保留
```

因此历史消息中的 `image_url` 块被原样发送给 GLM API，触发反序列化错误。

### 4. 没有视觉能力判断机制

代码库中不存在"该模型/provider 是否支持图片"的判断逻辑。`_REPLAY_UNSAFE_BLOCK_TYPES`（streaming.py:20）只包含 thinking/reasoning 相关的类型，不包含 `image_url`。catalog 中也没有标记哪些模型支持视觉。

## 影响范围

- **触发条件**：在支持视觉的模型（gpt-5.5、claude 等）下发送过图片，然后切换到不支持视觉的模型（GLM 纯文本系列、可能还包括其他 deepseek 协议 provider）。
- **严重程度**：高 — 会话完全不可用，每轮都报错，用户无法通过正常对话恢复。
- **受影响 provider**：至少包括智谱 GLM 纯文本系列。其他使用 deepseek 协议的 provider（deepseek、qwen、doubao、kimi、mimo 等）如果同样不支持 `image_url`，也会受影响。

## 涉及代码路径

| 文件 | 行号 | 职责 |
|------|------|------|
| `src/voidx/agent/attachments.py` | 147-149 | 构造带 image_url 的结构化 content |
| `src/voidx/agent/attachments.py` | 251-256 | `_image_part` 生成 image_url 块 |
| `src/voidx/agent/graph/streaming.py` | 120-142 | `_sanitize_messages_for_replay` — 发送前清洗，未处理 HumanMessage |
| `src/voidx/agent/graph/streaming.py` | 69 | `stream_llm` 调用清洗后直接 astream |
| `src/voidx/llm/provider.py` | 54-65 | provider → protocol 映射，GLM 走 deepseek 协议 |
| `src/voidx/llm/catalog.py` | 62-67 | GLM 模型列表，无视觉能力标记 |

## 修复方向（供后续参考）

### 方案 A：发送前剥离 image_url（最小改动）

在 `_sanitize_messages_for_replay` 中增加对 HumanMessage 的处理：当目标 provider/model 不支持视觉时，剥离 content 中的 `image_url` 块，只保留 `text` 块。

- 优点：改动小，立即解决问题，不丢文本内容
- 缺点：图片信息丢失，用户无感知

### 方案 B：发送前剥离 + 用户提示

在方案 A 基础上，检测到剥离时向用户输出一条警告（如"当前模型不支持图片，已忽略历史消息中的图片内容"）。

- 优点：用户有感知，知道图片没被处理
- 缺点：需要打通 UI 通知链路

### 方案 C：视觉能力注册表 + 上游拦截

在 catalog 或 provider 层增加视觉能力标记，在用户粘贴图片时就根据当前模型判断是否允许，或在切换模型时检查历史消息是否包含不兼容内容并提示。

- 优点：最完善，从源头避免问题
- 缺点：改动面大，需要维护视觉能力注册表

### 推荐

短期采用方案 B（剥离 + 提示），长期演进到方案 C。

## 复现方法

1. 启动 voidx，选择一个支持视觉的模型（如 gpt-5.5）
2. 粘贴一张图片，发送消息，确认正常响应
3. 通过 `/model` 切换到 glm-5.1
4. 发送任意文本消息 → 触发 400 错误
