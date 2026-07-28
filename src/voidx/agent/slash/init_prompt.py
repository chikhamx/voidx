"""Prompt for the /init slash command."""

from __future__ import annotations


INIT_PROMPT = """\
Generate an AGENTS.md file for this project. Write it to the workspace root.

## What to do

1. Scan the project structure using glob, grep, and read tools.
2. Detect the language, framework, test runner, linter, and build system.
3. Read key config files such as pyproject.toml, package.json, Cargo.toml,
   go.mod, Makefile, justfile, and README files to extract exact commands.
4. Write AGENTS.md to the workspace root.

## AGENTS.md Structure

Follow this section order. Keep the file concise: rules and facts, not essays.

### Project Shape

List top-level directories with their purpose. Be specific. For example,
describe the kind of source code, runtime layer, frontend, tests, scripts, or
docs each directory contains.

### Commands

List exact commands to run, test, lint, format, build, and start dev servers.
Use the actual package manager and flags detected from config files. Include a
full test command and a focused test command when possible.

### Code Rules

Infer conventions from code and config. Look at formatter/linter config,
typing config, module naming, import style, and existing local patterns.

### voidx Integration

Include only rules that help voidx agents work effectively with this project.
Cover:

- Workflow skills: mention only skills that exist in the local skill registry
  or are already referenced by this project.
- Permission awareness: note write/edit/bash/agent(implement)
  approval expectations when relevant.

### Document Lifecycle

If the project has design docs or specs, document where in-progress docs live,
when they move to archive, and what counts as complete.

### Safety

Include safety rules relevant to the project, especially:

- Do not commit local credentials, .env files, .voidx, generated secrets, or
  other local-only state.
- Preserve user work in a dirty tree; never revert unrelated changes.
- Run focused verification before broader test runs.
- Do not run destructive commands unless the user explicitly asks.

## Rules

- Use tools to discover facts. Do not guess commands or structure.
- Read actual config files before writing command recommendations.
- If an existing AGENTS.md is present because /init force was used, read it
  first and preserve custom project-specific sections when they are still
  accurate.
- Do not include made-up skills, tools, scripts, or commands.
- Keep the generated AGENTS.md practical and concise.
"""
