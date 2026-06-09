> **Status: Done**

# RFC: 移除 load_doc_template 工具

## Proposal

移除 `load_doc_template` 工具及其测试，将 SKILL.md 中的模板加载方式改为直接 `read` 模板文件。

## Motivation

1. **无 agent 使用** — 5 个 agent 的 tools 列表都没有 `load_doc_template`，注册了但没人能调用
2. **模板路径可直达** — 将模板文件移至顶层 `templates/` 目录，SKILL.md 中写明具体路径，LLM 直接 `read` 即可
3. **多一层间接** — 调工具读文件 vs 直接 read，效果一样但多占工具槽、多一轮调用
4. **项目级/全局级覆盖几乎无人使用** — 唯一差异功能，且 read 也能实现同样的查找逻辑

## Approach

### 删除文件

| 文件 | 说明 |
|------|------|
| `src/voidx/tools/doc_template.py` | 工具实现 |
| `tests/test_tools/test_doc_template.py` | 工具测试 |

### 移动文件

| 原路径 | 新路径 | 说明 |
|--------|--------|------|
| `src/voidx/skills/bundled/superpowers/writing-design-docs/templates/prd.md` | `templates/prd.md` | PRD 模板 |
| `src/voidx/skills/bundled/superpowers/writing-design-docs/templates/tech-design.md` | `templates/tech-design.md` | 技术设计模板 |
| `src/voidx/skills/bundled/superpowers/writing-design-docs/templates/rfc.md` | `templates/rfc.md` | RFC 模板 |
| `src/voidx/skills/bundled/superpowers/writing-design-docs/templates/api-doc.md` | `templates/api-doc.md` | API 文档模板 |
| `src/voidx/skills/bundled/superpowers/writing-design-docs/templates/readme.md` | `templates/readme.md` | README 模板 |

移动后删除原 `src/voidx/skills/bundled/superpowers/writing-design-docs/templates/` 目录。

### 修改文件

**`src/voidx/tools/registry.py`**

- 移除 `from voidx.tools.doc_template import LoadDocTemplateTool`（第 22 行）
- 移除 `_register_builtins` 中 `LoadDocTemplateTool` 的注册（第 57 行）

**`src/voidx/skills/bundled/superpowers/writing-design-docs/SKILL.md`**

- 第 64 行：将 `call \`load_doc_template\` with the appropriate \`doc_type\`` 改为 `read the template file at \`templates/{doc_type}.md\` (workspace root)`
- 文档类型表中 `doc_type` 列的值与 `templates/` 下的文件名一一对应，LLM 可直接拼接路径

### 不改的

- 模板文件移至顶层 `templates/` 目录，SKILL.md 中写明 `templates/{doc_type}.md` 路径，LLM 用 `read` 读取

## Alternatives

| 方案 | 描述 | 不选的理由 |
|------|------|----------|
| 保留工具，分配给 orchestrator | 让 orchestrator 能调用 | 仍然多一层间接，read 已够用 |
| 保留工具，加项目级覆盖引导 | 在 SKILL.md 里引导 LLM 先查覆盖再调工具 | 覆盖功能几乎无人用，YAGNI |

## Impact

- 移除一个注册但未使用的工具，减少工具列表噪音
- 模板加载从工具调用变为 `read`，对 LLM 来说更直接
- 移除 `tools/` 与 `skills/` 之间的隐式耦合（`_TEMPLATES_DIR` 路径），模板文件变为纯数据，仅由 LLM 通过 `read` 消费
- 项目级/全局级模板覆盖功能丢失（当前无人使用，需要时可在 SKILL.md 中引导 LLM 自行查找）

## Open Questions

- [ ] 项目级/全局级模板覆盖是否需要在 SKILL.md 中补回引导逻辑？
