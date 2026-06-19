# Git 工具增强 — 技术设计文档

> **Status: Done** — 实现已完成，代码 + 测试已合入。本文档记录最终实现细节，含与初始设计的偏差。

## Context

voidx 的 `git` 工具（`src/voidx/tools/git.py`）提供 9 个命令的结构化 JSON 输出，相比 Claude Code 将所有 git 操作走 Bash 的方案，在安全性、可审计性和输出结构化方面有架构优势。但功能覆盖存在明显缺口——切换分支、查看 commit 详情、标签管理等高频操作缺失，agent 只能退回 Bash 执行，丢失了结构化输出和路径沙箱保护。

本文档基于与 Claude Code 的对比分析，定义 git 工具的功能增强和现有命令优化方案。

## Goals and Non-Goals

### Goals

- 补齐 P0 功能缺口：`switch`（切换/创建分支）、`show`（查看 commit 详情）、`branch` 创建/删除
- 补齐 P1 功能：`tag` 管理、`stash` 操作、`status` 返回当前分支、`diff` 双 ref 对比、`commit` hook 结果捕获
- 保持现有架构一致性：Pydantic Args 模型、`_result()` 统一输出、路径沙箱、权限分级
- 更新 `bash_router` 路由提示和权限规则

### Non-Goals

- `push` / `pull` — sandbox 已阻止 push；pull 有合并冲突风险，保持走 bash
- `merge` / `rebase` / `cherry-pick` — 冲突处理复杂度高，本次不纳入
- `worktree` 管理 — 独立迭代，不阻塞本次增强
- `fetch` — 只读安全但需网络，优先级低
- PR 创建（`gh pr create`）— 非 git 核心操作，走 bash 即可


## Affected Files

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `src/voidx/tools/git.py` | 修改 | 新增 8 个命令实现 + 4 个现有命令优化 + Args 模型扩展 |
| `src/voidx/tools/bash_router.py` | 修改 | 从 `_UNHINTABLE_GIT_SUBCOMMANDS` 移除 `switch` 和 `stash`；新增 switch/show/branch_create/branch_delete/tag/stash 的路由提示函数 |
| `src/voidx/permission/rules.py` | 修改 | `_is_read_only_git_tool_command` 增加 show/tag_list；`GIT_READ_COMMANDS`/`GIT_WRITE_COMMANDS` 同步 |
| `tests/test_tools/test_git.py` | 修改 | 新增命令的单元测试 |

## Architecture

### 命令分类扩展

```
GIT_READ_COMMANDS (现有)
├── status          ← 优化：返回当前分支名
├── diff            ← 优化：支持双 ref 对比
├── log             ← 优化：增加 until 参数
├── blame
├── branch_list
└── remote_list

GIT_READ_COMMANDS (新增)
├── show            ← 新增：查看 commit 详情
└── tag_list        ← 新增：列出标签

GIT_WRITE_COMMANDS (现有)
├── add
├── commit          ← 优化：捕获 hook 输出
└── restore

GIT_WRITE_COMMANDS (新增)
├── switch          ← 新增：切换/创建分支
├── branch_create   ← 新增：创建分支
├── branch_delete   ← 新增：删除分支
├── tag_create      ← 新增：创建标签
├── tag_delete      ← 新增：删除标签
├── stash_push      ← 新增：保存工作区
└── stash_pop       ← 新增：恢复工作区
```

### 权限映射

| 命令 | 权限类别 | 说明 |
|------|---------|------|
| show | GIT_READ | 只读，查看 commit |
| tag_list | GIT_READ | 只读，列出标签 |
| switch | GIT_WRITE | 切换分支改变工作区 |
| branch_create | GIT_WRITE | 创建分支 |
| branch_delete | GIT_WRITE | 删除分支（需确认） |
| tag_create | GIT_WRITE | 创建标签 |
| tag_delete | GIT_WRITE | 删除标签 |
| stash_push | GIT_WRITE | 保存工作区状态 |
| stash_pop | GIT_WRITE | 恢复工作区状态 |

## Data Model

### 新增 Args 模型

