# 技能引用与管理增强 — 技术设计文档

> **Status: Done**

## Context

当前 regular skill 系统已有完整的发现、匹配、渲染管线，但用户与技能的交互方式有限：

- **引用方式单一**：仅支持 `$skill-name` 纯文本引用，无 TUI 交互式选择
- **管理模式粗糙**：`/skills` 只支持 `list|show|enable|disable|paths`，缺少 auto/manual 模式
- **可见性不可控**：manual 技能应默认对 LLM 不可见，只有用户显式引用后才把 name + description 带入用户消息；auto 技能应只把描述固定注入 system prompt，让 LLM 自行决定是否调用 `load_skills`

本文只讨论 global/project `SKILL.md` 技能。当前系统已经没有内置 skill；内置工作流节点由 workflow runtime 管理，不属于本设计范围。

本设计在现有架构上增加两个核心能力：

1. **`#` 技能引用**：TUI 输入框中输入 `#` 弹出技能选择面板，选中后展开为 `$skill-name` 引用
2. **技能模式管理**：每个技能可设为 `auto`（仅描述固定注入 system prompt）或 `manual`（默认，需 `#`/`$` 引用才把 name + description 带入用户消息）

## Goals and Non-Goals

### Goals

- `#` 触发技能选择面板，交互方式与 `@` 附件面板一致（上下键 + 回车）
- `#` 后可继续输入做模糊过滤
- 选中后输入框展开为 `$skill-name`，走现有显式引用路径
- `/skills auto <name>` 将技能设为 auto 模式，描述固定注入 system prompt 的 `## Available Skills` 段落
- `/skills manual <name>` 将技能设为 manual 模式（默认），从 system prompt 的 `## Available Skills` 段落移除，仅 `#`/`$` 引用时把 name + description 带入用户消息
- auto 模式技能在 `## Available Skills` 段落中标注 `[auto]`
- 技能安装/卸载不新增 slash command，由用户直接和 voidx 对话完成

### Non-Goals

- 不改变现有 workflow runtime/policy 的自动激活逻辑
- 不把内置 workflow node 纳入 `#` 技能选择、`/skills` 管理或 `load_skills` 加载范围
- 不做技能市场/注册中心（install 仅支持本地路径和 URL）
- 不做技能版本管理
- Web UI / Desktop UI 的 `#` 弹窗不在本次范围（仅 TUI）

## Architecture

```
用户输入 "#brain..."
       │
       ▼
┌─────────────────────────┐
│  TUI: SkillToken 检测    │  新增 find_skill_token()，类似 find_attachment_token()
│  (panels.py / picker)   │  检测 # 前缀 + 模糊查询
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│  TUI: 技能选择面板       │  新增 _skill_panel_active / _skill_matches
│  (panels.py / overlays) │  渲染逻辑类似 _render_attachment_panel
└──────────┬──────────────┘
           │ 选中
           ▼
┌─────────────────────────┐
│  展开为 $skill-name      │  复用现有 _EXPLICIT_REF_RE 匹配路径
│  走消息包装流程          │  提取 name + description 写入用户消息
└─────────────────────────┘


/skills auto <name>
       │
       ▼
┌─────────────────────────┐
│  settings_skills.py     │  skills.json 新增 "auto" 列表
│  持久化 auto 模式        │
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│  instruction.py         │  available_skills_section() 中
│  auto 技能注入描述       │  仅 auto 技能标注 [auto]，manual 不注入
└─────────────────────────┘
```

## Data Model

### skills.json 扩展

现有结构：

```json
{
  "version": 1,
  "enabled": ["docs-helper"],
  "disabled": ["sql-review"]
}
```

新增 `auto` 字段：

```json
{
  "version": 2,
  "enabled": ["docs-helper"],
  "disabled": ["sql-review"],
  "auto": ["docs-helper"]
}
```

```
skills.json (v2)
├── version: int (2)
├── enabled: list[str]   — 显式启用的技能名
├── disabled: list[str]  — 显式禁用的技能名
└── auto: list[str]      — auto 模式的技能名（描述固定注入 system prompt）
```

### SkillSelectionConfig 扩展

```python
class SkillSelectionConfig(BaseModel):
    enabled: set[str] = Field(default_factory=set)
    disabled: set[str] = Field(default_factory=set)
    auto: set[str] = Field(default_factory=set)  # 新增
```

### SkillToken（新增）

```python
@dataclass(frozen=True)
class SkillToken:
    start: int    # # 的位置
    end: int      # 光标位置
    query: str    # # 后面的查询文本
```

