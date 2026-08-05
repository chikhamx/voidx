---
name: src-voidx-modular-architecture-refactor
display_name: src/voidx 模块化架构重构规格
description: 在保持外部行为、UI 协议和持久化格式不变的前提下，将 src/voidx 重构为职责清晰、依赖单向、可独立测试的模块化架构
doc_type: design-and-execution-spec
audience: human+llm
status: approved
date: 2026-08-05
---

# `src/voidx` 模块化架构重构规格

## 1. 结论

采用分阶段、单轨、无内部兼容层的重构方式，将 `src/voidx` 收敛为一张可由静态测试证明的单向依赖 DAG。

本重构允许大规模移动、重命名、拆分和聚合内部 Python 模块，但不得改变用户可见逻辑。每个阶段必须先锁定行为契约，再增加会失败的目标架构约束，完成一个可独立运行的垂直切片后立即删除旧实现；禁止新旧架构长期并存。

最终依赖方向为：

```text
main
  ↓
bootstrap
  ├──→ presentation
  ├──→ agent adapters / application
  ├──→ tooling adapters / application
  └──→ config / llm / mcp / lsp / skills

presentation ──→ agent facade/contracts
agent adapters ──→ agent application ──→ agent domain + agent ports
tooling adapters ──→ tooling application ──→ tooling domain + tooling ports
feature adapters ──→ persistence / platform / observability
```

核心 application/domain 只能依赖抽象和领域模型；adapter 可以导入端口以实现它，但只有 `bootstrap` 负责选择具体 adapter 并把它绑定到端口。

## 2. 背景与当前状态

### 2.1 规模

截至本规格编写时：

- `src/voidx` 有 447 个 Python 文件、68,606 行。
- `src/voidx/agent` 有 136 个 Python 文件、21,039 行，是主要复杂度中心。
- 现有 `src/tests/test_agent/test_dependency_cycles.py` 已证明文件级运行时和完整 import graph 没有 SCC。
- 文件级无环不代表分层正确；顶层包仍存在多组双向依赖。

### 2.2 已确认的结构问题

1. `runtime ↔ workflow`
   - `src/voidx/runtime/task_state.py` 导入 `voidx.workflow.types`。
   - `src/voidx/workflow/reconcile.py` 导入 `voidx.runtime.task_state`。
2. `agent ↔ memory`
   - `src/voidx/memory/thread_store.py` 持有 Agent domain 类型。
   - `src/voidx/agent/application`、`runtime`、`goal`、`loop` 直接使用具体 `ThreadStore`。
3. `permission ↔ tools`
   - permission engine 直接导入 PowerShell tool sandbox。
   - shell tools 直接导入 permission implementation。
4. `mcp ↔ tools`
   - MCP manager/server/gateway 直接依赖 `ToolRegistry`、`BaseTool` 和 Web tools。
   - Web MCP tool 反向导入 MCP client/schema。
5. `application ↔ domain`
   - `agent.domain.prompt_policy` 依赖 application 的 prompt DTO 和 runtime context。
6. `application ↔ loop`
   - loop prompt materializer 使用 application attachments 的私有解析符号。
7. `AgentService` 同时负责应用生命周期、终端/Web 展示、Gateway 启动和协议命令。
8. composition 使用 `SimpleNamespace`、动态属性和 `getattr/hasattr` 隐式接线。

### 2.3 与历史计划的关系

`docs/archive/agent-architecture-refactor-plan.md` 已完成第一轮 Agent 分层，产出了 `domain/`、`application/`、`ports/`、`infrastructure/`、`composition.py` 和 `facade.py`。本规格不回退这些成果，也不重新搭建平行架构；它以当前代码为基线，继续解决上一轮未覆盖的包级反向依赖、持久化绑定、展示层泄漏和职责分散。

历史归档文档仅作背景，不是本次实施的规范来源；冲突时以本规格为准。

## 3. 目标与非目标

### 3.1 目标

- 每个目录和文件有唯一、可描述的主职责。
- 顶层包和包内层次都形成单向 DAG。
- Agent、Tooling 等核心用例依赖 ports，不依赖 SQLite、LangGraph、UI、MCP client 等具体实现。
- goal、loop、workflow、subagent 各自拥有完整垂直切片，不再横跨多个平行应用目录。
- UI/TUI/Web Gateway 只通过 facade、用例接口和语义事件与核心交互。
- composition 使用显式、类型化构造，不使用动态属性作为依赖注入。
- 架构规则由 AST 测试强制执行，并能报告精确 `source -> target`。
- 每个阶段结束时仓库可运行、相关测试通过且旧路径已删除。

### 3.2 非目标

- 不改变产品功能、Agent 决策逻辑、prompt 规则或状态机。
- 不重新设计 UI JSON-RPC 协议。
- 不迁移数据库 schema 或改变持久化 JSON 字段。
- 不更换 LangGraph、Pydantic、SQLite、Typer、WebSocket 等技术栈。
- 不优化性能、并发策略、错误文案或权限规则，除非是保持现有行为所必需的机械调整。
- 不保证 `src/voidx` 内部 Python import path 兼容。
- 不增加旧路径 re-export、`__getattr__` 代理或 deprecation shim。

## 4. 不可变兼容契约

以下内容必须保持不变；任何差异都视为行为变更，必须回退或单独立项：

### 4.1 CLI 和入口

- `pyproject.toml` 中 console script 仍为 `voidx = "voidx.main:cli"`。
- CLI 参数、短选项、默认值、exit code 和 `--help` 语义不变。
- `--web`、`--web-headless`、resume/new/chat 等启动路径不变。
- `src/voidx/main.py` 可以改内部 import，但保持 `cli` 公开入口。

### 4.2 Agent 与 prompt

- chat/coding/goal/loop/subagent profile 的可见 prompt 内容和拼装顺序不变。
- workflow gate、persona、tool filter、message trimming、compaction 和 turn event 顺序不变。
- goal/loop/workflow/subagent 的状态、合法转换、重试、取消、恢复和调度语义不变。
- tool call id、tool message replay、附件解析结果和 transcript 内容不变。

### 4.3 Tool 与权限

- 所有 tool id、description、输入 JSON Schema、默认值、输出文本和 metadata 语义不变。
- permission mode、sandbox mode、risk 分类、session rules、grant 合并、审批策略不变。
- Bash/PowerShell 路由、提示、超时、进程终止和路径检查行为不变。
- MCP/LSP tool 暴露方式、错误映射、timeout 和结果格式不变。

### 4.4 UI、Web 和桌面

- `frontend/src/rpc/protocol.schema.json` 内容不变。
- JSON-RPC method、notification、payload 字段和字段语义不变。
- TUI/Web/desktop 的 transcript、dock、状态、permission/clarify/checkpoint 交互不变。
- Gateway session/thread 的创建、切换、删除、重命名、fork 和恢复语义不变。

### 4.5 配置和持久化

- Settings 文件位置、环境变量、默认值、profile/API key/MCP 配置语义不变。
- SQLite 表、列、索引、事务边界和 JSONL 文件布局不变。
- runtime snapshot、workflow runs、todo、goal、loop、compaction、session time 和 transcript 的 JSON key 不变。
- 旧 session 数据必须继续成功恢复；不得要求用户执行数据迁移。

## 5. 设计原则

1. **依赖反转优先于目录搬迁**：先建立 port 和 mapper，再移动实现；禁止只改路径不改方向。
2. **单一 owner**：一个状态、规则或用例只能有一个 owner；adapter 可以映射但不能复制业务规则。
3. **垂直切片迁移**：一次迁移完整调用链及测试，不创建长期空壳目录。
4. **显式组合**：构造函数传入依赖；缺失依赖在启动阶段立即失败。
5. **窄端口**：按用例拆分 Protocol，单个端口原则上不超过 10 个成员，硬上限 15 个。
6. **语义事件**：核心发布技术无关事件；presentation adapter 映射成当前 UI event。
7. **无导入副作用注册**：provider/tool/plugin catalog 必须显式建立。
8. **无内部兼容层**：移动后全仓更新调用方并删除旧路径。
9. **保留 blame**：纯移动使用 `git mv`；移动和行为中立拆分尽量分成可审查提交。
10. **架构即测试**：禁止依赖必须由 AST 测试表达，不能只写在文档中。

## 6. 目标目录结构

```text
src/voidx/
├── __init__.py
├── main.py
├── bootstrap/
│   ├── __init__.py
│   ├── application.py
│   ├── agent.py
│   ├── tooling.py
│   ├── persistence.py
│   ├── providers.py
│   ├── integrations.py
│   └── resources.py
├── agent/
│   ├── __init__.py
│   ├── facade.py
│   ├── domain/
│   │   ├── task/
│   │   ├── turn/
│   │   ├── session/
│   │   ├── automation/
│   │   │   ├── goal.py
│   │   │   ├── loop.py
│   │   │   └── workflow.py
│   │   ├── subagent.py
│   │   ├── events.py
│   │   └── profile.py
│   ├── ports/
│   │   ├── turn.py
│   │   ├── session.py
│   │   ├── thread.py
│   │   ├── attempt.py
│   │   ├── outbox.py
│   │   ├── model.py
│   │   ├── tools.py
│   │   ├── events.py
│   │   ├── presentation.py
│   │   └── clock.py
│   ├── application/
│   │   ├── lifecycle.py
│   │   ├── runtime/
│   │   ├── turn/
│   │   ├── session/
│   │   ├── compaction/
│   │   ├── automation/
│   │   │   ├── goal/
│   │   │   ├── loop/
│   │   │   └── workflow/
│   │   └── subagent/
│   └── adapters/
│       ├── langgraph/
│       ├── persistence/
│       ├── tools/
│       └── subagent/
├── tooling/
│   ├── __init__.py
│   ├── domain/
│   │   ├── call.py
│   │   ├── result.py
│   │   ├── context.py
│   │   ├── permission.py
│   │   └── risk.py
│   ├── ports/
│   │   ├── tool.py
│   │   ├── approval.py
│   │   ├── events.py
│   │   ├── process.py
│   │   └── network.py
│   ├── application/
│   │   ├── registry.py
│   │   ├── executor.py
│   │   └── authorization.py
│   ├── policy/
│   │   ├── permission/
│   │   ├── shell/
│   │   ├── filesystem/
│   │   └── git/
│   ├── builtin/
│   │   ├── file/
│   │   ├── shell/
│   │   ├── git/
│   │   ├── web/
│   │   └── document.py
│   └── adapters/
│       ├── permission/
│       ├── mcp.py
│       ├── lsp.py
│       ├── skills.py
│       └── web.py
├── llm/
│   ├── domain/
│   ├── application/
│   └── adapters/providers/
├── mcp/
│   ├── domain/
│   ├── ports/
│   ├── application/
│   └── adapters/client/
├── lsp/
│   ├── domain/
│   ├── ports/
│   ├── application/
│   └── adapters/client/
├── skills/
│   ├── domain/
│   ├── application/
│   ├── adapters/
│   └── bundled/
├── config/
│   ├── domain/
│   ├── application/
│   ├── ports/
│   └── adapters/
├── presentation/
│   ├── protocol/
│   ├── gateway/
│   ├── output/
│   ├── terminal/
│   ├── slash/
│   ├── tools/
│   └── adapters/persistence/
├── persistence/
│   ├── sqlite.py
│   ├── jsonl.py
│   ├── transaction.py
│   ├── migrations.py
│   └── paths.py
├── platform/
│   ├── paths.py
│   ├── processes.py
│   ├── execution_context.py
│   ├── file_types.py
│   ├── retry.py
│   └── clock.py
├── observability/
│   ├── internal_error.py
│   ├── request_log.py
│   ├── tool_log.py
│   └── external.py
├── update/
│   └── service.py
└── data/
```

