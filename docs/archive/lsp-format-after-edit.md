# 文件编辑后自动 LSP 格式化设计

> **Status: Done** — Implemented in commit 63ca41ae: write/replace auto LSP range formatting with safe on-disk convergence.

## 结论

voidx 在文本文件编辑成功后，仅当对应语言服务器支持 `textDocument/rangeFormatting` 时，默认对本次编辑影响的最小行范围执行格式化。服务器不支持范围格式化时直接跳过，绝不退化为 `textDocument/formatting` 全文件格式化。

范围格式化是编辑流程中的 best-effort 收尾步骤：可用且受支持时应用格式化结果；不可用、不支持、超时或失败时保留原编辑，不把工具调用标记为失败。LSP 层只负责计算范围格式化后的文本，不直接写磁盘。最终落盘、版本快照、diff、mtime 和读取覆盖状态仍由文件工具统一管理。

## 背景

现有实现已经具备主要基础能力：

- `src/voidx/lsp/manager.py` 的 `LspManager.format_document()` 会发送 `textDocument/formatting` 请求并应用 `TextEdit`。
- `src/voidx/lsp/service.py` 暴露了格式化调用。
- `src/voidx/tools/lsp.py` 保留了未注册的 `LspFormatTool`。
- `src/voidx/tools/file/write.py` 和 `src/voidx/tools/file/replace.py` 负责文本编辑、安全写入、版本快照和 diff。

当前 `LspManager.format_document()` 会直接调用 `Path.write_text()`。若直接在编辑工具完成后调用它，会造成两个相互独立的写入阶段，并带来以下问题：

- 格式化写入绕过文件工具使用的 `SafePathExecutor`。
- 工具返回的 diff 可能只包含编辑结果，不等于磁盘最终内容。
- 版本快照、mtime 和读取覆盖状态可能被重复或错误更新。
- 格式化失败容易掩盖已经成功的原始编辑。

## 目标

1. `write` 和 `replace` 成功修改文本文件后，仅对本次编辑影响范围尝试 LSP 格式化。
2. 只有服务器声明 `documentRangeFormattingProvider` 时才发送请求。
3. 禁止将不支持范围格式化静默退化为全文件格式化。
4. 工具返回的 diff 始终表示“编辑前内容 → 最终磁盘内容”。
5. 格式化不绕过现有路径授权和安全写入机制。
6. 格式化失败不撤销、不覆盖、也不将原始编辑判定为失败。
7. 正确维护版本快照、mtime、读取覆盖和行号漂移状态。
8. 在没有语言服务器或服务器不支持范围格式化时保持低成本、无噪声退化。

## 非目标

- 不调用 `textDocument/formatting` 执行全文件格式化。
- 不引入非 LSP 格式化器，例如 Black、Prettier 或 rustfmt 的独立进程调用。
- 不在第一版支持按语言配置 formatter、格式化参数或保存超时。
- 不对 `bash` 或其他外部命令产生的文件修改自动格式化。
- 不格式化目录、二进制文件、删除操作、移动操作或空文件创建操作。
- 不保留无范围参数的全文件 `lsp_format` 行为；`lsp_format` 必须显式传入格式化范围。
- 不实现 on-type formatting，也不允许用户任意指定与本次编辑无关的范围。

## 用户行为

自动格式化默认开启。一次文件编辑的可观察结果如下：

| 场景 | 文件结果 | 工具结果 |
| --- | --- | --- |
| LSP 可用、支持范围格式化且返回修改 | 写入编辑并范围格式化后的内容 | 成功，注明已格式化 |
| LSP 范围格式化返回空 edits 或内容未变化 | 写入原编辑内容 | 成功，不额外提示 |
| 无对应 LSP | 写入原编辑内容 | 成功，不额外提示 |
| LSP 不支持范围格式化 | 写入原编辑内容 | 成功，不调用全文件格式化，也不额外提示 |
| LSP 范围格式化超时或失败 | 写入原编辑内容 | 成功，附带非阻断警告 |
| 自动格式化被关闭 | 写入原编辑内容 | 成功，不调用 LSP |

