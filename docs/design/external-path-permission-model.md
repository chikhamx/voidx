---
name: external-path-permission-model
display_name: Workspace 外路径权限模型
description: workspace 内保持现有沙箱行为，workspace 外按访问类型、对象类型和生命周期精确授权
doc_type: tech-design
audience: human+llm
---

# Workspace 外路径权限模型 — 技术设计文档

## TL;DR

当前 read 审批 workspace 外文件后，会把父目录加入 `sandbox_workspace_write`，导致一次只读审批实际授予整个目录在当前会话内的读写权限。

本方案把外部路径授权拆为三个正交维度：

- 访问类型：read / write
- 对象类型：file / directory
- 生命周期：session / persistent

用户授权由 session 与 persistent 两层组成，另有仅由启动代码注入的 runtime 授权；三者合并为有效授权。workspace 内继续保持现有 workspace-write 行为；workspace 外必须命中对应授权，或由支持路径审批的工具发起精确审批。write 授权隐含 read，read 授权不隐含 write。

安全边界的关键修正：

- move 会删除源文件，因此 src 与 dest 都要求 write
- Engine 对可审批的外部路径返回“延迟到工具审批”，不能提前 deny 阻断审批链
- shell 只有在路径可静态解析时才支持外部路径；所有外部路径统一要求 write，动态或模糊路径直接拒绝
- `resolve_safe` 直接删除，所有调用点迁移到 `resolve_access`
- 新文件可以按精确目标路径审批，不要求目标预先存在

## Context

### 当前行为

权限模型有两层路径检查：

1. **Permission Engine**：`authorize_tool_call` 调用 `sandbox_denial_reason`，在工具执行前检查 workspace 边界。
2. **Tool 内部**：文件工具执行时调用 `resolve_safe` 再次检查路径。

两层共用 `PermissionService.sandbox_workspace_write`：

- Engine 通过 `PermissionContext.sandbox_workspace_write` 获取快照
- Tool 通过 `ToolContext.sandbox_extra_paths` 获取允许路径

read 工具审批外部文件时，会调用 `ctx.add_extra_path(str(external.parent))`，把父目录加入写白名单。用户看到的是一次读取授权，实际得到的是目录级读写授权。

### 根因

| 层 | 问题 |
|---|---|
| 数据结构 | 单一列表不区分 read/write、file/directory、session/persistent |
| 审批回调 | `add_extra_path` 无差别写入目录级 write 白名单 |
| 路径解析 | `resolve_safe` 不知道调用者需要 read 还是 write |
| Engine | hard deny 会阻止工具进入路径审批流程 |
| 生命周期 | 当前授权直接修改 PermissionService 列表，没有 session/persistent 分层 |
| 上下文同步 | Pydantic 会复制 list，不能依赖 ToolContext 与 PermissionService 共享列表引用 |

## Goals / Non-Goals

### Goals

- workspace 外 read 与 write 权限严格分离
- 文件级授权不扩散到同目录其他文件
- 用户可选择文件级或目录级授权
- 用户可选择 session 或 persistent 生命周期
- write 授权隐含 read，read 授权不隐含 write
- workspace 内行为保持不变
- 所有调用点直接迁移到 `resolve_access`，不保留兼容包装
- 现有 `sandbox_workspace_write` 配置可无损迁移
- 主代理与子代理执行时使用一致的有效授权

### Non-Goals

- 不改变 workspace 内 workspace-write 行为
- 不改变 danger-full-access 与 read-only 的模式定义
- 不让 glob 搜索 workspace 外路径
- 不构建用户/角色 ACL
- 不承诺 shell 路径分析能抵御恶意混淆；无法静态判断时拒绝或要求 danger-full-access

## Permission Model

### 授权集合

每个授权来源都包含四个集合：

| | 文件级 | 目录级 |
|---|---|---|
| **read** | `readable_files` | `readable_dirs` |
| **write** | `writable_files` | `writable_dirs` |

```python
@dataclass(frozen=True)
class AccessGrants:
    readable_files: frozenset[str] = frozenset()
    readable_dirs: frozenset[str] = frozenset()
    writable_files: frozenset[str] = frozenset()
    writable_dirs: frozenset[str] = frozenset()


@dataclass(frozen=True)
class EffectiveAccessGrants:
    runtime: AccessGrants
    session: AccessGrants
    persistent: AccessGrants
```

检查时读取三个来源集合的并集，不在 ToolContext 内维护可变列表副本：

- `runtime`：应用内部预授权路径，例如当前自动注入的 `DATA_DIR`；只能由启动代码构建，不接受用户输入、不写入 settings
- `session`：当前会话内由用户审批的授权
- `persistent`：从 settings 加载或由用户明确持久化的授权

runtime grant 仅允许列入应用自身管理且已有明确用途的路径，不能成为绕过用户审批的通用注入口。

### 授权规则

1. workspace 内：保持现有 workspace-write 行为。
2. workspace 外 read：命中 readable 或 writable 的 file/dir 授权即可。
3. workspace 外 write：必须命中 writable 的 file/dir 授权。
4. 文件级授权仅精确匹配规范化后的目标路径。
5. 目录级授权通过 `Path.relative_to` 判断后代关系，禁止字符串前缀匹配。
6. write 隐含 read；read 不隐含 write。
7. read-only 模式始终阻止写操作，即使存在 writable grant。
8. danger-full-access 跳过本模型的路径限制。

### 路径规范化

所有授权写入与检查都使用相同的规范化函数：

- 展开 `~`
- 相对路径以 workspace 为基准
- 已存在路径使用 `Path.resolve()`
- 不存在的写目标：解析最近的已存在父目录，再拼接剩余路径段
- 拒绝无法解析、含非法路径段或解析后越过授权边界的路径

创建父目录或执行写入前必须再次检查解析后的目标，避免新建目录或符号链接改变最终落点。