说明：

- 上述目录代表最终职责，不要求在第一阶段全部创建。
- 小模块没有真实 domain/application/adapters 区分时不得机械创建空目录。
- `data/` 保留资源文件职责；`pyproject.toml` 的 `voidx.skills` 和 `voidx.data` package-data 行为保持。
- `main.py` 保持入口路径且只调用 `bootstrap` 暴露的 CLI application API；`agent/facade.py` 保持核心应用入口，但不由 `main.py` 直接导入。

## 7. 全局依赖 DAG

### 7.1 顶层包允许依赖矩阵

测试使用“显式允许列表”；未列出的跨包 import 一律失败。

| Source | Allowed internal targets |
|---|---|
| `platform` | 无 |
| `observability` | `platform` |
| `persistence` | `platform`, `observability` |
| `llm` | `platform`, `observability` |
| `skills` | `platform`, `observability` |
| `lsp` | `platform`, `observability` |
| `mcp` | `platform`, `observability` |
| `tooling` | `llm`, `mcp`, `lsp`, `skills`, `platform`, `observability` |
| `agent` | `llm`, `mcp`, `lsp`, `skills`, `tooling`, `persistence`, `platform`, `observability` |
| `config` | `agent`, `llm`, `mcp`, `lsp`, `skills`, `tooling`, `persistence`, `platform`, `observability` |
| `update` | `config`, `platform`, `observability` |
| `presentation` | `agent`, `config`, `mcp`, `lsp`, `skills`, `tooling`, `persistence`, `platform`, `observability`, `update` |
| `bootstrap` | 所有业务包 |
| `main` | `bootstrap` |
| `data` | 无 Python 业务依赖 |

约束：

- 允许列表表示最大边界，不要求使用所有边。
- `llm`、`mcp`、`lsp`、`skills`、`tooling` 和 `agent` 不得导入 `config`；bootstrap 将解析后的 feature domain config 注入各应用。
- `config` 对 `agent`、`llm`、`mcp`、`lsp`、`skills`、`tooling` 的常规依赖只能指向各自 domain config/enum；唯一额外边是 `config/adapters/permission_grant_repository.py -> tooling/ports/grant_repository.py + tooling/domain/grants.py`，用于实现持久 grant port。config 对 persistence 的依赖只能位于 `config/adapters/**`。
- `agent/domain` 和 `agent/ports` 不得依赖其他业务包；`agent/application` 只依赖本包 domain/ports 及 `platform`、`observability`。
- `agent` 对 `llm`、`mcp`、`lsp`、`skills`、`tooling`、`persistence` 的依赖只能位于 `agent/adapters/**`。
- `tooling/domain` 和 `tooling/ports` 不得依赖其他业务包；`tooling/application` 只依赖本包 domain/ports 及 `platform`、`observability`。
- `tooling` 对 `llm`、MCP、LSP、skills 的依赖只能位于 `tooling/adapters/**`。
- `presentation` 不得被 `agent`、`tooling`、`mcp`、`llm` 等核心包导入。Presentation 对 LSP 的依赖只允许在 `presentation/slash/**` 指向 `lsp/domain/**` 和 `lsp/ports/operations.py`，禁止导入 `lsp/application|adapters`。
- `bootstrap` 不得被任何其他 `voidx` 包导入。

### 7.2 包内层次规则

对存在标准分层的包，允许方向为：

```text
facade ──→ application ──→ domain
                    └────→ ports ──→ domain
adapters ──→ application / ports / domain
```

禁止：

- `domain -> application|ports|adapters|presentation`
- `ports -> application|adapters`
- `application -> adapters`
- adapter 被 domain/application 反向导入
- 同层通过聚合 `__init__.py` 造成初始化副作用环

### 7.3 动态 import 规则

架构测试必须识别：

- `import x`、`from x import y`
- `TYPE_CHECKING` 内 import
- 字面量形式的 `importlib.import_module("voidx...")`
- 字面量形式的 `__import__("voidx...")`

动态计算出的模块名禁止用于内部依赖注入或插件注册；需要动态选择时，bootstrap 显式构建名称到实现的映射。

## 8. 职责和 ownership

### 8.1 Agent domain

拥有：

- task intent、goal、turn、thread、attempt、lifecycle、workflow run state。
- goal/loop/workflow 的纯状态转换和不变量。
- profile、tool view、turn metadata、语义事件 DTO。

不得拥有：

- prompt 文件读取、Settings、数据库 row、asyncio Queue/Task、UI event、LangGraph state。

### 8.2 Agent application

拥有：

- 单轮执行、会话、compaction、goal、loop、workflow、subagent 用例。
- 调用端口、事件顺序、事务边界和错误分类。
- prompt section 的应用级编排。

不得拥有：

- 终端/Web 前端创建、Gateway server、SQLite SQL、具体 tool/MCP/LSP manager、LangGraph node。

### 8.3 Agent adapters

拥有：

- LangGraph turn engine。
- Agent port 到 SQLite 的 mapper 和 repository；不拥有 OutputTree transcript DTO 或其 JSONL。
- Agent-facing tool plugins，包括 task/todo、clarify/checkpoint、goal/loop/workflow、compaction、subagent 和 message。
- asyncio 子 Agent gateway transport。

不得拥有领域规则；adapter 只转换 DTO、调用外部能力并保持现有错误语义。

### 8.4 Tooling

- `domain/` 拥有 ToolCall、ToolResult、窄 `ToolExecutionContext`、authorization/grant/permission/risk DTO；禁止 manager/controller/registry/gateway/Callable service 字段。
- `application/` 拥有 registry、authorize、execute 的用例顺序。
- `policy/` 拥有纯 permission/sandbox/shell/git 判定。
- `builtin/` 拥有 file read/write/manage/search、shell、git、web 和 built-in document 等不依赖 Agent 状态的具体工具。
- `adapters/` 将 MCP/LSP/skills/web 能力包装为 Tool plugin。
- Agent 专属的 todo/task tracker、clarify/checkpoint、goal/loop/workflow/workflow-guidance、subagent/message 和 compaction 工具全部归 `agent/adapters/tools/`，避免 `tooling -> agent|presentation`。
- `ToolRegistry` 只接受 `ToolPlugin`；bootstrap 分别取得 Tooling builtin plugins、Tooling integration plugins 和 Agent tool plugins，按 P0 冻结顺序显式注册。Tooling 不导入 Agent plugin factory。

### 8.5 Presentation

拥有 UI protocol、WebSocket gateway、output tree、dock、terminal frontend、slash input adapter、UI-side tools，以及现有 OutputTree transcript 的 DTO、JSONL/index/checkpoint repository 和 tree mapper。

Presentation 可以读取 facade/contracts 和 domain DTO，但不能成为 Agent 核心状态 owner；`GatewaySession._threads` 等私有状态不得被 application 读取。Transcript 是 presentation view snapshot，不是 Agent domain transcript。

### 8.6 Persistence

`persistence/` 只提供 SQL connection、transaction、migration runner、JSONL、路径和时间等无业务原语。Agent session/thread/runtime-state/context-frame、config profile、presentation transcript、MCP cache 等业务 repository 必须放在对应 feature 的 `adapters/persistence/`；`persistence` 永不导入 feature。

## 9. 端口设计

### 9.1 Agent persistence ports

新增或拆分：

```python
class SessionRepository(Protocol): ...
class ThreadRepository(Protocol): ...
class AttemptRepository(Protocol): ...
class OutboxRepository(Protocol): ...
class RuntimeStateRepository(Protocol): ...
class ProfileRepository(Protocol): ...
```

要求：

- 端口签名只出现 Agent domain DTO、标准库和 Pydantic 公共类型。
- lease/fencing/CAS 语义必须保留，但由端口方法表达，不能泄漏 SQL connection。
- 若 thread/attempt/outbox 必须在同一事务提交，定义一个面向该原子用例的端口，不在 application 中拼接多个 repository 事务。
- `SessionStore` 不扩成新 Host；按恢复、写入、删除等用例保持窄接口。

### 9.2 Presentation ports

拆分当前宽 `ExecutionHost`：

```python
class TurnRunner(Protocol): ...
class SessionLifecycle(Protocol): ...
class RuntimeStatusReader(Protocol): ...
class GuidancePort(Protocol): ...
class InteractiveInputPort(Protocol): ...
class AgentEventPublisher(Protocol): ...
class PresentationSnapshotPort(Protocol):
    async def persist_current(self, session_id: str) -> None: ...
    async def restore_current(self, session_id: str, *, append: bool = False) -> bool: ...
    async def clear(self, session_id: str) -> None: ...
```

- presentation 的 command handler 组合这些端口。
- 不再向 presentation 暴露 config、permission、MCP、LSP、slash 等完整实现对象。
- application 不再持有 concrete frontend/app/gateway session。
- `PresentationSnapshotPort` 不暴露 `OutputTree`、`OutputNode` 或 `TranscriptNodeRow`；其实现由 presentation 拥有并封装当前 dock/tree 与 JSONL repository。

### 9.3 Tooling ports

- permission policy 接收结构化 ToolCall/PermissionContext，不导入工具实现。
- shell parser/sandbox policy 接收 shell kind 参数，不从 permission 导入 PowerShell/Bash 模块。
- AI approval 依赖结构化模型端口；policy 本身不得导入 `llm`。
- Tool event 通过 `ToolEventPublisher`，builtin tool 不直接导入 presentation event bus。

### 9.4 LSP integration port

`lsp/ports/operations.py` 定义 Tooling/presentation 可依赖的稳定 API：