### SkillCandidate（新增）

```python
@dataclass(frozen=True)
class SkillCandidate:
    name: str
    scope: str       # global / project
    description: str
    mode: str        # auto / manual
```

## API Contract

### find_skill_token(text, cursor) → SkillToken | None

- **Signature**: `find_skill_token(text: str, cursor: int) -> SkillToken | None`
- **Behavior**: 从光标位置向前查找 `#`，要求 `#` 前是行首或空白。返回 token 范围和查询文本。
- **Edge cases**: `#` 在单词中间不触发（如 `issue#123`）；`##` 不触发；`#` 后的 token 中若包含空白则不触发（如 `# heading`），避免 Markdown 标题输入时持续弹出面板；`#` 后紧跟光标（空 query）会触发面板，展示全部技能供选择

### list_skill_candidates(workspace, query, limit) → list[SkillCandidate]

- **Signature**: `list_skill_candidates(workspace: str, query: str, limit: int = 8) -> list[SkillCandidate]`
- **Behavior**: 从 SkillRegistry 获取所有 enabled global/project 技能（排除 bundled scope），按 query 模糊过滤 name 和 description，返回候选列表
- **Filtering**: query 为空时返回全部；非空时 name 前缀匹配优先，description 包含次之

### /skills 命令扩展

| 命令 | 行为 |
|------|------|
| `/skills` 或 `/skills list` | 列出所有技能，标注模式（auto/manual）和状态（enabled/disabled） |
| `/skills show <name>` | 显示技能详情（不变） |
| `/skills enable <name>` | 启用技能（不变） |
| `/skills disable <name>` | 禁用技能（不变） |
| `/skills auto <name>` | 将技能设为 auto 模式（描述固定注入 system prompt） |
| `/skills manual <name>` | 将技能设为 manual 模式（默认，需引用才在用户消息中带入说明） |
| `/skills paths` | 显示技能目录路径（不变） |

### Skill selection 行为

- `$skill-name` 显式引用会在当前 turn 把对应 enabled 技能的 name + description 带入用户消息，不注入 system prompt，也不自动注入 body
- 未显式引用时，不再通过 name、triggers 或 description 自动把 skill body 注入当前 turn
- auto 模式只影响 system prompt 可见性：auto 技能描述出现在 `## Available Skills` 中，LLM 可据此调用 `load_skills`
- manual 模式技能不出现在 `## Available Skills` 中，LLM 在用户未显式引用时看不到该技能

### 技能安装与卸载

不提供 `/skills install` / `/skills uninstall` 命令。用户直接与 voidx 对话表达意图：

- **安装/创建技能**："帮我创建一个代码审查技能" / "帮我安装一个技能，要求是..." → voidx 根据用户意图生成 SKILL.md 并保存到 `~/.voidx/skills/<name>/SKILL.md`
- **卸载/删除技能**："帮我删除 xxx 技能" / "我不需要这个技能了" → voidx 删除对应的 global/project 级技能目录
- **从 URL 安装**："帮我从这个 URL 安装技能 https://..." → voidx 下载并保存

voidx 通过文件读写工具完成操作，无需专用命令。若目标不是 global/project 级 `SKILL.md` 技能，voidx 应报错并说明没有可删除的本地技能目录。

**安全约束**：
- 删除技能前 voidx 应确认目标目录，避免误删
- 若技能同时存在于 global 和 project 级，voidx 应询问用户删除哪个（或两者都删）
- 安装时默认保存到 project 级（`.voidx/skills/`），用户指定 `--global` 时保存到 `~/.voidx/skills/`

### TUI 技能面板渲染

面板中每个候选项显示：名称、mode（auto 用绿色标注，manual 用 dim）、scope、description 摘要。

示例：
```
  ❯ docs-helper  [auto]  project — Helps write docs...
    sql-review  [manual]  global — Reviews SQL migrations...
```

### TUI 面板交互

| 按键 | 行为 |
|------|------|
| `#` | 在行首或空白后输入 `#`，弹出技能选择面板 |
| 继续输入 | `#` 后的文本作为模糊查询过滤技能列表 |
| ↑ / ↓ | 在候选列表中移动选中项 |
| Enter / Tab | 选中当前项，将 `#query` 替换为 `$skill-name ` |
| Esc | 关闭面板，保留 `#query` 文本 |

**与 `@` 附件面板的互斥**：`#` 技能面板和 `@` 附件面板不会同时激活。`find_skill_token` 和 `find_attachment_token` 的触发前缀不同（`#` vs `@`），同一时刻只有一个面板处于活跃状态。