## Runtime Data Model

### PermissionService

授权数据结构与结果类型放在独立模块 `permission/grants.py`，避免 PermissionService、ToolContext 与 Settings 之间形成循环依赖。

```text
PermissionService
├── runtime_grants: AccessGrants
├── session_grants: MutableAccessGrants
├── persistent_grants: MutableAccessGrants
├── persist_grants: PersistAccessGrantsCallback | None
├── get_effective_grants() -> EffectiveAccessGrants
├── add_grant(path, access, is_dir, persist) -> GrantUpdateResult
└── clear_session_permissions()
```

```python
PersistAccessGrantsCallback = Callable[[AccessGrants], GrantUpdateResult]
```

`clear_session_permissions()` 同时清除：

- 工具级 session allow/deny
- `session_grants` 中的四类路径授权

`add_grant(..., persist=False)` 只更新 session grants。`add_grant(..., persist=True)` 先构造新的 persistent grants 候选值，再调用 `persist_grants(candidate)`；只有回调返回 `ok=True` 才替换内存中的 persistent grants。

`build_permission_service` 必须接收 `Settings | None` 或等价的持久化回调。Graph 已持有 `self._settings`，创建和刷新 PermissionService 时都要传入；没有 Settings 的运行环境只能使用 session grants，选择持久化时返回明确错误。

切换到任一非 CUSTOM 权限模式时：

- 保留 runtime grants
- 清空 session grants 和工具级 session allow/deny
- 清空 live PermissionService 的 persistent grants
- 从 settings 删除四个 canonical grant 字段

这与现有切换 preset 时删除 `sandbox_workspace_write` 的行为一致，避免自定义路径授权在预设模式下静默残留。切回 CUSTOM 不自动恢复已删除的授权。

### ToolContext

ToolContext 不保存需要与 PermissionService 共享引用的四个 list。新增回调：

```python
@dataclass(frozen=True)
class GrantUpdateResult:
    ok: bool
    error: str = ""


GetAccessGrantsCallback = Callable[[], EffectiveAccessGrants]
AddGrantCallback = Callable[
    [str, Literal["read", "write"], bool, bool],
    GrantUpdateResult,
]

class ToolContext(BaseModel):
    get_access_grants: GetAccessGrantsCallback | None
    add_grant: AddGrantCallback | None
```

工具每次检查时调用 `ctx.get_access_grants()` 获取最新快照。审批成功后调用 `ctx.add_grant(...)`；只有返回 `ok=True` 才能重新执行路径检查，返回失败时直接向用户报告且不得产生副作用。这样不依赖 Pydantic list 引用共享，也能让持久化保存失败对工具可见。

`add_extra_path` 与 `sandbox_extra_paths` 直接删除，所有调用点迁移到 `get_access_grants` / `add_grant` callbacks。

### PermissionContext

PermissionContext 使用不可变授权快照：

```text
PermissionContext
└── access_grants: EffectiveAccessGrants
```

Engine 每轮授权时从 PermissionService 创建新快照，不依赖可变对象共享。

### 子代理

子代理继承创建时的 effective grants 快照：

- 可以使用父会话已经授予的路径
- 默认没有交互回调，不能自行扩大授权
- 不允许持久化新的路径授权
- 若未来支持子代理交互，必须通过父会话 PermissionService 统一审批和写入

## Access Resolution

### API

```python
def resolve_access(
    workspace: str,
    file_path: str,
    access: Literal["read", "write"],
    grants: EffectiveAccessGrants,
    *,
    allow_missing: bool = False,
) -> Path | None:
    """规范化路径并检查 workspace 或 access-scoped grants。"""
```

- read 默认要求目标存在；工具仍负责检查文件/目录类型
- write 可传 `allow_missing=True`，允许创建尚不存在的精确目标
- 函数只做规范化与授权判断，不发起 UI 交互

### 调用点迁移

`resolve_safe` 直接删除，不保留兼容包装。所有调用点（`tools/file/read.py`、`tools/file/write.py`、`tools/file/replace.py`、`tools/file/manage.py`、`tools/git.py`、`ui/session.py`）迁移到 `resolve_access`，显式传入 `access="read"` 或 `access="write"` 与 `ctx.get_access_grants()`。

## Engine and Tool Approval Flow

### Engine 检查结果

Engine 路径预检查从二态改为三态：

```text
ALLOW            路径在 workspace 内或已有授权
DEFER_TO_TOOL    外部路径缺少授权，但该工具已实现路径级审批
DENY             工具不支持路径审批、路径不可安全解析或模式禁止
```

`DEFER_TO_TOOL` 不等于授权，只允许工具执行到内部 `resolve_access` 与审批逻辑。工具在执行副作用前必须重新检查；未审批或审批失败时必须返回 error。

这样既保留 Engine 的前置防线，又不会让 hard deny 阻断工具内部审批。

### 路径访问描述

普通工具继续使用同步路径提取：

```python
@dataclass(frozen=True)
class PathAccess:
    path: str
    access: Literal["read", "write"]
    kind: Literal["file", "dir", "unknown"]
    allow_missing: bool = False


def path_accesses_for_tool(tool: str, args: dict) -> list[PathAccess]: ...
```

Engine 仅检查该函数明确返回的文件系统访问。没有路径参数的 webfetch、todo、document 等 READ_TOOLS 不参与文件路径检查。

Git 是两阶段例外，因为真实仓库根和元数据路径必须执行只读 discovery 后才能确定：

1. Engine 调用同步 `parse_git_command(args)`，从共享策略注册表得到 `GitStaticPlan`：read/write capability、用户输入 repo path、显式仓库外参数和是否需要运行时 discovery。
2. Engine 检查静态可见路径；若策略未知直接 DENY，若仍需发现 repo/metadata 路径则返回 DEFER_TO_TOOL，不能把静态检查结果当成完整授权。
3. Git 工具在无副作用的受控 discovery 环境中调用异步 `complete_git_access_plan(static_plan, ctx)`，生成完整 `GitRuntimePlan`。
4. 工具对 runtime plan 中所有路径完成授权与二次检查后才执行实际 Git 子命令。

