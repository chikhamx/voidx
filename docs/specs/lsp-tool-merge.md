# Spec: 合并 LSP 工具为统一 `lsp` 工具

> **Status**: In Progress
> **Created**: 2026-06-14

## 1. 目标

将当前 4 个独立 LSP 工具（`lsp_diagnostics`、`lsp_symbols`、`lsp_definition`、`lsp_references`）合并为 1 个统一的 `lsp` 工具，通过 `operation` 参数区分操作。`lsp_format` 代码保留但暂不暴露。

## 2. 动机

### 2.1 减少 LLM token 消耗

当前 4 个工具在 function calling schema 中各占一条，LLM 每轮都要处理 4 个工具定义。合并后减少为 1 个，显著降低 token 开销。

### 2.2 与行业实践对齐

opencode、Claude Code 等主流编码代理均采用单工具 + operation 参数的模式。统一接口更符合 LLM 的调用习惯。

### 2.3 lsp_format 保留但暂不暴露

`lsp_format` 代码保留在 `tools/lsp.py` 中，但不纳入合并后的 `operation` 枚举。后续需要时再启用，届时需处理写权限和 `lsp:format` denied_tools 逻辑。

### 2.4 不纳入的操作

| 操作 | 排除理由 |
|------|----------|
| `hover` | 需 LspManager 新增实现，部分语言服务器不支持，暂不纳入 |
| `workspaceSymbol` | grep + glob 可替代，低频 |
| `goToImplementation` | 仅 OOP 重项目有用，低频 |
| `prepareCallHierarchy` | 极低频，findReferences 可替代 |
| `incomingCalls` | 极低频，findReferences 可替代 |
| `outgoingCalls` | 极低频，grep 可替代 |

## 3. 设计

### 3.1 新工具定义

**工具 ID**: `lsp`

**参数 Schema**:

```python
class LspInput(BaseModel):
    operation: Literal[
        "diagnostics",
        "definition",
        "references",
        "symbols",
    ] = Field(description="The LSP operation to perform.")

    file_path: str | None = Field(
        default=None,
        description="Absolute or relative path to the file. "
        "Required for all operations except diagnostics (when omitted, returns cached diagnostics for opened files).",
    )

    line: int = Field(
        default=1,
        ge=1,
        description="1-based line number. Required for definition, references.",
    )

    character: int = Field(
        default=0,
        ge=0,
        description="0-based character offset. Required for definition, references.",
    )

    include_declaration: bool = Field(
        default=True,
        description="Include the symbol declaration in results. Only for references operation.",
    )
```

### 3.2 操作说明

| operation | 必需参数 | 说明 | 权限 |
|-----------|----------|------|------|
| `diagnostics` | file_path (可选) | 获取文件诊断或已打开文件的缓存诊断 | allow |
| `definition` | file_path, line, character | 查找符号定义位置 | allow |
| `references` | file_path, line, character | 查找符号引用 | allow |
| `symbols` | file_path | 获取文件内符号列表 | allow |

### 3.3 权限设计

所有 4 个 operation 均为只读，权限统一为 allow，无需子操作区分。

```python
# permission/rules.py
Rule(permission="lsp", pattern="*", action="allow"),
```

`lsp_format` 暂不暴露，后续启用时再处理写权限和 `lsp:format` denied_tools 逻辑。

### 3.4 工作流 denied_tools 适配

当前工作流节点用 `denied_tools=("write", "edit", "lsp_format")` 阻止格式化。合并后 `lsp_format` 不暴露，直接移除：

```python
denied_tools=("write", "edit")
```

后续启用 format 时再添加 `lsp:format`。

### 3.5 LspFormatTool 代码保留

`LspFormatTool` 类保留在 `tools/lsp.py` 中但不注册到工具列表。后续启用 format 操作时：
1. 在 `LspInput.operation` 枚举中添加 `"format"`
2. 在 `LspTool.execute` 中添加 format 分支
3. 处理写权限和 denied_tools

### 3.6 UI 渲染适配

当前 UI 对每个 LSP 工具有独立的状态文案：

```python
# console/app.py (当前)
"lsp_diagnostics": "checking", "lsp_symbols": "indexing",
"lsp_definition": "locating", "lsp_references": "finding",
"lsp_format": "formatting",
```

合并后改为按 operation 分发：

```python
# console/app.py (新)
"lsp": {
    "diagnostics": "checking",
    "definition": "locating",
    "references": "finding",
    "symbols": "indexing",
}
```

`dock/nodes.py` 和 `session.py` 同理适配。

## 4. 变更文件清单

| 文件 | 变更内容 |
|------|----------|
| `src/voidx/tools/lsp.py` | 重写：4 个工具类合并为 1 个 `LspTool` + `LspInput`，`LspFormatTool` 保留但不注册 |
| `src/voidx/lsp/service.py` | 无变更 |
| `src/voidx/lsp/manager.py` | 无变更 |
| `src/voidx/lsp/schema.py` | 无变更 |
| `src/voidx/permission/rules.py` | 4 条 LSP 规则合并为 1 条 `lsp: allow`，移除 `lsp_format: ask` |
| `src/voidx/permission/service.py` | 更新权限文案 |
| `src/voidx/permission/evaluate.py` | 无变更（暂不需要 `lsp:format` 子操作解析） |
| `src/voidx/agent/agents.py` | 工具列表更新 |
| `src/voidx/agent/runtime_context.py` | 更新约束文案 |
| `src/voidx/agent/slash/handler.py` | 更新 plan mode 文案 |
| `src/voidx/agent/slash/init.py` | 更新权限说明 |
| `src/voidx/workflow/nodes.py` | `denied_tools` 移除 `lsp_format` |
| `src/voidx/ui/output/console/app.py` | LSP 工具状态文案适配（4 个旧 key → 1 个 `lsp` dict） |
| `src/voidx/ui/output/dock/nodes.py` | LSP 工具渲染适配 |
| `src/voidx/ui/session.py` | 移除 `lsp_format` 写入检测 |
| `src/voidx/config/enums.py` | 更新 read-only 说明文案，移除 `lsp_format` 引用 |
| `tests/test_tools/test_basic.py` | 更新工具 ID 断言（4 个旧 ID → 1 个 `lsp`） |
| `tests/test_agent/test_stream_llm.py` | 更新工具名断言 |
| `tests/test_agent/test_core_flow.py` | 移除 `lsp_format` 相关断言 |
| `tests/test_agent/test_permission.py` | 更新权限断言（4 条 → 1 条 `lsp: allow`） |
| `tests/test_lsp.py` | 更新工具注册和执行测试 |

## 5. 向后兼容

### 5.1 旧工具 ID 不再注册

`lsp_diagnostics`、`lsp_symbols`、`lsp_definition`、`lsp_references` 这 4 个 ID 将从工具注册表中移除。`lsp_format` 代码保留但不注册。LLM 不再看到这些工具。

### 5.2 会话迁移

已有会话中的 tool_call 消息引用旧工具 ID，这些是历史记录，不影响新工具执行。无需迁移。

### 5.3 用户配置

用户 settings.json 中如有针对旧工具 ID 的权限规则，需手动更新为 `lsp`。在 release notes 中说明。

## 6. 风险

| 风险 | 缓解措施 |
|------|----------|
| LLM 可能不传必需参数（如 definition 缺少 line/character） | 在 execute 中校验，返回明确错误提示 |
| UI 渲染改动面较广 | 统一用 operation 字段分发，改动模式一致 |
| 后续启用 format/hover 时需再次改动 | 代码保留，启用时增量修改 |
