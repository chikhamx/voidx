# v1 协议清除 — 技术设计文档

## Context

voidx 的 UI 协议层存在 v1 和 v2 两套并存的局面。v2（JSON-RPC 2.0 + Thread/Turn/Item 模型）已经是实际运行的协议——前端 JS、WebSocket gateway、adapter 全部走 v2。v1 的 wire format（6 种 Envelope 类 + `parse_protocol_envelope`）是死代码，没有任何运行时路径使用它。

但 v1 目录下还住着一批 DTO（`UiSubmitCommand`、`UiRequest`、`TranscriptNode` 等），v2 代码仍在引用它们。这造成了认知混乱：开发者看到 `from voidx.ui.protocol import UiSubmitCommand` 会以为这是 v1 的东西，实际上它是 v2 method handler 在用的。

本次清除的目标是：删掉真正的 v1 死代码，把 v2 仍在用的 DTO 迁移到版本无关的位置，让 `src/voidx/ui/protocol/` 下不再有 v1/v2 混居。

## Goals and Non-Goals

### Goals

- 删除 v1 Envelope wire format（6 个类 + 解析函数 + `PROTOCOL_VERSION = 1`）
- 删除 v1 Envelope 的测试代码
- 清理 `__init__.py` 中对 v1 Envelope 的重导出
- 将 `export_protocol_schema()` 改为导出 v2 schema（而非 v1 `ProtocolEnvelope`）
- 将 v2 仍在用的 DTO（commands / requests / transcript）提升为版本无关的公共 DTO，消除 v1/v2 混居
- 所有测试通过，前端行为不变

### Non-Goals

- 不修改 v2 JSON-RPC 协议本身的行为
- 不修改前端 JS 代码（前端已经全部走 v2）
- 不修改 agent core 的事件系统（`UiEvent` 及其 38 个子类）
- 不修改 `UiFrontend` / `UiController` Protocol 接口

## Architecture

### 当前状态

```
src/voidx/ui/protocol/
├── __init__.py          ← 混合导出 v1 envelope + v1 DTO + v2 re-export
├── envelope.py          ← v1 死代码：6 个 Envelope 类 + parse_protocol_envelope
├── commands.py          ← v1 DTO，v2 仍在用（UiSubmitCommand, UiCancelCommand）
├── requests.py          ← v1 DTO，v2 仍在用（UiRequest, UiResponse 等）
├── transcript.py        ← v1 DTO，v2 仍在用（TranscriptNode, TranscriptSnapshot, tree_to_snapshot）
├── schema.py            ← 导出 v1 ProtocolEnvelope schema
└── v2/
    ├── envelope.py      ← v2 JSON-RPC envelope（活跃）
    ├── methods.py       ← v2 method dispatch（活跃）
    ├── snapshot.py      ← v2 WorkspaceSnapshot / ThreadSnapshot（活跃，引用 v1 TranscriptNode）
    └── threads.py       ← v2 ThreadInfo / TurnInfo / Item（活跃）
```

### 目标状态

```
src/voidx/ui/protocol/
├── __init__.py          ← 只导出公共 DTO + v2 re-export
├── commands.py          ← 不变（提升为版本无关公共 DTO）
├── requests.py          ← 不变（提升为版本无关公共 DTO）
├── transcript.py        ← 不变（提升为版本无关公共 DTO）
├── schema.py            ← 改为导出 v2 schema
└── v2/
    ├── __init__.py      ← 新增：v2 包导出
    ├── envelope.py      ← 不变
    ├── methods.py       ← 不变
    ├── snapshot.py      ← 不变（继续从 ../transcript 引用 TranscriptNode）
    └── threads.py       ← 不变
```

**删除的文件**：
- `src/voidx/ui/protocol/envelope.py` — 整个文件

**删除的测试**：
- `tests/test_ui/gateway/test_ui_frontend_protocol.py` — 整个文件（测试 v1 envelope 序列化）
  - 注意：该文件中也测试了 DTO 的 round-trip（`UiSubmitCommand`、`UiChoiceRequest`、`tree_to_snapshot` 等），这些测试需要迁移到新的测试文件

## Data Model

### 删除的类型（v1 Envelope wire format）

| 类型 | 文件 | 删除原因 |
|------|------|----------|
| `ProtocolEnvelopeBase` | `envelope.py` | v1 wire format 基类，无运行时引用 |
| `UiHello` | `envelope.py` | v1 hello 握手，v2 用 `workspace.snapshot` notification 替代 |
| `UiHelloEnvelope` | `envelope.py` | 同上 |
| `UiSnapshotEnvelope` | `envelope.py` | v2 用 `WorkspaceSnapshot` 替代 |
| `UiEventEnvelope` | `envelope.py` | v2 用 `JsonRpcNotification` 替代 |
| `UiRequestEnvelope` | `envelope.py` | v2 用 `JsonRpcRequest` 替代 |
| `UiResponseEnvelope` | `envelope.py` | v2 用 `JsonRpcResult` 替代 |
| `UiCommandEnvelope` | `envelope.py` | v2 用 `JsonRpcRequest` 替代 |
| `ProtocolEnvelope` | `envelope.py` | v1 union type，无运行时引用 |
| `parse_protocol_envelope` | `envelope.py` | v1 解析函数，无运行时引用 |
| `PROTOCOL_VERSION = 1` | `envelope.py` | v1 版本号，无运行时引用 |

### 保留的类型（版本无关公共 DTO）

