# Git 远程与合并命令支持 — 技术设计文档

## Context

当前 git 工具仅支持本地操作（add/commit/restore/switch/branch/tag/stash），不支持任何远程交互或合并操作。agent 若需 push/pull/fetch/merge/rebase，只能通过 bash 执行，但 sandbox 在 `workspace-write` 模式下会拦截 bash 中的 git push，要求 `danger-full-access`。这导致：

1. **权限管控不统一** — bash 远程操作走 sandbox 拦截，git 工具的其他写操作走 `git_write` 审批
2. **安全门槛过高** — push 必须完全放开 sandbox，无法细粒度控制
3. **缺少结构化输出** — bash 只返回原始文本，无法解析操作结果
4. **缺少参数校验** — bash 无分支名校验、无参数约束，容易误操作

## Goals and Non-Goals

### Goals

- 在 git 工具中添加 `push`、`pull`、`fetch`、`merge`、`rebase` 五个命令
- 所有新命令统一走 `git_write` 审批流程
- 提供结构化输出（remote、branch、冲突信息、commit 范围等）
- 为远程操作设置合理的网络超时（60s）
- 复用现有分支名校验（`_BRANCH_NAME_RE`）

### Non-Goals

- 不支持 `--force-with-lease`（语义复杂，首期简化为 force/非 force）
- 不支持 `--tags` 等细粒度选项（可后续迭代）
- 不修改 sandbox 对 bash git 操作的拦截逻辑（保持现有安全边界）
- 不支持 `cherry-pick`、`reset`（范围不同，可后续添加）

## Architecture

```
Agent 调用 git tool (command="push"|"pull"|"fetch"|"merge"|"rebase")
  → GitTool.execute()
    → _git_push() / _git_pull() / _git_fetch() / _git_merge() / _git_rebase()
      → _run_git(repo, [...], read_only=False, timeout=GIT_REMOTE_TIMEOUT_SECONDS)
        → git subprocess (GIT_TERMINAL_PROMPT=0)
      → 解析 stdout/stderr 为结构化结果
  → Permission engine: command ∉ read_only_set → GIT_WRITE → 需审批
```

权限路径：新命令均不在 `_is_read_only_git_tool_command` 的只读集合中 → 自动归为 `GIT_WRITE` → 审批流程与 commit/restore 等一致。

sandbox 层：git 工具的调用不经过 `check_sandbox_bash`，而是走 `PermissionCapability.GIT_WRITE`。在 `workspace-write` 模式下，`GIT_WRITE` 不被 sandbox 拦截，而是走审批流程。这与 commit 等写操作行为一致。

超时机制：现有 `_run_git` 硬编码使用 `GIT_TIMEOUT_SECONDS`（15s）。为支持远程操作，给 `_run_git` 和 `_run_process` 新增可选 `timeout` 参数，默认仍为 `GIT_TIMEOUT_SECONDS`。远程命令调用时传入 `GIT_REMOTE_TIMEOUT_SECONDS`（60s）。

## Data Model

```
GitPushArgs
├── remote: str (default="origin")
├── branch: str (default="" — 空=推送当前分支到同名远程分支)
├── force: bool (default=False)
└── all_branches: bool (default=False — 对应 --all)

GitPullArgs
├── remote: str (default="origin")
└── branch: str (default="" — 空=拉取当前分支的上游)

GitFetchArgs
├── remote: str (default="origin")
├── branch: str (default="" — 空=拉取整个 remote)
├── all: bool (default=False — 对应 --all)
└── prune: bool (default=False — 对应 --prune)

GitMergeArgs
├── branch: str (min_length=1 — 要合并进来的分支)
├── message: str (default="" — 合并提交消息，空=自动生成)
└── no_ff: bool (default=False — 对应 --no-ff)

GitRebaseArgs
├── branch: str (default="" — 空=变基到当前分支的上游)
├── onto: str (default="" — 对应 --onto)
└── continue_rebase: bool (default=False — 对应 --continue)
└── abort: bool (default=False — 对应 --abort)
```

