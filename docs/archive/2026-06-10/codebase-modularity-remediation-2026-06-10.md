# 代码模块化整改分阶段设计

> **Status: Done**
> **Date:** 2026-06-10
> **Source:** `docs/reviews/2026-06-09-full-code-review.md`

## Context

2026-06-09 的全量代码 review 识别了三类主要问题：

1. 若干核心文件过大，特别是 TUI renderer、agent graph、slash handler、file tools 和 UI events。
2. `runtime.ui` 被 agent graph 多处直接引用，agent 核心逻辑与 UI 输出通道耦合较深。
3. 测试文件体量过大，尤其是 `tests/test_pure_tui.py`，后续重构时定位和维护成本偏高。

这份文档把 review 结论转成可执行的阶段计划。目标不是一次性重写架构，而是在保持行为稳定的前提下逐步缩小文件、明确边界、降低后续改动风险。

## Current Snapshot

以下是 2026-06-10 启动整改前抽查到的工作区状态，用于校正文档中的旧数字：

| 文件 | 当前行数 | 说明 |
|------|----------|------|
| `src/voidx/ui/tui/renderer.py` | 905 | `_TerminalRendererMixin` 约 41 个方法，仍是最大 TUI 渲染集中点 |
| `src/voidx/agent/graph/core.py` | 676 | `VoidXGraph` 继承 5 个 mixin，初始化并持有大量运行时状态 |
| `src/voidx/agent/slash/handler.py` | 636 | `SlashHandler` 包含 host 访问代理和 dispatch 逻辑 |
| `src/voidx/tools/file_ops.py` | 618 | 文件读写工具与 ApplyPatch diff 引擎混在一起 |
| `src/voidx/ui/output/events/__init__.py` | 466 | 事件总线、组合 consumer、dock consumer 同处一个 `__init__.py` |
| `tests/test_pure_tui.py` | 3204 | TUI 渲染相关测试高度集中 |

`runtime.ui` 的直接引用主要集中在 agent graph 和 slash 层。排除 `runtime/__init__.py` 的 re-export 后，review 中的高扇入判断仍然成立。

## Goals

- 将 review 中的建议拆成可单独评审、可单独回滚、可单独验证的阶段。
- 优先处理低风险的模块边界问题，为后续架构调整减少噪音。
- 保持现有公开导入路径、工具 id、事件语义和用户可见行为。
- 为每个阶段定义明确的验证命令和退出条件。

## Non-Goals

- 不在同一个阶段内同时重构 graph、TUI renderer、slash handler 和 UI event bus。
- 不以减少行数作为唯一目标；如果拆分会降低 schema 可读性或增加间接层，则暂缓。
- 不改工具 id、slash 命令名、UI event payload 语义或 transcript/session 存储格式。
- 不在没有额外设计的情况下把 `VoidXGraph` 全面改成组合模式。

## Guiding Principles

- **先机械拆分，再行为重构。** 先把独立职责移到更合适的文件，保留原入口和测试覆盖。
- **先低耦合模块，再高耦合核心。** `tools/file_ops.py` 和 `ui/output/events` 比 graph 架构更适合先处理。
- **每阶段只移动一个边界。** 阶段内可以有多个文件改动，但只能服务于同一个边界。
- **测试先保护行为。** 纯移动代码时优先跑现有 focused tests；高风险阶段再补专门测试。

## Phase 1: Low-Risk Extraction

### 1A. Extract ApplyPatch

**Scope**

- 新增 `src/voidx/tools/apply_patch.py`。
- 将 `ApplyPatchInput`、`ApplyPatchTool`、patch dataclasses、diff parser、apply helpers、rollback helpers 从 `file_ops.py` 移入新模块。
- `file_ops.py` 保留普通文件工具：读文件、列目录、写文件、编辑文件。
- 原工具注册入口继续导出/注册同一个 `apply_patch` tool id。

**Compatibility**

- `ApplyPatchTool.id` 保持 `apply_patch`。
- 工具输入 schema 不变：`patch` 和 `dry_run`。
- 错误消息和 metadata 尽量保持原样，避免破坏测试或调用端断言。

**Validation**

- `.venv/bin/python -m pytest tests/test_tools/test_basic.py -v`
- 若有独立 apply patch 测试，补跑对应文件。
- 手动检查 `rg "ApplyPatch" src/voidx tests`，确认导入路径合理。

**Exit Criteria**

