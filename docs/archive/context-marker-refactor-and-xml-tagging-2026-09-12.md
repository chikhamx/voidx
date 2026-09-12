# 上下文内部哨兵标记清理与 XML 语义标签化规范

> **Status: Done** — Archived on 2026-09-12.

- 日期：2026-09-12
- 状态：设计完成，待评审与实现
- 适用范围：`agent/application/runtime_context.py`、`agent/application/prompts.py`、`automation/workflow/context.py`、`skills/context.py`、`mcp/context.py` 及相关上下文测试套件。

---

## 1. 背景与目标

当前 voidx 上下文管线中，使用了一系列以 `VOIDX_*` 开头的大写纯文本哨兵字符串（Sentinel Markers）用于生命周期控制和上下文剥离：
- `VOIDX_RUNTIME_CONTEXT`
- `VOIDX_WORKFLOW_CONTEXT`（伴随 `Scope: structured-workflow-runtime`）
- `VOIDX_SKILL_TOOL_CONTEXT` / `VOIDX_SKILL_TOOL_CONTEXT_STRIPPED`
- `VOIDX_MCP_TOOL_CONTEXT` / `VOIDX_MCP_TOOL_CONTEXT_STRIPPED`
- `VOIDX_COMPACTION_GUIDE` / `VOIDX_GOAL_RESOLUTION_GUIDE`
- 伪装在用户消息中的切分标题：`## Task Context` 与 `## User Message`

### 存在的问题
1. **SystemMessage 噪声与泄露**：
   `SystemMessage` 天然具备独立的物理通道（`role: "system"`），开头硬编码 `VOIDX_RUNTIME_CONTEXT` 及内部注释（如 `owned by the voidx runtime... for prompt-cache reuse`）属于不必要的内部工程细节外泄。
2. **User Message 伪装与切分脆弱**：
   动态状态（`Current Task State`）混入 `User Message` 是为了保住 SystemMessage 的 KV Cache，但目前使用 Markdown 平铺拼接及弱字符串切分（`_strip_turn_overlay_text` 依赖 `\n\n## Task Context`），若用户提问中包含该文本容易导致切分错乱与提示注入风险。
3. **缺乏闭合边界**：
   Markdown 只有起始标题，无明确闭合标记。对于动态加载的 Skill、MCP、Task State，模型难以精准分清“系统脚手架数据”与“用户真实输入”。

### 重构目标
1. **清理 SystemMessage**：彻底移除 `VOIDX_RUNTIME_CONTEXT` 和 `VOIDX_WORKFLOW_CONTEXT` 等无用哨兵标记，保持系统提示词干净纯粹。
2. **XML 语义标签化**：采用“外层 XML 封闭隔离，内层 Markdown 书写内容”的业界最佳实践，将动态注入的状态与工具内容标签化（例如 `<current_task_state>...</current_task_state>`）。
3. **稳健提取与清洗**：用确定性的闭合标签解析替代脆弱的字符串切分，实现可靠的上下文卫生（Context Hygiene）。
4. **向后兼容**：对已持久化历史会话中的旧标记保持剥离兼容，不破坏现有会话的回放与压缩。

---

## 2. 详细重构方案

### 2.1 SystemMessage 静态区域清理

#### (1) `VOIDX_RUNTIME_CONTEXT` 从系统提示词中退场
- **现状**：`_render_sections()` 默认在头部添加 `VOIDX_RUNTIME_CONTEXT`。
- **改动**：
  - 静态 `SystemMessage` 的渲染函数中，移除头部 `VOIDX_RUNTIME_CONTEXT`。
  - 直接以各 Section 标题（`## Base System` 等）平铺输出。
  - 保留 `SystemMessage` 整体的哈希指纹计算机制，确保不变前缀稳定命中大模型 Prompt Cache。

#### (2) `Workflow Runtime` 内部说明文本清理
- **现状**：
  ```markdown
  VOIDX_WORKFLOW_CONTEXT
  Scope: structured-workflow-runtime

  These are structured workflow definitions owned by the voidx runtime. The full workflow definitions are kept stable for prompt-cache reuse.
  ```
- **改动**：
  - 去除 `VOIDX_WORKFLOW_CONTEXT`、`Scope: ...` 以及开发注释语句。
  - 保留规范化的工作流节点定义与规则说明，直接融入 `## Workflow Runtime`。

---

### 2.2 动态任务状态（Task State）XML 标签化

为了保持 System KV Cache 命中率，`Current Task State` 继续作为动态叠层注入在最新轮次，但格式升级为标准的闭合 XML 标签。