格式化只改变当前工具已经获准写入的同一个文件，并只请求本次编辑影响范围；不申请第二次权限，也不允许语言服务器通过该流程修改其他文件。

## 总体设计

### 编辑流程

文本编辑工具采用单一的收尾流程：

1. 解析并验证编辑请求。
2. 获取写权限并读取原始内容。
3. 保存一次编辑前版本快照。
4. 在内存中计算 `edited_text` 及结构化 diff。
5. 从结构化 diff 计算编辑后文档中的最小影响范围 `format_range`。
6. 通过现有安全写入机制写入 `edited_text`。
7. 若自动格式化开启且 `format_range` 有效，尝试获取该范围的格式化结果。
8. 若范围格式化产生变化，通过同一个安全写入机制写入 `final_text`。
9. 基于 `original_text` 和 `final_text` 生成唯一对外 diff。
10. 更新 mtime 和文件读取状态。
11. 返回编辑和格式化的组合结果。

第 6 步需要先落盘，因为 `LspManager.open_document()` 当前从磁盘读取文档并通过 `didOpen` / `didChange` 同步给语言服务器。第一版保留这一同步方式，避免同时引入内存文档 API。工具对外仍表现为一次编辑操作。

### 格式化范围计算

格式化范围必须由实际应用后的结构化 diff 计算，而不是直接使用用户输入的行号提示。`replace` 的边界可能经过漂移修正，`insert` 还可能消耗 overlap，因此只有最终 diff 能代表磁盘中的真实影响范围。

范围使用 LSP 的 0-based、end-exclusive 语义，并按完整行传递：

```python
LspRange(
    start=LspPosition(line=start_line, character=0),
    end=LspPosition(line=end_line_exclusive, character=0),
)
```

计算规则：

- 单个插入或替换：覆盖编辑后新增或替换的完整行。
- 多处编辑：取所有变更块在编辑后文档中的最小包络范围；第一版只发送一次 `rangeFormatting` 请求，避免多个请求产生相互漂移的坐标。
- 纯删除导致新侧范围为空：锚定删除点之后的第一条存续行；若删除发生在 EOF，则锚定前一条存续行。
- 文件被删除为空：没有有效范围，跳过格式化。
- 单行文件或最后一行无换行符：end position 使用该行 UTF-16 长度，而不是构造越过文档结尾的下一行位置。

LSP character offset 按 UTF-16 code units 计数。由于通常按完整行传递，只有 EOF 边界需要计算字符长度；必须复用或新增明确的 UTF-16 position helper，不能使用 Python `len()` 代替。

### 文件编辑收尾模块

新增：

```text
src/voidx/tools/file/post_edit.py
```

该模块集中封装范围计算和自动格式化策略，不让 `write.py` 和 `replace.py` 分别复制异常处理及状态逻辑。

建议的数据结构：

```python
@dataclass(frozen=True)
class FormatAfterEditResult:
    final_text: str
    status: Literal[
        "disabled",
        "unavailable",
        "unsupported",
        "unchanged",
        "formatted",
        "failed",
    ]
    error: str = ""
```

建议入口：

```python
async def format_after_edit(
    ctx: ToolContext,
    path: Path,
    *,
    display_path: str,
    edited_text: str,
    format_range: LspRange,
) -> FormatAfterEditResult:
    ...
```

职责：

- 根据最终结构化 diff 计算或接收 `format_range`。
- 检查运行时开关和 `ctx.lsp_manager`。
- 请求 LSP 计算范围格式化文本。
- 仅在内容变化时调用文件工具的安全写入函数。
- 将预期退化和异常映射为稳定状态。
- 不保存版本快照、不生成对外 diff、不直接修改读取覆盖状态。

为避免 `write.py` 与 `replace.py` 各自保留一份 `_safe_write_text()`，实施时应将安全文本写入提取为文件工具内部共享函数，例如：

```text
src/voidx/tools/file/io.py
```

该重构只统一现有逻辑，不改变权限模型。

### 先改造 `lsp_format`