```python
class LspOperations(Protocol):
    async def diagnostics(self, file_path: str | None = None) -> str: ...
    async def symbols(self, file_path: str | None = None, query: str = "") -> str: ...
    async def definition(self, file_path: str, line: int, character: int) -> str: ...
    async def references(self, file_path: str, line: int, character: int, *, include_declaration: bool = True) -> str: ...
    async def format(self, file_path: str) -> tuple[bool, str, str]: ...
    async def format_range(self, file_path: str, range_: LspRange) -> tuple[bool, str, str]: ...
```

- `lsp/application/service.py` 实现该 port 并依赖 application manager；`lsp/application/manager.py` 依赖 `lsp/ports/client.py:LspClientFactory`，不导入 concrete client。
- `lsp/adapters/client/**` 实现 client factory。`bootstrap/integrations.py` 创建 concrete factory → manager → service，并把 `LspOperations` 注入 `tooling/adapters/lsp.py`、`lsp_post_edit.py` 和 presentation slash adapter。
- `tooling/adapters/lsp.py` 只能导入 `lsp/domain` 和 `lsp/ports/operations.py`，禁止导入 `lsp/application`、`lsp/adapters` 或 concrete manager/client。

### 9.5 Permission state and persistence ports

```python
class GrantRepository(Protocol):
    async def load(self) -> PersistentGrantSnapshot: ...
    async def commit(self, delta: GrantDelta, precondition: ApprovalPrecondition) -> GrantCommitResult: ...

class PermissionEventPublisher(Protocol): ...
class ApprovalModel(Protocol): ...
```

- `tooling/application/permission_service.py` 是 session allow/deny、runtime/session/persistent grants、state/permission revision、revocation 和 authorization use case owner；它只依赖 Tooling domain/policy/ports。
- `tooling/adapters/permission/in_memory_state.py` 是 PathGrantLock、commit lock、execution lease 和 runtime/session grant collection 的唯一进程内实现；不读写 Settings/SQLite。
- `config/adapters/permission_grant_repository.py` 实现 `GrantRepository`，保持当前 Settings key、revision、CAS/持久提交语义；它可导入 Tooling domain/port，但 Tooling 不反向导入 config。
- `tooling/application/ai_approval/service.py` 依赖 `ApprovalModel`；LLM adapter 由 bootstrap 提供，不读取 Settings。
- `bootstrap/tooling.py` 先从 config 构造 resolved `PermissionPreset/AuthorizationContext`，加载 GrantRepository snapshot，再构造 in-memory state 和 PermissionService，最后将 authorization/grant/lease/event 窄端口注入 Tooling/Agent plugins。

### 9.6 SQLite migration composition

`persistence/migrations.py` 只定义无业务依赖的执行器：

```python
@dataclass(frozen=True)
class MigrationStep:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]

@dataclass(frozen=True)
class MigrationPlan:
    target_version: int
    bootstrap_schema: tuple[Callable[[sqlite3.Connection], None], ...]
    steps: tuple[MigrationStep, ...]
    cleanup: tuple[Callable[[sqlite3.Connection], None], ...] = ()

class MigrationRunner:
    def migrate(self, connection: sqlite3.Connection, plan: MigrationPlan) -> None: ...
```

固定规则：

1. Agent persistence adapter 导出 session/runtime/thread/context schema 和属于它的 migration callables；config adapter 导出 `model_profiles` schema；presentation adapter 导出 transcript JSONL repository，不参与 SQLite schema。
2. `bootstrap/persistence.py` 显式导入这些 adapter 声明，按当前顺序组装唯一 plan：先执行现有基础 `CREATE TABLE/INDEX IF NOT EXISTS` 与 `_create_agent_thread_tables` 等价 bootstrap schema，再按 `v0→v1`、`v1→v2`、`v2→v3` 执行 step，设置 `PRAGMA user_version=3`，最后执行 legacy payload cleanup。
3. `bootstrap/application.py` 必须在构造任何 repository、Settings profile store、Agent runtime 或 presentation gateway 之前打开连接并完成 migration；repository 构造函数只接收已迁移 connection/factory，不得隐式触发 migration。
4. 每个 version step 在同一事务中执行；失败时 rollback 且 `user_version` 保持原值。legacy cleanup 保留当前 foreign-key off/on 和独立 rollback 语义。
5. fresh DB、v0、v1、v2、v3 重复初始化都必须成功；v3 重复执行不得改变 schema、数据或 `user_version`。
6. `persistence` 不导入任何 feature；禁止 decorator/import side effect 注册 migration；plan 中 version 必须从 1 连续到 3、无重复，runner 启动时验证。

### 9.7 Presentation transcript ownership

采用 UI-owned snapshot，不创建 Agent semantic transcript DTO：

- 持久化契约就是现有 `TranscriptNodeRow` 的字段集合，以及 `transcript.jsonl`、`transcript.idx.json`、`transcript.checkpoint.json` 的当前格式。
- P2 将 `memory/transcript.py` 和 `ui/transcript.py` 合并为 `ui/transcript_snapshot.py`，并将通用 JSONL I/O 注入该 repository；此时 `ui` 是唯一 owner，旧 memory 实现同阶段删除，不双写。
- P2 同时把 LangGraph `SessionRuntime.persist/restore_transcript_snapshot` 改为调用 `PresentationSnapshotPort`；该端口只接收 `session_id`、`append`，不暴露 UI row/tree。
- P4 用 `git mv` 将该唯一实现迁到 `presentation/adapters/persistence/transcript_snapshot.py`，只改 import；JSONL record、index、checkpoint 和 OutputTree mapper 不改逻辑。
- `presentation` 允许依赖通用 `persistence`，Agent domain/application/ports 不得导入 `TranscriptNodeRow`、`OutputTree`、`OutputNode` 或 presentation transcript adapter。

逐字段兼容：

- `TranscriptNodeRow`：`session_id, turn_id, node_id, parent_node_id, sort_order, node_type, header, body_lines, status, collapsed, elapsed, message_id, tool_call_id, agent_run_id, metadata, created_at, updated_at`。
- JSONL records：`transcript_reset`、`turn_start`、`node`、`turn_end`、`summary`、`node_update` 的现有字段、排序和 append 语义。
- index：`version, transcript_size, last_reset_offset, turn_offsets, summary_offsets, last_checkpoint_offset, last_checkpoint_path`。
- checkpoint：`version, offset, nodes`，以及 current loader 的 index-valid、checkpoint fallback、rebuild 路径。

## 10. 关键迁移映射

### 10.1 根模块和基础包

| Current | Target |
|---|---|
| `src/voidx/paths.py` | `src/voidx/platform/paths.py` |
| `src/voidx/selfupdate.py` | `src/voidx/update/service.py` |
| `src/voidx/logging/**` | `src/voidx/observability/**` |
| `src/voidx/memory/store.py` | 连接/锁/retry 与通用 runner 进 `persistence/sqlite.py`、`migrations.py`；schema/migration steps 归 feature adapter；bootstrap 组装有序计划 |
| `src/voidx/memory/jsonl_store.py` | `src/voidx/persistence/jsonl.py` |
| `src/voidx/runtime/processes.py`、`_win32_jobs.py` | `src/voidx/platform/processes.py` 和平台私有 helper |
| `src/voidx/runtime/execution_context.py` | `src/voidx/platform/execution_context.py` |
| `src/voidx/tools/retry.py` | `src/voidx/platform/retry.py` |
| `src/voidx/diffing.py` | 纯 DTO/parser/generator 进 `tooling/domain/diff.py`；git subprocess 进 `tooling/adapters/git_diff.py`；`language_from_path` 进 `platform/file_types.py` |

### 10.2 Runtime、workflow 和 Agent automation

| Current | Target |
|---|---|
| `src/voidx/runtime/intent.py` | `src/voidx/agent/domain/task/intent.py` |
| `src/voidx/runtime/task_state.py` | 按职责拆至 `agent/domain/task/`、`agent/domain/automation/`，含 `WorkflowRoute` 和 workflow state fields |
| `src/voidx/runtime/todo.py` | `src/voidx/agent/domain/task/todo.py` |
| `src/voidx/runtime/attachments.py` | `src/voidx/agent/domain/turn/attachments.py` |
| `src/voidx/runtime/reference_tokens.py` | `src/voidx/agent/domain/turn/references.py` |
| `src/voidx/runtime/goal.py` | `src/voidx/agent/domain/automation/goal.py` |
| `src/voidx/runtime/ui.py`、`ui_port.py` | contracts 移 `agent/ports/presentation.py`；lazy concrete proxy 在 P4 删除 |
| `src/voidx/workflow/types.py` | `src/voidx/agent/domain/automation/workflow.py` |
| `src/voidx/workflow/schema.py`、`dag.py`、`policy.py` | `src/voidx/agent/domain/automation/workflow_*.py` |
| `src/voidx/workflow/runtime.py`、`reconcile.py`、`route.py`、`auto_advance.py` | `src/voidx/agent/application/automation/workflow/` |
| `src/voidx/workflow/service.py`、`context.py`、`render.py`、`nodes.py` | 按用例移入 `agent/application/automation/workflow/`；静态文档数据可留 `data/` |
| `src/voidx/agent/domain/goal.py`、`loop.py` | `src/voidx/agent/domain/automation/` |
| `src/voidx/agent/application/goal_*` + `src/voidx/agent/goal/**` | `src/voidx/agent/application/automation/goal/` |
| `src/voidx/agent/application/loop_*` + `src/voidx/agent/loop/**` | `src/voidx/agent/application/automation/loop/` |
| `src/voidx/agent/runtime/**` | `src/voidx/agent/application/runtime/`；纯 contracts 移 domain/ports |
| `src/voidx/agent/infrastructure/langgraph/**` | `src/voidx/agent/adapters/langgraph/**` |
| `src/voidx/agent/infrastructure/{memory_session,runtime_state_mapper,message_rows}.py` | 对应 `agent/adapters/persistence/` 或 `agent/adapters/langgraph/` |
| `src/voidx/agent/infrastructure/tool_result_storage.py` | `src/voidx/agent/adapters/tools/result_storage.py`，storage root 经 platform path/构造参数注入 |

### 10.3 Memory