#### 格式定义
```xml
<current_task_state>
- Current persona: coordinate
- Turn state: running
- Goal: 检阅上下文提示词
- Active workflow nodes: design
- Todo: 任务处理中
  - active 1: 编写设计文档
</current_task_state>

{user_real_input}
```

#### 优势
- **边界明确**：模型能清晰识别 `<current_task_state>` 是执行框架注入的环境状态，标签之外是人类用户的提问。
- **杜绝脆弱分隔符**：彻底废弃容易与用户输入冲突的 `\n\n## Task Context` 和 `## User Message` 平铺切分符。

---

### 2.3 动态工具上下文（Skills / MCP）XML 标签化

对于工具调用返回的临时大块上下文，改用标准的结构化标签包裹：

#### (1) Skill 加载
- **当前格式**：
  ```markdown
  VOIDX_SKILL_TOOL_CONTEXT
  Scope: current-turn

  ## Skill: react-patterns
  ...
  ```
- **升级为**：
  ```xml
  <tool_context type="skill" name="react-patterns">
  ## Skill: react-patterns
  ...
  </tool_context>
  ```
- **历史剥离后**：
  ```xml
  <tool_context type="skill" name="react-patterns" status="stripped" />
  ```

#### (2) MCP 工具定义加载
- **当前格式**：
  ```markdown
  VOIDX_MCP_TOOL_CONTEXT
  Scope: current-turn

  ## MCP Server: context7
  ...
  ```
- **升级为**：
  ```xml
  <tool_context type="mcp" server="context7">
  ## MCP Server: context7
  ...
  </tool_context>
  ```
- **历史剥离后**：
  ```xml
  <tool_context type="mcp" server="context7" status="stripped" />
  ```

---

### 2.4 上下文清洗器（Sanitizer & Stripper）重构

在消息持久化或提取语义消息（`raw_semantic_messages`）时，剥离器需具备双模能力：

1. **新版标签解析**：
   - 使用正则匹配 `<current_task_state>[\s\S]*?</current_task_state>` 并剥离，提取纯粹的用户消息。
   - 工具结果中的 `<tool_context>...</tool_context>` 转化为自闭合的精简占位标签 `<tool_context ... status="stripped" />`。
2. **旧版哨兵兼容（Backward Compatibility）**：
   - 保留对旧会话中 `VOIDX_RUNTIME_CONTEXT`、`\n\n## Task Context`、`VOIDX_SKILL_TOOL_CONTEXT` 的兼容清洗逻辑，确保老会话数据回放时不会报错或格式泄露。

---

## 3. 受影响文件清单

1. **核心渲染与清洗**：
   - `src/voidx/agent/application/runtime_context.py`
     - 调整 `_render_sections`，移除 SystemMessage 中的 `_CONTEXT_MARKER`。
     - 重构 `_prepend_task_context` 为 XML 标签包裹模式。
     - 重构 `_strip_turn_overlay_text` 支持 XML 标签闭合提取，并保留旧分隔符兼容。
   - `src/voidx/agent/application/automation/workflow/context.py`
     - 清理 `WORKFLOW_CONTEXT_MARKER` 和 Scope 描述文本。
   - `src/voidx/skills/context.py`
     - 切换到 `<tool_context type="skill">` 结构。
   - `src/voidx/mcp/context.py`
     - 切换到 `<tool_context type="mcp">` 结构。

2. **测试用例与契约基准**：
   - `src/tests/fixtures/contracts/prompts.json`（更新渲染基准断言）
   - `src/tests/test_application/test_runtime_context_builder.py`
   - `src/tests/test_application/test_runtime_context_skill_stripping.py`
   - `src/tests/test_agent/adapters/langgraph/runtime/test_session_context_frames.py`
   - `src/tests/test_agent/adapters/langgraph/runtime/test_session_persistence.py`

---

## 4. 验收与质量门禁（Quality Gate）

1. **提示词纯净度**：
   - 导出的 SystemMessage 中不再包含 `VOIDX_RUNTIME_CONTEXT` 及 `VOIDX_WORKFLOW_CONTEXT`。
2. **缓存稳定性**：
   - 多轮对话中，SystemMessage 依然保持完全静态，哈希值不变，不破坏 LLM Prompt Cache。
3. **标签隔离与抗注入**：
   - 动态任务状态完整包裹在 `<current_task_state>` 内，当用户输入包含 Markdown 标题或代码块时，不会破坏状态的识别与剥离。
4. **测试套件全绿**：
   - 所有单测、契约测试、历史兼容剥离测试无回归并通过。
