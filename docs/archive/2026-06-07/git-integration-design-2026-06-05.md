# Git Integration Design

Date: 2026-06-05

> **Status: Done**

## Goal

Provide first-class Git tooling so voidx can inspect repository state and perform
explicit, permission-gated Git writes without going through the generic `bash`
tool. The first version focuses on structured data and user-work safety:

- expose structured `status`, `diff`, `log`, `blame`, `branch list`, and `remote list`;
- support explicit `add`, `commit`, and scoped `restore`;
- preserve existing dirty user work;
- integrate with the current permission engine and sandbox rules;
- keep `/rollback` as the default undo path for agent file edits.

## Current State

Key files:

- `src/voidx/tools/bash.py` — can run `git`, but returns raw terminal output.
- `src/voidx/permission/rules.py` — classifies many read-only `git` and `gh`
  bash commands as safe, while write operations remain write-capability calls.
- `src/voidx/permission/sandbox.py` — blocks `git push` in workspace-write
  sandbox unless danger-full-access is enabled.
- `src/voidx/ui/session.py` — `SessionChangeTracker` captures files touched by
  write/edit/apply_patch/lsp_format tools.
- `src/voidx/agent/slash/session.py` — `/rollback` restores captured snapshots
  from the most recent visible turn.

Observed gaps:

- Git data is not structured; the agent must parse raw text from `bash`.
- Git writes are indistinguishable from other shell commands in tool UX.
- The agent has no safe Git-native way to commit only selected paths.
- There is no structured repo status for prompts, UI, or future workflows.

Important existing capability:

- Undo for agent edits already exists through `/rollback`. It is path-scoped to
  files captured by `session_tracker`, so it is safer than broad Git reset.

## Non-Goals For V1

- No default auto-commit after every edit turn.
- No `git reset --hard` based undo.
- No automatic `git add -A`.
- No `commit --amend`.
- No `stash pop`, branch checkout/switch, worktree mutation, or PR creation.
- No network operations.

These can be revisited after the read/write Git tool is stable and has clear
dirty-tree protections.

## Design

### Approach: Dedicated Structured Git Tool

Add a `git` tool implemented with a controlled subprocess adapter:

- call `git` with an argument list, never through a shell string;
- set `cwd` to the workspace or discovered repo root as appropriate;
- set `GIT_TERMINAL_PROMPT=0` to avoid hanging on credentials;
- apply a short timeout to each command;
- parse stdout into Pydantic models;
- return typed JSON payloads in `ToolResult.output`;
- classify read vs write subcommands through the permission engine.

The phrase "without shelling out through bash" means "do not use the `bash`
tool." The implementation still invokes the Git binary through `subprocess`,
but with explicit argv, bounded environment, and structured parsing.

### Tool Input

Use one tool id, `git`, with a command discriminator and subcommand-specific
`args` validation:

```python
class GitInput(BaseModel):
    command: Literal[
        "status",
        "diff",
        "log",
        "blame",
        "branch_list",
        "remote_list",
        "add",
        "commit",
        "restore",
    ]
    args: dict[str, Any] = Field(default_factory=dict)
```

Each command validates `args` against an internal Pydantic model before running.
Invalid args return a structured tool error, not a raw traceback.

### Repo Discovery

Before every command:

1. Run `git rev-parse --show-toplevel` from `config.workspace`.
2. If the workspace is not inside a Git worktree, return:
   `{ "ok": false, "error": "not_a_git_repository" }`.
3. Record both `repo_root` and `workspace`.
4. All returned paths are normalized relative to the workspace when possible.
5. Write paths must resolve under the workspace or configured sandbox extra
   write roots; otherwise deny before invoking Git.

If the repo root is above the workspace, V1 still restricts pathspecs and write
targets to the workspace subtree. This avoids accidentally staging or restoring
files outside the user's active workspace.

### Read-Only Subcommands

These subcommands should be allowed by default and should not prompt:

| Command | Args | Implementation Notes |
|---------|------|----------------------|
| `status` | `{pathspec?: list[str]}` | Use `git status --porcelain=v1 -z -- <paths>` |
| `diff` | `{cached?: bool, pathspec?: list[str], ref?: str}` | Use `git diff --numstat` plus `git diff --unified=...` |
| `log` | `{limit?: int, path?: str, author?: str, since?: str}` | Use a delimiter-safe pretty format; cap `limit` |
| `blame` | `{path: str, start?: int, end?: int}` | Use `git blame --line-porcelain` |
| `branch_list` | `{all?: bool}` | Use `git branch --format=...`; no create/delete |
| `remote_list` | `{}` | Use `git remote -v` |

### Write Subcommands

These subcommands require permission:

| Command | Args | Safety Rules |
|---------|------|--------------|
| `add` | `{paths: list[str]}` | Path-scoped; no `-A`; reject empty list |
| `commit` | `{message: str, paths?: list[str]}` | If `paths` provided, stage only those paths first; reject empty message |
| `restore` | `{paths: list[str], staged?: bool, worktree?: bool}` | Path-scoped; reject empty list; warn that files may be overwritten |