| 类型 | 文件 | 保留原因 |
|------|------|----------|
| `UiSubmitCommand` | `commands.py` | v2 `session.submit` method handler 构造 |
| `UiCancelCommand` | `commands.py` | v2 `session.cancel` method handler 构造 |
| `UiCommand` | `commands.py` | union type，`parse_ui_command` 使用 |
| `parse_ui_command` | `commands.py` | 命令解析 |
| `UiChoiceRequest` | `requests.py` | v2 gateway session request 机制使用 |
| `UiTextRequest` | `requests.py` | 同上 |
| `UiPermissionRequest` | `requests.py` | 同上 |
| `UiRequest` | `requests.py` | union type |
| `UiResponse` | `requests.py` | v2 gateway session response |
| `parse_ui_request` | `requests.py` | 请求解析 |
| `TranscriptNode` | `transcript.py` | v2 `ThreadSnapshot` 内嵌 |
| `TranscriptSnapshot` | `transcript.py` | v2 snapshot 构造使用 |
| `tree_to_snapshot` | `transcript.py` | v2 `GatewaySession.broadcast_snapshot` 使用 |

## API Contract

### `export_protocol_schema()` 变更

**当前签名**（不变）：
```python
def export_protocol_schema() -> dict[str, Any]:
```

**当前行为**：导出 v1 `ProtocolEnvelope` 的 JSON Schema（包含 6 种 Envelope 的 union）

**变更后行为**：导出 v2 相关 schema，包含：
- `WorkspaceSnapshot`（连接时推送的完整状态）
- `ThreadSnapshot`（单 thread 的 transcript）
- `ThreadInfo` / `TurnInfo` / `Item`（v2 原语）
- `UiRequest`（`UiChoiceRequest` | `UiTextRequest` | `UiPermissionRequest`）
- `UiCommand`（`UiSubmitCommand` | `UiCancelCommand`）
- `TranscriptNode` / `TranscriptSnapshot`

**实现**：
```python
from voidx.ui.protocol.commands import UiCommand
from voidx.ui.protocol.requests import UiRequest
from voidx.ui.protocol.transcript import TranscriptSnapshot
from voidx.ui.protocol.v2.snapshot import WorkspaceSnapshot, ThreadSnapshot
from voidx.ui.protocol.v2.threads import ThreadInfo, TurnInfo, Item

def export_protocol_schema() -> dict[str, Any]:
    schema = TypeAdapter(
        WorkspaceSnapshot | ThreadSnapshot | ThreadInfo | TurnInfo | Item
        | TranscriptSnapshot | UiRequest | UiCommand
    ).json_schema(ref_template="#/$defs/{model}")
    defs = dict(schema.pop("$defs", {}))
    return {
        "title": "VoidxUiProtocol",
        "type": "object",
        "$defs": defs,
    }
```

### `__init__.py` 变更

**删除的导出**：
- `ProtocolEnvelope`
- `UiCommandEnvelope`
- `UiEventEnvelope`
- `UiHello`
- `UiHelloEnvelope`
- `UiRequestEnvelope`
- `UiResponseEnvelope`
- `UiSnapshotEnvelope`
- `parse_protocol_envelope`

**保留的导出**（不变）：
- `UiCancelCommand`, `UiSubmitCommand`, `UiCommand`, `parse_ui_command`
- `UiChoiceRequest`, `UiPermissionRequest`, `UiRequest`, `UiResponse`, `UiTextRequest`, `parse_ui_request`
- `TranscriptNode`, `TranscriptSnapshot`, `tree_to_snapshot`
- `export_protocol_schema`

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| 外部代码仍 import v1 Envelope 类 | 删除前全局搜索确认无运行时引用；`__init__.py` 不再导出，import 会直接报 `ImportError` |
| `export_protocol_schema()` 输出变化导致前端 `protocol.schema.json` 变化 | 重新运行 `npm run schema` 生成新的 `protocol.d.ts`；前端 JS 已全部走 v2，不受影响 |
| 测试文件删除后丢失 DTO round-trip 覆盖 | 将 DTO 测试迁移到新文件 `tests/test_ui/protocol/test_dto.py` |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 删除 `envelope.py` 整个文件 | 保留但标记 deprecated | 全局搜索确认无运行时引用，保留只会增加认知负担 |
| DTO 保留在原位置（`commands.py` / `requests.py` / `transcript.py`） | 迁移到 `v2/` 命名空间 | 这些 DTO 是版本无关的公共数据结构，不是 v2 专属；迁移到 v2 会造成反向依赖（v2 目录被 v1 目录引用）|
| 删除 `test_ui_frontend_protocol.py` 并迁移 DTO 测试 | 保留文件只删 envelope 测试 | 文件名暗示测试 v1 protocol，清除后应让测试组织反映新结构 |
| `export_protocol_schema()` 改为导出 v2 schema | 保留导出 v1 schema | 前端 `protocol.schema.json` 是自动生成的，v1 schema 已无实际消费方 |

## Open Questions

- [x] `frontend/src/protocol.schema.json` 和 `frontend/src/protocol.d.ts` 是否有前端代码实际引用？→ **已确认：前端 JS 代码没有 import 这两个文件**。它们是 `npm run schema` 自动生成的，但无实际消费方。`export_protocol_schema()` 的输出变更不会影响前端行为。
- [ ] 是否有外部工具或脚本（CI/CD、文档生成）引用了 v1 `ProtocolEnvelope`？→ 需在实施前全局搜索 `ProtocolEnvelope` 确认。
