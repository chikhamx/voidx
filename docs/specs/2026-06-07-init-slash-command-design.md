# /init Slash Command Design

Date: 2026-06-07

## Problem

voidx reads `AGENTS.md` to inject project-specific instructions into the LLM
context, but users must write this file manually. opencode's `/init` command
auto-generates `AGENTS.md` by scanning the project, and voidx should offer the
same convenience — adapted to voidx's own conventions and mechanisms.

Unlike opencode's deterministic scanner, voidx's `/init` delegates the scan and
generation to a child agent (implement). This lets the LLM use voidx's own
tools (repo_map, glob, grep, read) to deeply understand the project, then
produce a high-quality AGENTS.md that reflects the actual codebase — not just
config file presence checks. The key differentiator is a structured prompt that
guides the LLM to generate content aligned with voidx's specific mechanisms.

## Current State

| File | Role |
|------|------|
| `src/voidx/llm/instruction.py` | `InstructionService` loads AGENTS.md from global + workspace walk-up |
| `src/voidx/tools/repomap.py` | `RepoMapTool` extracts structural codebase overview |
| `src/voidx/tools/file_ops.py` | `FileReadTool` / `FileWriteTool` for reading/writing files |
| `src/voidx/tools/search.py` | `GrepTool` / `GlobTool` for searching files |
| `src/voidx/agent/slash/handler.py` | Slash command dispatch |
| `AGENTS.md` (this repo) | Example of a well-structured voidx AGENTS.md |

## Design

### `/init` — LLM-driven AGENTS.md generation

When the user runs `/init`:

1. If AGENTS.md already exists and `force` is not specified, print a message
   and return.
2. Inject a structured task prompt into the current turn, directing the
   orchestrator to delegate to the implement agent.
3. The implement agent scans the project using voidx tools, then writes the
   AGENTS.md.

The orchestrator receives the task as a synthetic user message appended to the
current turn. This avoids a separate LLM call infrastructure — it reuses the
existing agent loop.

### Init Prompt

The prompt is the core of this feature. It must guide the LLM to produce an
AGENTS.md that is specifically tailored to voidx's mechanisms:

```python
INIT_PROMPT = """\
Generate an AGENTS.md file for this project. Write it to the workspace root.

## What to do

1. Scan the project structure using repo_map, glob, and read tools.
2. Detect the language, framework, test runner, linter, and build system.
3. Read key config files (pyproject.toml, package.json, Cargo.toml, go.mod, etc.)
   to extract exact commands.
4. Write AGENTS.md to the workspace root.

## AGENTS.md structure

Follow this exact section order. Each section is required unless marked optional.

### Project Shape

List the top-level directories with their purpose. Be specific — don't just
say "source code", say what kind of source (e.g. "LangGraph orchestration,
roles, slash commands, runtime state").

### Commands

List the exact commands to run, test, lint, and build. Use the actual package
manager and flags detected from config files. Include:

- Full test command (with verbose flag if appropriate)
- Focused test command (how to run a single test file)
- Lint/format command
- Build command
- Dev server command (if applicable)
- Any other project-specific commands (migrations, codegen, etc.)

### Code Rules

Infer conventions from the codebase. Look at:

- Linter/formatter config (ruff, eslint, prettier, etc.)
- Existing code patterns (naming, module structure, import style)
- Type checking config (mypy, pyright, TypeScript strict mode)
- Any .editorconfig or similar

### voidx Integration

This section is specific to voidx. Include the rules that help voidx agents
work effectively with this project:

- **Tool preferences**: Which voidx tools to prefer for this project.
  - Use `repo_map` for structural overview before deep dives.
  - Use `edit` with exact `old_string` matches for surgical changes.
  - Use `apply_patch` for multi-file changes.
  - Use `lsp_format` after editing if the project has a formatter.
  - Use `lsp_diagnostics` to verify changes compile/type-check.
- **Agent delegation**: When to use which child agent.
  - `explore` for codebase understanding questions.
  - `plan` for architecture and design before implementation.
  - `implement` for broad or isolated coding tasks.
  - `review` after non-trivial implementation work.
- **Workflow skills**: Which skills are relevant for this project.
  - `test-driven-development` for feature/bug work.
  - `verification-before-completion` before claiming done.
  - `systematic-debugging` for bug investigation.
  - `writing-plans` before complex implementations.
- **Permission awareness**: Note which tools require approval in this project
  (write, edit, bash, lsp_format, agent(implement)) and plan accordingly —
  batch reads before edits, minimize approval prompts.

### Document Lifecycle (if applicable)

If the project has design docs or specs:

- Where design docs live while in progress.
- When to move docs to archive (only after code + tests exist).
- Where RFC/exploratory docs go.

### Safety

Standard safety rules:

- Do not commit `.voidx/`, `.env*`, or local credentials.
- Preserve user work in a dirty tree; never revert unrelated changes.
- Run the relevant focused tests before broad test runs.
- Do not run destructive commands (drop table, rm -rf, force push) unless
  the user explicitly asks.

## Rules

- Use tools to discover facts. Do not guess commands or structure.
- Read actual config files to get exact commands and flags.
- Keep the file concise — rules and facts, not essays.
- Do not add comments unless they explain non-obvious intent or constraints.
- If the project has an existing AGENTS.md, read it first and preserve any
  custom sections not covered by the standard structure.
"""
```