| Current | Target |
|---|---|
| `src/voidx/memory/session.py` | `src/voidx/agent/adapters/persistence/session_repository.py` |
| `src/voidx/memory/runtime_state.py` | `src/voidx/agent/adapters/persistence/runtime_state_repository.py` |
| `src/voidx/memory/thread_store.py` | 拆为 `thread_repository.py`、`attempt_repository.py`、`outbox_repository.py`；共享事务可由内部 unit-of-work 保持 |
| `src/voidx/memory/transcript.py` + `src/voidx/ui/transcript.py` | P2 合并到 `src/voidx/ui/transcript_snapshot.py` 成为唯一 owner；P4 原样迁到 `src/voidx/presentation/adapters/persistence/transcript_snapshot.py` |
| `src/voidx/memory/context_frames.py` | `src/voidx/agent/adapters/persistence/context_frame_repository.py` |
| `src/voidx/memory/subagents.py` | `src/voidx/agent/adapters/persistence/subagent_repository.py` |
| `src/voidx/memory/cleanup.py` | `src/voidx/agent/adapters/persistence/session_cleanup.py` |
| `src/voidx/memory/model_profiles.py`、`profile_store.py` | `src/voidx/config/adapters/profile_repository.py` |
| `src/voidx/memory/service.py` | 删除；调用方改用 feature port/application service |

### 10.4 Tools、permission、MCP、LSP

| Current | Target |
|---|---|
| `src/voidx/tools/base.py` | 拆至 `tooling/domain/` 和 `tooling/ports/tool.py` |
| `src/voidx/tools/registry.py` | `src/voidx/tooling/application/registry.py` |
| `src/voidx/permission/**` | 按 10.4.3 分拆到 `tooling/domain|policy|application|ports|adapters`；本行仅摘要，不表示整体进入 policy |
| `src/voidx/tools/file/**` | `src/voidx/tooling/builtin/file/**` |
| `src/voidx/tools/bash/**`、`powershell/**`、`shell/**` | 聚合到 `src/voidx/tooling/builtin/shell/**`，共享 policy 进 `tooling/policy/shell/` |
| `src/voidx/tools/git/**` | `src/voidx/tooling/builtin/git/**` |
| `src/voidx/tools/web/**` | `src/voidx/tooling/builtin/web/**`；MCP wrapper 进 adapters |
| `src/voidx/tools/clarify.py`、`checkpoint.py` | `src/voidx/agent/adapters/tools/interaction.py`；状态 patch/workflow gate 继续使用 Agent domain/application |
| `src/voidx/tools/agent.py`、`agent_control.py`、`message.py` | `src/voidx/agent/adapters/tools/subagent.py` |
| `src/voidx/tools/goal.py`、`loop.py`、`workflow/**`、`workflow_guidance.py` | `src/voidx/agent/adapters/tools/automation/` |
| `src/voidx/tools/todo.py` | `src/voidx/agent/adapters/tools/todo.py`；Todo domain DTO 归 `agent/domain/task/todo.py` |
| `src/voidx/tools/task_tracker.py` | `src/voidx/agent/application/runtime/task_tracker.py` |
| `src/voidx/tools/compact.py` | `src/voidx/agent/adapters/tools/compaction.py` |
| `src/voidx/tools/document.py` | `src/voidx/tooling/builtin/document.py` |
| `src/voidx/tools/search.py` | `src/voidx/tooling/builtin/file/search.py` |
| `src/voidx/tools/skills.py` | `src/voidx/tooling/adapters/skills.py`；settings 输入改为 `SkillSelectionConfig`/SkillService port |
| `src/voidx/tools/output_policy.py` | `src/voidx/tooling/domain/output_policy.py` |
| `src/voidx/tools/service.py` | 删除聚合 facade；消费者改为 domain/ports 或 Agent adapter，注册由 bootstrap plugin list 取代 |
| `src/voidx/tools/lsp.py` | `src/voidx/tooling/adapters/lsp.py` |
| `src/voidx/mcp/gateway.py` | `src/voidx/tooling/adapters/mcp.py` |
| `src/voidx/mcp/server/web.py` | `src/voidx/tooling/adapters/mcp_web_server.py` |
| `src/voidx/mcp/client/**` | `src/voidx/mcp/adapters/client/**` |
| `src/voidx/mcp/manager.py` | `src/voidx/mcp/application/manager.py`，移除 ToolRegistry/PermissionService 依赖 |
| `src/voidx/lsp/manager.py` | `src/voidx/lsp/application/manager.py`；只依赖 `lsp/ports/client.py` 创建的 client factory |
| `src/voidx/lsp/service.py` | `src/voidx/lsp/application/service.py`，实现 `lsp/ports/operations.py:LspOperations` |
| `src/voidx/lsp/schema.py` | `src/voidx/lsp/domain/` |
| `src/voidx/lsp/client/**` | `src/voidx/lsp/adapters/client/**`，实现 `lsp/ports/client.py` |

### 10.4.1 Authoritative tool file manifest

本表是 P3 删除 `src/voidx/tools/` 前的权威清单，覆盖当前每个 Python 文件。一个 source 只出现一次；“拆至”后的所有 target 必须在同一阶段完成，旧 source 随即删除。10.4 上方目录映射仅作摘要，冲突时以本表为准。

| Source | Final target / disposition |
|---|---|
| `src/voidx/tools/__init__.py` | 删除；新 `tooling/__init__.py` 不做 catalog re-export 或注册 |
| `src/voidx/tools/agent.py` | `agent/adapters/tools/subagent.py` |
| `src/voidx/tools/agent_control.py` | `agent/adapters/tools/subagent_control.py` |
| `src/voidx/tools/base.py` | 按 10.4.2 拆分：Tool DTO/schema/contract 进 Tooling domain/ports；authorization/file-state/process/invoker 进 Tooling application/policy/ports；Agent、MCP、LSP capability 由各 adapter 注入；旧大 `ToolContext` 删除 |
| `src/voidx/tools/bash/__init__.py` | `tooling/builtin/shell/bash/__init__.py` |
| `src/voidx/tools/bash/core.py` | `tooling/builtin/shell/bash/core.py` |
| `src/voidx/tools/bash/edit_router.py` | `tooling/builtin/shell/bash/edit_router.py` |
| `src/voidx/tools/bash/hint/__init__.py` | `tooling/builtin/shell/bash/hint/__init__.py` |
| `src/voidx/tools/bash/hint/file.py` | `tooling/builtin/shell/bash/hint/file.py` |
| `src/voidx/tools/bash/hint/git.py` | `tooling/builtin/shell/bash/hint/git.py` |
| `src/voidx/tools/bash/hint/search.py` | `tooling/builtin/shell/bash/hint/search.py` |
| `src/voidx/tools/bash/router.py` | `tooling/builtin/shell/bash/router.py` |
| `src/voidx/tools/bash/safety.py` | 拆至 `tooling/policy/shell/bash.py`（blocked/sandbox decision）和 `tooling/builtin/shell/bash/safety.py`（将 `ToolExecutionContext` 映射为 policy input） |
| `src/voidx/tools/bash/tool.py` | `tooling/builtin/shell/bash/tool.py` |
| `src/voidx/tools/checkpoint.py` | `agent/adapters/tools/interaction/checkpoint.py` |
| `src/voidx/tools/clarify.py` | `agent/adapters/tools/interaction/clarify.py` |
| `src/voidx/tools/compact.py` | `agent/adapters/tools/compaction.py` |
| `src/voidx/tools/document.py` | `tooling/builtin/document.py` |
| `src/voidx/tools/file/__init__.py` | `tooling/builtin/file/__init__.py` |
| `src/voidx/tools/file/io.py` | `tooling/builtin/file/io.py` |
| `src/voidx/tools/file/manage.py` | `tooling/builtin/file/manage.py` |
| `src/voidx/tools/file/overlap.py` | `tooling/domain/file_overlap.py` |
| `src/voidx/tools/file/post_edit.py` | `tooling/adapters/lsp_post_edit.py`；抽取 `tooling/ports/post_edit.py` 供 file tools 注入 |
| `src/voidx/tools/file/read.py` | `tooling/builtin/file/read.py` |
| `src/voidx/tools/file/replace.py` | `tooling/builtin/file/replace.py` |
| `src/voidx/tools/file/replace_resolve.py` | `tooling/builtin/file/replace_resolve.py` |
| `src/voidx/tools/file/safe_path.py` | `tooling/policy/filesystem/safe_path.py` |
| `src/voidx/tools/file/state.py` | 拆至 `tooling/domain/file_tracking.py`、`tooling/application/file_state.py`、`tooling/adapters/persistence/{file_snapshot,result_storage}.py` |
| `src/voidx/tools/file/types.py` | `tooling/domain/file.py` |
| `src/voidx/tools/file/write.py` | `tooling/builtin/file/write.py` |
| `src/voidx/tools/git/__init__.py` | `tooling/builtin/git/__init__.py` |
| `src/voidx/tools/git/access.py` | `tooling/policy/git/access.py` |
| `src/voidx/tools/git/constants.py` | 拆至 `tooling/policy/git/constants.py`（分类/拒绝集合）和 `tooling/builtin/git/constants.py`（timeout/output limits） |
| `src/voidx/tools/git/handlers.py` | `tooling/builtin/git/handlers.py` |
| `src/voidx/tools/git/models.py` | `tooling/builtin/git/models.py` |
| `src/voidx/tools/git/parsers.py` | `tooling/builtin/git/parsers.py` |
| `src/voidx/tools/git/process.py` | `tooling/builtin/git/process.py`；进程生命周期改用 `platform/processes.py` |
| `src/voidx/tools/git/results.py` | `tooling/builtin/git/results.py` |
| `src/voidx/tools/git/routing.py` | 拆至 `tooling/policy/git/routing.py`（read/write/denied classification）和 `tooling/builtin/git/routing.py`（argv/pathspec/raw dispatch） |
| `src/voidx/tools/git/tool.py` | `tooling/builtin/git/tool.py` |
| `src/voidx/tools/goal.py` | `agent/adapters/tools/automation/goal.py` |
| `src/voidx/tools/loop.py` | `agent/adapters/tools/automation/loop.py` |
| `src/voidx/tools/lsp.py` | `tooling/adapters/lsp.py` |
| `src/voidx/tools/message.py` | `agent/adapters/tools/subagent_message.py` |
| `src/voidx/tools/output_policy.py` | `tooling/domain/output_policy.py` |
| `src/voidx/tools/powershell/__init__.py` | `tooling/builtin/shell/powershell/__init__.py` |
| `src/voidx/tools/powershell/core.py` | `tooling/builtin/shell/powershell/core.py` |
| `src/voidx/tools/powershell/hint/__init__.py` | `tooling/builtin/shell/powershell/hint/__init__.py` |
| `src/voidx/tools/powershell/hint/file.py` | `tooling/builtin/shell/powershell/hint/file.py` |
| `src/voidx/tools/powershell/hint/search.py` | `tooling/builtin/shell/powershell/hint/search.py` |
| `src/voidx/tools/powershell/router.py` | `tooling/builtin/shell/powershell/router.py` |
| `src/voidx/tools/powershell/safety.py` | `tooling/policy/shell/powershell_blocked.py` |
| `src/voidx/tools/powershell/sandbox.py` | `tooling/policy/shell/powershell_sandbox.py`；输入改为结构化 workspace/grants，不导入 ToolContext |
| `src/voidx/tools/powershell/tool.py` | `tooling/builtin/shell/powershell/tool.py` |
| `src/voidx/tools/registry.py` | `tooling/application/registry.py`；只接受显式 ToolPlugin list |
| `src/voidx/tools/retry.py` | `platform/retry.py` |
| `src/voidx/tools/search.py` | `tooling/builtin/file/search.py` |
| `src/voidx/tools/service.py` | 删除；所有消费者改导 domain/ports，bootstrap plugin list 取代 facade |
| `src/voidx/tools/shell/__init__.py` | `tooling/builtin/shell/__init__.py` |
| `src/voidx/tools/shell/common.py` | 拆至 `tooling/domain/shell_result.py`、`tooling/builtin/shell/common.py`；进程函数移 `platform/processes.py` |
| `src/voidx/tools/shell/hint/__init__.py` | `tooling/builtin/shell/hint/__init__.py` |
| `src/voidx/tools/shell/hint/git.py` | `tooling/builtin/shell/hint/git.py` |
| `src/voidx/tools/skills.py` | `tooling/adapters/skills.py` |
| `src/voidx/tools/task_tracker.py` | `agent/application/runtime/task_tracker.py` |
| `src/voidx/tools/todo.py` | `agent/adapters/tools/todo.py`；Todo DTO/transition 归 `agent/domain/task/todo.py` |
| `src/voidx/tools/web/__init__.py` | `tooling/builtin/web/__init__.py` |
| `src/voidx/tools/web/content.py` | `tooling/builtin/web/content.py` |
| `src/voidx/tools/web/fetch.py` | `tooling/builtin/web/fetch.py`；RetryConfig 改为 Tooling domain config 注入 |
| `src/voidx/tools/web/mcp.py` | `tooling/adapters/web_mcp.py`；builtin web 通过 `tooling/ports/web_route.py` 注入，不导入 MCP adapter |
| `src/voidx/tools/web/search.py` | `tooling/builtin/web/search.py`；RetryConfig 改为 Tooling domain config 注入 |
| `src/voidx/tools/workflow/__init__.py` | `agent/adapters/tools/automation/workflow.py` |
| `src/voidx/tools/workflow/actions.py` | `agent/adapters/tools/automation/workflow_actions.py` |
| `src/voidx/tools/workflow/queries.py` | `agent/adapters/tools/automation/workflow_queries.py` |
| `src/voidx/tools/workflow/result.py` | `agent/adapters/tools/automation/workflow_result.py` |
| `src/voidx/tools/workflow/state.py` | `agent/adapters/tools/automation/workflow_state.py` |
| `src/voidx/tools/workflow_guidance.py` | `agent/adapters/tools/automation/workflow_guidance.py` |