```
GitSwitchArgs
├── branch: str (必填，目标分支名)
├── create: bool = False (是否创建新分支，等价 git switch -c)
└── start_point: str = "" (新分支的起点 ref，默认 HEAD)

GitShowArgs
├── ref: str = "HEAD" (commit hash / ref / range)
├── stat: bool = False (仅显示文件级统计，不含 diff hunks)
└── pathspec: list[str] = [] (限定文件范围)

GitBranchCreateArgs
├── name: str (必填，分支名)
└── start_point: str = "" (起点 ref，默认 HEAD)

GitBranchDeleteArgs
├── name: str (必填，分支名)
└── force: bool = False (强制删除，等价 -D)

GitTagListArgs
├── pattern: str = "" (通配符过滤，如 "v2.*")
└── sort: str = "" (排序字段，如 "-version:refname" 按版本号降序、"-creatordate" 按创建时间降序；空则默认字母序)

GitTagCreateArgs
├── name: str (必填，标签名)
├── ref: str = "" (打标签的目标 ref，默认 HEAD)
├── message: str = "" (附注标签消息，非空则创建附注标签)
└── force: bool = False (覆盖已有同名标签)

GitTagDeleteArgs
├── name: str (必填，标签名)

GitStashPushArgs
├── message: str = "" (stash 描述，等价 git stash push -m)
├── pathspec: list[str] = [] (仅 stash 指定文件)

GitStashPopArgs
├── index: int = 0 (stash@{n}，默认最新，ge=0)
└── keep: bool = False (pop 后保留 stash 条目，等价 git stash apply)
```

### 现有 Args 模型变更

```
GitStatusArgs (变更)
├── pathspec: list[str] (不变)
└── [输出变更] data.branch: str — 当前分支名

GitDiffArgs (变更)
├── cached: bool (不变)
├── pathspec: list[str] (不变)
├── ref: str (不变，向后兼容，单 ref)
└── base: str = "" (新增，对比基准 ref，如 "main"；与 ref 组合为 git diff base ref)

GitLogArgs (变更)
├── limit: int (不变)
├── path: str (不变)
├── author: str (不变)
├── since: str (不变)
└── until: str = "" (新增，结束日期过滤)

GitCommitArgs (变更)
├── message: str (不变)
├── paths: list[str] (不变)
└── [输出变更] data.hook_output: str — pre-commit 等 hook 的 stdout/stderr
```

### GitArgs 联合模型扩展

在 `GitArgs` 中增加所有新命令的字段，保持现有模式：

```
GitArgs (新增字段)
├── branch: str = ""          # switch 的目标分支
├── create: bool = False      # switch -c
├── start_point: str = ""     # switch / branch_create 的起点
├── stat: bool = False        # show 的统计模式
├── base: str = ""            # diff 的基准 ref
├── until: str = ""           # log 的结束日期
├── name: str = ""            # branch_create/delete, tag_create/delete
├── force: bool = False       # branch_delete, tag_create/delete
├── pattern: str = ""         # tag_list 的通配符
├── sort: str = ""            # tag_list 的排序字段
├── index: int = 0            # stash_pop 的 stash 索引
└── keep: bool = False        # stash_pop 保留条目
```


> **技术债说明**：`GitArgs` 扁平联合模型在 17 个命令下已达 ~22 个字段，存在字段名冲突风险（如 `name` 被 branch 和 tag 命令共用）和无类型安全（拼写错误静默默认）问题。本次迭代保持现有模式以控制变更范围，但应在后续迭代中迁移为 discriminated union：`GitArgs = Annotated[Union[GitStatusArgs, GitDiffArgs, ...], Field(discriminator='command')]`。实现时需注意 LLM schema 生成的兼容性——discriminated union 的 JSON Schema 可能比扁平模型更复杂，需验证 `model_to_json_schema()` 输出。

### GitInput.command 扩展

```python
command: Literal[
    # 现有
    "status", "diff", "log", "blame", "branch_list", "remote_list",
    "add", "commit", "restore",
    # 新增
    "show", "switch",
    "branch_create", "branch_delete",
    "tag_list", "tag_create", "tag_delete",
    "stash_push", "stash_pop",
]
```

## API Contract

### switch — 切换/创建分支

- **底层命令**: `git switch [--create] <branch> [<start_point>]`
- **Request**: `GitSwitchArgs`
- **Response**:
  ```json
  {
    "data": {
      "branch": "feature-auth",
      "created": false,
      "previous_branch": "main"
    }
  }
  ```
- **Errors**: `not_a_git_repository`, `branch_not_found`（分支不存在且 `create=false`）, `dirty_conflict`（有未提交更改且 switch 失败，data 含 `dirty_files` 列表和 `suggestion: "stash_push before switching branches"`）