- `file_ops.py` 不再包含 unified diff parser。
- `apply_patch.py` 可以独立阅读，不依赖 file read/write tool 的实现细节。
- 现有工具测试通过。

### 1B. Split UI Event Consumers

**Scope**

- 将 `src/voidx/ui/output/events/__init__.py` 拆成小文件：
  - `bus.py`: `UiEventBus` 和 `_QueuedEvent`
  - `consumers.py`: `CompositeEventConsumer`、`DockEventConsumer`
  - `schema.py`: 保持现状
  - `__init__.py`: 只做兼容导出
- 不改变事件处理顺序、direct emit 语义或 dock mutation 行为。

**Compatibility**

- 现有导入路径继续可用，例如 `from voidx.ui.output.events import UiEventBus`。
- `ui_events` 单例的创建位置保持对调用方透明。

**Validation**

- `.venv/bin/python -m pytest tests/test_ui_events.py -v`
- `.venv/bin/python -m pytest tests/test_agent/test_stream_llm.py -v`
- `rg "voidx.ui.output.events" src/voidx tests` 检查导入兼容性。

**Exit Criteria**

- `events/__init__.py` 只负责 public API re-export 和单例导出。
- bus 与 consumer 可分别测试和阅读。

### 1C. Split Pure TUI Tests By Feature

**Scope**

- 将 `tests/test_pure_tui.py` 按功能域拆为多个测试文件。
- 首轮只做测试移动，不修改生产代码。
- 建议目标文件：
  - `tests/test_tui_status_rendering.py`
  - `tests/test_tui_input_rendering.py`
  - `tests/test_tui_choice_handling.py`
  - `tests/test_tui_dock_rendering.py`
  - `tests/test_tui_terminal_edges.py`

**Compatibility**

- fixture 和 helper 若被多个测试文件使用，提取到 `tests/conftest.py` 或 `tests/tui_helpers.py`。
- 不改变测试断言内容，避免把行为变更混入文件移动。

**Validation**

- `.venv/bin/python -m pytest tests/test_pure_tui.py -v` 在拆分前作为 baseline。
- `.venv/bin/python -m pytest tests/test_tui_*.py -v` 在拆分后验证。
- `.venv/bin/python -m pytest tests/ -q` 作为可选广覆盖验证。

**Exit Criteria**

- 单个 TUI 测试文件不再超过约 900 行。
- 测试 helper 的命名能说明用途，不形成新的大杂烩。

## Phase 2: TUI Renderer Decomposition

**Scope**

- 拆分 `src/voidx/ui/tui/renderer.py` 中的职责，但不改变 `PureTui` 对 renderer mixin 的调用方式。
- 优先提取纯函数和小型 helper：
  - 状态栏渲染
  - 输入区域渲染
  - busy activity 渲染
  - pinned todo 渲染
  - frame layout / terminal size 计算

**Proposed Files**

- `src/voidx/ui/tui/render_status.py`
- `src/voidx/ui/tui/render_input.py`
- `src/voidx/ui/tui/render_activity.py`
- `src/voidx/ui/tui/render_todo.py`
- `src/voidx/ui/tui/render_frame.py`

**Constraints**

- 不改变终端 escape sequence 策略。
- 不改变 streaming flicker 相关优化语义。
- 不把 `PureTui` 状态散落到多个双向依赖模块里；优先传入显式数据结构或简单参数。

**Validation**

- `.venv/bin/python -m pytest tests/test_tui_*.py -v`
- `.venv/bin/python -m pytest tests/test_agent/test_core_flow.py -v`
- 如本地可交互，手动跑 TUI，覆盖普通输入、流式输出、choice prompt、busy 状态、pinned todo。

**Exit Criteria**

- `renderer.py` 只保留 orchestration 和 mixin glue。
- 单个渲染 helper 文件有清晰职责和 focused tests。

## Phase 3: Slash Handler Boundary

**Scope**

- 降低 `SlashHandler` 对 `VoidXGraph` 私有状态的 feature envy。
- 引入明确的 host interface 或 `SlashContext` 数据对象，替代零散 `_host_*` 代理方法。

**Candidate Approach**

优先使用 `Protocol` 表达 slash handler 真正需要的 host 能力：

```python
class SlashHost(Protocol):
    @property
    def app(self) -> PureTui | None: ...
    @property
    def permission(self) -> PermissionService: ...
    async def persist_runtime_state(self) -> None: ...
```

如果需要一次性传递大量稳定数据，再引入 `SlashContext`。不要为了减少方法数量而把可变状态快照化，避免 slash 命令读到过期状态。

