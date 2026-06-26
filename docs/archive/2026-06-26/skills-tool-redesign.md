> **Status: Done**

# SkillsTool 重设计 — 技术设计文档

## Context

当前 `LoadSkillsTool`（id: `"skill"`）只支持按名称加载 skill body，功能单一。需要扩展为统一的 skill 管理入口，覆盖加载、创建和列出三个核心操作。第一期不包含 destroy/enable/disable，后续按需追加。

## Goals and Non-Goals

### Goals

- 将 `LoadSkillsTool` 重命名为 `SkillsTool`，tool id 保持 `"skill"` 不变
- 新增 `op` 参数，支持：`load`、`create`、`list`
- `load` 改为单 name 调用（原支持批量 names），LLM 需要多个时调多次
- `create` 支持 project 和 global 两种 scope，写入 SKILL.md 文件
- `list` 返回所有已发现 skill 的名称、scope、enabled 状态、description

### Non-Goals

- 不支持 destroy/enable/disable（后续迭代）
- 不修改 `SkillService` 的选择/匹配逻辑
- 不支持在线编辑 skill body（用户直接改文件）
- 不处理 bundled skill（当前无 bundled skill，`include_bundled` 参数已移除；`load`/`list` 对 bundled scope 透明处理）

## Architecture

```
用户/Agent
    │
    ▼
SkillsTool (id="skill")          ← 工具层，单一 SkillsInput model 按 op 分发
    │
    ├── load   → SkillService.get() + render_instruction()      [READ_TOOLS, allow]
    ├── create → SkillRegistry.create_skill()  ← 新增，写 SKILL.md  [FILE_WRITE, ask]
    └── list   → SkillRegistry.discover() + SkillService.is_enabled()  [READ_TOOLS, allow]
```

## Data Model

使用单一 `SkillsInput` flat model + `op` literal 分发，与现有 `TodoWriteTool`（`src/voidx/tools/todo.py`）模式一致。所有 op 的字段放在同一个 model 里，`parameters_schema()` 返回统一 schema，LLM 一次看到所有字段，`execute` 内按 `args["op"]` 手动 dispatch。相比 discriminated union，这种方式生成的 JSON schema 更简洁，LLM 理解成本更低，也避免了 Pydantic `Discriminator` 对嵌套 union variant 的限制。

> **约束**：`BaseTool.parameters_schema()` 是无参方法，在工具注册时调用一次返回静态 schema（`src/voidx/tools/base.py:137`、`src/voidx/tools/registry.py:60`），无法根据 `args["op"]` 动态切换。因此必须用单一 Input model，不能按 op 返回不同子类 schema。

```python
class SkillsInput(BaseModel):
    """Unified skill management: load, create, or list skills."""
    op: Literal["load", "create", "list"] = Field(
        description="Operation: 'load' (fetch a skill's instructions), 'create' (write a new SKILL.md), 'list' (enumerate discovered skills)."
    )
    name: str | None = Field(
        default=None,
        description="Skill name. Required for 'load' and 'create'. Lowercase, hyphen-separated (e.g. 'react-patterns')."
    )
    description: str | None = Field(
        default=None,
        description="One-line summary. Required for 'create'."
    )
    body: str | None = Field(
        default=None,
        description="Markdown instruction body. Required for 'create'."
    )
    scope: Literal["project", "global"] = Field(
        default="project",
        description="Write scope for 'create': 'project' → .voidx/skills/<name>/SKILL.md, 'global' → ~/.voidx/skills/<name>/SKILL.md."
    )
```

> **注意**：`load` 使用已有的 `normalize_skill_name`（`strip().lower()`）做名称归一化，name 正则约束仅在 `create` 时校验，避免影响已存在的不符合正则的 skill 名。
>
> **bundled skills**：当前没有 bundled skill（`src/voidx/skills/bundled/` 为空目录），`include_bundled` 参数已移除。`load` 和 `list` 对 bundled scope 的 skill 透明处理——若未来出现 bundled skill，`load` 直接返回其内容，`list` 正常展示。

