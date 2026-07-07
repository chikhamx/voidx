# Document Tool 改造：支持 usage-guide

Date: 2026-07-06

> **Status: Spec** — 等待实现。

## Goal

复用现有 `document` 工具（`LoadDocTemplateTool`），新增 `option` 参数。无参数时返回所有可选文档类型列表（模板类型 + usage-guide 类型），传入 `doc_type` 时返回对应文档内容。usage-guide 按章节分类返回，而非全文。

## Background

voidx 作为分发给用户的独立产品，部署在用户环境。agent 需要能回答"voidx 怎么用""有哪些命令"等问题，但：

- 用户环境没有项目 AGENTS.md，不能靠指令文件注入
- usage-guide 全文约 280 行，一次性返回浪费 token
- 现有 `document` 工具只支持文档模板（prd/rfc/readme 等），不支持 usage-guide

`document` 工具已有 `importlib.resources.files("voidx.data")` 读取打包资源的现成模式，改造比新建工具更自然。

## Current State

`src/voidx/tools/load_doc_template.py`：

- id = `"document"`
- 参数：`doc_type: str`，枚举 `("prd", "tech-design", "rfc", "api-doc", "readme")`
- 读取 `voidx/data/templates/{doc_type}.md`
- 无 `option` 参数，agent 必须知道 doc_type 才能调用

## Design

### 参数变更

```
LoadDocTemplateInput:
    doc_type: str | None    # 可选。文档类型，传入时返回对应内容
    option: str | None      # 可选。传 "list" 或不传时返回所有可选类型
```

两个参数都可选。行为：

| 调用方式 | 返回 |
|----------|------|
| `document(option="list")` 或 `document()` | 所有可选文档类型列表（模板 + guide） |
| `document(doc_type="prd")` | prd 模板内容（现有行为不变） |
| `document(doc_type="usage-guide-quickstart")` | usage-guide 的"快速上手"章节 |

### doc_type 枚举

原有模板类型不变，新增 usage-guide 类型：

**模板类型**（读 `voidx/data/templates/{type}.md`）：
`prd` / `tech-design` / `rfc` / `api-doc` / `readme`

**usage-guide 类型**（读 `voidx/data/usage-guide.md`，按 `##` 章节切分）：

| doc_type | 章节 | 覆盖内容 |
|----------|------|----------|
| `usage-guide-quickstart` | 快速上手 | 启动方式、运行形态、CLI 参数 |
| `usage-guide-session` | 会话管理 | 新建/恢复/删除/回滚/标题 |
| `usage-guide-model` | 模型与 Provider | 切换/创建/测试/删除 profile |
| `usage-guide-mode` | 交互模式 | auto/plan/goal |
| `usage-guide-permission` | 权限与沙箱 | permission-mode、sandbox、allow/deny |
| `usage-guide-workflow` | 工作流 | 8 个内置节点、guide/parallel |
| `usage-guide-extension` | 扩展能力 | MCP、LSP、技能系统 |
| `usage-guide-context` | 上下文管理 | compact、usage、diff |
| `usage-guide-preferences` | 用户偏好 | lang、tone、init、code-ide |
| `usage-guide-web` | Web 搜索 | Tavily key 配置 |
| `usage-guide-debug` | 调试与日志 | debug、log 开关 |
| `usage-guide-upgrade` | 升级 | check、now、on/off |
| `usage-guide-reference` | 命令速查 | 全部命令速查表 |

usage-guide 类型统一前缀 `usage-guide-`，便于识别和路由。

### option="list" 返回格式

