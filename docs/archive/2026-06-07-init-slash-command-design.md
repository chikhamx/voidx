# /init Slash Command Design

> **Status: Done**

Date: 2026-06-07

## Problem

voidx reads `AGENTS.md` to inject project-specific instructions into the LLM
context, but users must write this file manually. opencode's `/init` command
auto-generates `AGENTS.md` by scanning the project, and voidx should offer the
same convenience — adapted to voidx's own conventions and mechanisms.

Unlike opencode's deterministic scanner, voidx's `/init` delegates the scan and
generation to the normal orchestrator turn. The orchestrator may use child
agents when appropriate, but the command does not force an extra subagent layer.
This lets the LLM use voidx's own tools (repo_map, glob, grep, read) to deeply
understand the project, then produce a high-quality AGENTS.md that reflects the
actual codebase — not just config file presence checks. The key differentiator
is a structured prompt that guides the LLM to generate content aligned with
voidx's specific mechanisms.

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
2. If the current interaction mode is plan mode, refuse with a message telling
   the user to run `/unplan` first. `/init` writes a file, so it should not try
   to bypass plan-mode write restrictions.
3. Start a synthetic agent turn with a structured task prompt, directing the
   orchestrator to generate AGENTS.md. The orchestrator may delegate to the
   implement agent or write directly, subject to normal permissions.
4. The agent scans the project using voidx tools, then writes AGENTS.md.

The orchestrator receives the task as a synthetic user message in a normal
agent turn. This avoids a separate LLM call infrastructure, but it must not use
the mid-turn guidance path: `submit_guidance()` collapses whitespace, caps
content at 2000 characters, and does not start a new agent run by itself.

### Init Prompt

The prompt is the core of this feature. It must guide the LLM to produce an
AGENTS.md that is specifically tailored to voidx's mechanisms:

```python
INIT_PROMPT = """\
Generate an AGENTS.md file for this project. Write it to the workspace root.

## What to do

1. Scan the project structure using repo_map, glob, grep, and read tools.
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

- **Workflow skills**: Which discovered voidx skills are relevant for this
  project. Mention a skill only if it exists in the local skill registry or is
  already referenced by this project. Examples may include
  `test-driven-development`, `verification-before-completion`,
  `systematic-debugging`, and `writing-plans`.
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
- If the project has an existing AGENTS.md because /init force was used, read it
  first and preserve custom project-specific sections when they are still
  accurate.
"""
```

### Changes

#### 1. `src/voidx/agent/slash/init.py` — new file

```python
class SlashInitMixin:
    async def _init(self, args: str) -> None:
        arg = args.strip().lower()
        if arg not in {"", "force"}:
            ui.error("Usage: /init [force]")
            return

        if self._host_interaction_mode_value() == "plan":
            ui.error("/init writes AGENTS.md. Run /unplan first.")
            return

        existing = Path(self._host_workspace()) / "AGENTS.md"

        if existing.exists() and arg != "force":
            ui.print("[dim]AGENTS.md already exists. Use /init force to regenerate.[/dim]")
            return

        await self._g.run_synthetic_turn(INIT_PROMPT, display_text="/init")
```

`run_synthetic_turn()` reuses the normal agent loop while keeping the prompt out
of the guidance queue. The display text keeps the UI compact (`/init`) while
the persisted user message and LLM input contain the full init prompt.

#### 2. Register in handler

Add `SlashInitMixin` to `SlashHandler`'s MRO and `"/init"` to the dispatch
table:

```python
"/init": lambda: self._init(args),
```

#### 3. Add to command palette

Add entries to `COMMANDS` in `src/voidx/ui/commands.py`:

```python
("/init", "Generate AGENTS.md for this project"),
("/init force", "Regenerate AGENTS.md even if it already exists"),
```

#### 4. Add synthetic turn host method

Add a public graph method:

```python
async def run_synthetic_turn(self, text: str, *, display_text: str | None = None) -> None:
    await self._run_once(text, display_text=display_text)
```

Update `GraphTurnMixin._run_once()` to accept `display_text: str | None = None`
and use it only for `TurnStarted` / `dock.start_turn()` display text. The full
`text` still goes through `build_user_message_payload()`, persistence, intent
resolution, and LLM context.

### What does NOT change

- **`InstructionService`** — no changes to how AGENTS.md is loaded.
- **No new LLM infrastructure** — reuses the existing agent loop via a
  synthetic user turn.
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
| `test_init_dispatches_synthetic_turn` | `/init` calls `run_synthetic_turn()` with the full init prompt and display text `/init` |
| `test_init_refuses_existing_without_force` | `/init` prints message when AGENTS.md exists |
| `test_init_force_dispatches_with_existing` | `/init force` calls `run_synthetic_turn()` even when AGENTS.md exists |
| `test_init_rejects_invalid_args` | `/init anything-else` prints usage |
| `test_init_rejects_plan_mode` | `/init` refuses to run in plan mode |
| `test_run_synthetic_turn_uses_display_text_without_losing_prompt` | synthetic turn displays `/init` while preserving the full prompt for the LLM |
| `test_init_command_is_in_palette` | command palette includes `/init` and `/init force` |

These tests verify the slash command dispatch logic. The actual AGENTS.md
generation quality is tested by running `/init` on real projects — it's an
LLM output, not a deterministic function.

### Acceptance Criteria

- `/init` triggers the orchestrator to scan the project and write AGENTS.md.
- Generated AGENTS.md includes all standard sections, especially `voidx Integration`.
- Existing AGENTS.md is not overwritten without `force`.
- `/init` does not use the guidance path and does not truncate the init prompt.
- `/init` refuses to run in plan mode.
- The `voidx Integration` section covers tool preferences, agent delegation,
  workflow skills, and permission awareness.
- No separate LLM call infrastructure — reuses the normal agent loop.
