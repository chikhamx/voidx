# 技能引用与管理增强 — 技术设计文档

## Context

当前技能系统已有完整的发现、匹配、状态机、渲染管线，但用户与技能的交互方式有限：

- **引用方式单一**：仅支持 `$skill-name` 纯文本引用，无 TUI 交互式选择
- **管理模式粗糙**：`/skills` 只支持 `list|show|enable|disable|paths`，缺少 auto/manual 模式、install/uninstall
- **auto 模式缺失**：所有技能默认走 workflow policy 自动匹配或 `$` 显式引用，无法将某个技能固定注入 system prompt

本设计在现有架构上增加两个核心能力：

1. **`#` 技能引用**：TUI 输入框中输入 `#` 弹出技能选择面板，选中后展开为 `$skill-name` 引用
2. **技能模式管理**：每个技能可设为 `auto`（固定注入描述到 system prompt）或 `manual`（默认，需 `#`/`$` 引用才加载 body）

## Goals and Non-Goals

### Goals

- `#` 触发技能选择面板，交互方式与 `@` 附件面板一致（上下键 + 回车）
- `#` 后可继续输入做模糊过滤
- 选中后输入框展开为 `$skill-name`，走现有显式引用路径
- `/skills auto <name>` 将技能设为 auto 模式，描述固定注入 system prompt
- `/skills manual <name>` 将技能设为 manual 模式（默认），仅 `#`/`$` 引用时加载 body
- `/skills install <source>` 从本地路径或 URL 安装技能到 `~/.voidx/skills/`
- `/skills uninstall <name>` 删除 global/project 级技能文件（bundled 技能不可卸载，只能 disable）
- auto 模式技能在 `## Available Skills` 段落中标注 `[auto]`

### Non-Goals