> **实现偏差**：原始设计要求"脏文件冲突时预检查并返回冲突文件列表"。实际实现采用"先尝试 switch，失败时返回 `dirty_conflict`"策略——脏文件不冲突时 switch 正常成功（git 自身允许），冲突时 git 报错被捕获为结构化 `dirty_conflict`。这比预检查更实用，因为预检查冲突需要 `git merge-tree` 等复杂操作。

### show — 查看 commit 详情

- **底层命令**: 多次 git 调用分离元数据与 diff：
  1. `git show --format=%H%x1f%an%x1f%ad%x1f%s%x1f%P --no-patch [<ref>]` — 获取 commit 元数据
  2. `stat=true` 时：`git show --format= --numstat [<ref>]` + `git show --format= --shortstat [<ref>]` — 获取文件列表和增删统计
  3. `stat=false` 时：`git show --format= --unified=3 [<ref>]` + `git show --format= --numstat [<ref>]` — 获取 diff hunks 和增删统计
- **实现策略**: 两次 git 调用分离元数据与 diff。`git show` 默认在 diff 前输出 commit header，直接解析边界模糊。通过第一次调用 `--format=... --no-patch` 单独获取元数据，后续调用 `--format=""` 抑制 header，diff 部分可复用 `_diff_hunks_by_path()` 解析。`stat=true` 模式使用 `--numstat` + `--shortstat` 替代 `--stat`，避免解析 locale 依赖的文本格式
- **Merge commit**: merge commit 的 diff 为 combined diff 格式（多父），`_diff_hunks_by_path()` 不支持。`stat=true` 时正常返回文件统计；`stat=false` 时对 merge commit 返回 `hunks: []`、`truncated: false`，并在 data 中增加 `"merge": true` 标记
- **Request**: `GitShowArgs`
- **Response**:
  ```json
  {
    "data": {
      "hash": "abc1234",
      "author": "user",
      "date": "2026-06-19T10:00:00+08:00",
      "message": "feat: add auth",
      "parents": ["def5678"],
      "merge": false,
      "files_changed": ["src/auth.py", "tests/test_auth.py"],
      "stats": { "additions": 42, "deletions": 7 },
      "hunks": ["@@ ..."],
      "truncated": false
    }
  }
  ```
- **Errors**: ref 不存在

### branch_create — 创建分支

- **底层命令**: `git branch <name> [<start_point>]`
- **Request**: `GitBranchCreateArgs`
- **Response**:
  ```json
  {
    "data": {
      "name": "feature-auth",
      "start_point": "main",
      "hash": "abc1234"
    }
  }
  ```

### branch_delete — 删除分支

- **底层命令**: `git branch [-D] <name>`
- **Request**: `GitBranchDeleteArgs`
- **Response**:
  ```json
  {
    "data": {
      "name": "old-feature",
      "force": false
    }
  }
  ```
- **Errors**: 分支不存在, 分支未合并（需 `force=true`）, 不能删除当前分支

### tag_list — 列出标签

- **底层命令**: `git tag -l --format='%(refname:short) %(objectname:short)' [-l <pattern>] [--sort=<sort>]`（单次调用获取名称和 hash，避免 N+1 查询）
- **Request**: `GitTagListArgs`
- **Response**:
  ```json
  {
    "data": {
      "entries": [
        {"name": "v1.0.0", "hash": "abc1234"},
        {"name": "v1.1.0", "hash": "def5678"}
      ]
    }
  }
  ```

### tag_create — 创建标签

- **底层命令**: `git tag [-a -m <message>] [-f] <name> [<ref>]`
- **Request**: `GitTagCreateArgs`
- **Response**:
  ```json
  {
    "data": {
      "name": "v2.0.0",
      "ref": "HEAD",
      "hash": "abc1234",
      "annotated": true
    }
  }
  ```

### tag_delete — 删除标签

- **底层命令**: `git tag -d <name>`
- **Request**: `GitTagDeleteArgs`
- **Response**:
  ```json
  {
    "data": {
      "name": "v0.9.0-rc"
    }
  }
  ```

### stash_push — 保存工作区

- **底层命令**: `git stash push [-m <message>] [-- <pathspec>...]`，成功后执行 `git stash show --name-only stash@{0}` 获取 `files_stashed`
- **路径沙箱**: `pathspec` 非空时必须通过 `_pathspecs()` 校验，与 `add`/`commit`/`restore` 一致；`pathspec` 为空时不做路径校验（stash 整个工作区是合法操作）
- **Request**: `GitStashPushArgs`
- **Response**:
  ```json
  {
    "data": {
      "index": 0,
      "message": "WIP on main: abc1234 feat: add auth",
      "files_stashed": ["src/auth.py", "tests/test_auth.py"]
    }
  }
  ```