现有 `src/voidx/tools/lsp.py` 已包含未注册的 `LspFormatTool`，但输入只有 `file_path`，底层调用全文件 `format_document()`。第一阶段先将它改造成显式范围格式化工具，并注册到 `ToolRegistry`。

建议输入模型复用 LSP 的位置语义，但对模型暴露清晰字段：

```python
class LspFormatInput(BaseModel):
    file_path: str
    start_line: int = Field(ge=1, description="1-based inclusive start line.")
    start_character: int = Field(default=0, ge=0, description="0-based UTF-16 character offset.")
    end_line: int = Field(ge=1, description="1-based inclusive line containing the end position.")
    end_character: int = Field(ge=0, description="0-based UTF-16, end-exclusive character offset.")
```

外部行号保持与其他 voidx 文件工具一致的 1-based 习惯，character 遵循 LSP 的 0-based UTF-16 语义；工具在边界处统一转换成 `LspRange`。输入必须满足 `end >= start`，且位置不能越出当前文档。范围是必填参数，不提供全文件默认值。

`LspFormatTool` 的行为：

1. 通过现有写授权流程解析目标文件，不能只调用 `_resolve_tool_path()` 静默失败。
2. 校验范围并保存一次格式化前版本快照。
3. 调用共享的范围格式化 service。
4. 通过 `SafePathExecutor` 写入结果，不允许 manager 直接写磁盘。
5. 基于格式化前后内容生成 diff，更新 mtime，并按最终 diff 重映射 read coverage。
6. 服务器不支持 `documentRangeFormattingProvider` 时返回明确的 unsupported 结果，绝不回退到全文件格式化。

注册 `lsp_format` 后，需要同步调整现有约束：

- `src/voidx/tools/registry.py` 注册 `LspFormatTool`。
- `src/tests/test_tools/test_tool_registry.py` 从断言未注册改为断言存在。
- 保留 plan mode 禁止写入的授权测试。
- 更新 tool schema 和 invalid args 测试，覆盖必填范围、反向范围及 UTF-16 字段语义。

### 共享 LSP 范围格式化接口

工具层和自动编辑流程不能互相调用工具；两者共同依赖无写入的 service。建议在 `LspManager` 中提供：

```python
async def formatted_range_text(
    self,
    file_path: str,
    range_: LspRange,
) -> tuple[bool, str, str]:
    """Return changed, source_text, formatted_text without writing the file."""
```

执行步骤：

1. 解析并读取目标文件。
2. 调用 `open_document()`，确保服务器看到最新内容。
3. 检查 `client.capabilities["documentRangeFormattingProvider"]`。
4. 不支持时返回明确的 unsupported 结果，不检查或调用全文件 formatting provider。
5. 将 `LspRange` 序列化后发送 `textDocument/rangeFormatting`。
6. 使用 `apply_text_edits()` 在内存中计算新内容。
7. 校验服务器返回的每个 `TextEdit.range` 都不越出请求范围；越界视为失败，避免有缺陷的服务器扩大 diff。
8. 返回结果，不写磁盘。

`LspService` 对应暴露这个只读磁盘、只返回文本的范围格式化接口。显式 `lsp_format` 和编辑后的自动格式化都调用它，各自负责所属文件工具流程中的授权、快照、安全落盘、diff 和状态维护。

现有 `format_document()` 可暂时保留给已有测试，但不再被注册工具或自动格式化路径调用，后续无调用者时删除。

### 能力检查

客户端初始化后已保存服务器 capabilities。发送范围格式化请求前必须检查：

```python
client.capabilities.get("documentRangeFormattingProvider")
```

以下值视为支持：

- `True`
- LSP 允许的 provider options 对象

缺失、`False` 或 `None` 视为不支持。`unsupported` 是正常退化，不写警告，也不查询 `documentFormattingProvider` 作为替代。

语言识别失败、没有启用的服务器或服务器命令不可用均归类为 `unavailable`，不写警告。服务器已被选中但范围格式化请求超时、返回协议错误、返回越界 edits 或进程异常归类为 `failed`，向用户显示简短警告。

