# clarify 富 UI 卡片渲染 — 技术设计文档

## Context

`plan_checkpoint` 工具已实现"富 UI 卡片"渲染：执行时发射 `CheckpointPromptShown` 事件，dock 树中生成 `node_type="checkpoint"` 节点展示 plan 详情与选项；用户决定后发射 `CheckpointDecisionSubmitted`，节点被 resolve 并追加 `User: <response>` 子节点。

`clarify` 工具当前完全没有 UI 事件发射，只走 `ctx.interact` → `app.ask_text`/`ask_choice`。用户看到的是 TUI 底部裸输入框，问题以 prompt 形式出现在输入区，回答后仅以 `clarify: <answer>` 普通工具结果节点呈现，缺乏问题上下文的可视化。

本设计将 clarify 改造为与 checkpoint 一致的 dock 卡片渲染模式，**但用户输入仍保持手动文本输入**（clarify 的 `list[str]` options 是"建议答案"而非强制选项）。

## Goals and Non-Goals

### Goals

- clarify 执行时在 dock 树中生成专属卡片节点，展示问题文本与建议选项
- 用户回答后卡片节点被 resolve，追加 `User: <answer>` 子节点
- 保持现有 `ctx.interact` → `ask_text` 输入流程不变（手动文本输入）
- 与 checkpoint 的渲染模式对称，便于前端统一处理

### Non-Goals

- 不将 clarify 的 options 改为可点选按钮（保持手动输入语义）
- 不改变 clarify 的 `list[str]` options 类型语义
- 不改变 `_infer_state_patch` 等业务逻辑

## Architecture

数据流（与 checkpoint 对称）：

```
ClarifyTool.execute
  ├─ 生成 clarify_id
  ├─ _emit_clarify_shown(clarify_id, question, options)  ← 发射 ClarifyPromptShown
  │     └─ DockEventConsumer → dock.show_clarify()       ← dock 树生成 "clarify" 节点
  ├─ ctx.interact(UserInteraction(prompt=...))            ← 等待用户手动输入
  │     └─ app.ask_text()                                ← TUI 底部输入框
  └─ _emit_clarify_answer(clarify_id, answer, ...)       ← 发射 ClarifyAnswerSubmitted
        └─ DockEventConsumer → dock.resolve_clarify()    ← 节点标记 done + 追加 User 子节点
```

UI 事件只负责"同步渲染卡片"，真正的用户输入仍走 `ctx.interact`。当 UI 事件总线活跃时，`ctx.interact` 传 **空 options + prompt=`"Question:"`**（卡片已展示完整问题与建议项，输入框不再重复 suggestions）；否则传完整 question + options（兼容无 UI 运行时，suggestions 拼入 prompt 显示）。

> **为何 UI 活跃时传空 options**：`tool_executor/helpers.py:196-202` 的 `interact` 回调在 `options` 非空（list[str]）时会把 suggestions 拼进 prompt（`f"{prompt} ({suggestions})"`）。若 UI 活跃时仍传 options，输入框会显示 `Question: (opt1 / opt2)`，与卡片中的 Suggestions 重复。传空 options 可命中 `helpers.py:203-207` 的 `else` 分支，直接用 prompt 不拼接。完整 options 已通过 `ClarifyPromptShown` 事件交给 dock 卡片渲染，信息不丢失。

## Data Model

### 新增 UI 事件（`src/voidx/ui/output/events/schema.py`）

```
ClarifyPromptShown
├── kind: Literal["clarify_prompt.shown"] = "clarify_prompt.shown"
├── agent_id: int = -1                          (继承自 UiEventBase)
├── clarify_id: str
├── question: str
└── options: list[str] = []                     (建议答案，保持 list[str] 语义)

ClarifyAnswerSubmitted
├── kind: Literal["clarify_answer.submitted"] = "clarify_answer.submitted"
├── agent_id: int = -1
├── clarify_id: str
├── answer: str
├── cancelled: bool = False
└── was_custom_input: bool = True               (恒为 True，仅为与 checkpoint 事件对称而保留；前端不应依赖此字段区分分支)
```

无需单独的 Payload 类（clarify 的 question + options 结构简单，直接内联在事件中）。

### 新增 dock 状态（`src/voidx/ui/output/dock/app.py`）

```
BottomInputDock
├── _clarify_nodes: dict[str, OutputNode] = {}   (镜像 _checkpoint_nodes)
```

