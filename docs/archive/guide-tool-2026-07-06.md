# Document Tool 改造：内置知识库

Date: 2026-07-06

> **Status: Done**。

## Goal

将 `document` 工具从"文档模板加载器"改造成"voidx 内置知识库读取器"。工具不再接收 `doc_type`，而是通过 `action` 和 `path` 支持目录索引与文件读取：

- `list`：读取某个知识库目录下的 `README.md`，返回该目录的文档索引和使用提示
- `read`：读取某个明确的 Markdown 文档文件

本次改造**不支持旧逻辑兼容**：`doc_type="prd"`、`doc_type="usage-guide-quickstart"` 等旧式调用全部移除。

## Background

voidx 作为分发给用户的独立产品，agent 需要能回答“voidx 怎么用”“有哪些命令”“怎么写 PRD/RFC”等问题，但现有 `document` 工具只支持固定模板枚举：

- 必须事先知道 `doc_type`
- 模板分类写死在代码里，不利于扩展
- usage guide 如果按单文件章节切分，需要在工具逻辑里维护章节映射
- 后续新增内置知识类型时容易继续堆特殊分支

更好的抽象是：`document` 表示 voidx 自带的只读知识库；`templates/`、`voidx-guide/` 只是知识库下的不同目录。

## Current State

`src/voidx/tools/document.py`（类 `DocumentTool`，旧名 `LoadDocTemplateTool`）：

- id = `"document"`
- 参数：`doc_type: str`（必填）
- 有固定枚举：`("prd", "tech-design", "rfc", "api-doc", "readme")`
- 读取路径：`voidx/data/templates/{doc_type}.md`
- 不支持 list，也不支持读取任意内置文档路径

## Design

### 新工具语义

`document` 是只读内置知识库工具。

知识库根目录：

```text
src/voidx/data/documents/
```

目录结构：

```text
src/voidx/data/documents/
  README.md
  templates/
    README.md
    prd.md
    tech-design.md
    rfc.md
    api-doc.md
    readme.md
  voidx-guide/
    README.md
    quickstart.md
    session.md
    model.md
    mode.md
    permission.md
    workflow.md
    extension.md
    context.md
    preferences.md
    web.md
    debug.md
    upgrade.md
    reference.md
```

### 参数变更

```python
class DocumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["list", "read"]
    path: str | None = None
```

命名说明：`action` 表示工具调用动作；不使用 `type`，避免和文档类型、模板类型混淆。

行为：

| 调用方式 | 返回 |
|----------|------|
| `document(action="list")` | `documents/README.md` |
| `document(action="list", path="templates")` | `documents/templates/README.md` |
| `document(action="list", path="voidx-guide")` | `documents/voidx-guide/README.md` |
| `document(action="read", path="templates/prd.md")` | PRD 模板全文 |
| `document(action="read", path="voidx-guide/quickstart.md")` | 快速上手指南全文 |

### `list` 行为

`list` 不扫描目录动态生成列表，而是读取目标目录下的 `README.md`。

原因：

- README 可以包含比文件名更适合 LLM 的说明、关键词和调用建议
- README 是目录索引的单一维护入口
- 新增文档时只需要新增 `.md` 文件并更新同目录 `README.md`

README 应作为 LLM 路由索引，而不是普通文件清单。建议每条文档至少包含：

- `path`：可直接传给 `read` 的路径
- `use_when`：什么时候应该读取该文档
- `keywords`：LLM 可匹配的同义词、命令名、触发词

规则：

- `path` 为空：读取 `documents/README.md`
- `path` 为目录：读取 `documents/{path}/README.md`
- `path` 指向 `.md` 文件：返回错误，提示 `list` 只能用于目录
- `path` 指向不存在的目录：返回错误，提示 `document(action="list")` 查看可用的目录
- 目录存在但下没有 `README.md`：返回错误，提示可用上级目录索引

### `read` 行为

`read` 只读取明确的 Markdown 文件。

规则：

- `path` 必填
- `path` 必须以 `.md` 结尾
- `path` 不能是目录
- 读取 `documents/{path}` 的内容
- 找不到文件时返回错误，并提示先调用 `document(action="list")` 或 `document(action="list", path="<dir>")`
- 返回值 `metadata` 中附带 `action`、`path`、`kind`、`directory` 字段，便于 agent 追踪内容来源

### 路径安全

所有路径必须限制在 `src/voidx/data/documents/` 打包资源内。

校验规则：

- 使用 `pathlib.PurePosixPath` 做 POSIX 相对路径解析
- 禁止绝对路径
- 禁止空 segment、`.`、`..`
- 禁止空路径用于 `read`
- 禁止反斜杠路径分隔符，统一使用 `/`
- 禁止读取非 `.md` 文件
- `list` 只读取目录下的 `README.md`
- `read` 只读取显式 `.md` 文件