## 配置

第一版增加 workspace setting：

```json
{
  "lsp": {
    "format_after_edit": true
  }
}
```

默认值为 `true`。建议新增 `SettingsLspMixin`，并在 `src/voidx/config/settings.py` 中注册：

```python
def get_lsp_format_after_edit(self) -> bool: ...
def set_lsp_format_after_edit(self, enabled: bool) -> Path: ...
```

`lsp` 加入 `WORKSPACE_ONLY_KEYS`，使项目可以根据仓库约定关闭自动格式化，且不会意外影响其他 workspace。

运行时在构造 `ToolContext` 时注入：

```python
lsp_format_after_edit: bool = True
```

工具只读取上下文，不直接实例化或读取 `Settings`。第一版不要求新增 UI 设置页或 slash command；用户可编辑 workspace 的 `.voidx/settings.json`。后续若需要再补充交互入口。

## 写入、快照与状态语义

### 版本快照

一次工具调用只在首次落盘前调用一次 `save_file_version()`，快照内容必须是 `original_text`。格式化前不得再次保存版本。

### Diff

只生成一份：

```text
original_text → final_text
```

不得先生成编辑 diff 再追加格式化 diff，否则 UI 展示和撤销语义会分裂。

### mtime

在最终一次写入后调用 `record_mtime()`。如果格式化失败，编辑内容已经落盘，也必须记录该内容对应的最终 mtime。

### 读取覆盖与行号漂移

- 未调用格式化，或范围格式化结果未变化：沿用当前基于编辑 diff 的覆盖重映射逻辑。
- 范围格式化改变内容：基于 `original_text → final_text` 的最终结构化 diff 统一重映射覆盖和 line drift maps，不再因格式化而清除整文件状态。

范围格式化 edits 已被约束在请求范围内，因此可以沿用现有 diff remap 机制。若服务器返回越界 edit，则拒绝整个格式化结果并保留原编辑内容。

### 原子性

第一版不承诺编辑与格式化两个内部写入之间的事务原子性。若格式化失败，编辑后的有效文件保留。每次写入都必须经过 `SafePathExecutor`，不得使用裸 `Path.write_text()`。

未来若安全写入层支持同目录临时文件和原子替换，可以改为在内存中构造 LSP 文档版本后只落盘一次；这不属于第一版范围。

## 工具返回值

现有 `ToolResult.metadata` 增加可选字段：

```json
{
  "formatting": {
    "status": "formatted"
  }
}
```

失败时：

```json
{
  "formatting": {
    "status": "failed",
    "error": "request timed out"
  }
}
```

约定：

- `formatted` 和 `failed` 写入 metadata，便于 UI 和测试观察。
- `disabled`、`unavailable`、`unsupported`、`unchanged` 可以省略 metadata，减少普通结果噪声。
- `failed` 不设置顶层 `metadata["error"]`。
- 格式化成功时，输出追加一句 `LSP formatting applied.`。
- 格式化失败时，输出追加一句 `LSP formatting skipped: <reason>.`。
- 失败原因应去除 traceback、命令行和服务器 stderr 等可能冗长或敏感的信息。

## 安全与并发

- 自动格式化复用原文件工具已经批准的同一目标路径，不扩大授权范围。
- 自动格式化只能应用 `textDocument/rangeFormatting` 返回且位于请求范围内的当前文档 `TextEdit[]`。
- 服务器返回任一越界 edit 时拒绝整批格式化结果，不做部分应用，也不回退到全文件格式化。
- 不处理服务器发起的 `workspace/applyEdit`，也不写入其他 URI。
- 格式化落盘前应确认目标路径仍是原先解析并授权的路径。
- 若安全写入阶段检测到路径替换、符号链接变化或其他 TOCTOU 风险，应保留已完成的原编辑并将格式化标记为失败。
- 同一 `LspManager` 已维护文档版本；自动格式化继续通过 `open_document()` 发送必要的 `didChange`。

## 涉及文件

预计修改：