在 `__init__`（`app.py:45`）和 `reset()`（`app.py:178`）中初始化/清空。clarify 节点跨 turn 保留（`start_turn` 不清理 `_clarify_nodes`），与 `_checkpoint_nodes` 行为一致——已 resolve 的卡片作为历史记录留在 dock 树中。

## API Contract

### 注册点清单（新增 UI 事件类型必须接入的 4 处）

新增 `ClarifyPromptShown` / `ClarifyAnswerSubmitted` 事件后，除下方各小节描述的逻辑改动外，还必须在以下 4 处完成注册，否则事件无法被发射或消费：

| 文件 | 改动 |
|------|------|
| `src/voidx/ui/output/events/schema.py` | 定义两个事件类（见 Data Model）；在 `UiEvent` 联合类型（`schema.py:288-325`）末尾追加 `\| ClarifyPromptShown \| ClarifyAnswerSubmitted` |
| `src/voidx/ui/output/events/__init__.py` | 在 import 块（L8-51）和 `__all__`（L62+）中导出 `ClarifyPromptShown`、`ClarifyAnswerSubmitted` |
| `src/voidx/ui/output/events/consumers.py` | 在 import 块（L18-35）加入 `ClarifyPromptShown`、`ClarifyAnswerSubmitted`，否则 `DockEventConsumer` 的 case 分支会 `NameError` |
| `src/voidx/ui/output/dock/nodes.py` | 在 `DockNodeMixin` 基类列表（L27-32）追加 `DockClarifyNodeMixin`，并在文件头 import（L24 附近）；否则 `BottomInputDock` 无 `show_clarify`/`resolve_clarify` 方法 |

### ClarifyTool.execute（`src/voidx/tools/clarify.py`）

- **Signature**: `async def execute(self, args: dict, ctx: ToolContext) -> ToolResult`
- **变更**:
  1. 生成 `clarify_id = uuid4().hex`
  2. 调用 `ctx.interact` 前调用 `_emit_clarify_shown(clarify_id, inp)`，返回 `event_ui_active: bool`
  3. `ctx.interact` 的参数：
     - UI 活跃（`event_ui_active=True`）：`prompt="Question:"`，**`options=[]`**（空，避免 `helpers.py` 把 suggestions 拼入输入框 prompt；完整 options 已通过事件交给卡片渲染）
     - UI 未活跃：`prompt=inp.question`，`options=inp.options`（退化为原有行为，suggestions 拼入 prompt 显示）
  4. 拿到 response 后调用 `_emit_clarify_answer(clarify_id, response)`
  5. 其余逻辑（cancelled 处理、`_infer_state_patch`、ToolResult 构造）不变

### DockClarifyNodeMixin（`src/voidx/ui/output/dock/nodes_clarify.py`，新建）

```python
class DockClarifyNodeMixin:
    def show_clarify(
        self,
        clarify_id: str,
        question: str,
        options: list[str],
        *,
        parent: OutputNode | None = None,
    ) -> OutputNode:
        # node_type="clarify", header="● voidx clarify"
        # body 由 _clarify_body() 生成（富文本 rich markup，与 _checkpoint_body 风格对齐）
        # payload: {"interaction": "clarify", "clarify_id": ..., "question": ..., "options": ...}
```

**`_clarify_body` 渲染规范**（对齐 `_checkpoint_body` 的富文本风格）：

```python
_QUESTION_LABEL = "[#EBCB8B]Question:[/#EBCB8B]"      # 与 _PLAN_LABEL 同色
_SECTION_TITLE = "[bold #D8DEE9]{}:[/bold #D8DEE9]"   # 与 _checkpoint_body 同值，在本模块重新定义
_BODY = "[#D8DEE9]{}[/#D8DEE9]"                       # 与 _checkpoint_body 同值，在本模块重新定义
_SUGGESTION_PREFIX = "[#61AFEF]-[/#61AFEF]"           # 与 _STEP_NUM 同色系

def _clarify_body(question: str, options: list[str]) -> list[str]:
    body: list[str] = []
    if question.strip():
        body.append(f"{_QUESTION_LABEL} {_BODY.format(escape(question))}")
    suggestions = [str(o) for o in options if str(o).strip()]
    if suggestions:
        if body:
            body.append("")
        body.append(_SECTION_TITLE.format("Suggestions"))
        body.extend(f"{_SUGGESTION_PREFIX} {_BODY.format(escape(s))}" for s in suggestions)
    return body
```