**Validation**

- `.venv/bin/python -m pytest tests/test_agent/test_core_flow.py -v`
- `.venv/bin/python -m pytest tests/test_agent/test_slash*.py -v`，若存在匹配测试。
- 手动覆盖 `/model`、`/mcp`、`/session`、`/skills`、diff display 相关命令。

**Exit Criteria**

- `SlashHandler` 的 host 访问边界可以通过一个 interface 阅读。
- `VoidXGraph` 不再需要暴露额外私有字段给 slash handler。

## Phase 4: UI Port Boundary

**Scope**

- 为 agent graph 定义 UI 输出端口，逐步替换对 `runtime.ui` 的直接依赖。
- 目标是把 graph 层依赖从具体 UI singleton 迁移到明确接口，不是立即删除 `runtime.ui`。

**Proposed Shape**

- 新增 graph-facing interface，例如 `AgentUiPort`：
  - print/status/warning/error 输出
  - tool lifecycle events
  - assistant stream events
  - permission prompt events
  - todo events
- 默认实现适配现有 `runtime.ui` 和 `ui_events`。
- `VoidXGraph.__init__` 接收或构造 `AgentUiPort`，各 mixin 通过 `self._ui` 使用。

**Migration Strategy**

1. 先为 `runtime.ui` 包一层 adapter，不改调用语义。
2. 每次迁移一个 graph module，例如 `streaming.py`、`tool_execution.py`、`permissions.py`。
3. 清点 slash 层 UI 入口：`agent/slash/runtime.py`、`agent/slash/handler.py`、session rollback/dock reset 和 code IDE 状态 helper 可一起迁到 UI port，避免 slash 层继续直接依赖旧 `runtime.ui` facade。
4. 保留兼容路径，直到所有 graph 直接导入都消失。

**Validation**

- `rg "from voidx.runtime.ui" src/voidx/agent/graph`
- `.venv/bin/python -m pytest tests/test_agent/test_core_flow.py -v`
- `.venv/bin/python -m pytest tests/test_agent/test_stream_llm.py -v`
- `.venv/bin/python -m pytest tests/test_ui_events.py -v`

**Exit Criteria**

- agent graph 不再直接导入 `voidx.runtime.ui`。
- UI event ordering 和 transcript 输出保持不变。

## Phase 5: Graph Composition Refactor

**Scope**

- 在 UI port 边界稳定后，再评估将部分 mixin 迁移为组合组件。
- 这一阶段需要单独设计，不应作为 Phase 1-4 的顺手重构。

**Subphases**

- **5A: `CompactionCoordinator`** — 管理 compaction 状态、触发条件、summary agent、持久化 pruning。低风险，已有 `tests/test_compaction.py` 和 core flow 覆盖，作为组合组件模式的首个切片。
- **5B: `SessionRuntime`** — 管理 session persistence、transcript snapshot、title generation、clear/resume runtime reset。需要在 5A 验证组件接线模式后再做。
- **5C1: `GraphToolExecutor`** — 先拆 `_execute_tools` tool lifecycle。`GraphToolExecutionMixin` 保留 `_execute_tools` 和 `_tool_result_ok` 旧入口，只作为兼容代理。
- **5C2: `GraphTurnRunner`** — 拆 `_run_once` turn lifecycle。`GraphTurnMixin` 保留 `_run_once` 和 turn helper 旧入口，只作为兼容代理。

**Constraints**

- `VoidXGraph` 仍是外部入口和 orchestration facade。
- 每次只迁移一个组件，保留原测试，并为新组件增加 focused tests。
- 不改变 LangGraph state contract。
- 保留原有 graph 私有方法名作为兼容代理，例如 `_maybe_compact` 仍可被测试和旧调用点 patch。

**Validation**

- `.venv/bin/python -m pytest tests/test_agent/test_core_flow.py -v`
- `.venv/bin/python -m pytest tests/test_agent/ -v`
- 视改动范围跑 `.venv/bin/python -m pytest tests/ -v`

**Exit Criteria**

- `VoidXGraph` 的初始化职责和运行职责分离。
- 核心 turn loop、tool execution、session persistence 可以分别测试。

## Deferred Items

以下 review 建议暂不作为前两阶段目标：

- **合并 `tools/git.py` Args 模型。** 当前 per-command Pydantic 模型增强了校验和代码可读性。除非工具 schema 需要显著收敛，否则不优先处理。
- **拆 `agent/runtime_context.py` 顶层 helper。** 可以在触碰 runtime context 功能时顺手做，但单独拆分收益有限。
- **重构 `ui/output/tree.py`。** tree 同时承担结构和渲染映射，耦合有实际业务原因。需要先明确新边界再动。