共享类型、策略注册表和纯同步解析放在独立 `permission/git_policy.py`。`permission/rules.py` 与 `tools/git.py` 都依赖该模块；该模块不得反向导入 rules 或 GitTool，避免循环依赖。

### 工具审批流程

1. Tool 调用 `resolve_access(..., ctx.get_access_grants())`。
2. workspace 内或已授权：继续。
3. 未授权且工具不支持路径审批或 `ctx.interact is None`：阻止。
4. 规范化并确认审批对象；read 目标必须存在，write 目标可以不存在。
5. 显示文件/目录 × session/persistent + deny 五个选项。
6. 调用 `ctx.add_grant(...)`；若返回 `ok=False`，报告错误并终止，不能产生副作用。
7. 返回成功后重新获取 grants，并再次调用 `resolve_access`。
8. 只有二次检查成功后才能产生副作用。

### 审批选项

read：

```text
读取 workspace 外的文件: /path/to/file.txt
[本次会话读取此文件]
[本次会话读取此目录]
[持久化读取此文件]
[持久化读取此目录]
[拒绝]
```

write：

```text
写入 workspace 外的文件: /path/to/file.txt
[本次会话写入此文件]
[本次会话写入此目录]
[持久化写入此文件]
[持久化写入此目录]
[拒绝]
```

“本次会话”在当前 session 结束或清除 session permissions 后失效，不是单次工具调用。

## Tool-Specific Rules

| Tool / Operation | Path | Access | Approval Object |
|---|---|---|---|
| read | `file_path` | read | file 或 parent dir |
| grep | `path` | read | file/dir 按实际目标 |
| lsp | `file_path` | read | file 或 parent dir |
| write / replace | `file_path` | write | file 或 parent dir |
| manage create/delete | each path | write | file 或 parent dir |
| manage move | src | write | file 或 parent dir |
| manage move | dest | write | file 或 parent dir |
| git read operation | repository `path` | read | directory only |
| git write operation | repository `path` | write | directory only |
| bash / powershell | each external literal path | write | file/dir，见 shell 规则 |

### move

move 不等价于 read source + write destination。`shutil.move` 成功后源文件被删除，因此：

- src 要求 write
- dest 要求 write
- 两端分别审批
- 若任一端未授权，不得执行移动

未来若增加 copy 操作，copy 才使用 src=read、dest=write。

### Git

Git 的权限边界包括工作树、Git 元数据、对象库、索引和显式仓库外参数。规划拆为同步静态解析与异步运行时补全：

```python
@dataclass(frozen=True)
class GitStaticPlan:
    capability: Literal["read", "write"]
    input_repo_path: PathAccess
    explicit_paths: tuple[PathAccess, ...]
    policy_id: str
    needs_discovery: bool = True


@dataclass(frozen=True)
class GitRuntimePlan:
    static: GitStaticPlan
    worktree_root: PathAccess | None  # bare repo 为 None
    git_dir: PathAccess
    common_dir: PathAccess
    index_path: PathAccess | None
    object_dirs: tuple[PathAccess, ...]
    config_files: tuple[PathAccess, ...]
    explicit_paths: tuple[PathAccess, ...]
```

运行时补全不能先执行可能读取未授权元数据的 `git rev-parse`。采用授权感知的文件系统预检：

1. `parse_git_command(args)` 仅做纯同步解析，从策略注册表产生 `GitStaticPlan`；未知子命令或参数组合直接拒绝。
2. 先授权并规范化用户输入 repository path 和静态可见的 explicit paths。
3. workspace 内允许在 workspace 边界内向上定位仓库根；workspace 外要求 `path` 直接指向 worktree root 或 bare git dir，禁止为了搜索仓库根而读取未授权父目录。
4. 在已授权输入目录内检查 `.git`：目录形式直接作为 git dir；文件形式只读取该已授权文件并解析 `gitdir:` 指针。指针目标尚未授权时先审批，审批成功前不得读取目标目录。
5. 在已授权 git dir 内读取 `commondir`（若存在）。若其解析目标位于当前授权之外，先审批 common dir，再读取其中内容。
6. 从已授权 git dir/common dir 推导 index 与 objects 路径；读取 objects/info/alternates 前必须已授权该对象库。alternates 中的每个目标规范化后分别审批，无法可靠解析时直接拒绝。
7. 从已授权 `common_dir/config` 读取仓库级配置，但首次解析不跟随 include。`common_dir/config` 始终加入 `config_files`；linked worktree 不能错误地从 `git_dir/config` 代替它。
8. 从 `common_dir/config` 读取 `extensions.worktreeConfig`。仅当其为 true 时，将 `git_dir/config.worktree` 加入配置来源；文件存在时必须先获得 read grant 再读取，不存在则按 Git 的空 worktree 配置处理。未启用该扩展时不得读取 `config.worktree`。
9. 对 `common_dir/config`、可选的 `git_dir/config.worktree` 及其已授权 include 递归解析。先提取 `include.path` 与可静态求值的 `includeIf.*.path`，对每个新配置文件先审批、后读取；支持的条件和求值输入必须在策略中列举。动态条件、循环、深度超限或无法规范化的路径直接拒绝。
10. 对完整合并配置执行危险行为检查：shell alias、hooks、external diff/textconv、filter clean/smudge/process、credential helper、fsmonitor、editor、GPG/SSH command 等可能启动外部进程的配置必须由策略显式禁用，否则该命令组合 DENY。
11. 完整路径均授权后，在受控环境中先运行 `git config --show-origin --show-scope --null --list --includes`。只提取 `file:` origin 并规范化；每个实际来源都必须属于已授权的 `config_files` 或应用维护的空 global config。出现计划外来源、同一 origin 规范化到不一致目标或不可规范化的 origin 时立即拒绝。空文件、仅注释文件或未产生有效配置项的已授权文件可以不出现在 origin 输出中；缺失 origin 本身不构成失败。
12. 配置来源校验通过后运行 `rev-parse` 做元数据一致性验证；其 worktree/git-dir/common-dir/index/objects 输出必须与预检计划一致，否则拒绝执行。
13. 所有路径再次通过授权检查后，才使用同一受控环境执行实际 Git 子命令。