P3 架构测试从本表解析 source 列，与 `src/voidx/tools/**/*.py` 的 P3 开始前清单做集合全等比较；目标集合允许多 target，但每个 source 必须恰好一行且 disposition 不能包含待定条件。P3 Acceptance 还要断言所有非 delete target 存在、所有 source/旧 package 不存在。

### 10.4.2 `tools/base.py` responsibility split

不保留当前可挂任意服务的 Pydantic `ToolContext`；按字段和函数组固定拆分：

| Current responsibility | Final owner |
|---|---|
| `ToolResult`、timeout metadata、nullish/arg normalization、schema conversion/inline helpers、`SKIP_DIRS/SUFFIXES` | `tooling/domain/{result,schema,arguments,filesystem}.py` |
| `BaseTool` 的 id/description/schema/execute contract | `tooling/ports/tool.py` 的 `ToolPlugin` Protocol；不保留 ABC inheritance requirement |
| `UserInteraction`、`UserResponse`、callback | DTO 进 `tooling/domain/interaction.py`，能力进 `tooling/ports/interaction.py: InteractionPort` |
| workspace/session/persona/turn、resolved permission/sandbox、approved risk、file tracking references | `tooling/domain/context.py: ToolExecutionContext`；只含不可变值/Tooling DTO，不含 manager/controller/registry/Callable service |
| `permission_mode -> sandbox_mode/approval_policy` | bootstrap/config 解析成 `tooling/domain/authorization.py: AuthorizationContext` 后注入；Tooling 不导入 config enum |
| access grants、revocation epoch、add grant、target lock、execution lease | `tooling/ports/authorization.py` 的 `GrantReader/GrantWriter/GrantLock/ExecutionLease`，由 permission adapter 实现 |
| `_resolve_tool_path_for_access`、approval precondition/add-grant/release orchestration、sandbox paths | `tooling/application/authorization.py: AuthorizedPathService`；依赖 authorization + interaction ports 和 `tooling/policy/filesystem`，builtin file/git/search 只调用该 service port |
| file mtimes/read coverage/line drift mutable maps | `tooling/application/file_state.py` + `tooling/ports/file_state.py`；bootstrap 为一个 Agent run 注入同一实例，Tooling context 不存共享私有 dict |
| `tool_registry` route-hint 再调用 | `tooling/ports/invoker.py: ToolInvoker`，由 registry adapter 实现；builtin shell 不持 registry concrete |
| `process_sandbox` | `tooling/ports/process.py: ProcessSandbox` |
| workflow runs/route/repeat tracker、goal/loop controllers/phases、task intent/goal target | `agent/adapters/tools/context.py: AgentToolRuntime`，只注入 Agent tool plugins；不进入 Tooling context |
| `agent_gateway/agent_run_id` | `agent/adapters/tools/subagent_context.py: SubagentToolRuntime` |
| `mcp_manager` | `tooling/adapters/mcp.py` 构造时注入 `McpToolCaller` port 实现；不出现在通用 context |
| `lsp_manager/format_after_edit_enabled` | `tooling/adapters/lsp.py` 和 `lsp_post_edit.py` 构造时注入 `lsp/ports/operations.py:LspOperations` 与 resolved boolean；不得导入 `lsp/adapters/client`，不出现在通用 context |

`bootstrap/tooling.py` 在每次 run/turn 构造 `ToolExecutionContext` 及上述窄 capability；`bootstrap/agent.py` 额外构造 AgentToolRuntime。P3 架构测试禁止 `ToolExecutionContext` 字段类型为 `Any`、Callable service、manager/controller/registry/gateway，以及禁止 `tooling/domain|application|ports|policy|builtin -> agent|mcp|lsp|config|presentation`；integration adapter 仅导入对应 port/domain。

### 10.4.3 Authoritative permission file manifest

本表覆盖当前每个 `src/voidx/permission/**/*.py`。纯 DTO/规则、状态化 application、窄 ports 和 adapter 实现分开；上方 `permission/** → tooling/policy/**` 仅为旧摘要并由本表替代。

| Source | Final target / disposition |
|---|---|
| `src/voidx/permission/__init__.py` | 删除旧 re-export；`tooling/{domain,application,ports,policy,adapters}` 各自使用最小 `__init__.py` |
| `src/voidx/permission/ai_approval/__init__.py` | `tooling/application/ai_approval/__init__.py` |
| `src/voidx/permission/ai_approval/models.py` | `tooling/domain/ai_approval.py` |
| `src/voidx/permission/ai_approval/parsing.py` | `tooling/application/ai_approval/parsing.py` |
| `src/voidx/permission/ai_approval/prompt.py` | `tooling/application/ai_approval/prompt.py` |
| `src/voidx/permission/ai_approval/redaction.py` | `tooling/policy/ai_approval_redaction.py` |
| `src/voidx/permission/ai_approval/service.py` | `tooling/application/ai_approval/service.py`；依赖 `tooling/ports/approval_model.py`，不导入 Settings/LLM concrete |
| `src/voidx/permission/constants.py` | 按使用者拆到 `tooling/policy/{filesystem,git,shell}/constants.py` |
| `src/voidx/permission/context.py` | DTO/decision 进 `tooling/domain/authorization.py`；删除 `from_service` 和 config enum import，bootstrap 传 resolved `AuthorizationContext` |
| `src/voidx/permission/engine.py` | `tooling/application/authorization.py`；PowerShell precheck 依赖 `tooling/policy/shell`，不导入 builtin tool |
| `src/voidx/permission/evaluate.py` | `tooling/policy/permission/evaluate.py` |
| `src/voidx/permission/git_policy.py` | `tooling/policy/git/policy.py` |
| `src/voidx/permission/grants.py` | grant/intent/result DTO 与纯 resolve 函数进 `tooling/domain/grants.py`、`tooling/policy/filesystem/grants.py`；epoch gate/path lock manager 进 `tooling/adapters/permission/in_memory_state.py` |
| `src/voidx/permission/presets.py` | `tooling/policy/permission/presets.py`；输入 resolved `PermissionPreset` domain enum，不导入 config |
| `src/voidx/permission/process_sandbox.py` | capability DTO 进 `tooling/domain/process_sandbox.py`，default/system detection 进 `tooling/adapters/process_sandbox.py` |
| `src/voidx/permission/risk.py` | `tooling/domain/risk.py` |
| `src/voidx/permission/rules.py` | 通用 classification/rules 进 `tooling/policy/permission/rules.py`；workflow/persona 推导移 `agent/adapters/tools/permission_projection.py`，policy 接收已解析 `AgentInvocationClass` |
| `src/voidx/permission/sandbox.py` | `tooling/policy/filesystem/sandbox.py` 与 `tooling/policy/shell/bash_sandbox.py` |
| `src/voidx/permission/schema.py` | `tooling/domain/permission.py` |
| `src/voidx/permission/service.py` | 状态和授权用例进 `tooling/application/permission_service.py`；grant/lease/lock 实现在 `tooling/adapters/permission/in_memory_state.py`；持久 grant 经 `tooling/ports/grant_repository.py` 注入，config adapter 实现并由 bootstrap 绑定；notifier 经 event port 注入 |
| `src/voidx/permission/session_rules.py` | `tooling/policy/permission/session_rules.py` |
| `src/voidx/permission/shell_policy.py` | `tooling/policy/shell/policy.py` |
| `src/voidx/permission/wildcard.py` | `tooling/policy/permission/wildcard.py` |

P3 架构测试对 source 集合执行与 10.4.1 相同的集合全等、target existence/delete 和旧 source 不存在检查。`tooling/adapters/permission/in_memory_state.py` 是 PathGrantLock、revocation epoch、execution lease 和 session/runtime grants 的唯一状态 owner；`config/adapters/permission_grant_repository.py` 只实现持久 grant port，不拥有授权规则。


### 10.5 Presentation