## Implementation Progress

- 2026-06-10: Phase 1A completed. `ApplyPatchTool` moved to `src/voidx/tools/apply_patch.py`; shared file mtime/staleness helpers moved to `src/voidx/tools/file_state.py`.
- 2026-06-10: Phase 1B completed. `UiEventBus` moved to `src/voidx/ui/output/events/bus.py`; event consumers moved to `src/voidx/ui/output/events/consumers.py`; `events/__init__.py` now provides public re-exports and the `ui_events` singleton.
- 2026-06-10: Phase 1C completed. `tests/test_pure_tui.py` was removed and split into six focused `tests/test_tui_*.py` files plus `tests/tui_helpers.py`.
- 2026-06-10: Phase 2 completed. `src/voidx/ui/tui/renderer.py` is now a composition entrypoint; frame, input, busy activity, pinned todo, and status rendering helpers moved to focused `render_*.py` modules.
- 2026-06-10: Phase 3 completed. `SlashCommandHost` and `SlashHostAdapter` moved to `src/voidx/agent/slash/host.py`; host compatibility for legacy `_app`, `_settings`, `_session`, manager, and task state fields is centralized there. `SlashHandler` no longer owns `_host_*` proxy methods, and slash mixins now access graph-owned state through `self.host`.
- 2026-06-10: Phase 3 review follow-up completed. Session lifecycle compat fallback was moved fully behind `SlashHostAdapter`; `SlashCommandHost` now documents the adapter surface used by slash mixins, including manager access, guidance capability, and interaction mode value.
- 2026-06-10: Phase 4 completed. `AgentUiPort` and `RuntimeUiPort` live in `src/voidx/runtime/ui_port.py`; `VoidXGraph` now owns `self._ui`, graph mixins use that port for console, dock, session tracker, and UI event access.
- 2026-06-10: Phase 4 review follow-up completed. `SlashCommandHost` manager annotations no longer import MCP/LSP managers at runtime; `voidx.lsp` and `voidx.tools` package exports use lazy loading to avoid manager/registry cycles; `AgentUiPort` now exposes typed child protocols for UI sink, dock, event bus, and session tracker; stale `via_events`/`ui_events` test patches were replaced with fake UI ports; `ui_port.__all__` no longer advertises singleton/private helper names.
- 2026-06-10: Phase 5A completed. `GraphCompactionCoordinator` moved compaction execution behind a composition component while `GraphCompactionMixin` keeps `_maybe_compact`, `_compact_session_history`, `_run_compaction_agent`, and related private method names as compatibility delegates. `VoidXGraph` now owns `_compaction_coordinator`.
- 2026-06-10: Phase 5B completed. `GraphSessionRuntime` now owns runtime state persistence, transcript snapshot persistence, and smart title generation while `GraphSessionMixin`, `GraphTranscriptMixin`, and `GraphTitleMixin` keep their existing private/public method names as compatibility delegates. `VoidXGraph` now owns `_session_runtime`.
- 2026-06-10: Phase 5C1 completed. `GraphToolExecutor` now owns tool execution lifecycle, tool UI events, result state patches, barrier/deferred message handling, and child-agent buffer ordering while `GraphToolExecutionMixin` remains a compatibility proxy. `VoidXGraph` now owns `_tool_executor`.
- 2026-06-10: Phase 5C2 completed. `GraphTurnRunner` now owns top-level turn execution, user message persistence, compaction-before-invoke, runtime snapshot persistence, turn-end transcript/todo cleanup, and cancellation rollback while `GraphTurnMixin` remains a compatibility proxy. `VoidXGraph` now owns `_turn_runner`.
- 2026-06-10: Phase 5 review follow-up completed. Composition proxy lazy-init branches are documented as bare mixin/test-host compatibility paths, and `tool_execution.py` now only re-exports the compatibility helper surface still used by existing callers; state patch helper tests import directly from `tool_executor.py`.
- 2026-06-10: Deferred UI/type-boundary follow-up completed. `runtime/ui_port.py` now exposes only graph-facing UI port protocols plus `RuntimeUiPort`/`runtime_ui_port`; event schemas, output tree/types, TUI, gateway, transcript, code-IDE, and slash command imports use their native modules or the runtime UI facade directly. `GraphComponentHost` was split into concrete component host protocols for run loop, compaction, tool execution, and permission surfaces, with `GraphComponentHost` retained only as a whole-graph composite check.