这样 discovery 只读取当前已授权范围内的入口文件；发现新的外部元数据路径时先审批、后访问。外部调用若传入仓库子目录，返回 `external_git_path_must_be_repo_root`，而不是越过授权边界搜索父目录。

授权规则：

- read-only Git 操作要求工作树、元数据、索引和对象库的 read grant；write grant 同样可满足
- write Git 操作要求可能被修改的工作树、元数据、索引、对象库和显式输出目标的 write grant
- linked worktree 的 `git_dir` 与 `common_dir` 必须分别检查；授权工作树或其子目录不能隐含授权位于其他位置的 Git 元数据
- 外部仓库的工作树与 Git 元数据目录只接受目录级授权；显式输出文件仍可使用文件级 write grant

Git 子进程不得继承影响路径解析或配置来源的任意 `GIT_*` 环境变量。`_run_process` 为 Git 构造最小环境：

- 从基础环境只保留运行 Git 所需的 PATH、locale 和平台必需变量
- 清除 `GIT_DIR`、`GIT_WORK_TREE`、`GIT_COMMON_DIR`、`GIT_INDEX_FILE`、`GIT_OBJECT_DIRECTORY`、`GIT_ALTERNATE_OBJECT_DIRECTORIES`、`GIT_CONFIG_*` 及其他继承的 `GIT_*`
- 固定 `GIT_TERMINAL_PROMPT=0`；read discovery/operation 固定 `GIT_OPTIONAL_LOCKS=0`
- 固定 `GIT_CONFIG_NOSYSTEM=1`，并将 `GIT_CONFIG_GLOBAL` 指向 runtime grant 下由应用维护的空配置文件，禁止隐式读取用户或系统 Git 配置
- 所有 Git 调用固定 `core.hooksPath` 到 runtime grant 下的空目录；禁止执行仓库 hooks
- diff/show/log 等读取命令强制关闭 ext-diff 与 textconv；可能触发 filter、credential、editor、GPG、SSH 或其他 helper 的命令，只有在策略证明相关执行路径已禁用时才允许
- `config_files` 必须包含 `common_dir/config`、启用 `extensions.worktreeConfig` 时的 `git_dir/config.worktree`，以及全部递归 include；每个外部文件均先授权后读取
- 受控 `git config --show-origin --show-scope --null --list --includes` 返回的每个规范化 `file:` origin 都必须包含在已授权计划中；空或仅注释的计划文件无需出现在输出中，`rev-parse` 不能替代配置来源验证
- 不允许调用参数重新注入 `--git-dir`、`--work-tree`、`--namespace`、`--config-env` 或改变对象库/索引/配置来源的等价选项

raw Git 必须 fail closed。策略注册表同时定义 capability、参数校验、显式路径提取和 discovery 需求：

- `repo_only` 策略：只允许明确不会访问计划外路径的子命令与参数组合
- `extra_path` 策略：明确提取路径及 access，例如 `worktree add/move`、`archive --output`、`format-patch -o/--output-directory`、`bundle create/list/verify`、`config --file`
- `config --global`、`config --system`、改变 Git 目录/工作树/对象数据库/索引的选项，以及没有策略定义的组合，在 workspace-write 模式下直接拒绝
- pathspec 必须限制在发现后的真实 worktree root 内；`--` 后路径不能逃逸工作树
- danger-full-access 可绕过 workspace 外路径授权，但仍使用受控 Git 环境并保留 destructive deny 规则

Engine、capability 分类和 Git execute 都调用 `permission/git_policy.py` 的 `parse_git_command`；只有 Git execute 调用异步 `complete_git_access_plan`。这样共享静态策略而不要求同步 Engine 执行 `rev-parse`，也避免 rules 与 GitTool 的循环依赖。

### LSP

当前 LSP 操作仅包含 diagnostics/definition/references/symbols，输入文件统一按 read 检查。LSP 返回值也可能引用其他文件，因此输出必须进行二次授权过滤：

- diagnostics 无 `file_path` 时，只返回路径仍命中 effective read grants 的已打开文档结果
- definition/references 返回的每个 `LspLocation.path` 都调用 `resolve_access(access="read")`；未授权位置从结果中移除
- document symbols 只返回已授权输入文件的符号
- workspace symbols 若未来通过工具暴露，必须逐条过滤 symbol path；当前工具不提供无路径 workspace symbols
- 若过滤后为空，返回“没有可访问的结果”，不得泄露未授权路径名称、数量或位置

过滤逻辑放在 Tool/LspService 边界，并接收本次调用的 effective grants；不能只依赖 LspManager 的 workspace，因为语言服务器可能返回 workspace 外位置。

## Shell Rules

shell 不能仅检查重定向目标，否则 `cat /etc/hosts` 等读命令会绕过外部路径模型。

workspace-write 模式下采用以下规则：

1. 保留现有写目标提取：重定向、tee、rm/cp/mv/touch 等。
2. 新增简单命令的字面量路径操作数提取，包括绝对路径、`~` 路径，以及在 `cd` 后解析出的相对路径。
3. 任一解析到 workspace 外的路径，无论命令看似 read 还是 write，都要求 writable grant。
4. 外部路径授权仅支持可静态确定的字面量路径。
5. 命令替换、变量展开、通配符扩展或其他无法确定最终路径的写法，如果可能访问 workspace 外，则拒绝并提示使用显式路径或 danger-full-access。
6. shell 不消费 readable grants；先对 read 工具授权不能放行 shell。