- `src/voidx/lsp/manager.py`：增加无写入的范围格式化计算接口并检查服务器能力。
- `src/voidx/lsp/service.py`：暴露无写入范围格式化结果。
- `src/voidx/lsp/errors.py`：如采用异常表达，增加不支持范围格式化的明确错误类型。
- `src/voidx/tools/lsp.py`：为 `LspFormatInput` 增加必填范围，改用共享 service 和安全写入。
- `src/voidx/tools/registry.py`：注册改造后的 `LspFormatTool`。
- `src/voidx/tools/file/post_edit.py`：新增编辑后自动范围格式化策略与结果模型。
- `src/voidx/tools/file/io.py`：集中 `lsp_format`、`write` 和 `replace` 共用的安全文本读写辅助函数。
- `src/voidx/tools/file/write.py`：在最终结果生成前接入收尾流程。
- `src/voidx/tools/file/replace.py`：在普通编辑和自动创建路径接入收尾流程。
- `src/voidx/tools/base.py`：在 `ToolContext` 增加自动格式化开关。
- `src/voidx/config/settings_lsp.py`：增加 LSP workspace setting。
- `src/voidx/config/settings.py`：注册 mixin 和 workspace-only 配置键。
- `src/voidx/config/models.py`：如子代理继续通过 `Config` 传递设置，在运行配置中增加对应字段。
- `src/voidx/agent/graph/tool_executor/executor.py`：主代理构造 `ToolContext` 时注入开关。
- `src/voidx/agent/graph/subagent.py`：子代理构造 `ToolContext` 时传递同一开关。
- `src/voidx/agent/loop/slash.py`：核对 slash 工具上下文是否已从公共 kwargs 继承该字段。
- `src/tests/test_lsp/`：覆盖范围请求、显式工具和无写入 service。
- `src/tests/test_tools/test_tool_registry.py`：覆盖 `lsp_format` 注册。
- `src/tests/test_tools/test_tool_schemas.py`：覆盖范围参数 schema。
- `src/tests/test_tools/file/`：覆盖编辑与范围格式化组合行为。
- `src/tests/test_config/`：覆盖默认值、持久化和 workspace scope。

实施时必须同时覆盖主代理与子代理上下文；不能仅修改主图执行器，否则相同文件工具会因执行入口不同而产生不一致行为。

## 测试方案

### LSP 单元测试

1. 支持 `documentRangeFormattingProvider` 时发送 `textDocument/rangeFormatting`，携带准确范围并返回格式化文本，但不写磁盘。
2. provider 为对象时视为支持。
3. range capability 缺失或为 `False` 时不发送任何格式化请求并返回 unsupported，即使服务器支持 `documentFormattingProvider`。
4. 空 edits、`None` 或等价内容返回 unchanged。
5. 返回范围内的多个 edits 时正确应用。
6. 任一 edit 越出请求范围时拒绝整批结果。
7. 非法或重叠 edits 延续 `apply_text_edits()` 的既有错误语义。

### 文件工具测试

分别覆盖 `write` 和 `replace` 的关键路径：

1. 插入、替换和 append 分别计算准确的编辑后范围。
2. 多处编辑合并为最小包络范围且只发送一次请求。
3. 纯删除在中间、EOF 和清空文件时分别得到正确锚点或跳过结果。
4. 最后一行无换行符且包含非 BMP Unicode 时使用 UTF-16 character offset。
5. 范围格式化改变内容，磁盘和 diff 都包含最终格式化结果。
6. 范围格式化未改变内容，保留现有编辑 diff 和覆盖重映射。
7. 无 LSP 或不支持范围格式化时，编辑正常成功、无警告且不调用全文件格式化。
8. LSP 超时、协议错误、越界 edits 或服务器异常时，编辑保留，工具不返回 error，并包含失败 metadata。
9. 格式化后的最终 diff 正确重映射 read coverage 和 line drift maps。
10. 一次调用只保存一个编辑前版本。
11. 最终 mtime 与磁盘内容一致。
12. 安全写入失败时不使用裸路径写入作为回退。
13. 自动创建文本文件后可范围格式化；空内容和无后缀文件不产生无意义调用。
14. 配置关闭时不调用 LSP。

