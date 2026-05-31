"""Instruction service — AGENTS.md / CLAUDE.md project memory.

Aligns with opencode's instruction.ts: hierarchical loading, dedup,
per-message claims tracking, live re-read on every turn.

Resolution order:
  1. ~/.voidx/AGENTS.md    (global)
  2. ~/.claude/CLAUDE.md   (compat, only if ~/.voidx/ not found)
  3. Workspace walk-up AGENTS.md (first match wins, like opencode)
  4. Config URL instructions
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import httpx

INSTRUCTION_FILES = ["AGENTS.md", "CLAUDE.md"]  # CLAUDE.md for compat


@dataclass(frozen=True)
class SkillRuntimeContext:
    instructions: list[str]
    active: list[str]


class InstructionService:
    """Manages project instructions injection into system prompt."""

    def __init__(self, workspace: str, settings=None) -> None:
        self._workspace = Path(workspace).resolve()
        self._settings = settings
        self._global_dir = Path.home() / ".voidx"
        self._claude_dir = Path.home() / ".claude"

        # Per-message claims: track which instruction files have been
        # attached to which assistant message to avoid duplicates.
        self._claims: dict[str, set[str]] = {}
        # Cached system paths (refreshed each turn)
        self._system_paths: list[str] = []

    # ── public API ──────────────────────────────────────────────────────

    def clear(self, message_id: str) -> None:
        """Clear claims for a message (called when message is removed)."""
        self._claims.pop(message_id, None)

    async def system_paths(self) -> list[str]:
        """Discover all instruction file paths. Refreshed each call."""
        paths: list[str] = []

        # 1. Global: ~/.voidx/AGENTS.md first, then ~/.claude/CLAUDE.md
        global_voidx = self._global_dir / "AGENTS.md"
        if global_voidx.exists():
            paths.append(str(global_voidx.resolve()))
        else:
            claude_global = self._claude_dir / "CLAUDE.md"
            if claude_global.exists():
                paths.append(str(claude_global.resolve()))

        # 2. Project: walk-up from workspace, first match wins
        current = self._workspace
        root = Path(current.anchor)
        while current != root:
            for filename in INSTRUCTION_FILES:
                candidate = current / filename
                if candidate.exists() and str(candidate.resolve()) not in paths:
                    paths.append(str(candidate.resolve()))
                    break
            else:
                current = current.parent
                continue
            break  # first match wins (one level only, opencode semantics)
            # Actually opencode tries each filename at each level
            # and breaks out on the FIRST filename that has any match

        self._system_paths = paths
        return paths

    async def system(self) -> list[str]:
        """Read all system instruction files. Returns list of
        'Instructions from: <path>\n<content>' strings."""
        paths = await self.system_paths()
        return await self._read_all(paths)

    async def skill_context_for(
        self,
        user_text: str,
        *,
        agent: str = "",
        task_intent: str | None = None,
        interaction_mode: str | None = None,
    ) -> SkillRuntimeContext:
        from voidx.skills.registry import SkillRegistry
        from voidx.skills.service import SkillService

        selection = self._settings.get_skill_selection() if self._settings is not None else None
        service = SkillService(
            SkillRegistry(str(self._workspace)),
            selection=selection,
        )
        matches = await asyncio.to_thread(
            service.select,
            user_text,
            agent=agent,
            task_intent=task_intent,
            interaction_mode=interaction_mode,
        )
        return SkillRuntimeContext(
            instructions=[service.render_instruction(match.skill) for match in matches],
            active=[f"{match.name} ({match.reason})" for match in matches],
        )

    async def skills_for(
        self,
        user_text: str,
        *,
        agent: str = "",
        task_intent: str | None = None,
        interaction_mode: str | None = None,
    ) -> list[str]:
        context = await self.skill_context_for(
            user_text,
            agent=agent,
            task_intent=task_intent,
            interaction_mode=interaction_mode,
        )
        return context.instructions

    async def resolve(
        self,
        filepath: str,
        message_id: str,
        already_loaded: set[str] | None = None,
    ) -> list[str]:
        """When a file is read by the read tool, check if there are
        nearby instruction files that should be injected. Returns list
        of 'Instructions from: <path>\n<content>' strings.

        Walks up from the file's directory looking for instruction files
        that haven't been loaded yet (not in system_paths, not already loaded).
        Each file is attached at most once per message_id.
        """
        sys_paths = set(self._system_paths) if self._system_paths else set(await self.system_paths())
        already = already_loaded or set()

        target = Path(filepath).resolve()
        current = target.parent if target.is_file() else target
        root = self._workspace

        results: list[str] = []

        while current != root and str(current) != str(current.anchor):
            for filename in INSTRUCTION_FILES:
                candidate = current / filename
                candidate_str = str(candidate.resolve())

                if not candidate.exists():
                    continue
                if candidate_str == str(target):
                    continue
                if candidate_str in sys_paths or candidate_str in already:
                    continue

                # Check claims for this message_id
                if message_id not in self._claims:
                    self._claims[message_id] = set()
                if candidate_str in self._claims[message_id]:
                    continue

                self._claims[message_id].add(candidate_str)
                content = await self._read_file(str(candidate))
                if content:
                    results.append(f"Instructions from: {candidate_str}\n{content}")

            current = current.parent

        return results

    # ── helpers ─────────────────────────────────────────────────────────

    async def _read_file(self, path: str) -> str:
        try:
            return await asyncio.to_thread(lambda: Path(path).read_text(encoding="utf-8", errors="replace"))
        except Exception:
            return ""

    async def _read_all(self, paths: list[str]) -> list[str]:
        """Parallel read all paths. Returns formatted instruction strings."""

        async def read_one(p: str) -> str | None:
            content = await self._read_file(p)
            if content:
                return f"Instructions from: {p}\n{content}"
            return None

        results = await asyncio.gather(*[read_one(p) for p in paths])
        return [r for r in results if r is not None]

    @staticmethod
    def loaded_from_history(messages: list) -> set[str]:
        """Extract already-loaded instruction paths from conversation history.
        Mimics opencode's Instruction.loaded() — scans tool parts for
        metadata.loaded paths, skipping compacted parts."""
        paths: set[str] = set()
        for msg in messages:
            if not hasattr(msg, "tool_calls") and not hasattr(msg, "content"):
                continue
            # Scan tool parts from message
            content = getattr(msg, "content", "")
            if isinstance(content, str) and "Instructions from:" in content:
                for line in content.split("\n"):
                    if line.startswith("Instructions from:"):
                        p = line.replace("Instructions from:", "").strip()
                        if p:
                            paths.add(p)
        return paths