Validated with:

- `.venv/bin/python -m compileall -q src/voidx/tools src/voidx/ui/output/events`
- `.venv/bin/python -m compileall -q src/voidx/ui/tui`
- `.venv/bin/python -m compileall -q src/voidx/agent/slash`
- `.venv/bin/python -m compileall -q src/voidx/agent/slash src/voidx/agent/graph`
- `.venv/bin/python -m compileall -q src/voidx/runtime src/voidx/agent/graph src/voidx/agent/slash`
- `rg "^from voidx\.runtime\.ui import" src/voidx/agent`
- `.venv/bin/python -c "from voidx.lsp.manager import LspManager; print(LspManager.__name__)"`
- `.venv/bin/python -c "from voidx.tools import ToolRegistry; print(ToolRegistry.__name__)"`
- `.venv/bin/python -m pytest tests/test_tools/test_basic.py -v`
- `.venv/bin/python -m pytest tests/test_ui_events.py -v`
- `.venv/bin/python -m pytest tests/test_agent/test_stream_llm.py -v`
- `.venv/bin/python -m pytest tests/test_agent/test_core_flow.py -v`
- `.venv/bin/python -m pytest tests/test_agent/test_slash_model.py tests/test_agent/test_slash_session.py tests/test_agent/test_slash_mcp.py tests/test_agent/test_slash_skills.py tests/test_agent/test_slash_init.py tests/test_lsp.py -q`
- `.venv/bin/python -m pytest tests/test_agent/test_core_flow.py tests/test_agent/test_stream_llm.py tests/test_ui_events.py tests/test_agent/test_slash_model.py tests/test_agent/test_slash_session.py tests/test_agent/test_slash_mcp.py tests/test_agent/test_slash_skills.py tests/test_agent/test_slash_init.py tests/test_lsp.py -q`
- `.venv/bin/python -m pytest tests/test_agent/test_slash_model.py tests/test_agent/test_slash_session.py tests/test_agent/test_slash_mcp.py tests/test_agent/test_slash_skills.py tests/test_agent/test_slash_init.py tests/test_agent/test_run_loop.py tests/test_agent/test_todo_events.py tests/test_compaction.py tests/test_lsp.py -q`
- `.venv/bin/python -m pytest tests/test_compaction.py -q`
- `.venv/bin/python -m pytest tests/test_agent/test_core_flow.py -q`
- `.venv/bin/python -m pytest tests/test_agent/test_run_loop.py -q`
- `.venv/bin/python -m pytest tests/test_agent/test_session.py -q`
- `.venv/bin/python -m pytest tests/test_agent/test_slash_session.py tests/test_agent/test_session.py -q`
- `.venv/bin/python -m pytest tests/test_tui_*.py -v`
- `.venv/bin/python -m pytest tests/test_agent/test_core_flow.py tests/test_agent/test_todo_events.py tests/test_tools/test_basic.py -q`
- `.venv/bin/python -m compileall -q src/voidx/agent/graph`
- `git diff --check`
- `.venv/bin/python -m pytest tests/test_agent/test_run_loop.py tests/test_agent/test_core_flow.py tests/test_compaction.py -q`
- `.venv/bin/python -m compileall -q src/voidx/runtime src/voidx/agent/graph src/voidx/agent/slash`
- `.venv/bin/python -m pytest tests/test_agent/test_core_flow.py tests/test_agent/test_run_loop.py tests/test_agent/test_session.py tests/test_agent/test_slash_model.py tests/test_agent/test_slash_session.py tests/test_agent/test_todo_events.py tests/test_tools/test_basic.py tests/test_compaction.py -q`

## Rollout Order

推荐按以下 PR 顺序执行：

1. PR 1: Extract `ApplyPatchTool` into `tools/apply_patch.py`
2. PR 2: Split `ui/output/events/__init__.py`
3. PR 3: Split `tests/test_pure_tui.py`
4. PR 4: Decompose TUI renderer helpers
5. PR 5: Introduce slash host interface
6. PR 6+: Introduce graph UI port and migrate graph modules incrementally
7. Follow-up design: Graph composition refactor

每个 PR 都应独立可合并。不要把机械文件移动和行为变更混在同一个 PR 中。

## Open Questions

- TUI renderer 拆分时，是否同步引入更细的 render state dataclass，还是只做函数提取。