### SKILL.md 文件格式（create 写入）

```markdown
---
name: {name}
description: {description}
enabled: true
---

{body}
```

> **注意**：frontmatter 仅写入 name / description / enabled 三个字段。现有的 `triggers` 字段由用户手动维护，`create` 不写入。
>
> **创建后的默认状态**：`create` 只写 SKILL.md 文件，**不修改 `settings.json` 的 `skills.auto` 列表**。因此新 skill 的 `mode` 为 `manual`（`SkillService.is_auto()` 要求 name 在 `selection.auto` 中，`service.py:61-63`）。skill 处于 `enabled` 状态（frontmatter `enabled: true` + 不在 disabled 列表），但不会自动触发——需用户显式 `#skill-name` 引用，或通过 `/skills` 命令手动加入 auto 列表。`create` 的成功输出已包含 triggers 引导提示，但**不**提示加入 auto 列表（避免 LLM 自行修改用户偏好配置）。

### Skill name 约束

- 正则：`^(?=.{1,64}$)[a-z0-9]([a-z0-9-]*[a-z0-9])?$`（单字符时无连字符限制）
- 长度 1-64 字符，不允许以 `-` 开头或结尾
- `create` 时若已存在同名 skill，返回提示信息给 LLM（不抛异常、不报错），告知已存在及文件路径

> **校验层级**：name 正则校验**必须在 `SkillRegistry.create_skill()` 方法内部首行执行**，不通过则抛 `ValueError`。不能仅依赖 tool 层的 Pydantic Field 约束——`create_skill` 是 registry 层公共方法，可能被其他调用方（如 CLI、测试）直接调用。这是 sandbox 跳过路径检查后的**唯一**路径逃逸防线（详见 Permission / Capability 升级）。

### `load` 路径注入防护

`load` 保留现有 `LoadSkillsTool` 的 `_looks_like_path()` 检查（`src/voidx/tools/load_skills.py:154-162`），拒绝含 `/`、`\`、以 `.` 开头、以 `.md`/`.markdown` 结尾的名称。虽然 `SkillRegistry.get()` 内部走 `normalize_skill_name` + 名称匹配，不会直接拼接路径，但保留此检查作为纵深防御，防止 LLM 误传文件路径当作 skill 名。

## API Contract

### SkillsTool.execute

- **Signature**: `async def execute(self, args: dict, ctx: ToolContext) -> ToolResult`
- **Input**: 按 `args["op"]` dispatch，统一用 `SkillsInput` model 校验
- **Output per op**:

| op | 成功输出 | metadata |
|----|---------|----------|
| `load` | `VOIDX_SKILL_TOOL_CONTEXT` 包裹的 skill 指令 | `{loaded_skills: [{name, scope, path}], count, truncated}` |
| `list` | 每行一个 skill：`{name}\t{scope}\t{enabled}\t{description}`（人类可读 TSV） | `{skills: [{name, scope, enabled, description}], count}`（结构化，供程序消费） |
| `create` | 确认消息 + 文件路径 + triggers 引导提示 | `{path, name, scope}` |

> **`list` metadata 说明**：遵循 Code Rules「Prefer structured metadata over parsing rendered text」，`list` 的 metadata 返回结构化 `skills` 数组，output 文本保留 TSV 格式供人类阅读。description 中的 tab/换行不会破坏 metadata 解析。
>
> **`create` 成功输出示例**：`Created skill 'react-patterns' at .voidx/skills/react-patterns/SKILL.md. 使用时在对话中输入 #react-patterns 引用该 skill，或手动编辑该文件添加 triggers 字段以启用自动触发。`

### SkillRegistry 新增方法

