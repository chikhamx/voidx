# Git 工具裸传重构 — 技术设计文档

## Context

当前 git 工具的 schema 是 `command`（枚举）+ `args`（`GitArgs` 巨型模型，33 个字段全部无 description）。LLM 调用时频繁出错，根本原因：

1. 33 个字段无描述，LLM 不知道每个字段对应哪个子命令
2. 字段冲突（`name`/`force`/`all` 在不同命令语义不同）无法从 schema 推断
3. 必填/互斥规则只在 `model_validator` 里，schema 层面不可见

LLM 对原生 git 命令行语法远比自定义字段名熟悉。重构为裸传形式：`path` + `args` 字符串，让 LLM 直接写 git 参数，runtime 内部做白名单路由和结构化输出。

## Goals and Non-Goals

### Goals

- git 工具 schema 简化为 `path`（可选）+ `args`（字符串），LLM 零学习成本
- runtime 内部白名单路由：核心读命令返回结构化 JSON，其余返回原生 stdout
- 保留安全边界：读/写命令分类，写命令仍需 approval
- 保留路径安全验证：pathspec 仍在 workspace 范围内

### Non-Goals

- 不实现完整的 git 参数解析器（不追求覆盖所有 flag 组合的结构化输出）
- 不改变 approval 策略本身（读命令免审批，写命令需审批）
- 不重构 bash 工具本身

## Architecture

```
LLM 调用: git(path=".", args="status --porcelain")
                    │
                    ▼
            GitTool.execute
                    │
        ┌───────────┴───────────┐
        │ shlex.split(args)     │
        │ 提取 subcommand       │
        └───────────┬───────────┘
                    │
        ┌───────────┴───────────┐
        │ 白名单路由            │
        │ read: status/diff/log │
        │ /blame/show/branch/   │
        │ remote/tag            │
        └───────┬───────┬───────┘
                │       │
     命中白名单 │       │ 未命中
                ▼       ▼
        结构化处理器   原生执行
        (复用现有      _run_git()
         _parse_*      返回 stdout
         解析逻辑)     + returncode
```

### 白名单分类

**结构化读命令**（返回解析后的 JSON）：

| 子命令 | 结构化处理器 | 解析输出 |
|--------|-------------|---------|
| `status` | `_git_status` | `{entries, branch}` |
| `diff` | `_git_diff` | `{entries: [{path, additions, deletions, hunks}]}` |
| `log` | `_git_log` | `{entries: [{hash, author, date, message, files_changed}]}` |
| `blame` | `_git_blame` | `{entries: [{line, commit, author, date, content}]}` |
| `show` | `_git_show` | `{hash, author, date, message, files_changed, stats, hunks}` |
| `branch` (无写参数) | `_git_branch_list` | `{entries: [{name, current, upstream, ahead, behind}]}` |
| `remote` | `_git_remote_list` | `{entries: [{name, url, type}]}` |
| `tag` (无写参数) | `_git_tag_list` | `{entries: [{name, hash}]}` |

**原生执行命令**（返回 stdout + returncode）：

所有不在结构化白名单内的命令，包括：
- 读命令：`ls-files`、`rev-parse`、`describe`、`shortlog`、`reflog` 等
- 写命令：`add`、`commit`、`push`、`pull`、`fetch`、`merge`、`rebase`、`stash`、`restore`、`switch`、`checkout` 等

**拒绝命令**（直接报错，不执行）：

`reset --hard`、`clean -x`、`filter-branch`、`reflog expire`、`gc --prune=now` 等破坏性操作。

## Data Model

### 新 GitInput

```
GitInput
├── path: str (default="", 可选执行路径，空则用 workspace)
└── args: str (min_length=1, git 子命令及参数)
```

### ToolResult 输出格式

**结构化命令**（不变，复用现有 `_result()`）：
```json
{
  "ok": true,
  "command": "status",
  "repo_root": "...",
  "workspace": "...",
  "data": { "entries": [...], "branch": "main" },
  "error": ""
}
```

**原生命令**（新格式）：
```json
{
  "ok": true,
  "command": "add",
  "repo_root": "...",
  "workspace": "...",
  "data": { "stdout": "...", "stderr": "...", "returncode": 0 },
  "error": ""
}
```

## API Contract

### GitTool.execute

- **Signature**: `async def execute(self, args: dict, ctx: ToolContext) -> ToolResult`
- **Request**: `{"path": ".", "args": "status --porcelain"}`
- **Response**: `ToolResult` with JSON payload (见上)
- **Errors**:
  - `not_a_git_repository`: workspace 不在 git 仓库内
  - `invalid_args`: shlex 解析失败或子命令为空
  - `command_denied`: 子命令在拒绝列表中
  - `unsafe_path`: path 参数逃逸出 workspace
  - git 原生错误: stdout/stderr + returncode

### 参数解析规则

结构化处理器从 args 字符串提取参数的方式：

| 子命令 | 提取逻辑 |
|--------|---------|
| `status` | `--` 后的 token 为 pathspec |
| `diff` | `--cached`/`--staged` → cached; 第一个非 flag token 为 ref; `--` 后为 pathspec |
| `log` | `-n N` 或 `-N` → limit; `--author=` → author; `--since=` → since; `--until=` → until; `--` 后为 path |
| `blame` | `-L start,end` → start/end; 第一个非 flag token 为 path |
| `show` | 第一个非 flag token 为 ref; `--stat` → stat; `--` 后为 pathspec |
| `branch` | 无参数或 `-a`/`--all` → list; `-d`/`-D` → delete; 否则 → create |
| `remote` | `-v`/`--verbose` → list |
| `tag` | 无参数或 `-l` → list; `-d` → delete; 否则 → create |

未识别的 flag 传给结构化处理器时，处理器忽略并使用默认值，保证健壮性。

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| shlex 解析失败 | 返回 `invalid_args` 错误 |
| 子命令为空 | 返回 `invalid_args` 错误 |
| 子命令在拒绝列表 | 返回 `command_denied` 错误，不执行 |
| path 逃逸 workspace | 返回 `unsafe_path` 错误 |
| 结构化处理器解析失败 | 回退为原生 stdout 返回 |
| git 命令执行失败 | 返回 returncode + stderr |
| 命令超时 | 返回 timeout 错误（现有逻辑） |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 用 shlex 解析 args 字符串 | 用 shell=True 直接执行 | shlex 解析后可白名单校验子命令，避免 shell 注入 |
| 结构化处理器从字符串提取参数 | 保留结构化 args 子模型 | 裸传的核心优势就是 LLM 不用学自定义字段，结构化提取由 runtime 承担 |
| 未命中白名单返回原生 stdout | 拒绝所有非白名单命令 | 覆盖面太低，LLM 需要执行 `ls-files`/`rev-parse` 等命令 |
| 保留现有 _parse_* 解析逻辑 | 重写解析器 | 现有解析逻辑经过 80 个测试验证，可靠 |
| branch/tag 根据参数区分读写 | 拆成独立子命令 | 减少白名单复杂度，一个子命令多种模式更符合 git 习惯 |

## Open Questions

- [x] `checkout` 命令是否允许？**已确认：允许，标记为写命令（需 approval）。**
- [x] `stash` 子命令的读写性？**已确认：`stash list` 走结构化读，其余 `stash` 子命令走原生写。**