| Current | Target |
|---|---|
| `src/voidx/ui/protocol/**` | `src/voidx/presentation/protocol/**` |
| `src/voidx/ui/gateway/**` | `src/voidx/presentation/gateway/**` |
| `src/voidx/ui/output/**` | `src/voidx/presentation/output/**` |
| `src/voidx/ui/frontend.py`、`commands.py`、`command_catalog.py` | `src/voidx/presentation/terminal/**` |
| `src/voidx/ui/tools/**` | `src/voidx/presentation/tools/**` |
| `src/voidx/agent/slash/**` | `src/voidx/presentation/slash/**` |
| UI startup/run-loop 部分的 `agent/application/agent_service.py` | `src/voidx/presentation/terminal/run_loop.py`、`startup.py` |
| Web command 部分的 `agent/application/agent_service.py` | `src/voidx/presentation/gateway/command_handler.py` |

### 10.6 Agent Gateway 和 composition

| Current | Target |
|---|---|
| `src/voidx/agent/gateway/models.py` 的 run/message 领域模型 | `src/voidx/agent/domain/subagent.py` |
| `src/voidx/agent/gateway/gateway.py` 的路由/授权规则 | `src/voidx/agent/application/subagent/router.py` |
| 同文件的 asyncio Queue/Task/Event 实现 | `src/voidx/agent/adapters/subagent/inprocess_gateway.py` |
| `src/voidx/agent/composition.py` | `src/voidx/bootstrap/agent.py` |
| `_make_goal_result_notifier()` | 由显式 `ParentResultPublisher` adapter 替代 |
| `src/voidx/agent/facade.py` | 保留路径，扩展为类型化 facade；不得泄漏 concrete execution host |

## 11. 明确禁止的实现方式

- 保留 `voidx.tools`、`voidx.ui`、`voidx.memory` 等旧包作为 re-export。
- 用 `sys.modules` alias、import hook 或 `__getattr__` 伪装兼容。
- 在新路径复制实现而不删除旧 owner。
- 用 `Any`、`SimpleNamespace`、无字段 dict 替代应有的端口或 DTO。
- 用 `getattr/hasattr` 探测可选服务来完成组合。
- 为通过架构测试扩大通配 allowlist。
- 从 domain 导入 prompt/application DTO、Settings、UI event 或数据库 row。
- 从 permission policy 导入 Bash/PowerShell tool 类。
- 从 MCP core 导入 ToolRegistry、BaseTool 或 builtin web tools。
- 从 tools/core 导入 Agent gateway、goal/loop/workflow service。
- 在 package `__init__.py` 中通过导入所有 provider/tool 触发注册副作用。
- 在同一阶段顺便改变用户文案、策略、超时、默认值或状态机。

## 12. 实施程序

每个阶段均采用以下固定步骤：

1. 写目标边界测试并运行，确认因当前 offender 而 RED。
2. 运行阶段相关行为测试，记录 GREEN 基线。
3. 用 `git mv` 移动纯文件；拆分时先保持函数体和调用顺序。
4. 建立 port/adapter 和 mapper，更新全部生产与测试 import。
5. 删除旧文件、旧 `__init__` export 和临时 allowlist。
6. 运行阶段测试、架构测试、backend 测试。
7. 只有全部 GREEN 才进入下一阶段。

### P0 — 契约和架构护栏

创建架构测试：

- `src/tests/test_architecture/import_graph.py`：AST import 解析共享 helper。
- `src/tests/test_architecture/test_import_cycles.py`：迁移并增强现有 SCC 测试。
- `src/tests/test_architecture/test_package_dependencies.py`：顶层允许矩阵。
- `src/tests/test_architecture/test_layer_dependencies.py`：domain/ports/application/adapters 规则。
- `src/tests/fixtures/architecture/current_edges.json`：当前精确 source-target 边；每条 debt 标注 `remove_by: P1..P7`。

创建 contract manifest 和测试：

| Contract | Test | Fixture / required cases |
|---|---|---|
| CLI | `test_contracts/test_cli_contract.py` | `cli.json`：`--help`、`--version`、`version`、`sessions` 空列表、`--web-headless` 无 `--web`、不存在的 `--resume`；记录 argv、exit code、stdout/stderr（去 ANSI）和 Typer option name/short/default/help |
| Tool catalog/schema | `test_contracts/test_tool_contract.py` | `tool_catalog.json`：严格注册顺序及每项 id、description、完整 JSON Schema/default/required；覆盖 Tooling builtin/integration 和 Agent tools |
| Tool results | 同上 | `tool_results.json`：每类 file/shell/git/web/document/skills、Agent interaction/automation/subagent、MCP、LSP 的 success、invalid args、permission denied/unavailable、timeout 案例；记录 `title, output, summary, metadata, diff, next_step_hint, display` |
| Config | `test_contracts/test_config_contract.py` | `config.json`：Settings workspace/global 路径、GLOBAL_KEYS/WORKSPACE_ONLY_KEYS 合并、legacy `voidx.json`、runtime key→profile key、TAVILY/BOCHA env→file、CLI model/provider override、MCP transport autodetect、默认 Config 的完整 JSON |
| Provider catalog | `test_contracts/test_provider_catalog_contract.py` | `providers.json`：有序 provider name、protocol、default_base_url、context_limit、static_models；每个 provider 对 reasoning efforts 和 temperature override 的输出矩阵 |
| Slash catalog | `test_contracts/test_slash_command_contract.py` | `slash_commands.json`：有序 command/description/category/execution/dangerous/requiresArgs/uiTarget |
| Prompt | `test_contracts/test_prompt_contract.py` | `prompts.json`：chat/coding/goal/loop/subagent 代表性输入的最终 section name、顺序和完整文本；包含 workflow、todo、skill、session time、compaction 有/无案例 |
| Agent state machine | `test_contracts/test_state_machine_contract.py` | `state_machine.json`：goal/loop/workflow/subagent/thread 合法和非法转换、retry、cancel、resume/recovery、tool filter、message trimming、compaction、turn success/failure/cancel 事件和持久化顺序 |
| UI protocol | `test_ui/protocol/**` | 整个 v2 envelope/method/snapshot/thread 测试 + checked-in schema exact JSON compare |
| Persistence | `test_contracts/test_persistence_contract.py` + feature tests | 见下方 persistence manifest |

比较与归一化规则：

- catalog/list 使用数组并严格比较顺序；结构化对象使用 `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False)` 后比较；用户可见 `output/summary/help/prompt` 使用完整文本 exact compare。
- 测试必须注入固定 UTC clock、UUID、session/thread/run/tool-call id、workspace `${WORKSPACE}`、home `${HOME}`、port `${PORT}` 和 web token `${TOKEN}`。仅这些占位符允许归一化；不得删除字段、排序数组、截断文本或笼统忽略时间/路径/ID。
- secret 在调用实现前注入 `sk-test`，fixture 中统一写 `${SECRET}`；禁止把真实环境 credential 写入 fixture。
- 每个 P1-P7 阶段都运行 `src/tests/test_contracts`，fixture 必须 zero-diff；更新 fixture 视为行为变更，必须退出本重构另行审批。

Persistence manifest：

- `src/tests/fixtures/persistence/schema_v3.json` 完整记录 `sqlite_master` 的 table/index SQL、每表 `PRAGMA table_info`、foreign keys 和 `user_version=3`。
- `src/tests/fixtures/persistence/v0.db`、`v1.db`、`v2.db`、`v3.db` 和 fresh DB 覆盖逐版本升级、legacy payload table cleanup、失败 rollback、重复初始化；迁移后 schema manifest 和保留数据 exact compare。
- JSON payload fixtures 覆盖 runtime/workflow-runs/todo/goal/loop/compaction/session-time/profile/thread/attempt/outbox；使用 canonical JSON 比较，固定 clock/id 后不允许额外忽略字段。
- JSONL fixture 覆盖 `messages.jsonl`、`runtime.jsonl`、`runtime_debug.jsonl`、`context/deletes.jsonl`、context frame 文件、subagent records、`transcript.jsonl`、`transcript.idx.json`、`transcript.checkpoint.json`；逐行 byte compare（固定 clock/id，末尾换行保留）。
- thread transaction tests 使用真实公开操作：`save_state` stale version 必须 conflict；`begin_attempt` 对 source outbox 幂等且 expired takeover 增加 fencing token；旧 fencing token 的 `mark_side_effect_started`/`renew_attempt_lease`/`commit_decision` 失败；`commit_decision` 故障注入后 state、attempt、outbox 全部 rollback；成功时三者原子可见；`claim_outbox`/`claim_next_outbox` 与 ack 幂等；recovery worker 只恢复合法 lease/outbox。

先运行 contract/persistence 行为测试并确认 GREEN；再增加不带 debt 例外的 package/layer 目标规则并单独运行，确认它因当前 offender 而 RED。随后把这次输出逐边写入 `current_edges.json`（source、target、rule、reason、remove_by），让 P0 最终验证 GREEN；新边不在 manifest 时立即失败，例外不得使用目录通配，P1-P7 逐项删除，P7 后为空。

验证：

```bash
./test.py --backend -- src/tests/test_architecture src/tests/test_contracts src/tests/test_ui/protocol src/tests/test_memory src/tests/test_agent_runtime/test_thread_store.py src/tests/test_agent_runtime/test_recovery.py -v
```

Acceptance：上述 contract manifest 均生成、可重复且 GREEN；无例外目标规则的首次运行有保存的 RED 证据并打印具体边；加入精确 debt manifest 后 P0 最终命令 GREEN；P0 不修改生产行为。

### P1 — 收口 task/workflow/goal/loop ownership

先增加 RED：禁止 `agent/domain -> agent/application` 和 `agent/loop -> agent/application`，禁止 `runtime ↔ workflow`。

动作：

- 按 10.2 迁移 runtime task DTO 和 workflow。
- 将 prompt policy 所需的 `BaseSystemProfile`、profile spec、section contract 放入 domain-neutral contracts；application 负责实际 section materialization。
- 将 `_ATTACHMENT_RE`、`_pasted_spans` 等附件 token 解析收口到 `agent/domain/turn/attachments.py`，公开语义化函数，loop 不导入 application 私有符号。
- 聚合 `agent/goal`、`agent/loop` 与对应 application 文件。
- 更新所有 `memory`、`tools`、`ui`、测试消费者到 Agent domain/application 新路径。
- 删除 `src/voidx/workflow/` 和已迁空的 `src/voidx/runtime/{intent,task_state,todo,attachments,reference_tokens,goal}.py`；`runtime/ui.py`、`ui_port.py` 留到 P4 删除。

验证：

```bash
./test.py --backend -- src/tests/test_agent src/tests/test_runtime/test_execution_context.py src/tests/test_runtime/test_processes.py src/tests/test_runtime/test_runtime_ui.py -v
./test.py --backend -- src/tests/test_architecture -v
./test.py --backend
```