```python
def create_skill(self, name: str, description: str, body: str, *, scope: Literal["project","global"] = "project") -> Path | None:
    """创建 SKILL.md 文件，返回路径；已存在时返回 None。
    scope=project 写入 .voidx/skills/<name>/SKILL.md
    scope=global 写入 ~/.voidx/skills/<name>/SKILL.md

    安全约束：
    - 方法首行用 SKILL_NAME_RE 正则校验 name，不通过则抛 ValueError。
      这是 sandbox 跳过路径检查后的唯一防线，不能仅依赖 tool 层校验。
    - 写入路径为 <known_dir>/<name>/SKILL.md，name 正则保证不含 /、.. 等逃逸字符。

    竞态说明：
    - path.exists() 与写入之间存在微小 TOCTOU 窗口，在单 agent 顺序执行假设下可接受。
    - 目录不存在时自动创建（parents=True, exist_ok=True）。
    - 写入前用 path.exists() 检查文件是否已存在，已存在则返回 None
     （由调用方返回提示给 LLM，不抛异常）。
    - 写入成功后自动调用 self.invalidate() 刷新 discover 缓存。"""
```

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| `load` 名称不存在 | 返回错误，附带所有可用 skill 名称列表（通过 `discover()` 获取） |
| `load` 名称存在但已禁用 | 返回错误，提示 skill 已禁用及当前 enabled 状态 |
| `create` skill 已存在 | 返回提示信息（非错误），告知名称已占用及现有文件路径，引导 LLM 改名或直接编辑现有文件 |
| `create` name 格式非法 | 返回错误，说明命名规则（正则 + 长度） |
| `create` 目标目录不可写 | 返回错误，提示目录写入失败（OS 层异常） |

> **路径安全说明**：`create` 不经过 `resolve_safe`。它直接使用 `SkillRegistry` 持有的已知目录（`project_dir` / `global_dir`），写入路径为 `<dir>/<name>/SKILL.md`。name 正则约束在 `create_skill()` 方法内部首行强制校验（见 Skill name 约束），保证路径不会逃逸到父目录。`~/.voidx/skills/` 是全局可读写的已知路径。

### Permission / Capability 升级

`create` 是写操作，必须走 `FILE_WRITE` permission 流程，而非现有的 `READ_TOOLS`。需修改两处：

1. **`capability_for_tool`**（`src/voidx/permission/rules.py:359`）：将 `"skill"` 从 `READ_TOOLS` 集合移出，改为按 `args["op"]` 分类——`op="create"` 归为 `FILE_WRITE`，其余 op 保持 `READ_TOOLS`。与 `git` 工具的 read/write 分类逻辑一致（`rules.py:370`）。

2. **`build_pattern`**（`src/voidx/permission/rules.py:109`）：`skill` 工具目前走 fallback 返回 `"*"`。`create` op 需返回写入路径（`<dir>/<name>/SKILL.md`），使 sandbox 的 `workspace-write` 模式能做路径检查。但 `create` 的参数是 `name`/`scope` 而非 `file_path`，sandbox 的 `check_sandbox_filepath` 读取 `args.get("file_path")`（`engine.py:73`），找不到会跳过检查。因此需在 `build_pattern` 中为 `skill` + `op="create"` 计算实际写入路径并填入 pattern，或在 `capability_for_tool` 归为 `FILE_WRITE` 后由 sandbox 直接放行（因为 `~/.voidx` 通过 `DATA_DIR` 加入 `extra_write_paths`，project 目录在 workspace 内，两个目标路径都合法；详见下方 sandbox 行为说明）。

3. **`BASIC_RULES`**（`src/voidx/permission/rules.py:40`）：现有 `Rule(permission="skill", pattern="*", action="allow")` 会使 `create` 在 `strategy_action_for_tool` 中被放行。需改为 `action="ask"`，或改为按 pattern 区分（`create` 走 `ask`，`load`/`list` 走 `allow`）。推荐改为 `ask`，让 `create` 在默认 `untrusted` approval policy 下需要用户确认，与 `file`/`write`/`replace` 行为一致。

