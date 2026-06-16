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
from collections.abc import Iterable
from dataclasses import dataclass, field
import logging
from pathlib import Path

import httpx

from voidx.skills.registry import SkillRegistry
from voidx.skills.schema import SkillSelectionConfig
from voidx.skills.service import SkillService
from voidx.workflow.service import WorkflowService
from voidx.workflow.types import WorkflowRunState

INSTRUCTION_FILES = ["AGENTS.md", "CLAUDE.md"]  # CLAUDE.md for compat

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkflowRuntimeContext:
    instructions: list[str]
    active: list[str]
    content: str = ""
    runs: list[WorkflowRunState] = field(default_factory=list)


@dataclass
class _FileContentCacheEntry:
    mtime_ns: int
    size: int
    content: str


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
        self._file_cache: dict[str, _FileContentCacheEntry] = {}
        self._skill_registry = SkillRegistry(str(self._workspace))
        self._skill_service: SkillService | None = None
        self._skill_service_signature: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]] | None = None
        self._workflow_service = WorkflowService()
        self._debug = False

    # ── public API ──────────────────────────────────────────────────────

    def clear(self, message_id: str) -> None:
        """Clear claims for a message (called when message is removed)."""
        self._claims.pop(message_id, None)

    def set_debug(self, value: bool) -> None:
        self._debug = value

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

        # 2. Project: walk up from workspace. At each level, try
        # INSTRUCTION_FILES in order and stop after the first matching file.
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
            break  # first matching instruction file wins

        self._system_paths = paths
        return paths

    async def system(self) -> list[str]:
        """Read all system instruction files. Returns list of
        'Instructions from: <path>\n<content>' strings."""
        paths = await self.system_paths()
        instructions = await self._read_all(paths)
        available_skills = await self.available_skills_section()
        if available_skills:
            instructions.append(available_skills)
        return instructions

    async def available_skills_section(self) -> str:
        service = self._skill_service_for_current_selection()
        summaries = await asyncio.to_thread(service.available_skill_summaries)
        if not summaries:
            return ""
        return "## Available Skills\n" + "\n".join(summaries)

    async def workflow_context_for(
        self,
        user_text: str,
        *,
        agent: str = "",
        task_intent: str | None = None,
        goal_type: str | None = None,
        interaction_mode: str | None = None,
        scope: str = "",
        runtime_trigger: str | None = None,
        exclude_names: list[str] | None = None,
        active_names: list[str] | None = None,
        workflow_start: str | None = None,
    ) -> WorkflowRuntimeContext:
        service = self._workflow_service
        nodes = await asyncio.to_thread(service.nodes)

        if workflow_start:
            matches = await asyncio.to_thread(
                service.select_from_start,
                str(workflow_start),
                goal_type=goal_type,
            )
        else:
            matches = []

        active = _merged_names(active_names or (), [match.name for match in matches])
        instructions = [
            service.render_instruction(node)
            for node in nodes
            if node.name in active
        ]
        return WorkflowRuntimeContext(
            instructions=instructions,
            active=[f"{match.name} ({match.reason})" for match in matches],
            content=service.context(active_names=active),
            runs=service.runs_from_matches(matches, goal_type=goal_type, scope=scope),
        )

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
                    if self._debug:
                        logger.debug(
                            "Injected instruction file for %s: %s",
                            filepath,
                            candidate_str,
                        )

            current = current.parent

        return results

    # ── helpers ─────────────────────────────────────────────────────────

    def _skill_service_for_current_selection(self) -> SkillService:
        selection = self._settings.get_skill_selection() if self._settings is not None else None
        signature = _skill_selection_signature(selection)
        if self._skill_service is None or self._skill_service_signature != signature:
            self._skill_service = SkillService(
                self._skill_registry,
                selection=selection,
            )
            self._skill_service_signature = signature
        return self._skill_service

    async def _read_file(self, path: str) -> str:
        target = Path(path)
        resolved = str(target.resolve())
        try:
            stat = await asyncio.to_thread(target.stat)
            cached = self._file_cache.get(resolved)
            if (
                cached is not None
                and cached.mtime_ns == stat.st_mtime_ns
                and cached.size == stat.st_size
            ):
                return cached.content
            content = await asyncio.to_thread(
                lambda: target.read_text(encoding="utf-8", errors="replace")
            )
            self._file_cache[resolved] = _FileContentCacheEntry(
                mtime_ns=stat.st_mtime_ns,
                size=stat.st_size,
                content=content,
            )
            return content
        except Exception:
            self._file_cache.pop(resolved, None)
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


def _skill_selection_signature(
    selection: SkillSelectionConfig | None,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    if selection is None:
        return (), (), ()
    return tuple(sorted(selection.enabled)), tuple(sorted(selection.disabled)), tuple(sorted(selection.auto))


def _merged_names(*groups: Iterable[str]) -> set[str]:
    names: set[str] = set()
    for group in groups:
        for name in group:
            normalized = name.strip().lower()
            if normalized:
                names.add(normalized)
    return names