- 不改变现有 workflow policy 的自动激活逻辑
- 不改变 bundled 技能的转移链和优先级
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
│  走显式引用流程          │  service.select() 中 explicit 分支
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
│  auto 技能注入描述       │  auto 技能标注 [auto]，描述更丰富
└─────────────────────────┘
```

## Data Model

### skills.json 扩展

现有结构：

```json
{
  "version": 1,
  "enabled": ["brainstorming"],
  "disabled": ["writing-plans"]
}
```

新增 `auto` 字段：

```json
{
  "version": 2,
  "enabled": ["brainstorming"],
  "disabled": ["writing-plans"],
  "auto": ["systematic-debugging"]
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
    scope: str       # bundled / global / project
    description: str
    mode: str        # auto / manual
```

## API Contract

### find_skill_token(text, cursor) → SkillToken | None

- **Signature**: `find_skill_token(text: str, cursor: int) -> SkillToken | None`
- **Behavior**: 从光标位置向前查找 `#`，要求 `#` 前是行首或空白。返回 token 范围和查询文本。
- **Edge cases**: `#` 在单词中间不触发（如 `issue#123`）；`##` 不触发（Markdown 标题）

### list_skill_candidates(workspace, query, limit) → list[SkillCandidate]

- **Signature**: `list_skill_candidates(workspace: str, query: str, limit: int = 8) -> list[SkillCandidate]`
- **Behavior**: 从 SkillRegistry 获取所有 enabled 技能，按 query 模糊过滤 name 和 description，返回候选列表
- **Filtering**: query 为空时返回全部；非空时 name 前缀匹配优先，description 包含次之

### /skills 命令扩展

| 命令 | 行为 |
|------|------|
| `/skills` 或 `/skills list` | 列出所有技能，标注模式（auto/manual）和状态（enabled/disabled） |
| `/skills show <name>` | 显示技能详情（不变） |
| `/skills enable <name>` | 启用技能（不变） |
| `/skills disable <name>` | 禁用技能（不变） |
| `/skills auto <name>` | 将技能设为 auto 模式（描述固定注入 system prompt） |
| `/skills manual <name>` | 将技能设为 manual 模式（默认，需引用才加载） |
| `/skills paths` | 显示技能目录路径（不变） |

### 技能安装与卸载

不提供 `/skills install` / `/skills uninstall` 命令。用户直接与 voidx 对话表达意图：

- **安装/创建技能**："帮我创建一个代码审查技能" / "帮我安装一个技能，要求是..." → voidx 根据用户意图生成 SKILL.md 并保存到 `~/.voidx/skills/<name>/SKILL.md`
- **卸载/删除技能**："帮我删除 xxx 技能" / "我不需要这个技能了" → voidx 删除对应的 global/project 级技能目录
- **从 URL 安装**："帮我从这个 URL 安装技能 https://..." → voidx 下载并保存

voidx 通过文件读写工具完成操作，无需专用命令。bundled 技能不可删除，voidx 应提示用户用 `/skills disable` 替代。

### TUI 技能面板渲染

面板中每个候选项显示：名称、mode（auto 用绿色标注，manual 用 dim）、scope、description 摘要。

示例：
```
  ❯ brainstorming  [auto]  bundled — Use before creating features...
    systematic-debugging  [manual]  bundled — Use when debugging bugs...
```

### TUI 面板交互

| 按键 | 行为 |
|------|------|
| `#` | 在行首或空白后输入 `#`，弹出技能选择面板 |
| 继续输入 | `#` 后的文本作为模糊查询过滤技能列表 |
| ↑ / ↓ | 在候选列表中移动选中项 |
| Enter / Tab | 选中当前项，将 `#query` 替换为 `$skill-name ` |
| Esc | 关闭面板，保留 `#query` 文本 |

### 多技能引用与消息包装

用户可在一次输入中引用多个技能，例如：`$brainstorming $systematic-debugging 帮我分析这个bug的设计方案`。

发送给 LLM 时，将用户消息包装为自然语言：

```
用户指定了技能 [brainstorming, systematic-debugging]，帮我分析这个bug的设计方案
```

包装规则：
- 提取用户消息中所有 `$skill-name` 引用，汇总到 `用户指定了技能 [...]` 前缀
- 从用户消息正文中移除 `$skill-name` 标记，保留纯文本
- 若无 `$` 引用，不添加前缀，原样发送
- auto 模式技能不在此处重复列出（已在 system prompt 的 Available Skills 段落中）

### auto 模式注入行为

auto 模式技能在 system prompt 的 `## Available Skills` 段落中标注 `[auto]`，注入完整 name + description：

```
## Available Skills
- brainstorming [auto]: Use before creating features, building components, or modifying behavior. Explores intent, requirements, and design before implementation.
- systematic-debugging: Use when debugging bugs, failed tests, build failures, tracebacks, crashes, or unexpected behavior.
```

auto 技能注入完整 `meta.description`；manual 技能仅显示 name。

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| `#` 后无匹配技能 | 面板显示 "No matching skills"，不阻止输入 |
| `/skills auto` 对 disabled 技能 | 先自动 enable，再设 auto |
| 用户说"帮我安装技能"但源不存在 | voidx 报错并提示检查路径/URL |
| 用户说"帮我删除技能"但技能是 bundled | voidx 提示 "Bundled skills cannot be deleted. Use /skills disable instead." |
| skills.json v1 读取 | 自动迁移到 v2，auto 字段默认为空 |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| `#` 选中后展开为 `$skill-name` | `#` 直接作为引用语法 | 复用现有显式引用路径，不增加新的解析分支 |
| auto 模式仅注入描述不注入 body | auto 也注入 body | body 可能很长，全注入浪费 token；描述已足够让 LLM 知道何时用 `load_skills` 加载 |
| install/uninstall 不做独立命令 | 做 `/skills install`/`uninstall` 命令 | 用户直接对话更自然，voidx 已有文件读写能力，无需额外命令入口 |
| `##` 不触发技能面板 | 允许 `##` 触发 | `##` 是 Markdown 标题，冲突概率高 |
| skills.json 新增 auto 字段 | 在 SKILL.md frontmatter 中加 mode 字段 | mode 是用户偏好而非技能属性，应存放在用户配置中 |
| bundled 技能不可 uninstall | 允许删除 bundled 文件 | bundled 随包发布，删除后下次更新会恢复；disable 更合理 |

## Open Questions

- [x] auto 模式是否需要在 `## Available Skills` 中注入完整 description？→ 是，auto 注入完整 name + description，manual 仅显示 name
- [x] `/skills install`/`uninstall` 是否需要独立命令？→ 不需要，用户直接与 voidx 对话，voidx 根据意图生成/安装/删除技能
- [x] 技能面板是否需要显示技能的 mode（auto/manual）？→ 是，`/skills list` 和 `#` 技能面板都显示 mode 状态
- [x] 多个 `#` 引用时的交互：是否支持一次输入中引用多个技能？→ 支持，每个 `#` 独立展开，发给 LLM 时包装用户消息