错误示例：

| 输入 | 结果 |
|------|------|
| `document(action="read")` | 错误：`read` requires `path` |
| `document(action="read", path="../secret.md")` | 错误：invalid path |
| `document(action="read", path="/tmp/a.md")` | 错误：invalid path |
| `document(action="read", path="voidx-guide")` | 错误：`read` requires a `.md` file path |
| `document(action="list", path="voidx-guide/quickstart.md")` | 错误：`list` requires a directory path |

## Document Content

### 根索引：`documents/README.md`

根 README 是知识库入口，说明有哪些目录、各目录用途和下一步调用方式。

建议内容结构：

```md
# voidx Built-in Documents

Use `document(action="list", path="<directory>")` to inspect a directory.
Use `document(action="read", path="<file>.md")` to load a document.
Start with this root index when unsure which built-in document to read.

## Directories

| path | use_when | keywords |
|------|----------|----------|
| `templates/` | 需要编写 PRD、技术设计、RFC、API 文档或 README 时查看。 | template, PRD, RFC, technical design, API doc, README |
| `voidx-guide/` | 用户询问 voidx 用法、命令、模式、权限、工作流、调试或扩展能力时查看。 | usage, guide, command, mode, workflow, MCP, debug |
```

### 模板索引：`documents/templates/README.md`

模板 README 记录每个模板文件的用途和关键词。

```md
# Document Templates

| path | use_when | keywords |
|------|----------|----------|
| `templates/prd.md` | 编写产品需求、功能规格、用户故事和验收标准时读取。 | PRD, product requirements, requirements, user story |
| `templates/tech-design.md` | 编写实现方案、架构设计、模块拆分和测试计划时读取。 | technical design, architecture, implementation plan |
| `templates/rfc.md` | 编写方案评审、取舍分析、决策记录或征求意见稿时读取。 | RFC, proposal, decision, trade-off |
| `templates/api-doc.md` | 编写接口、请求响应、错误码和示例调用文档时读取。 | API, endpoint, request, response, schema |
| `templates/readme.md` | 编写项目说明、安装步骤、使用方式和开发指南时读取。 | README, quickstart, install, usage |
```

### 使用指南索引：`documents/voidx-guide/README.md`

`usage-guide.md` 拆成多个文件，每个二级章节一个文件。README 记录文件说明和关键词。

```md
# voidx Usage Guide

| path | use_when | keywords |
|------|----------|----------|
| `voidx-guide/quickstart.md` | 用户第一次使用 voidx，询问启动方式、运行形态或 CLI 参数时读取。 | quickstart, start, CLI, install, run |
| `voidx-guide/session.md` | 用户询问会话管理、恢复、清理、回滚或标题时读取。 | /clear, /session, /resume, /rollback, /title |
| `voidx-guide/model.md` | 用户询问模型、Provider、profile、reasoning 或上下文窗口时读取。 | /model, provider, profile, reasoning, context window |
| `voidx-guide/mode.md` | 用户询问 auto、plan、goal 等交互模式差异时读取。 | auto mode, plan mode, goal mode, interaction |
| `voidx-guide/permission.md` | 用户询问权限、沙箱、审批、allow/deny 或 permission-mode 时读取。 | permission, sandbox, approval, allow, deny |
| `voidx-guide/workflow.md` | 用户询问 brainstorm、design、plan、tdd、verify、review、feedback、debug 工作流时读取。 | workflow, TDD, verify, review, debug |
| `voidx-guide/extension.md` | 用户询问 MCP、LSP、skills 或扩展能力时读取。 | MCP, LSP, skills, extension |
| `voidx-guide/context.md` | 用户询问上下文压缩、usage、diff 或上下文管理时读取。 | compact, usage, diff, context |
| `voidx-guide/preferences.md` | 用户询问语言、语气、初始化或 IDE 偏好时读取。 | lang, tone, init, code-ide, preferences |
| `voidx-guide/web.md` | 用户询问 Web 搜索或 Tavily 配置时读取。 | web search, Tavily, search |
| `voidx-guide/debug.md` | 用户询问调试命令、日志或排错方式时读取。 | debug, log, troubleshooting |
| `voidx-guide/upgrade.md` | 用户询问升级检查、自动升级或关闭升级提示时读取。 | upgrade, update, check |
| `voidx-guide/reference.md` | 用户需要命令速查或完整命令列表时读取。 | reference, command list, cheat sheet |
```

## File Structure