`commit` never stages the whole repository implicitly. If `paths` is omitted, it
commits the current index only and reports the staged files before committing.

### Structured Output Models

```python
class GitResult(BaseModel):
    ok: bool = True
    command: str
    repo_root: str
    workspace: str
    data: dict[str, Any] = Field(default_factory=dict)
    error: str = ""

class GitStatusEntry(BaseModel):
    path: str
    staged: str       # "" | "added" | "modified" | "deleted" | "renamed" | "typechange"
    unstaged: str     # "" | "modified" | "deleted" | "typechange"
    untracked: bool = False
    original_path: str = ""

class GitDiffEntry(BaseModel):
    path: str
    additions: int
    deletions: int
    hunks: list[str]
    binary: bool = False

class GitCommitResult(BaseModel):
    hash: str
    message: str
    files_changed: list[str]
```

The tool output should be compact JSON. Large diffs are capped per file and
include truncation metadata.

## Dirty Tree Policy

V1 must protect user work in a dirty tree.

Rules:

- Never run `git add -A`.
- Never run `git reset --hard`.
- Never restore files without explicit path arguments.
- Before `commit`, inspect `git status --porcelain` and report which files are
  staged and which requested paths will be staged.
- If `commit(paths=[...])` sees unstaged changes outside the requested paths,
  leave them untouched and include them in `data.unstaged_uncommitted`.
- If `restore(paths=[...])` targets a file that was not captured by the current
  agent turn, require normal write permission and include a warning in the
  tool result.

Agent-turn undo remains `/rollback`, not Git reset. `/rollback` uses captured
pre-edit snapshots and is safer for local, uncommitted work.

## Permission Model

Add `git` to the permission classifier as a first-class tool:

| Command | Capability | Default |
|---------|------------|---------|
| `status`, `diff`, `log`, `blame`, `branch_list`, `remote_list` | `READ_TOOLS` or `GIT_READ` | allow |
| `add`, `commit`, `restore` | `GIT_WRITE` | ask |

Implementation options:

- add `PermissionCapability.GIT_READ` and `PermissionCapability.GIT_WRITE`; or
- classify `git` read commands as `READ_TOOLS` and write commands as
  `BASH_WRITE`-equivalent.

The cleaner option is adding explicit Git capabilities because UI copy and
permission rules can then say `git -> commit` instead of treating it as shell.

Plan mode should deny Git write commands. Read-only Git commands remain allowed.

## Sandbox Rules

Sandbox checks must run before invoking Git:

- all pathspecs resolve under workspace or allowed extra write roots;
- `restore`, `add`, and `commit(paths=...)` reject paths outside the sandbox;
- no network command is present in V1, so `git push` remains unavailable through
  this tool;
- submodule paths are treated as paths and must still pass workspace checks.

## Interaction With Existing `/rollback`

The Git tool does not replace `/rollback`.

- `/rollback` remains the default user-facing undo for the most recent agent
  file-edit turn.
- Git `restore` is a lower-level explicit tool for user-requested Git restore.
- Future Git checkpointing can use `session_tracker` to know which paths the
  agent touched, but V1 does not auto-commit those paths.

## Future Work

Deferred items:

- opt-in `/git checkpoint` command that commits only current-turn touched paths;
- multi-turn rollback history;
- `stash push/list/show` without `pop`;
- branch create/switch with dirty-tree checks;
- PR creation via `gh` after explicit branch/push design;
- git-aware prompt context showing compact status.

## Implementation Plan

1. Add `src/voidx/tools/git.py` with the Git subprocess adapter, input models,
   output models, and read-only subcommands.
2. Register `git` in the tool registry.
3. Add path validation helpers shared by read/write commands.
4. Add write subcommands `add`, `commit`, and `restore`.
5. Extend permission classification for `git` read/write commands.
6. Add tests for parsing, permissions, sandbox checks, dirty tree behavior, and
   non-Git repositories.

## Testing

| Test | Description |
|------|-------------|
| `test_git_status_structured` | status returns typed entries for staged, unstaged, untracked, renamed files |
| `test_git_diff_structured` | diff returns additions, deletions, hunks, and truncation metadata |
| `test_git_log_structured` | log returns typed commits with path filtering |
| `test_git_blame_structured` | blame returns line records for a bounded range |
| `test_git_non_repo_returns_structured_error` | non-Git workspace returns `ok=false` and `not_a_git_repository` |
| `test_git_add_requires_paths_and_permission` | add rejects empty paths and is classified as write |
| `test_git_commit_paths_does_not_stage_unrelated_dirty_files` | commit with paths preserves unrelated dirty changes |
| `test_git_restore_is_path_scoped` | restore cannot run without explicit paths |
| `test_git_restore_rejects_outside_workspace` | sandbox rejects outside paths before invoking Git |
| `test_git_permission_read_only` | read-only Git commands do not prompt |
| `test_git_permission_write` | write Git commands require permission |