### 多技能引用与消息包装

用户可在一次输入中引用多个技能，例如：`$docs-helper $sql-review 帮我分析这个迁移说明`。

发送给 LLM 时，将用户消息包装为带技能说明的自然语言前缀。

包装规则：
- 提取用户消息中所有 `$skill-name` 引用，按 enabled 技能解析出 name + description，并汇总到用户消息前缀
- 从用户消息正文中移除 `$skill-name` 标记，保留纯文本
- 若无 `$` 引用，不添加前缀，原样发送
- auto 模式不会因为自身 mode 在此处重复列出；若用户显式写了 `$auto-skill`，仍按显式引用包装
- 显式引用不会把 skill body 注入 system prompt；若 LLM 需要完整正文，应继续调用 `load_skills`

示例：
```
用户指定了技能：
- docs-helper: Helps write docs...
- sql-review: Reviews SQL migrations...

帮我分析这个迁移说明
```

### auto/manual 注入行为

auto 模式技能在 system prompt 的 `## Available Skills` 段落中标注 `[auto]`，注入完整 name + description。manual 技能不出现在该段落中，LLM 在用户未显式引用时看不到该技能的说明或正文。

```
## Available Skills
- docs-helper [auto]: Helps write docs and release notes for this repository.
```

auto 技能仅注入 `meta.description`，不注入 body。LLM 判断 auto 技能相关时，应调用 `load_skills` 获取完整正文。manual 技能只在用户通过 `#` 或 `$skill-name` 显式引用后，把 name + description 带到用户消息中；它不进入 system prompt，也不自动加载 body。

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| `#` 后无匹配技能 | 面板显示 "No matching skills"，不阻止输入 |
| `/skills auto` 对 disabled 技能 | 先自动 enable，再设 auto |
| 用户说"帮我安装技能"但源不存在 | voidx 报错并提示检查路径/URL |
| 用户说"帮我删除技能"但本地不存在对应 skill 目录 | voidx 报错并提示该技能不可删除或不存在 |
| skills.json v1 读取 | 自动迁移到 v2，auto 字段默认为空 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| `#` 选中后展开为 `$skill-name` | `#` 直接作为引用语法 | 复用现有显式引用路径，不增加新的解析分支 |
| manual 技能默认对 LLM 不可见 | manual 也在 `Available Skills` 中仅列 name | manual 的目的就是用户手动引入；否则 LLM 会看到技能存在并可能主动调用 |
| manual 显式引用只包装 name + description | 显式引用直接注入 body | 用户引用只表达“这个技能相关”；body 仍应由 LLM 在需要时通过 `load_skills` 拉取，避免隐式扩大上下文 |
| auto 模式仅注入描述不注入 body | auto 也注入 body | body 可能很长，全注入浪费 token；描述已足够让 LLM 知道何时用 `load_skills` 加载 |
| install/uninstall 不做独立命令 | 做 `/skills install`/`uninstall` 命令 | 用户直接对话更自然，voidx 已有文件读写能力，无需额外命令入口 |
| `##` 不触发技能面板 | 允许 `##` 触发 | `##` 是 Markdown 标题，冲突概率高 |
| skills.json 新增 auto 字段 | 在 SKILL.md frontmatter 中加 mode 字段 | mode 是用户偏好而非技能属性，应存放在用户配置中 |
| 内置 workflow node 不纳入 skill 管理 | 把 workflow node 当作 bundled skill | 当前系统已经没有内置 skill；workflow node 有独立 runtime、context 和状态机 |

## Resolved Decisions

| 问题 | 决策 |
|------|------|
| auto 模式是否需要在 `## Available Skills` 中注入完整 description？ | 是，auto 注入完整 name + description；manual 不显示 |
| manual 技能通过 `#` 引用时是否注入 system prompt 或 body？ | 不注入。只把 name + description 带到用户消息 |
| `/skills install`/`uninstall` 是否需要独立命令？ | 不需要，用户直接与 voidx 对话，voidx 根据意图生成/安装/删除技能 |
| 技能面板是否需要显示技能的 mode（auto/manual）？ | 是，`/skills list` 和 `#` 技能面板都显示 mode 状态 |
| 多个 `#` 引用时的交互：是否支持一次输入中引用多个技能？ | 支持，每个 `#` 独立展开，发给 LLM 时包装用户消息 |
| 是否还有内置 skill 需要管理？ | 没有。内置 workflow node 不属于本设计的 skill 范围 |