> **实现偏差**：原始设计用 `git diff --cached --name-only` 获取 `files_stashed`，但 stash push 后工作区已重置，`--cached` 返回空。改为 `git stash show --name-only stash@{0}` 从 stash 条目本身获取文件列表。

### stash_pop — 恢复工作区

- **底层命令**: `git stash pop|apply stash@{<index>}`。pop 前执行 `git diff --name-only` 记录 `pre_dirty` 集合，pop 后再次执行取 `post_dirty`，差集 `post_dirty - pre_dirty` 即为 `files_restored`
- **Request**: `GitStashPopArgs`
- **Response**:
  ```json
  {
    "data": {
      "index": 0,
      "applied": true,
      "kept": false,
      "conflicts": [],
      "files_restored": ["src/auth.py", "tests/test_auth.py"]
    }
  }
  ```
- **Errors**: stash 索引不存在, 合并冲突（返回 `conflicts` 列表）

> **实现偏差**：原始设计用 `git diff --name-only` 直接获取 `files_restored`，但这会包含 pop 前已有的脏文件。改为 pop 前后差集计算，仅返回 stash 实际恢复的文件。

### 现有命令变更

#### status — 增加当前分支

- **底层命令**: 额外执行 `git symbolic-ref --short HEAD`（比 `rev-parse --abbrev-ref` 更快，仅读 symbolic ref；detached HEAD 时返回非零退出码，此时 `branch` 为空字符串）
- **Response 变更**: `data.branch: str`（当前分支名，detached HEAD 时为空）

#### diff — 双 ref 对比

- **底层命令**: `git diff [<base> <ref>] [-- <pathspec>...]`
- **逻辑**: `base` 非空时，构造 `git diff <base> <ref>`（空格分隔，非三点语法）；`base` 为空时保持现有行为。注意：`base...ref`（三点）是对称差，`base..ref`（双点）是 log 范围语法，两者均非 diff 的双 ref 对比意图。`git diff <base> <ref>` 直接对比两个 ref 的树差异，语义最准确
- **Response**: 不变

#### log — until 参数

- **底层命令**: `git log ... --until=<until>`
- **Response**: 不变

#### commit — hook 输出捕获

- **底层命令**: 不变
- **Response 变更**: `data.hook_output: str`（hook 的 stdout 追加到 stderr 之后，以 `\n---\n` 分隔；无 hook 时为空字符串。截断阈值 `HOOK_OUTPUT_MAX_CHARS = 4000`，超出时截断尾部并追加 `[truncated]`）

## Security Considerations

### switch 安全约束

`switch` 是所有新命令中安全敏感度最高的——它改变整个工作树内容和 HEAD。以下约束必须在实现中强制执行：