因此 `cat /etc/hosts` 在没有 `/etc/hosts` writable file grant 时被阻止；这是有意的保守策略。

## Persistence and Config Migration

### Canonical 配置字段

```text
sandbox_readable_files
sandbox_readable_dirs
sandbox_writable_files
sandbox_writable_dirs
```

不保留 `sandbox_workspace_write` 字段。

### 加载与写回规则

- 若加载时只存在旧 `sandbox_workspace_write`：一次性迁移为 `sandbox_writable_dirs`，写入 canonical 字段并删除旧字段。
- 若存在 `sandbox_writable_dirs`：直接使用。
- 写回时只写四个 canonical 字段，排序、去重。
- Settings snapshot 只返回四个 canonical 字段。

Settings API 必须先解析并校验完整 `permissions` patch，再执行任何 setter：

- grant 字段集合包括四个 canonical 字段
- 若同一 patch 将 `permission_mode` 设置为任一非 CUSTOM preset，同时包含任一 grant 字段，则整体返回参数错误；不得忽略 grants，也不得先切模式再写回路径
- 若 patch 只切换到非 CUSTOM preset，则通过一次事务删除四个 grant 字段，并刷新 live PermissionService
- 若 patch 保持或切换到 CUSTOM，可在同一事务中更新全部 grant 字段
- 任一校验或持久化失败时，permission mode、grant 配置和 live PermissionService 均保持请求前状态

### 持久化写入

`add_grant(..., persist=True)` 通过注入的 `persist_grants(candidate)` 完成事务型保存，不能直接调用会先修改 `_data` 的普通 setter。Settings 新增专用方法：

```python
def replace_access_grants(self, grants: AccessGrants) -> GrantUpdateResult: ...
```

事务顺序：

1. 基于当前 `_data` 的深拷贝构造 candidate settings，写入四个 canonical 字段。
2. 将完整 JSON 写入同目录临时文件，执行 flush/fsync，并使用 `os.replace` 原子替换目标文件。
3. 仅在替换成功后更新 `Settings._data`、清空 effective cache，并返回 `ok=True`。
4. 任一步失败时删除临时文件、保持原 `_data` 与原配置文件不变，并返回 `GrantUpdateResult(ok=False, error=...)`。
5. PermissionService 只有收到 `ok=True` 才替换内存 persistent grants。

session grant 不写入 settings。Settings API 批量更新 grant 字段时也必须复用同一事务入口，不能逐字段调用 setter 造成部分写入。

## UI Changes

权限审批需要封闭的五选一。交互能力由 `UserInteraction` 明确声明，helper 不再猜测或无条件追加选项：

```python
class UserInteraction(BaseModel):
    prompt: str
    options: list[str | tuple[str, str, str]] = Field(default_factory=list)
    timeout: float | None = None
    allow_other: bool = False
    selected: int = 0
    anchor: str = ""
```

完整调用链：

1. 工具构造 `UserInteraction`。
2. `_make_interact_callback` 读取 `allow_other`；仅在其为 True 时追加唯一的 “Other…” value，并在选中后 fallback 到 `ask_text`。
3. helper 将最终 choices、`selected`、`anchor` 传给 `app.ask_choice`。
4. `InteractionFrontend.ask_choice`、Gateway frontend 和 TUI 继续接收 choices/selected/anchor，不再自行追加 “Other…”。
5. `UiChoiceRequest` 与 `UiPermissionRequest` 新增并保存 `selected`、`anchor`；`allow_other` 只存在于 `UserInteraction` 层，由 helper 消费，避免远端 UI 重复生成选项。

调用规则：

- 权限审批显式使用 `allow_other=False`，只能选择四种授权或 deny
- clarify 与 checkpoint 需要自由修改时显式使用 `allow_other=True`
- 普通封闭选择保持默认 `allow_other=False`
- `selected` 必须落在选项范围内，越界时 clamp 到有效索引
- Esc、timeout 或 cancel 等价于 deny
- 权限审批不接受 `free_text=True` 的响应；即使异常前端返回自由文本也按 deny 处理

## Implementation Scope