| 文件 | 改动 |
|------|------|
| `src/voidx/tools/document.py` | 重写：`LoadDocTemplateTool` → `DocumentTool`，`LoadDocTemplateInput` → `DocumentInput`；参数 `doc_type` → `action`、`path`；支持 `list`/`read` |
| `src/voidx/data/documents/README.md` | 新增：内置知识库根索引 |
| `src/voidx/data/documents/templates/*.md` | 从 `src/voidx/data/templates/*.md` **迁移**（git mv），删除原 templates/ 目录 |
| `src/voidx/data/documents/templates/README.md` | 新增：模板索引 |
| `src/voidx/data/documents/voidx-guide/*.md` | 新增：由 `docs/usage-guide.md` 按章节拆分后的使用指南 |
| `src/voidx/data/documents/voidx-guide/README.md` | 新增：使用指南索引 |
| `pyproject.toml` | package-data 改为 `"documents/**/*.md"`，删除旧的 `"templates/*.md"` |
| `src/voidx/workflow/nodes.py` | `WRITING_DESIGN_DOCS` IO schema 中 `doc_type` 改为 `action` |
| `src/voidx/tools/registry.py` | 更新 import：`LoadDocTemplateTool` → `DocumentTool` |
| `src/tests/test_tools/test_load_doc_template.py` | 改名或重写为 document 知识库工具测试 |
| `src/tests/test_tools/test_*.py`（其余 17 个文件） | 更新 import：`LoadDocTemplateTool` → `DocumentTool`，`LoadDocTemplateInput` → `DocumentInput` |
| `docs/usage-guide.md` | 保留为用户可读完整指南；本次实现先以该文件为源拆分到 `documents/voidx-guide/*.md`，后续可补生成脚本避免长期双维护 |

## Implementation Plan

1. **资源目录迁移**：新建 `src/voidx/data/documents/`，用 `git mv` 将 5 个模板移入 `documents/templates/`，新增根 README 和模板 README。
2. **指南拆分**：按 `docs/usage-guide.md` 的二级章节生成 `documents/voidx-guide/*.md`，并补齐 `voidx-guide/README.md` 的 `path / use_when / keywords` 路由表。
3. **工具重写**：在 `src/voidx/tools/document.py` 中将输入模型改为 `DocumentInput(action, path)`，工具类改为 `DocumentTool`，移除旧枚举和旧类名。
4. **路径校验**：在读取资源前统一调用 `_clean_relative_path()`；`list` 只拼接目录 `README.md`，`read` 只接受显式 `.md` 文件。
5. **调用方迁移**：更新 registry、workflow node schema 和所有测试 import，不保留 `LoadDocTemplateTool/Input` 别名。
6. **测试收口**：重写 document 工具测试，增加 package-data、README 引用完整性、旧字段拒绝和路径逃逸覆盖。
7. **验证命令**：先跑 `./test.py --backend -- src/tests/test_tools/test_load_doc_template.py src/tests/test_tools/test_tool_registry.py`，再跑 `./test.py --backend -- src/tests/test_tools`。

## Implementation Notes

### 类命名

旧类名 `LoadDocTemplateTool` / `LoadDocTemplateInput` 不再准确，改为：

```python
class DocumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["list", "read"] = Field(...)
    path: str | None = Field(default=None)

class DocumentTool(BaseTool):
    id = "document"
```

⚠ 直接在 `document.py` 中完成改名，**不保留** `LoadDocTemplateTool` / `LoadDocTemplateInput` 向后兼容别名。

### 工具描述

```text
Read voidx built-in documents only.
Use action="list" to read a directory README index.
Use action="read" with a Markdown path to load a specific document.
This tool does not read user files, generate documents, or search external sources.
Start with document(action="list") when unsure what is available.
```

### 资源读取

```python
from pathlib import PurePosixPath

_DOCUMENTS_PACKAGE = "voidx.data"
_DOCUMENTS_ROOT = "documents"


def _clean_relative_path(path: str) -> str:
    if not path or path.startswith("/") or "\\" in path:
        raise ValueError("invalid path")
    rel = PurePosixPath(path)
    if rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts):
        raise ValueError("invalid path")
    return rel.as_posix()


safe_path = _clean_relative_path(inp.path)
ref = importlib.resources.files(_DOCUMENTS_PACKAGE).joinpath(_DOCUMENTS_ROOT, safe_path)
content = ref.read_text(encoding="utf-8")
```

`safe_path` 必须先通过路径校验，不允许用户输入逃逸资源根目录。

## Tests