### 配置测试

1. 未配置时默认开启。
2. workspace 设置为 `false` 后正确持久化和读取。
3. `lsp` 不迁移到全局设置。
4. 非布尔或畸形配置安全回退到默认值。

## 实施顺序

1. 为 `formatted_range_text()` 编写失败测试并扩展 `LspManager` / `LspService`，完成 capability、range request 和越界 edits 校验。
2. 为 `LspFormatInput` 编写 schema 与校验测试，加入必填起止位置。
3. 改造 `LspFormatTool` 的授权、安全落盘、diff 和状态维护，并注册到 `ToolRegistry`。
4. 运行显式 `lsp_format` 的 LSP、schema、registry、权限和版本快照测试，确认该基础能力独立可用。
5. 为自动格式化范围计算补充插入、替换、多处编辑、纯删除、EOF 和 UTF-16 边界测试。
6. 提取文件工具共享安全 I/O，确认现有 write/replace 和 `lsp_format` 测试保持通过。
7. 为 `post_edit` 编写 unavailable、unsupported、formatted、越界 edits 和 failed 测试。
8. 接入 `write`，验证请求范围、最终 diff、快照和状态。
9. 接入 `replace` 及自动创建路径。
10. 增加 Settings 和 `ToolContext` 注入。
11. 运行 LSP、文件工具和配置 focused tests，再运行完整 backend suite。

## 验收标准

- `lsp_format` 已注册，范围参数必填，能独立完成授权、范围请求、安全落盘、diff 和状态更新。
- `lsp_format` 不提供或接受隐式全文件模式。
- 默认配置下，仅当服务器声明 `documentRangeFormattingProvider` 时，`write` 或 `replace` 才发送 `textDocument/rangeFormatting`。
- 请求范围是本次实际编辑在新文档中的最小影响范围；多处编辑合并为一个最小包络范围。
- 不支持范围格式化时直接跳过，即使服务器支持 `documentFormattingProvider` 也不得调用全文件格式化。
- 服务器返回越出请求范围的 edit 时拒绝整批格式化结果，并保留原编辑内容。
- 返回 diff 与磁盘最终内容一致。
- 没有 LSP、不支持范围格式化或范围格式化失败时，原编辑不会丢失，也不会被标记为工具失败。
- 自动格式化路径没有任何裸 `Path.write_text()`。
- 每次编辑只产生一个编辑前版本快照。
- 格式化后的最终 diff 正确维护读取覆盖和行号漂移状态。
- 配置可在 workspace 中关闭，默认值为开启。
- LSP、文件工具、配置 focused tests 和 backend suite 全部通过。

## 风险与取舍

### 编辑延迟增加

默认开启意味着首次编辑某种语言时可能需要启动语言服务器。不可用服务器应快速退化；请求仍受现有 LSP timeout 约束。若实际体验受影响，可在后续增加独立的格式化短超时或仅对已启动服务器自动格式化。

### 两阶段内部写入

第一版为了复用现有磁盘驱动的文档同步，会先写编辑内容再写范围格式化内容。优点是改动局部、协议状态清晰；代价是文件观察者可能看到两次变化。未来可以给 LSP manager 增加显式内存文档同步，将最终结果一次落盘。

### 范围格式化的语言差异

不同语言服务器对完整行范围的扩展方式可能不同。设计通过 capability 检查、最小影响范围和返回 edit 越界校验限制改动规模；不支持或行为异常时保留原编辑，绝不回退到全文件格式化。

### 多处编辑的包络范围

一次工具调用包含相距较远的多处编辑时，最小包络范围会包含中间未编辑代码。第一版接受这一取舍，以避免连续 range formatting 请求造成坐标漂移。若实践中仍产生过大 diff，可后续设计按变更块倒序请求并逐次同步文档版本。

### 默认开启的兼容性

这是有意的行为变化。安全退化、workspace 关闭开关和失败不阻断规则用于降低风险。发布说明应明确仅在服务器支持范围格式化时自动执行，以及关闭方法。