| Path | Expected Change |
|---|---|
| `src/voidx/permission/grants.py` | 新增 AccessGrants、EffectiveAccessGrants、GrantUpdateResult、规范化与合并辅助函数 |
| `src/voidx/tools/base.py` | 新增 resolve_access 与 grant callbacks；UserInteraction 新增 allow_other/selected/anchor；保留安全的 resolve_safe 兼容包装 |
| `src/voidx/permission/service.py` | runtime/session/persistent grants、事务持久化回调、模式切换和清理逻辑 |
| `src/voidx/permission/context.py` | 增加 effective grants 快照 |
| `src/voidx/permission/sandbox.py` | 三态路径预检查；file/dir 精确匹配；shell 字面量路径检查 |
| `src/voidx/permission/git_policy.py` | 共享 GitStaticPlan/GitRuntimePlan 类型、策略注册表、同步 parse_git_command 和异步计划补全辅助契约 |
| `src/voidx/permission/rules.py` | 新增 `path_accesses_for_tool`；使用 parse_git_command 做 Git 静态分类；覆盖 grep 与 manage 双路径 |
| `src/voidx/permission/engine.py` | 处理 ALLOW/DEFER_TO_TOOL/DENY，不按 READ_TOOLS 粗粒度检查 |
| `src/voidx/tools/file/read.py` | read 路径审批、封闭选择和二次检查 |
| `src/voidx/tools/file/write.py` | write 路径审批；支持不存在的精确目标 |
| `src/voidx/tools/file/replace.py` | write 路径审批与二次检查 |
| `src/voidx/tools/file/manage.py` | create/delete=write；move src/dest 均为 write |
| `src/voidx/tools/search.py` | grep path=read |
| `src/voidx/tools/lsp.py` | 输入路径 read 检查；将 effective grants 传给输出过滤层 |
| `src/voidx/lsp/service.py` | 过滤 diagnostics/symbols/definition/references 中未授权路径 |
| `src/voidx/tools/git.py` | 异步补全 GitRuntimePlan；发现 worktree/git-dir/common-dir/index/object dirs；隔离 GIT_* 环境；授权全部路径后执行 |
| `src/voidx/tools/bash/safety.py`、`src/voidx/tools/bash/*` | 外部静态路径统一按 write；未知或动态外部访问 fail closed |
| `src/voidx/tools/powershell/sandbox.py`、`src/voidx/tools/powershell/*` | 与 bash 相同的保守外部路径规则 |
| `src/voidx/agent/graph/tool_executor/executor.py` | 注入 get/add grant callbacks，不共享 list 引用 |
| `src/voidx/agent/graph/tool_executor/helpers.py` | 按 UserInteraction.allow_other 条件追加 Other；透传 selected/anchor |
| `src/voidx/agent/graph/subagent.py` | 注入父会话 effective grants 快照，不提供 add/persist callback |
| `src/voidx/agent/graph/wiring.py` | 接收 Settings/持久化回调；构建 runtime 与 persistent grants |
| `src/voidx/agent/graph/core/voidx_graph.py` | 创建与刷新 PermissionService 时传入 `self._settings` |
| `src/voidx/config/models.py` | 新增四个 canonical 配置字段 |
| `src/voidx/config/settings.py` | 新增临时文件 + fsync + `os.replace` 的事务写入原语 |
| `src/voidx/config/settings_permissions.py` | grant 批量替换、legacy 迁移、冲突检测和 preset 清理规则 |
| `src/voidx/ui/gateway/session/method/settings.py` | 预校验完整 permissions patch；原子拒绝非 CUSTOM + grants 混合请求；复用事务入口 |
| `src/voidx/ui/protocol/requests.py` | UiChoiceRequest/UiPermissionRequest 新增 selected/anchor |
| `src/voidx/ui/output/types.py` | 保持 ask_choice 的 choices/selected/anchor 契约，不在前端生成 Other |
| `src/voidx/ui/gateway/frontend.py` | 将 selected/anchor 写入 request DTO |
| `tui/voidx_cli/choice_mixin.py` | 继续渲染 helper 已生成的最终 choices，并应用 selected/anchor |
| `src/voidx/tools/clarify.py`、`src/voidx/tools/checkpoint.py` | 需要自由输入的 tuple 选择显式设置 allow_other=True |

## Invariants

- workspace 内行为不变
- read-only 永远阻止写操作
- danger-full-access 跳过外部路径限制，但不跳过既有 destructive deny 规则
- glob 始终限制在 workspace 内
- read grant 不允许任何写工具产生副作用
- write grant 隐含同路径 read
- move 的 src 与 dest 都要求 write
- 工具内部路径审批必须在副作用前完成二次检查
- Engine 的 DEFER_TO_TOOL 不能被解释为路径已授权
- 未迁移的 `resolve_safe` 调用点只能看到 effective writable dirs，不能看到 readable grants
- session grant 不落盘，persistent grant 必须事务保存成功后才进入内存
- canonical 与 legacy writable-dir 配置写回时保持一致
- 非 CUSTOM 模式不存在用户 persistent/session path grants
- Git 必须校验 worktree root、git dir、common dir、index 和所有对象库；授权工作树不能隐含授权外置元数据
- Git 子进程使用清理后的受控环境，不能继承或由参数注入改变路径/配置来源的 `GIT_*` 语义
- Git 未登记的 raw 子命令或参数组合在 workspace-write 模式下 fail closed
- LSP 输出不得包含未授权路径或可推断该路径存在的信息
- 权限审批不提供 Other/free-text 路径

## Failure Paths and Test Coverage