| 测试 | 验证点 |
|------|--------|
| `test_list_root` | `document(action="list")` 返回根 `README.md`，包含 `templates/` 和 `voidx-guide/` |
| `test_list_directory` | `document(action="list", path="voidx-guide")` 返回 guide 索引 |
| `test_list_rejects_file_path` | `list` 传 `.md` 文件路径返回错误 |
| `test_read_template` | `document(action="read", path="templates/prd.md")` 返回 PRD 模板 |
| `test_read_guide_section` | `document(action="read", path="voidx-guide/quickstart.md")` 返回快速上手指南 |
| `test_read_requires_path` | `read` 不传 path 返回错误 |
| `test_read_requires_markdown_file` | `read` 传目录或非 `.md` 返回错误 |
| `test_reject_absolute_path` | 禁止绝对路径 |
| `test_reject_parent_traversal` | 禁止 `..` |
| `test_reject_backslash` | 禁止反斜杠路径 |
| `test_missing_file_points_to_list` | 文件不存在时提示先 `list` |
| `test_schema_no_doc_type` | 参数 schema 不再包含 `doc_type`，包含 `action` 和 `path` |
| `test_schema_forbids_extra_fields` | schema 禁止额外字段，避免旧字段被静默忽略 |
| `test_old_doc_type_not_supported` | `document(doc_type="prd")` 返回参数错误；`document(action="read", doc_type="prd")` 也因额外字段失败，不做兼容映射 |
| `test_package_data_contains_documents` | 构建/资源层面能读取 `documents/README.md` 和代表性子文档 |
| `test_all_docs_referenced_in_readme` | 扫描 `documents/` 下所有 `.md`，确保每个非 README 文件在同级 README 中被引用 |

## Migration

本设计不保留旧接口兼容。

### 删除

- `_VALID_DOC_TYPES`
- `_SUBDIR = "templates"`
- `LoadDocTemplateInput.doc_type`
- `LoadDocTemplateInput` / `LoadDocTemplateTool` 类名
- `doc_type` 枚举校验
- `doc_type="prd"` 到模板文件的隐式映射
- `usage-guide-*` 类型映射表
- 运行时按 `##` 切分 `usage-guide.md` 的逻辑
- `pyproject.toml` 中 `templates/*.md` 的 package-data 条目

### 迁移为

- `document(action="list")`
- `document(action="list", path="templates")`
- `document(action="read", path="templates/prd.md")`
- `document(action="list", path="voidx-guide")`
- `document(action="read", path="voidx-guide/quickstart.md")`

### 需要更新 import 的文件

`LoadDocTemplateTool` → `DocumentTool`，`LoadDocTemplateInput` → `DocumentInput`（无别名）：

- `src/voidx/tools/registry.py`
- 下列 17 个测试文件：
  `test_clarify_tool.py`, `test_user_interaction_models.py`, `test_task_tracker.py`,
  `test_make_interact_callback.py`, `test_tool_state_patch.py`, `test_interactive_tools.py`,
  `test_search.py`, `test_tool_error_handling.py`, `test_workflow_tool.py`,
  `test_tool_registry.py`, `test_state_update_from_executed_tools.py`, `test_plan_checkpoint.py`,
  `bash/test_tool.py`, `test_infer_state_patch.py`, `test_tool_schemas.py`,
  `test_interactive_tools_write.py`, `test_load_doc_template.py`

### 工作流节点更新

`src/voidx/workflow/nodes.py` 中 `WRITING_DESIGN_DOCS` 的 IO schema：

- `input.doc_type` → `input.action`
- `output.doc_type` → `output.action`

### 模板文件迁移

`src/voidx/data/templates/*.md` 中的 5 个文件（prd.md, tech-design.md, rfc.md, api-doc.md, readme.md）移到 `src/voidx/data/documents/templates/`，使用 `git mv` 保留历史。迁移后删除 `src/voidx/data/templates/`。

## Risks

- **破坏旧调用**：旧 `doc_type` 调用会失败。这里是有意设计，测试应明确覆盖"不支持旧逻辑"。
- **README 与目录不同步**：新增文档后忘记更新 README。缓解：测试可扫描 `documents/**.md`，确保每个非 README 文件在同目录 README 中被引用。
- **完整指南与拆分文档不同步**：`docs/usage-guide.md` 和 `documents/voidx-guide/*.md` 可能分叉。缓解：后续明确生成方向，建议以拆分文件为源生成完整指南，或以完整指南为源生成拆分文件，但不要长期手工双维护。
- **package-data glob 不生效**：`documents/**/*.md` 需要确认 setuptools 支持预期递归匹配。缓解：增加构建后资源读取验证。
- **路径逃逸风险**：用户传入 `../`、绝对路径或反斜杠。缓解：在 joinpath 前做严格字符串校验，并用测试覆盖。
- **目录层级扩展**：未来新增更深层目录时，README 索引模式仍可扩展，但 list/read 的路径校验必须允许安全的多级相对路径。