> **对齐说明**：`_clarify_body` 采用与 `_checkpoint_body` 相同的 rich markup 常量值与分段风格（label + section title + 列表项），保持视觉一致性。由于 `_SECTION_TITLE`/`_BODY` 在 `nodes_checkpoint.py` 中是模块级私有常量（下划线前缀），跨模块 import 不规范，因此在 `nodes_clarify.py` 中重新定义同值常量。颜色方案：Question 用 `#EBCB8B`（与 Plan 同色），Suggestions 标题用 `bold #D8DEE9`，建议项前缀用 `#61AFEF`（与 step 序号同色系）。

    def resolve_clarify(
        self,
        clarify_id: str,
        answer: str,
        *,
        cancelled: bool = False,
        was_custom_input: bool = True,
    ) -> None:
        # 节点标记 done，header 更新为 "● voidx clarify answered/skipped"
        # 追加 "User: <answer>" 子节点（cancelled 时为 "User: skipped"）
        # 子节点 payload={"full_width_user_row": True}（与 checkpoint 对齐，全宽背景色渲染）
```

> **对齐说明**：`resolve_clarify` 的 User 子节点必须携带 `payload={"full_width_user_row": True}`，与 `nodes_checkpoint.py:69` 最新改动（commit `2ecf588`）一致。该 payload 被 `tree.py` 的 `_is_full_width_user_row()` 检测，使 `User:` 行以全宽背景色渲染（`_permission_row`），与 permission/checkpoint 的 User 行视觉风格统一。

### DockEventConsumer

新增两个 case 分支：

```python
case ClarifyPromptShown() as e:
    return self._dock.show_clarify(
        e.clarify_id,
        e.question,
        e.options,
        parent=self._agent_parent(e.agent_id),
    )
case ClarifyAnswerSubmitted() as e:
    return self._dock.resolve_clarify(
        e.clarify_id,
        e.answer,
        cancelled=e.cancelled,
        was_custom_input=e.was_custom_input,
    )
```

### 事件发射辅助函数（`src/voidx/tools/clarify.py`）

```python
def _emit_clarify_shown(clarify_id: str, inp: ClarifyInput) -> bool:
    # 镜像 _emit_checkpoint_shown：try import ui_events + schema，is_running 检查，emit_direct

def _emit_clarify_answer(
    clarify_id: str,
    response: UserResponse,
) -> None:
    # 镜像 _emit_checkpoint_decision
```

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| UI 事件总线未运行（`ui_events.is_running` 为 False） | `_emit_clarify_shown` 返回 False，`ctx.interact` 传完整 question + options，退化为原有行为（`helpers.py` 把 suggestions 拼入 prompt 显示） |
| `voidx.ui.output.events` 导入失败（非 TUI 运行时） | `_emit_clarify_shown` 捕获 ImportError 返回 False，同上降级 |
| 未知 clarify_id 的 `ClarifyAnswerSubmitted` | `resolve_clarify` 记录 debug 日志并返回（镜像 `resolve_checkpoint`） |
| 用户取消输入（`response.cancelled`） | 发射 `ClarifyAnswerSubmitted(cancelled=True)`，dock 节点标记为 skipped |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| options 保持 `list[str]` 渲染为 Suggestions 文本 | 改为可点选按钮（像 checkpoint） | clarify 语义是"建议答案"，用户仍需手动输入；保持与 `UserInteraction` 的 `list[str]` 路由一致 |
| 不新建 ClarifyPayload 类 | 新建 `ClarifyQuestionPayload` | clarify 的 question + options 结构简单，内联在事件中即可，无需额外 Payload 类 |
| `was_custom_input` 恒为 True，保留字段 | 移除该字段 | clarify 始终走 `ask_text` 手动输入，字段无信息量；但保留可让 `ClarifyAnswerSubmitted` 与 `CheckpointDecisionSubmitted` 结构对称，降低前端/消费侧的分支差异。schema 注释明确"恒为 True，前端不应依赖" |
| prompt 在 UI 活跃时简化为 "Question:" | 始终用完整 question | 与 checkpoint 的 `prompt="Plan:"` 模式对称；卡片已展示完整问题，避免重复 |
| 新建独立 `nodes_clarify.py` | 合并进 `nodes_checkpoint.py` | 遵循项目"模块小且按职责命名"规则；checkpoint 与 clarify 是不同交互类型 |

## Open Questions

- [ ] 前端（web/desktop）是否需要单独处理 `clarify_prompt.shown` / `clarify_answer.submitted` 事件，还是复用 checkpoint 的渲染组件？（本设计只覆盖 TUI dock 层，前端适配由前端侧决定）