Acceptance：workflow 成为 Agent automation 的单一 owner；两个层级环消失；行为测试不变。

### P2 — 持久化反转和 memory 拆除

先增加 RED：禁止 `agent/domain|application|ports -> voidx.memory|voidx.persistence`，禁止 `persistence -> feature`，禁止 Agent 核心导入 presentation transcript DTO。

动作：

- 按 9.1 建立 Agent persistence ports；按 9.4 建立 `MigrationStep/Plan/Runner` 和 `bootstrap/persistence.py`。
- 保持现有 SQL、表、列、CAS、lease、fencing、outbox 和事务顺序，将 `memory/thread_store.py` 拆为 adapter 内部 repository；跨 thread/attempt/outbox 原子操作由一个 runtime transaction port 表达，不在 application 拼 repository 事务。
- 将 session、runtime-state、context-frame、subagent 和 cleanup 迁到 Agent persistence adapters；将 profile persistence 迁到 config adapter，删除 `config.ports` 的全局 bind 和 memory fallback。
- 按 9.5 执行 transcript 单轨迁移：`memory/transcript.py` + `ui/transcript.py` 合并到 `ui/transcript_snapshot.py`；LangGraph runtime 改调 `PresentationSnapshotPort`；原 memory transcript owner 同阶段删除。
- 通用 SQLite connection/locking/retry、JSONL I/O 和 MigrationRunner 迁入 `persistence/`。bootstrap 在任何 repository/Settings/Agent/Gateway 构造前完成唯一 plan；repository 不再隐式初始化 schema。
- 实现 P0 定义的 fresh/v0/v1/v2/v3、schema manifest、JSON/JSONL byte fixture、migration rollback/idempotency、CAS/lease/fencing/outbox 故障注入测试。
- 全仓改为依赖 feature application/ports，不再依赖 `memory.service` 聚合 facade；`tools/file/state.py` 和 `agent/infrastructure/tool_result_storage.py` 的 `store.DATA_DIR` 改为注入 storage root 或调用当前 `voidx.paths.voidx_home()`，P7 再机械迁到 `platform.paths`，P2 后不得有任何 `voidx.memory` import/string monkeypatch target。
- 测试迁移：
  - `test_memory/test_schema_migration.py`、`test_jsonl_store.py` → `test_persistence/`。
  - session/runtime/context/thread tests → `test_agent/adapters/persistence/`。
  - profile tests → `test_config/adapters/`。
  - transcript tests → P2 临时 `test_ui/transcript_snapshot/`，P4 原样迁到 `test_presentation/adapters/persistence/`。
  - `test_memory/test_main.py`、`test_main_startup.py` → `test_entrypoint/`。
- 删除 `src/voidx/memory/` 和 `src/tests/test_memory/`。

验证：

```bash
./test.py --backend -- src/tests/test_persistence src/tests/test_agent/adapters/persistence src/tests/test_config/adapters src/tests/test_ui/transcript_snapshot src/tests/test_entrypoint -v
./test.py --backend -- src/tests/test_architecture src/tests/test_contracts -v
./test.py --backend
```

Acceptance：P0 persistence manifest 全部 zero-diff；fresh/v0-v3 migration、rollback、重复初始化、CAS/lease/fencing/outbox 原子性通过；Agent 核心可用 in-memory fake；transcript 只有 `ui/transcript_snapshot.py` 一个 owner；`memory` 包和测试目录删除。

### P3 — Tooling/permission/MCP/LSP 单向化

先增加 RED：禁止 `tooling/domain|ports|application|policy|builtin -> agent|presentation|config`，禁止 `mcp -> tooling|llm|config`，禁止 permission policy 导入具体 tool。

动作：

- 建立 `tooling/domain`、`ports`、`application`、`policy`；先移动 ToolCall/Result/Context 和 permission/risk DTO，再迁 authorization 流程。
- 将 PowerShell/Bash 共用 sandbox/path/shell policy 移到 `tooling/policy/shell`；以 `shell_kind` 或 parser port 区分，不由 policy 导入 tool。
- 按 9.5 和 10.4.3 拆分 permission：纯 DTO/rules、状态化 PermissionService、in-memory locks/lease、config-owned GrantRepository、event/AI model ports 各有唯一 owner；bootstrap 按规定顺序绑定。
- AI approval orchestration 依赖 `ApprovalModel`，LLM adapter 由 bootstrap 注入。
- 按 9.4 建立 `LspOperations`，Tooling LSP adapter 只依赖该 port；bootstrap 创建 concrete client factory/manager/service 后注入。
- 严格按 10.4 逐文件迁移：通用 file/search/shell/git/web/document 进 builtin；MCP/LSP/skills 进 Tooling adapters；todo/task/clarify/checkpoint/goal/loop/workflow/compaction/subagent/message 进 Agent domain/application/adapters。
- 定义 `ToolPlugin`（`id, description, parameters_schema, execute`）和三个显式 factory：`build_builtin_plugins(...)`、`build_integration_plugins(...)`、`build_agent_plugins(...)`。`bootstrap/tooling.py` 按 P0 `tool_catalog.json` 的当前严格顺序拼接并调用 registry；重复 id 启动失败。
- 删除 `tools/service.py` 聚合 facade 和 registry 内的 package import registration；Tooling 不导入 Agent factory，Agent adapter 可以实现 Tooling port。
- MCP manager 移除 ToolRegistry、PermissionService 和 Settings 依赖，只管理 client/catalog/status并接收 MCP domain config。
- MCP description generation 通过 `StructuredTextGenerator` port；具体 LLM adapter 在 bootstrap 注入，MCP core 不导入 LLM。
- MCP/LSP wrapper 只位于 `tooling/adapters`；MCP built-in web server 作为下游 integration，不进入 MCP core。
- 测试按 owner 移到 `test_tooling/**` 和 `test_agent/adapters/tools/**`；MCP/LSP 保持 feature 目录。
- 删除 `src/voidx/tools/` 和 `src/voidx/permission/`。

验证：

```bash
./test.py --backend -- src/tests/test_tooling src/tests/test_agent/adapters/tools src/tests/test_mcp src/tests/test_lsp -v
./test.py --backend -- src/tests/test_agent_runtime src/tests/test_infrastructure/runtime -v
./test.py --backend -- src/tests/test_architecture src/tests/test_contracts -v
./test.py --backend
```

Acceptance：P0 `tool_catalog.json` 和 `tool_results.json` 对 success/invalid/denied/unavailable/timeout 全部 zero-diff，ToolResult 七个字段无漂移；注册顺序和 tool id 集合不变；10.4.1 tools 与 10.4.3 permission source 集合分别和 P3 开始清单全等、每项 final target 存在或明确 delete、旧 source 全部不存在；LSP Tool adapter 只导入 LSP domain/operations port；permission/MCP/tooling/Agent tool 依赖符合矩阵；旧 tools/permission 包删除。

### P4 — Presentation 隔离和 AgentService 拆分

先增加 RED：禁止 `agent/application|domain|ports -> presentation`；禁止 application 创建 frontend、Gateway server、consumer。

动作：

- 按 9.2 拆分 `ExecutionHost`，先由现有 LangGraph execution 实现窄端口。
- 从 `agent/application/agent_service.py` 提取：
  - `presentation/terminal/run_loop.py`
  - `presentation/terminal/startup.py`
  - `presentation/gateway/command_handler.py`
  - `presentation/gateway/session_adapter.py`
- application lifecycle 只启动/停止核心服务并执行 use case。
- presentation 通过 status reader 获取模型、usage、workflow、MCP 状态，不读取 execution 私有属性。
- 工具交互事件通过 port 发布，clarify/checkpoint/goal 不直接 import UI event bus。
- 迁移 `ui/**` 为 `presentation/**`，同步更新：
  - `scripts/export_ui_protocol_schema.py`
  - `tui/**`
  - `src/tests/test_ui/**` 到 `src/tests/test_presentation/**`
  - P2 的 `ui/transcript_snapshot.py` 和 `test_ui/transcript_snapshot/**` 原样移到 `presentation/adapters/persistence/` 与对应测试目录
  - frontend schema 导出 import
- bootstrap 注入 presentation-owned `PresentationSnapshotPort` 实现；Agent/LangGraph 不导入 transcript adapter。
- 删除 `src/voidx/ui/`。

验证：

```bash
./test.py --backend -- src/tests/test_presentation src/tests/test_agent/application src/tests/test_entrypoint tui/tests -v
./test.py --backend -- src/tests/test_architecture src/tests/test_contracts src/tests/test_presentation/protocol -v
./python.py scripts/export_ui_protocol_schema.py
git diff --exit-code -- frontend/src/rpc/protocol.schema.json
./test.py --frontend
./test.py --desktop
```

P4 开始前的行为基线使用旧路径 `src/tests/test_ui/**`；完成移动后的 Acceptance 命令只使用新路径 `src/tests/test_presentation/**`。迁移提交必须同步更新脚本和 CI 引用。

Acceptance：整个 protocol suite 和 UI schema 无差异；transcript JSONL/index/checkpoint fixture zero-diff 且唯一 owner 位于 presentation；Agent application 不导入 presentation；TUI/Web/desktop 行为通过。

### P5 — Subagent Gateway 和显式 composition

先增加 RED：禁止领域路由规则和 asyncio transport 同文件；禁止 `SimpleNamespace`、依赖动态属性和宽 Host Protocol。

动作：

- 将 AgentRun/AgentMessage/状态转换迁入 `agent/domain/subagent.py`。
- 将父子授权、路由和 terminal policy 迁入 `agent/application/subagent/`。
- 将 Queue/Task/Event、容量和 timeout 实现迁入 `agent/adapters/subagent/inprocess_gateway.py`。
- 用 dataclass `ApplicationResources`、`AgentResources`、`IntegrationResources` 显式组装依赖。
- 将 `agent/composition.py` 移到 `bootstrap/agent.py`。
- 用 `ParentResultPublisher` 替代 `_make_goal_result_notifier()` 内部异步 local import 和异常吞噬。
- 删除 `execution.loop_service = ...`、`execution.goal_service = ...` 和对应 `getattr/hasattr`。
- `agent/facade.py` 仅暴露稳定 run/session/use-case 入口。
- 将 `src/tests/test_gateway/**` 拆到 `src/tests/test_agent/application/subagent/**` 与 `src/tests/test_agent/adapters/subagent/**`；P3 已迁移的 agent tool 测试继续位于 `src/tests/test_agent/adapters/tools/**`。
- 将 `src/tests/test_agent_runtime/**` 与 `src/tests/test_infrastructure/**` 按 application runtime 和 LangGraph adapter ownership 移到 `src/tests/test_agent/application/runtime/**`、`src/tests/test_agent/adapters/langgraph/**`。