## API Contract

### push

- **Command**: push
- **Args**: GitPushArgs
- **Success Response**:
```json
{
  "ok": true,
  "data": {
    "remote": "origin",
    "branch": "main",
    "force": false,
    "summary": "2 commits pushed to origin/main"
  }
}
```
- **Error Responses**:

| error | 场景 |
|-------|------|
| `push_rejected` | 远程拒绝（非 fast-forward） |
| `remote_not_found` | 指定的 remote 不存在 |
| `push_failed` | 网络、认证等错误 |

### pull

- **Command**: pull
- **Args**: GitPullArgs
- **Success Response**:
```json
{
  "ok": true,
  "data": {
    "remote": "origin",
    "branch": "main",
    "fast_forward": true,
    "summary": "Fast-forward"
  }
}
```
- **Error Responses**:

| error | 场景 |
|-------|------|
| `merge_conflict` | 合并冲突 |
| `remote_not_found` | remote 不存在 |
| `pull_failed` | 其他错误 |

### fetch

- **Command**: fetch
- **Args**: GitFetchArgs
- **Success Response**:
```json
{
  "ok": true,
  "data": {
    "remote": "origin",
    "summary": "Fetched 3 branches"
  }
}
```
- **Error Responses**:

| error | 场景 |
|-------|------|
| `remote_not_found` | remote 不存在 |
| `fetch_failed` | 网络、认证等错误 |

### merge

- **Command**: merge
- **Args**: GitMergeArgs
- **Success Response**:
```json
{
  "ok": true,
  "data": {
    "branch": "feature-x",
    "fast_forward": false,
    "hash": "abc1234",
    "conflicts": []
  }
}
```
- **Error Responses**:

| error | 场景 |
|-------|------|
| `merge_conflict` | 合并冲突，data 中包含冲突文件列表 |
| `branch_not_found` | 要合并的分支不存在 |
| `merge_failed` | 其他错误 |

### rebase

- **Command**: rebase
- **Args**: GitRebaseArgs
- **Success Response**:
```json
{
  "ok": true,
  "data": {
    "branch": "main",
    "onto": "",
    "summary": "Rebased successfully"
  }
}
```
- **Error Responses**:

| error | 场景 |
|-------|------|
| `rebase_conflict` | 变基冲突，data 中包含冲突文件列表 |
| `branch_not_found` | 目标分支不存在 |
| `rebase_failed` | 其他错误 |

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| push 非 fast-forward 被拒绝 | 返回 `push_rejected`，suggestion 提示 force |
| pull/merge 冲突 | 返回 `merge_conflict`，data 中包含冲突文件列表 |
| rebase 冲突 | 返回 `rebase_conflict`，data 中包含冲突文件列表和提示（abort/continue） |
| 认证失败 | 返回 `*_failed`，包含 stderr 关键信息 |
| 网络超时 | 60s 超时，超时返回错误 |
| remote 不存在 | 返回 `remote_not_found` |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 全部走 git_write 审批 | 按命令分不同权限级别 | 与现有写操作一致，避免权限模型膨胀 |
| 远程操作超时 60s | 复用 15s 全局超时 | push/pull/fetch 涉及网络 IO，15s 不够 |
| rebase 支持 continue/abort | 不支持，要求 bash 处理 | rebase 冲突后需要继续或中止，这是核心工作流 |
| fetch 支持 --prune | 不支持 | prune 是常见需求，清理已删除远程分支 |
| merge 默认 fast-forward | 默认 --no-ff | 与 git 默认行为一致，由 no_ff 参数控制 |
| 不修改 sandbox bash 拦截 | 移除 sandbox 对 bash git push 的拦截 | 保持双通道安全边界清晰 |

## Open Questions

- [ ] 是否需要在审批时对 force push 做更严格的提示？
- [ ] rebase --skip 是否需要支持？（首期可省略，用 abort + 重新 rebase 替代）
