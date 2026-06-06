# Git Deep Integration Design

Date: 2026-06-05

## Goal

Provide first-class Git tooling so voidx can commit, diff, log, stash, branch, undo changes, and create PRs without shelling out through bash. This gives the agent structured Git data, enables safe undo, and builds user trust through auto-commit safety nets.

## Current State

Key files:

- `src/voidx/tools/bash.py` — the only way to run git commands today. No structured output.
- `src/voidx/tools/file_ops.py` — `file_mtimes` staleness guard, but no awareness of git status.
- `src/voidx/permission/sandbox.py` — blocks `git push --force to main/master` via regex.
- `src/voidx/agent/graph/tool_execution.py` — no git-specific hooks (no auto-commit after edits).

Observed gaps:

- No structured git data — `git log`, `git diff`, `git blame` output is raw text the LLM must parse.
- No auto-commit — after a series of edits, there's no checkpoint the user can roll back to.
- No undo — if the agent makes unwanted changes, the only recovery is manual `git checkout`.
- No PR creation — agent can't propose changes for review.
- No git-aware context — agent doesn't know which files are modified, staged, or untracked.
- Bash-based git is slow (process spawn per command) and unstructured.

## External References

- **Claude Code** `git` tool: commit, diff, log, stash, blame, restore with structured output.
- **Aider** auto-commit: commits after each successful edit with descriptive messages; `/undo` reverts last commit.
- **Cursor** git panel: visual diff, commit, branch management.
- **GitHub CLI** (`gh`): PR creation, issue linking, review management.

References:

- https://code.claude.com/docs/en/tools
- https://aider.chat/docs/faq.html#how-does-aider-use-git
- https://cli.github.com/

## Design

### Approach: Dedicated Git Tool + Auto-Commit Hook

Add a `git` tool with subcommands for structured Git operations, plus an auto-commit hook that creates checkpoints after agent edit sessions.

### Tool Definition

```python
class GitInput(BaseModel):
    command: str = Field(
        description=(
            "Git subcommand: status, diff, log, blame, add, commit, stash, "
            "restore, branch, checkout, pr_create"
        )
    )
    args: dict = Field(
        default_factory=dict,
        description="Subcommand-specific arguments"
    )
```

### Subcommands

#### Read-only (no permission prompt)

| Subcommand | Args | Returns |
|------------|------|---------|
| `status` | `{pathspec?: str}` | Structured status: list of `{path, staged, unstaged, untracked}` |
| `diff` | `{cached?: bool, path?: str, ref?: str}` | Structured diff: list of `{file, additions, deletions, hunks}` |
| `log` | `{n?: int, path?: str, author?: str, since?: str}` | List of `{hash, author, date, message, files_changed}` |
| `blame` | `{path: str, line?: int}` | List of `{line, commit, author, date, content}` |
| `branch` | `{all?: bool}` | List of `{name, current, upstream, ahead, behind}` |
| `remote` | `{}` | List of `{name, url, type}` |

#### Write (requires permission)

| Subcommand | Args | Returns |
|------------|------|---------|
| `add` | `{paths: list[str]}` | Files staged |
| `commit` | `{message: str, amend?: bool}` | `{hash, message, files_changed}` |
| `stash` | `{action: "push"|"pop"|"list", message?: str}` | Stash result |
| `restore` | `{paths: list[str], staged?: bool}` | Files restored |
| `checkout` | `{branch: str, create?: bool}` | Branch result |
| `pr_create` | `{title: str, body?: str, base?: str, draft?: bool}` | `{url, number}` (via `gh`) |

### Structured Output

All subcommands return typed Pydantic models, not raw text:

```python
class GitStatusEntry(BaseModel):
    path: str
    staged: str       # "" | "added" | "modified" | "deleted" | "renamed"
    unstaged: str     # "" | "modified" | "deleted"
    untracked: bool

class GitDiffEntry(BaseModel):
    file: str
    additions: int
    deletions: int
    hunks: list[str]  # raw hunk text for LLM consumption

class GitLogEntry(BaseModel):
    hash: str
    author: str
    date: str
    message: str
    files_changed: list[str]
```

### Auto-Commit Hook

After a batch of edits (when the agent finishes a tool execution cycle), automatically commit changes:

1. `git add -A` (all changes in workspace)
2. `git commit -m "voidx: <summary of changes>"`
3. If commit fails (nothing to commit), skip silently.

This gives users an easy undo path: `git reset --hard HEAD~1` reverts the entire agent action.

### Permission Model

| Subcommand | Permission | Rationale |
|-----------|-----------|-----------|
| status, diff, log, blame, branch, remote | Allow | Read-only |
| add, commit, stash, restore, checkout | Ask | Modifies repo state |
| pr_create | Ask | Pushes to remote |

### Testing

| Test | Description |
|------|-------------|
| `test_git_status_structured` | status returns typed entries |
| `test_git_diff_structured` | diff returns typed entries with hunks |
| `test_git_log_structured` | log returns typed entries |
| `test_git_commit_auto` | auto-commit after edits |
| `test_git_restore_undo` | restore undoes agent changes |
| `test_git_permission_read_only` | read-only subcommands don't prompt |
| `test_git_permission_write` | write subcommands require permission |