```
Available document types:

Templates:
  prd              — Product Requirements Document
  tech-design      — Technical Design Doc
  rfc              — Request for Comments
  api-doc          — API Documentation
  readme           — README / Usage Guide

Usage Guide:
  usage-guide-quickstart    — 启动方式、运行形态、CLI 参数
  usage-guide-session       — 会话管理
  usage-guide-model         — 模型与 Provider
  usage-guide-mode          — 交互模式
  usage-guide-permission    — 权限与沙箱
  usage-guide-workflow      — 工作流
  usage-guide-extension     — 扩展能力（MCP/LSP/技能）
  usage-guide-context       — 上下文管理
  usage-guide-preferences   — 用户偏好
  usage-guide-web           — Web 搜索
  usage-guide-debug         — 调试与日志
  usage-guide-upgrade       — 升级
  usage-guide-reference     — 命令速查

Use document(doc_type="<type>") to load the content.
```

### 解析逻辑

`doc_type` 以 `usage-guide-` 开头时：

1. `importlib.resources.files("voidx.data").joinpath("usage-guide.md").read_text()`
2. 按 `## ` 切分章节
3. 映射 `usage-guide-{suffix}` → 章节标题，返回该标题到下一个 `## `（或文件末尾）的内容

映射表：

```python
_GUIDE_SECTIONS = {
    "quickstart": "快速上手",
    "session": "会话管理",
    "model": "模型与 Provider",
    "mode": "交互模式",
    "permission": "权限与沙箱",
    "workflow": "工作流",
    "extension": "扩展能力",
    "context": "上下文管理",
    "preferences": "用户偏好",
    "web": "Web 搜索",
    "debug": "调试与日志",
    "upgrade": "升级",
    "reference": "命令速查",
}
```

`doc_type` 不以 `usage-guide-` 开头时：走现有模板逻辑不变。

### description 更新

```
Load a document template or usage guide section by type.
Call with option="list" to see all available types.
Use when writing structured docs (design docs, RFCs, PRDs, API docs, READMEs)
or when the user asks about voidx features, commands, or usage.
```

## File Structure

| 文件 | 改动 |
|------|------|
| `src/voidx/tools/load_doc_template.py` | 新增 `option` 参数、usage-guide 类型支持、`_GUIDE_SECTIONS` 映射、章节切分逻辑 |
| `src/voidx/data/usage-guide.md` | 新增：打包的使用指南资源 |
| `pyproject.toml` | package-data 的 `"voidx.data"` 加 `"usage-guide.md"` |
| `docs/usage-guide.md` | 源文件（维护入口），构建时复制到 `src/voidx/data/` |
| `src/tests/test_tools/test_load_doc_template.py` | 新增 usage-guide 和 option 参数的测试 |

## Tests

| 测试 | 验证点 |
|------|--------|
| `test_option_list` | `option="list"` 返回所有类型，含模板和 guide |
| `test_no_args` | 无参数等价 `option="list"` |
| `test_usage_guide_section` | `doc_type="usage-guide-quickstart"` 返回"快速上手"章节内容 |
| `test_usage_guide_boundary` | 返回内容从 `## 标题` 到下一个 `## `，不含下一节 |
| `test_usage_guide_invalid` | `doc_type="usage-guide-xxx"` 返回错误并列出合法值 |
| `test_template_unchanged` | `doc_type="prd"` 行为不变 |
| `test_all_guide_types_mapped` | 遍历所有 `usage-guide-*`，每个都能找到对应章节 |

## Risks

- **usage-guide 维护两份**：`docs/` 和 `src/voidx/data/` 可能不同步。缓解：构建脚本加复制步骤，或直接在 `src/voidx/data/` 下维护。
- **章节标题改名**：usage-guide 的 `##` 标题改了，映射表要同步。缓解：`test_all_guide_types_mapped` 会捕获。
- **打包遗漏**：pyproject.toml 配置错误导致 usage-guide.md 没打进 wheel。缓解：构建后验证 `importlib.resources.files("voidx.data").joinpath("usage-guide.md").exists()`。
- **doc_type 命名冲突**：未来新增模板类型可能与 `usage-guide-` 前缀冲突。缓解：`usage-guide-` 作为保留前缀。
