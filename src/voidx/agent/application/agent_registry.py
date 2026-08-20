"""Agent profile registry — three-layer discovery, caching, and resolution.

Mirrors ``SkillRegistry``: bundled first, then global, then project; later
sources override earlier sources with the same profile name. A corrupted file
never enters the registry — the last valid snapshot keeps serving and the
corruption is reported as diagnostics. No file watcher: every ``discover``
compares the on-disk signature and rebuilds when it changed.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from voidx.agent.application.agent_profile_loader import (
    ProfileLoaderContext,
    ProfileLoadError,
    load_profile,
)
from voidx.agent.domain.agent_profile import (
    AgentProfileInfo,
    ProfileDiagnostic,
    ProfileSource,
    ResolvedAgentProfile,
    normalize_profile_name,
)
from voidx.platform.paths import voidx_global_agents_dir, voidx_workspace_agents_dir

DEFAULT_BUNDLED_AGENTS_DIR = Path(__file__).resolve().parent.parent / "bundled" / "agents"


class AgentRegistry:
    """Discovers bundled, global, and project agent profiles."""

    def __init__(
        self,
        workspace: str,
        *,
        bundled_dir: Path | None = None,
        global_dir: Path | None = None,
        project_dir: Path | None = None,
        loader_context: ProfileLoaderContext | Callable[[], ProfileLoaderContext] | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.bundled_dir = bundled_dir or DEFAULT_BUNDLED_AGENTS_DIR
        self.global_dir = global_dir or voidx_global_agents_dir()
        self.project_dir = project_dir or voidx_workspace_agents_dir(self.workspace)
        self._loader_context = loader_context
        self._resolved: dict[str, ResolvedAgentProfile] | None = None
        self._infos: list[AgentProfileInfo] | None = None
        self._signature: tuple[tuple[str, str, int, int], ...] | None = None
        # Last valid snapshot per file path; survives later corruption so a
        # broken edit never evicts a previously working profile.
        self._last_good: dict[str, ResolvedAgentProfile] = {}

    def discover(self) -> list[AgentProfileInfo]:
        signature = self._discover_signature()
        if self._infos is not None and self._signature == signature:
            return self._infos
        context = self._resolve_context()
        resolved: dict[str, ResolvedAgentProfile] = {}
        infos: dict[str, AgentProfileInfo] = {}
        for scope, root in self._layers():
            for path in self._profile_files(root):
                info, profile = self._load_file(path, scope, context)
                key = normalize_profile_name(info.name)
                infos[key] = info
                if profile is not None:
                    resolved[key] = profile
                else:
                    # An unavailable override shadows lower layers: falling back
                    # to a different source would silently change behavior.
                    resolved.pop(key, None)
        self._resolved = resolved
        self._infos = [infos[name] for name in sorted(infos)]
        self._signature = signature
        return self._infos

    def resolve(self, name: str) -> ResolvedAgentProfile:
        """Resolve one profile by name to its immutable snapshot."""
        self.discover()
        assert self._resolved is not None and self._infos is not None
        key = normalize_profile_name(name)
        profile = self._resolved.get(key)
        if profile is not None:
            return profile
        for info in self._infos:
            if info.name == key:
                diagnostics = list(info.diagnostics) or [
                    ProfileDiagnostic(path="", code="unavailable", message=f"agent profile unavailable: {key}")
                ]
                raise ProfileLoadError(diagnostics)
        raise KeyError(f"unknown agent profile: {key}")

    def invalidate(self) -> None:
        self._resolved = None
        self._infos = None
        self._signature = None

    def _layers(self) -> tuple[tuple[ProfileSource, Path], ...]:
        return (
            ("bundled", self.bundled_dir),
            ("global", self.global_dir),
            ("project", self.project_dir),
        )

    def _resolve_context(self) -> ProfileLoaderContext:
        context = self._loader_context
        if context is None:
            return ProfileLoaderContext()
        return context() if callable(context) else context

    @staticmethod
    def _profile_files(root: Path) -> list[Path]:
        if not root.is_dir():
            return []
        return sorted(root.glob("*.yaml"))

    def _discover_signature(self) -> tuple[tuple[str, str, int, int], ...]:
        entries: list[tuple[str, str, int, int]] = []
        for scope, root in self._layers():
            for path in self._profile_files(root):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                entries.append((scope, str(path), stat.st_mtime_ns, stat.st_size))
        return tuple(entries)

    def _load_file(
        self,
        path: Path,
        scope: ProfileSource,
        context: ProfileLoaderContext,
    ) -> tuple[AgentProfileInfo, ResolvedAgentProfile | None]:
        path_key = str(path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            diagnostic = ProfileDiagnostic(path="", code="read_error", message=str(exc))
            return (
                AgentProfileInfo(name=path.stem, source=scope, available=False, diagnostics=(diagnostic,)),
                None,
            )
        try:
            resolved, warnings = load_profile(
                text, source=scope, context=context, expected_name=path.stem
            )
        except ProfileLoadError as exc:
            stale = self._last_good.get(path_key)
            if stale is not None:
                info = self._info_from_resolved(
                    stale, available=False, diagnostics=tuple(exc.diagnostics)
                )
                return info, None
            return (
                AgentProfileInfo(
                    name=path.stem,
                    source=scope,
                    available=False,
                    diagnostics=tuple(exc.diagnostics),
                ),
                None,
            )
        self._last_good[path_key] = resolved
        return self._info_from_resolved(resolved, available=True, diagnostics=warnings), resolved

    @staticmethod
    def _info_from_resolved(
        resolved: ResolvedAgentProfile,
        *,
        available: bool,
        diagnostics: tuple[ProfileDiagnostic, ...],
    ) -> AgentProfileInfo:
        return AgentProfileInfo(
            name=resolved.snapshot.profile_id,
            display_name=resolved.runtime_profile.name,
            revision=resolved.snapshot.revision,
            content_hash=resolved.snapshot.content_hash,
            source=resolved.snapshot.source,
            run_mode=resolved.run_config.run_mode,
            hitl_mode=resolved.resource_policy.hitl_mode,
            available=available,
            diagnostics=diagnostics,
        )


_REGISTRIES: dict[str, AgentRegistry] = {}


def agent_registry_for(workspace: str) -> AgentRegistry:
    """Process-wide registry cache keyed by workspace.

    Each registry keeps its own signature cache, so file changes are picked up
    on the next discover() without a watcher; reuse only avoids re-construction.
    """
    key = str(Path(workspace).resolve())
    registry = _REGISTRIES.get(key)
    if registry is None:
        registry = AgentRegistry(key)
        _REGISTRIES[key] = registry
    return registry