验证：

```bash
./test.py --backend -- src/tests/test_agent/application/subagent src/tests/test_agent/adapters/subagent src/tests/test_agent/adapters/tools src/tests/test_agent/application/runtime src/tests/test_agent/adapters/langgraph src/tests/test_entrypoint -v
./test.py --backend -- src/tests/test_architecture -v
./test.py --backend
```

Acceptance：组合缺失在启动时明确失败；subagent route 与 transport 可分别使用纯测试/fake 测试；`agent/composition.py` 和混合 gateway 删除。

### P6 — LLM/config/skills 内部分层和显式 catalog

先增加 RED：禁止 provider 注册依赖 package import 副作用；禁止 config fallback 导入 persistence adapter；禁止 `skills.references ↔ skills.service`。

动作：

- 按 domain/application/adapters 整理 LLM；将 `ModelConfig`、`ReasoningEffort` 和 provider contracts 迁入 `llm/domain`，保持 provider 名称、protocol、默认 URL、模型、context limit、reasoning 和 temperature 映射。
- provider 模块导出纯 `ProviderSpec` 或 factory；`bootstrap/providers.py` 按 P0 `providers.json` 的严格顺序显式创建列表并注入 `llm/application/catalog.py`，不通过 `providers/__init__.py` 导入副作用注册。
- config 只负责解析、校验和构建各 feature 的 domain config；feature 核心不反向导入 config，profile repository 经 port 注入。
- 保持 P0 `config.json` 的 workspace/global 路径和 key scope、legacy migration、runtime/profile/env/file/CLI 优先级、MCP transport/default Config 全案例；保持 `${SECRET}` 脱敏。
- 运行 P0 state-machine contract，确保本阶段 DTO/enum 移动未改变合法/非法 transition、retry/cancel/resume/recovery 和事件/持久化顺序。
- skill reference 解析成为纯函数/应用用例；`SkillService` 不再通过 local import 反向调用 references。
- Settings 不在 feature service 内隐式创建；bootstrap 创建一次并注入 selection/config。
- 保持 `pyproject.toml` 的 bundled skill package data。

验证：

```bash
./test.py --backend -- src/tests/test_llm src/tests/test_config src/tests/test_skills -v
./test.py --backend -- src/tests/test_architecture src/tests/test_contracts -v
./test.py --backend
```

Acceptance：P0 config/providers/state-machine fixtures 全部 zero-diff；provider/skills 行为不变；无 import 副作用注册和 config persistence fallback；`agent|tooling|llm|mcp|lsp|skills -> config` 的 debt 全部删除。

### P7 — Foundation 重命名、旧包清理和最终拓扑

动作：

- 按 10.1 迁移 platform、observability、update 和 diffing。
- 创建最终 bootstrap API 并让 `main.py` 只导入/调用 `voidx.bootstrap`；`main.py` 不直接导入 presentation 或 agent facade/concrete adapter。
- 更新 `pyproject.toml`、scripts、TUI、tests 和所有 import。
- 删除遗留空包：`runtime`、`workflow`、`memory`、`permission`、`tools`、`ui`、`logging` 及旧 `agent/goal`、`agent/loop`、`agent/runtime`、`agent/infrastructure`、`agent/gateway`、`agent/slash`。
- 删除所有临时 architecture debt allowlist。
- 更新 `src/AGENTS.md` 的模块图、ownership 和依赖矩阵。
- 在可执行范围 `src/`、`tui/`、`scripts/`、`desktop/`、`frontend/`、`pyproject.toml`、`voidx.py` 和活跃 `docs/design|specs` 的命令/import 示例中搜索旧路径；禁止 Python import、字面量 dynamic import、monkeypatch target、entry point 和 shell command 引用。排除 `docs/archive/**` 及本规格第 2/10/12 节描述历史 Current 路径的 prose/table。
- 唯一 executable-command 豁免是本规格 P0-P6 各“验证”代码块中指向当时阶段后路径的命令；P7 扫描器按本文件路径 + phase heading + `验证` fenced block 精确跳过，不跳过本规格 P7 命令、不跳过其他 active spec，也不跳过任何生产/测试/脚本文件。P0-P6 命令是已执行的阶段历史记录，不得在 P7 后重新执行。

验证：

```bash
./test.py --backend -- src/tests/test_architecture src/tests/test_contracts -v
./test.py --backend
./test.py --frontend
./test.py --desktop
./python.py scripts/export_ui_protocol_schema.py
git diff --exit-code -- frontend/src/rpc/protocol.schema.json
./python.py -m compileall -q src/voidx
./python.py scripts/package.py --format all --clean --verify
```

最终 Acceptance：

- 目标 package/layer 规则零 debt。
- 无文件级 runtime 或 type import SCC。
- 旧包和兼容 shim 不存在。
- 全量测试和 package verify 通过。
- UI schema、tool/CLI contract、持久化 fixtures 无差异。

## 13. 测试目录迁移

生产文件移动时，测试同步镜像目标结构：

| Current tests | Target tests |
|---|---|
| `test_domain`、`test_application`、`test_runtime` 的 task 部分、`test_workflow`、`test_goal`、`test_loop` | P1：`test_agent/domain`、`test_agent/application/automation` |
| `test_memory` | P2：`test_persistence` + `test_agent/adapters/persistence` + `test_config/adapters` + 临时 `test_ui/transcript_snapshot`；其中 `test_main*.py` 进 `test_entrypoint` |
| `test_tools`、`test_permission` | P3：`test_tooling` + `test_agent/adapters/tools` |
| `test_ui`（含 P2 transcript 临时 owner） | P4：`test_presentation`，transcript 进 `test_presentation/adapters/persistence` |
| `test_gateway` | P5：`test_agent/application/subagent` + `test_agent/adapters/subagent` |
| `test_agent_runtime`、`test_infrastructure` | P5：`test_agent/application/runtime` + `test_agent/adapters/langgraph` |
| `test_logging` | P7：`test_observability` |
| `test_selfupdate` | P7：`test_update` |

测试移动不得改变断言语义；若因构造方式改变，只替换 fixture/adapter 构造，不降低覆盖或放宽断言。每阶段命令仅引用该阶段完成后的目标路径；开始阶段前先用旧路径运行同一测试集记录 GREEN 基线。

## 14. 架构测试设计

`src/tests/test_architecture/import_graph.py` 应输出：

```text
forbidden dependency:
  source: src/voidx/agent/application/foo.py
  target: voidx.presentation.gateway
  rule: agent.application may depend only on agent.domain and agent.ports
```

检查维度：

1. 文件级 SCC，运行时和 TYPE_CHECKING 图均为零。
2. 顶层包允许矩阵。
3. 包内层次规则。
4. composition binding：`domain/application/ports/facade/main` 不得导入 `adapters`；一个 feature 的 adapter 不得导入另一个 feature 的 adapter；跨 feature concrete adapter import 只允许 `bootstrap/**`。同一 adapter subtree 内部 helper import 允许。
5. 禁止旧包名。
6. 禁止跨层 private import（导入 `_name`）。
7. 禁止注册副作用 `__init__.py`。
8. 禁止 `SimpleNamespace` 和 dependency probing 出现在 bootstrap/application。

允许例外必须包含：精确 source、精确 target、理由和删除阶段；P7 后例外列表为空。

## 15. 风险与控制

### 15.1 行为漂移

风险：移动过程中顺便清理代码，改变事件顺序、错误消息或默认值。

控制：先建立 contract fixture；移动阶段禁止逻辑优化；所有行为变化单独立项。

### 15.2 持久化事务被拆散

风险：将 ThreadStore 拆为多个 repository 后破坏 CAS/outbox/attempt 原子性。

控制：端口按原子用例而非数据库表拆分；保留单事务 adapter；增加并发、lease 和 recovery fixture。

### 15.3 import 副作用变化

风险：provider/tool 注册顺序变化导致可用工具或模型列表变化。

控制：记录顺序敏感 snapshot；显式 catalog 使用当前顺序；bootstrap 测试验证完整注册集合。

### 15.4 大范围重命名影响 monkeypatch 和打包

风险：测试字符串路径、TUI、scripts、setuptools package-data 漏改。

控制：全仓 AST/文本搜索旧路径；运行 backend/frontend/desktop/package verify。

### 15.5 工作树和并行开发冲突

风险：重构触及大量文件，与功能分支冲突。

控制：每阶段独立、短生命周期；开始阶段前记录 dirty tree；不得回退无关改动；优先完成正在修改的垂直切片后再移动其目录。

### 15.6 过度分层

风险：产生大量只有 re-export 或单一空类的目录。

控制：只有真实存在 domain/application/adapter 职责时建层；小模块可保留单文件；禁止空壳和仅兼容用途的模块。

## 16. 回滚策略

- 回滚单位是完整阶段，不是单个随机文件。
- 每阶段开始前确保前一阶段全量 backend GREEN。
- 纯移动和依赖反转尽量拆成相邻提交，但阶段结束前一起验收。
- 数据格式不变，因此代码回滚不需要数据回滚。
- 如果某阶段无法在当日恢复 GREEN，回滚该阶段全部变更，不保留双轨临时状态。

## 17. 完成定义

全部满足后才可声明重构完成：

- [ ] P0-P7 的 Acceptance 全部通过。
- [ ] `./test.py` 全套通过或仅有项目已记录的环境性 skip。
- [ ] wheel build/verify 通过。
- [ ] UI protocol schema 无 diff。
- [ ] persistence compatibility fixtures 无 diff。
- [ ] tool/CLI contract fixtures 无 diff。
- [ ] architecture allowlist 无 debt，SCC 为零。
- [ ] 所有旧包删除，无 re-export/shim。
- [ ] `src/AGENTS.md` 与最终代码结构一致。
- [ ] 实际实现文件存在并可运行后，才执行：

```bash
./scripts/archive.py docs/specs/src-voidx-modular-architecture-refactor-2026-08-05.md
```

## 18. 实施顺序摘要

```text
P0 契约护栏
  → P1 Agent task/workflow/goal/loop ownership
  → P2 persistence ports + 删除 memory
  → P3 tooling/permission/MCP/LSP 单向化
  → P4 presentation 隔离 + 拆 AgentService
  → P5 subagent gateway + 显式 bootstrap
  → P6 LLM/config/skills 显式 catalog
  → P7 foundation 重命名 + 删除旧包 + 全量验证
```

不得跳过 P0；P1-P6 若因并行开发调整顺序，必须先证明两阶段没有文件 ownership 重叠，并分别保持依赖矩阵单向。P7 只能在前序阶段全部 GREEN 后执行。