1. **分支名校验**：拒绝包含 `..`、`@`、`~`、`^`、`:`、`\`、空格、`.lock` 后缀的分支名，防止 ref 注入和路径穿越。校验在构造 git 命令前执行，使用两层正则：`_BRANCH_NAME_RE = r"^(?!\.)(?!-)[a-zA-Z0-9/_-]+(\.[a-zA-Z0-9/_-]+)*$"` 确保合法字符，`_BRANCH_NAME_DENY = r"\.\.|[@~^:\\\s]|\.lock$"` 拒绝危险模式
2. **脏工作区检查**：switch 前先执行 `git status --porcelain` 检查是否有未提交更改。若有脏文件，仍尝试 `git switch`——成功则正常返回（脏文件不冲突时 git 允许 switch），失败则返回 `ok=false`、`error="dirty_conflict"`，附带 `dirty_files` 列表和 `suggestion: "stash_push before switching branches"`
3. **工作树一致性**：switch 成功后，后续命令的路径解析基于新的工作树状态。`_discover_repo()` 返回的 `repo_root` 不变（同一仓库），但 `_pathspecs()` 解析的文件内容可能已变。无需特殊处理——每次 tool 调用都是独立的 git 子进程，自然反映最新工作树状态
4. **start_point 校验**：`start_point` 仅在 `create=true` 时有效，且必须为合法 ref（通过 `git rev-parse --verify` 校验）

### stash_push 路径沙箱

`stash_push` 的 `pathspec` 参数必须通过 `_pathspecs()` 校验，与 `add`/`commit`/`restore` 保持一致。未指定 pathspec 时不做路径校验（stash 整个工作区是合法操作）。

### branch_delete / tag_delete

删除操作通过权限审批（`GIT_WRITE` → `ask`）提供用户确认层。`force=true` 不会绕过权限审批，仅改变 git 命令参数（`-D` vs `-d`）。

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| switch 时有未提交更改冲突 | 返回 `ok=false`，error="dirty_conflict"，data 含 `dirty_files` 列表和 `suggestion: "stash_push before switching branches"` |
| branch_delete 未合并分支 | 返回 `ok=false`，error="branch_not_merged"，提示 force=true |
| branch_delete 当前分支 | 返回 `ok=false`，error="cannot_delete_current_branch" |
| stash_pop 合并冲突 | 返回 `ok=false`，data.conflicts 包含冲突文件，stash 保留 |
| show ref 不存在 | 返回 `ok=false`，error="ref_not_found" |
| tag_create 已存在 | force=false 时返回 `ok=false`，error="tag_already_exists" |
| switch 到不存在的分支 | create=false 时返回 `ok=false`，error="branch_not_found" |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| switch 而非 checkout | 复用 checkout 语义 | `git switch` 是 Git 2.23+ 的推荐命令，语义更清晰；checkout 职责过载 |
| branch_create/delete 拆分 | 复用 branch + 子参数 | 与现有 branch_list 命名对齐，避免单个命令参数膨胀；权限粒度更细 |
| tag_list/tag_create/tag_delete 拆分 | 合并为 tag + 子命令 | 同上，与 branch_list/branch_create/branch_delete 模式一致 |
| stash_push/stash_pop 拆分 | 合并为 stash + 子命令 | 同上；且 push 是写入、pop 也是写入，但 list 是只读，拆分更清晰 |
| diff 双 ref 用 base 参数 | 新增 head 参数 | `base` 语义更直观（对比基准），`ref` 已有（当前端点），向后兼容 |
| commit hook 输出合并到 data | 单独 hook_output 字段 | 不改变现有 data 结构，仅追加字段，向后兼容 |
| show 分离元数据与 diff | 直接解析 `git show` 完整输出 | `git show` 输出混合了 commit header 和 diff body，解析边界模糊且 merge commit 格式不同。两次调用（`--no-patch` 取元数据 + `--format=""` 取 diff）更可靠，diff 部分可复用 `_diff_hunks_by_path()` |
| stash_pop 的 keep 参数 | 拆为 stash_pop + stash_apply | 一个参数控制行为，减少命令数量；apply = pop + keep |
| show stat 用 --numstat + --shortstat | 用 --stat | `--stat` 输出格式依赖 locale，解析脆弱。`--numstat` 机器可读，`--shortstat` 提供增删总数 |
| tag_list 单次调用获取 hash | N+1 查询（每 tag 一次 rev-list） | `--format='%(refname:short) %(objectname:short)'` 一次获取所有标签名和 hash，避免 O(N) 子进程 |
| stash_push 用 stash show 获取文件列表 | 用 diff --cached | stash push 后工作区已重置，`--cached` 返回空。`git stash show --name-only stash@{0}` 从 stash 条目获取 |
| stash_pop 用前后差集计算 files_restored | 用 diff --name-only | `diff --name-only` 包含所有脏文件，差集仅返回 stash 实际恢复的文件 |
| switch 脏工作区先尝试再报错 | 预检查冲突文件 | 预检查需 `git merge-tree` 等复杂操作；让 git 自身处理更可靠，不冲突时 switch 正常成功 |
| 分支名两层正则校验 | 单一正则 | `_BRANCH_NAME_RE` 确保合法字符，`_BRANCH_NAME_DENY` 拒绝 `..`、`@`、`.lock` 等危险模式，比单一正则更清晰 |

## Open Questions

- [ ] `switch` 是否需要 `--detach` 模式（分离 HEAD）？当前设计不包含，可后续补充
- [x] `stash_list` 是否纳入本次？→ **不纳入**。只读命令，实现简单，但使用频率低于 push/pop，可在后续迭代独立添加
- [x] `tag_list` 是否需要返回标签的 commit message（附注标签）？→ **不返回**。会增加输出体积，agent 可通过 `show` 命令查看附注标签详情
- [x] `branch_delete` 是否需要二次确认机制（除 force 外）？→ **不需要额外机制**。权限审批（`GIT_WRITE` → `ask`）已提供用户确认层，force 仅控制 git 参数
- [x] `show` 的 hunk 截断阈值是否复用 `DIFF_HUNK_MAX_CHARS`？→ **复用**。show 的 diff 输出与 diff 命令格式相同，无需独立常量