### Changes

#### 1. `src/voidx/agent/slash/init.py` — new file

```python
class SlashInitMixin:
    async def _init(self, args: str) -> None:
        workspace = self._host_workspace()
        existing = Path(workspace) / "AGENTS.md"

        if existing.exists() and args.strip() != "force":
            ui.print("[dim]AGENTS.md already exists. Use /init force to regenerate.[/dim]")
            return

        # Inject the init task as a guide message into the current turn.
        # The orchestrator will pick it up and delegate to implement.
        await self._guide(INIT_PROMPT)
```

The `_guide` method already exists on `SlashHandler` — it appends a guidance
message to the running agent turn. This means `/init` reuses the existing
guidance mechanism rather than building a new LLM call path.

#### 2. Register in handler

Add `SlashInitMixin` to `SlashHandler`'s MRO and `"/init"` to the dispatch
table:

```python
"/init": lambda: self._init(args),
```

#### 3. Add to command palette

Add entries to `COMMANDS` in `src/voidx/ui/commands.py`:

```python
("/init", "Generate or update AGENTS.md for this project"),
("/init force", "Regenerate AGENTS.md even if it already exists"),
```

### What does NOT change

- **`InstructionService`** — no changes to how AGENTS.md is loaded.
- **No new LLM infrastructure** — reuses the existing agent loop via `_guide`.
- **No deterministic scanner** — the LLM does the scanning using voidx tools,
  producing higher-quality output than config-file-presence checks.
- **No interactive wizard** — V1 is fully automatic via the prompt.

### Why LLM-driven instead of deterministic

1. **Quality**: A deterministic scanner can detect that `pytest` is configured,
   but an LLM can read `pyproject.toml` and extract the exact command with
   flags (`-v`, `--tb=short`, specific test paths).
2. **voidx-specific content**: The `voidx Integration` section requires
   understanding the project's relationship to voidx tools and agents — this
   is fundamentally an LLM task.
3. **Contextual rules**: Inferring code conventions from existing patterns
   (naming, module structure, import style) requires reading and understanding
   code, not just checking for config files.
4. **Existing content preservation**: Merging with an existing AGENTS.md
   requires understanding what sections exist and what's custom.

### Testing

| Test | Description |
|------|-------------|
| `test_init_dispatches_guide` | `/init` calls `_guide` with the init prompt |
| `test_init_refuses_existing_without_force` | `/init` prints message when AGENTS.md exists |
| `test_init_force_dispatches_with_existing` | `/init force` calls `_guide` even when AGENTS.md exists |
| `test_init_dispatches_when_no_agents_md` | `/init` calls `_guide` when no AGENTS.md exists |

These tests verify the slash command dispatch logic. The actual AGENTS.md
generation quality is tested by running `/init` on real projects — it's an
LLM output, not a deterministic function.

### Acceptance Criteria

- `/init` triggers the orchestrator to scan the project and write AGENTS.md.
- Generated AGENTS.md includes all standard sections, especially `voidx Integration`.
- Existing AGENTS.md is not overwritten without `force`.
- The `voidx Integration` section covers tool preferences, agent delegation,
  workflow skills, and permission awareness.
- No new LLM call infrastructure — reuses `_guide`.