| Case | Expected Behavior | Test |
|---|---|---|
| read 文件后 write 同文件 | write 仍需审批 | `test_read_grant_does_not_allow_write` |
| read 文件后 read sibling | sibling 仍需审批 | `test_file_grant_does_not_cover_sibling` |
| read 目录后 read child | 放行 | `test_read_dir_grant_covers_child` |
| write 文件后 read 同文件 | 放行 | `test_write_grant_implies_read` |
| move src 只有 read grant | 阻止并要求 src write | `test_move_source_requires_write` |
| move src/dest 均有 write | 放行 | `test_move_cross_write_grants` |
| write 创建不存在文件 | 可对精确目标审批并创建 | `test_write_missing_external_target` |
| Engine 遇到可审批外部 read | DEFER_TO_TOOL，不 hard deny | `test_engine_defers_approvable_read` |
| Engine 遇到未迁移工具外部路径 | deny | `test_engine_denies_non_approvable_tool` |
| tool 收到 DEFER 但用户拒绝 | 无副作用 | `test_deferred_path_denied_by_user` |
| Pydantic 复制上下文字段 | 新 grant 仍可通过 callback 获取 | `test_context_grants_are_refreshed` |
| legacy resolve_safe + readable grant | 不放行 | `test_resolve_safe_ignores_readable_grants` |
| bash `cat /etc/hosts` | 无 writable grant 时阻止 | `test_bash_external_read_requires_write` |
| bash 动态外部路径 | 阻止并提示显式路径/full access | `test_bash_dynamic_external_path_denied` |
| PowerShell 外部只读路径 | 无 writable grant 时阻止 | `test_powershell_external_read_requires_write` |
| 外部 Git path 指向仓库子目录 | 不向上搜索，返回 `external_git_path_must_be_repo_root` | `test_git_external_path_must_be_repo_root` |
| linked worktree 的 common dir 位于未授权位置 | 阻止并要求授权 common dir | `test_git_requires_linked_worktree_common_dir` |
| linked worktree 仅读取 `common_dir/config` | 不以 `git_dir/config` 代替仓库级配置 | `test_git_reads_common_dir_config` |
| `extensions.worktreeConfig=true` | 授权并读取 `git_dir/config.worktree` | `test_git_reads_worktree_config_when_enabled` |
| 未启用 `extensions.worktreeConfig` | 不读取 `git_dir/config.worktree` | `test_git_ignores_worktree_config_when_disabled` |
| Git 对象库 alternates 指向未授权目录 | 阻止；无法解析 alternates 时 fail closed | `test_git_requires_alternate_object_dirs` |
| 进程环境含 `GIT_DIR`/`GIT_OBJECT_DIRECTORY` | discovery 与执行均清除继承值 | `test_git_sanitizes_path_environment` |
| 本地 config include 指向未授权外部文件 | 先审批配置文件，未授权不得读取 | `test_git_config_include_requires_grant` |
| config include 条件动态、循环或无法解析 | fail closed，不启动 Git | `test_git_rejects_unsafe_config_include` |
| `git config --show-origin` 返回计划外来源 | 严格集合比较失败并拒绝执行 | `test_git_rejects_unplanned_config_origin` |
| 空 global/config.worktree/include 文件不产生 origin | 允许缺失，但所有实际 origin 仍必须已授权 | `test_git_allows_empty_planned_config_without_origin` |
| 本地 config 含 hooks/helper/filter/ext-diff 等隐式执行项 | 策略未显式禁用时拒绝命令 | `test_git_denies_implicit_executable_config` |
| Git 参数含 `--git-dir`/`--work-tree`/`--config-env` | workspace-write 下直接拒绝 | `test_git_denies_path_override_options` |
| Git `config --global/--system` | workspace-write 下直接拒绝 | `test_git_denies_global_and_system_config` |
| Git `archive --output` 指向外部文件 | 单独要求目标 write grant | `test_git_archive_output_requires_grant` |
| Git `worktree add` 指向外部目录 | 单独要求目标 write grant | `test_git_worktree_target_requires_grant` |
| Git 未登记 raw 参数组合 | fail closed，不执行 Git | `test_git_unknown_raw_policy_denied` |
| LSP definition/reference 返回未授权位置 | 过滤该位置且不泄露路径 | `test_lsp_filters_external_locations` |
| 无路径 diagnostics 包含失效授权文件 | 过滤该文件诊断 | `test_lsp_filters_ungranted_open_documents` |
| session grant 后 clear | 授权消失 | `test_clear_session_grants` |
| persistent grant 保存失败 | 文件、Settings._data 和 PermissionService 都保持原值 | `test_persistent_grant_save_failure_rolls_back` |
| 原子替换成功 | canonical 字段和 legacy alias 同时更新 | `test_replace_access_grants_atomic_success` |
| 新旧 writable dirs 冲突 patch | Settings API 拒绝且不部分写入 | `test_settings_rejects_alias_conflict` |
| 切换非 CUSTOM preset | 删除用户 session/persistent grants 与五个配置字段 | `test_preset_clears_path_grants` |
| 同一 Settings patch 含非 CUSTOM preset 和 grant 字段 | 整体拒绝，模式与授权均不改变 | `test_settings_rejects_non_custom_with_grants` |
| 子代理使用父会话已有 grant | 放行 | `test_subagent_inherits_effective_grants` |
| 子代理尝试扩大 grant | 阻止 | `test_subagent_cannot_add_grant` |
| 权限选择 allow_other=False | helper 不追加 Other | `test_permission_choice_has_no_other` |
| clarify/checkpoint allow_other=True | 追加 Other 并可 fallback 到文本 | `test_open_choice_supports_other` |
| selected/anchor 经过 gateway | request DTO 保留字段 | `test_gateway_choice_preserves_selected_anchor` |
| 权限选择返回 free_text | 按 deny 处理 | `test_permission_free_text_is_denied` |

## Test Plan

下列命令全部使用仓库中现有测试文件或目录；新增用例写入对应现有文件，避免测试计划引用不存在的测试模块。

| Area | Command |
|---|---|
| Access resolution / file approvals | `./test.py --backend -- src/tests/test_tools/test_resolve_safe.py src/tests/test_tools/file/test_read.py src/tests/test_tools/file/test_read_write.py src/tests/test_tools/file/test_write_file.py` |
| Engine / grant lifecycle / runtime DATA_DIR | `./test.py --backend -- src/tests/test_agent/test_permission.py src/tests/test_agent/test_permission_append.py` |
| Tool interaction helper | `./test.py --backend -- src/tests/test_tools/test_make_interact_callback.py` |
| Config migration / atomic persistence | `./test.py --backend -- src/tests/test_config/test_config.py src/tests/test_config/test_config_advanced.py` |
| Settings API / choice DTO | `./test.py --backend -- src/tests/test_ui/gateway/test_gateway_v2_dispatch.py src/tests/test_ui/gateway/test_gateway_headless_frontend.py` |
| Bash external paths | `./test.py --backend -- src/tests/test_tools/bash/test_tool.py src/tests/test_tools/bash/test_router_safety.py` |
| PowerShell external paths | `./test.py --backend -- src/tests/test_tools/test_powershell_tool.py` |
| Git policy and external paths | `./test.py --backend -- src/tests/test_tools/test_git_tool_raw_permissions.py src/tests/test_tools/test_git_tool_destructive.py src/tests/test_tools/test_git_tool_structured.py` |
| LSP input/output filtering | `./test.py --backend -- src/tests/test_lsp/test_lsp.py src/tests/test_lsp/test_lsp_advanced.py` |
| Child-agent grant inheritance | `./test.py --backend -- src/tests/test_agent/graph/test_subagent_runner.py src/tests/test_agent/graph/test_parallel_subagents.py` |
| Focused regression | `./test.py --backend -- src/tests/test_agent/test_permission.py src/tests/test_tools/test_resolve_safe.py src/tests/test_tools/test_make_interact_callback.py src/tests/test_tools/file/ src/tests/test_tools/bash/ src/tests/test_tools/test_powershell_tool.py src/tests/test_tools/test_git_tool_raw_permissions.py src/tests/test_lsp/ src/tests/test_config/test_config_advanced.py src/tests/test_ui/gateway/test_gateway_v2_dispatch.py` |

