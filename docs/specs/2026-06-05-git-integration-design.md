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
    unstaged: str      # "" | "modified" | "deleted"
    untracked: bool

class GitCommitInfo(BaseModel):
    hash: str
    short_hash: str
    author: str
    date: str
    message: str
    files_changed: int
    insertions: int
    deletions: int

class GitDiffFile(BaseModel):
    path: str
    old_path: str | None = None   # for renames
    additions: int
    deletions: int
    binary: bool
    patch: str   # unified diff text for this file
```

### Implementation: GitService

Create `src/voidx/tools/git_service.py` that wraps `git` CLI calls with structured parsing:

```python
class GitService:
    def __init__(self, workspace: str):
        self._workspace = workspace

    async def status(self, pathspec: str | None = None) -> list[GitStatusEntry]: ...
    async def diff(self, cached: bool = False, path: str | None = None, ref: str | None = None) -> list[GitDiffFile]: ...
    async def log(self, n: int = 10, path: str | None = None) -> list[GitCommitInfo]: ...
    async def blame(self, path: str, line: int | None = None) -> list[GitBlameLine]: ...
    async def add(self, paths: list[str]) -> None: ...
    async def commit(self, message: str, amend: bool = False) -> GitCommitInfo: ...
    async def stash_push(self, message: str | None = None) -> str: ...
    async def stash_pop(self) -> str: ...
    async def restore(self, paths: list[str], staged: bool = False) -> list[str]: ...
    async def branch_list(self, all: bool = False) -> list[GitBranch]: ...
    async def checkout(self, branch: str, create: bool = False) -> str: ...
    async def pr_create(self, title: str, body: str | None = None, base: str | None = None, draft: bool = False) -> GitPrResult: ...
```

All methods run `git` via `asyncio.create_subprocess_exec` (not bash), parse output with dedicated parsers, and return typed models.

### Auto-Commit Hook

After the agent finishes a batch of edits (all tool calls in a single AIMessage), automatically create a checkpoint commit:

```python
async def _auto_commit_if_dirty(self, state) -> None:
    """After tool execution, auto-commit if files were modified."""
    if not self._settings.get_auto_commit():
        return
    service = GitService(self._workspace)
    status = await service.status()
    dirty = [e for e in status if e.staged or e.unstaged or e.untracked]
    if not dirty:
        return
    # Stage all agent-modified files
    agent_files = [e.path for e in dirty if e.path in self._edited_files]
    if not agent_files:
        return
    await service.add(agent_files)
    message = _generate_commit_message(agent_files, self._current_task_description)
    await service.commit(message)
```

Configuration:

```json
{
  "git": {
    "auto_commit": true,
    "auto_commit_prefix": "voidx:",
    "undo_enabled": true
  }
}
```

Auto-commit messages are prefixed with `voidx:` so users can distinguish agent commits from their own.

### Undo Command

Add `/undo` slash command that reverts the last auto-commit:

```python
async def _undo(self) -> None:
    service = GitService(self._workspace)
    log = await service.log(n=1)
    if not log or not log[0].message.startswith("voidx:"):
        ui.error("No voidx auto-commit to undo.")
        return
    await service.restore(paths=["."], staged=True)  # git reset HEAD~1
    ui.print(f"Undid: {log[0].message}")
```

### Git-Aware Context

Add git status to the runtime context injected into the system prompt:

```
## Git Status
- Branch: main (ahead 2, behind 0)
- Modified: src/voidx/tools/file_ops.py, src/voidx/agent/graph.py
- Untracked: src/voidx/tools/apply_patch.py
- Last commit: a3f2c1d "feat: add apply_patch tool" (2 min ago)
```

This gives the agent awareness of what's changed without needing to run `git status` explicitly.

### Permission Integration

| Subcommand | Capability | Default Action |
|------------|-----------|---------------|
| status, diff, log, blame, branch, remote | `GIT_READ` | allow |
| add, commit, stash, restore, checkout | `GIT_WRITE` | ask |
| pr_create | `GIT_WRITE` | ask |
| `commit --amend` | `GIT_WRITE` | ask (extra warning) |

New rules in `BASIC_RULES`:

```python
Rule(permission="git", pattern="status|diff|log|blame|branch|remote", action="allow"),
Rule(permission="git", pattern="*", action="ask"),
```

### Smart Commit Messages

Auto-generate commit messages from the edit context:

```python
def _generate_commit_message(files: list[str], task_description: str) -> str:
    # Use the task description as the commit subject
    # List changed files in the body
    subject = task_description[:72] if task_description else "agent edits"
    body = f"Files changed: {', '.join(files)}"
    return f"voidx: {subject}\n\n{body}"
```

For manual commits via the `git commit` subcommand, the LLM provides the message directly.

## Scope

In scope:

- `git` tool with 12 subcommands (6 read-only, 6 write).
- `GitService` with structured parsing.
- Auto-commit hook after agent edit sessions.
- `/undo` slash command.
- Git status in runtime context.
- Smart commit message generation.
- Permission integration.

Out of scope:

- Interactive rebase (too complex, use bash).
- Merge conflict resolution (future — needs UI support).
- Git hooks management.
- Submodule support.
- Git worktree support.
- Visual diff UI (TUI/Web already have basic diff rendering).

## File Changes

| File | Change |
|------|--------|
| `src/voidx/tools/git.py` | New — `GitTool`, `GitInput`, subcommand dispatch |
| `src/voidx/tools/git_service.py` | New — `GitService`, structured parsers, Pydantic result models |
| `src/voidx/tools/registry.py` | Register `GitTool` |
| `src/voidx/permission/engine.py` | Add `GIT_READ`, `GIT_WRITE` capabilities; add git rules to `BASIC_RULES` |
| `src/voidx/agent/slash/handler.py` | Add `/undo` command |
| `src/voidx/agent/graph/tool_execution.py` | Add auto-commit hook after tool execution |
| `src/voidx/agent/runtime_context.py` | Add git status to runtime context |
| `src/voidx/config.py` | Add `GitConfig` with `auto_commit`, `auto_commit_prefix`, `undo_enabled` |
| `src/voidx/agent/agents.py` | Update prompts to mention `git` tool and `/undo` |
| `tests/test_tools/test_git.py` | New — subcommand tests, parser tests, auto-commit tests |

## Risks

| Risk | Mitigation |
|------|-----------|
| Auto-commit creates noise in git history | Prefix with `voidx:`, configurable, can be squashed later |
| `gh` CLI not installed for PR creation | Detect at startup, disable `pr_create` subcommand gracefully |
| Git commands fail in non-git repos | `GitService.__init__` checks for `.git` directory, returns clear error |
| Auto-commit during partial edits creates broken state | Only auto-commit after all tool calls in a turn complete |
| Structured parsing breaks on unusual git output | Fallback to raw text output with parse error metadata |
| `/undo` conflicts with user's own commits | Only undo commits with `voidx:` prefix, refuse otherwise |