> **sandbox 行为**：`workspace-write` 模式下，`FILE_WRITE` capability 会检查 `args["file_path"]`（`engine.py:72-74`）。`skill create` 没有 `file_path` 参数，`build_pattern` 对 `skill` 走 fallback 返回 `"*"`，sandbox 检查跳过（`if file_path:` 为假）。
>
> 这意味着 **`skill create` 的 FILE_WRITE capability 在 `workspace-write` 模式下完全不经过 sandbox 路径检查，安全性 100% 依赖 `create_skill()` 方法内部的 name 正则校验**。该正则 `^(?=.{1,64}$)[a-z0-9]([a-z0-9-]*[a-z0-9])?$` 禁止 `/`、`\`、`..` 等路径逃逸字符，是唯一防线。`~/.voidx` 通过 `DATA_DIR`（`memory/store.py:13`，`DATA_DIR = Path.home() / ".voidx"`）加入 `extra_write_paths`（`runtime_context.py:54-65`，`ExecutionPolicy.from_config`），project 目录在 workspace 内，两个目标路径本身合法。

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| tool id 保持 `"skill"` | 改为 `"skills"` | 避免影响 display policy 中已有的 `"skill"` 配置（`display_policy.py:119`）。但 permission rules 需调整：`create` op 升级为 `FILE_WRITE` + `ask`，`load`/`list` 保持 `READ_TOOLS` + `allow` |
| 单一 `SkillsInput` model | 按 op 返回不同子类 schema | `BaseTool.parameters_schema()` 是无参方法，注册时调用一次返回静态 schema，无法按 `args["op"]` 动态切换。与 `TodoWriteTool` 一致，所有字段放同一 model，LLM 一次看到全部字段 |
| flat model + op 分发 | discriminated union | Pydantic `Discriminator` 要求每个 variant 自身包含 `op` 字段，嵌套 union 结构复杂且生成的 schema 不直观；flat model 按 op 手动 dispatch 更简单可控 |
| `load` 改为单 name | 保持 `names: list[str]` | 与 `create` 共用 `name` 字段，语义一致；批量加载调多次即可 |
| create 默认 project scope | 必须显式传 scope | project 是最常见场景，减少参数噪音 |
| `~/.voidx/skills/` 直接读写 | 经过 `resolve_safe` | `resolve_safe` 只允许 workspace 和 extra_paths，global skill 目录不在其中；使用 `SkillRegistry` 已知目录 + `create_skill()` 内部 name 正则校验即可保证安全。`~/.voidx` 通过 `DATA_DIR`（`memory/store.py:13`）加入 `extra_write_paths`（`runtime_context.py:54-65`） |
| `create` 走 `FILE_WRITE` + `ask` | 保持 `READ_TOOLS` + `allow` | `create` 是写操作，默认放行不安全。按 `args["op"]` 在 `capability_for_tool` 中分类，`create` 归 `FILE_WRITE`；`BASIC_RULES` 中 `"skill"` 改为 `ask`，与 `file`/`write`/`replace` 行为一致 |
| `BASIC_RULES` 双规则（`*`→allow + `create`→ask） | 全局 `skill`→ask | `build_pattern` 为 `skill`+`op="create"` 返回 `"create"`，其余返回 `"*"`。`BASIC_RULES` 中 `create` 规则在 `*` 规则之后（findLast 语义，后者优先级更高），使 `load`/`list` 保持 `allow`、仅 `create` 走 `ask`。全局 `ask` 会误伤 `load`/`list` |
| `create_skill` 已存在返回 `None` | 抛 `FileExistsError` | 抛异常会被 tool executor 当作错误处理；返回 `None` 让调用方返回提示信息给 LLM，引导改名或编辑现有文件，体验更好 |
| `create_skill` 写入后自动 `invalidate()` | 依赖 mtime 缓存自然过期 | 新文件的 mtime 与旧 cache signature 不一致，但 `discover()` 可能在同一 session 内被再次调用；显式 invalidate 确保一致性 |
| 移除 `include_bundled` 参数 | 保留在 `load`/`list` | 当前没有 bundled skill（`src/voidx/skills/bundled/` 为空），参数是死代码。`load`/`list` 对 bundled scope 透明处理，未来出现时自动生效 |
| 一期只做 load/create/list | 包含 destroy/enable/disable | 最小可用集，后续按需追加 |