## Decisions

| Decision | Alternatives | Rationale |
|---|---|---|
| runtime/session/persistent 三来源 grants | 仅 session/persistent | 隔离应用内置路径与用户授权，避免 DATA_DIR 被误写入持久化配置 |
| ToolContext 使用 getter callback | 共享可变 list | 避免 Pydantic 复制导致授权状态不同步 |
| Engine 三态结果 | Engine hard deny 或完全跳过 | 既保持前置检查，又允许工具路径审批可达 |
| move 两端均为 write | src=read, dest=write | move 会删除源文件 |
| shell 外部路径统一 write | 按命令 read/write 分类 | shell 静态分析不可靠，保守规则更安全 |
| shell 只支持静态外部路径 | 尝试解析所有动态 shell | 无法可靠确定最终路径时必须 fail closed |
| resolve_safe 只接 effective writable dirs | 包装为完整 read grants | 保持旧调用点兼容，同时防止使用只读授权写文件 |
| persistent grant 使用事务回调 | PermissionService 直接修改 Settings | 解耦模块并保证磁盘、Settings._data 与服务内存同时成功或同时失败 |
| 非 CUSTOM preset 删除用户路径 grants | 暂时停用并保留配置 | 与现有 preset 清理语义一致，避免隐藏授权在切回 CUSTOM 后恢复 |
| canonical 字段优先，冲突拒绝 patch | 两字段直接 union | 避免旧值静默扩大授权 |
| Other 由 interaction helper 生成 | 每个前端各自生成 | 保证封闭权限选择不会被前端差异绕过，避免重复选项 |
| Git 使用同步静态策略 + 异步授权感知补全 | Engine 同步执行 Git discovery 或各层重复实现 | 共享分类与参数策略，同时避免在授权前访问 Git 元数据 |
| Git 外部仓库只允许目录授权 | 单文件授权 | Git 会访问工作树、元数据、索引和对象库 |
| 外部 Git path 必须直指 worktree root/bare git dir | 自动向上搜索仓库根 | 禁止 discovery 越过已授权入口读取父目录 |
| Git 元数据、对象库与配置 include 分别授权 | 只授权工作树 | linked worktree、alternates 和 config include 可位于工作树之外 |
| Git 使用受控环境并禁用隐式可执行扩展 | 继承用户环境和仓库执行配置 | 防止 hooks、helpers、filters 和环境变量绕过路径模型 |
| LSP 输入与输出都做 read 过滤 | 只检查输入文件 | definition/references/diagnostics 可能返回其他文件路径 |

## Risks / Trade-offs

| Risk | Impact | Mitigation |
|---|---|---|
| 调用点迁移遗漏 | 权限行为不一致 | Engine 仅对明确标记为 approval-capable 的工具 DEFER；其余 fail closed |
| 五选一审批较复杂 | 用户认知负担 | 默认选择 session file，文案显示规范化路径 |
| shell 规则偏严格 | 合理外部只读命令也需 write grant | 使用 read/grep 工具，或显式授予 shell writable access |
| shell 对未知命令和隐式配置路径分析不完备 | 外部访问可能无法可靠识别 | 只允许已登记的静态路径策略；未知或动态组合 fail closed |
| Git 策略注册表维护成本 | 新子命令默认不可用 | 每个新增策略同时提供分类、路径提取和安全测试 |
| persistent 配置增加 | settings 体积增长 | 去重、排序，仅显式持久化时写入 |
| 事务写入的平台差异 | fsync/replace 在不同文件系统上行为不同 | 临时文件必须与目标同目录；针对 macOS/Linux/Windows 增加失败注入测试 |
| 新文件路径存在 symlink/TOCTOU race | 检查后最终落点可能变化 | 解析现存父目录、副作用前二次检查；文档明确该方案缩小但不能完全消除竞争窗口 |
| 双字段兼容期复杂 | 配置可能冲突 | canonical 优先、API 拒绝冲突、写回保持相同 |
| LSP 过滤改变结果完整性 | definition/reference 结果可能减少 | 仅返回已授权位置；不暴露被过滤路径或数量 |

## Forbidden Changes

- 不把 move src 降级为 read
- 不把 Engine 的 DEFER_TO_TOOL 当作 allow
- 不让 shell 使用 readable grants
- 不让 shell 或 Git 的未知路径组合默认放行
- 不让外部 Git path 通过向上搜索越过已授权入口；外部路径必须直指 worktree root 或 bare git dir
- 不让 Git 只校验工作树而跳过 git dir、common dir、index、对象库和配置 include
- 不让 Git 继承影响路径/配置来源的环境变量，或执行未被策略禁用的 hooks/helpers/filters
- 不允许 Git capability 与静态路径策略使用不同逻辑源
- 不让 LSP 返回未授权位置或其路径信息
- 不让 `resolve_safe` 消费新 readable grants
- 不依赖 Pydantic 模型字段保持 list 对象引用
- 不允许持久化失败后只更新磁盘、Settings._data 或 PermissionService 中的一部分
- 不在权限审批中启用 Other/free-text
- 不在 Gateway/TUI 重复生成 Other 选项
- 不在非 CUSTOM preset 下保留用户 session/persistent path grants
- 不修改 glob 的 workspace-only 范围
- 不修改 BASIC_RULES 的工具级策略
- 不改变 danger-full-access 和 read-only 的模式定义
