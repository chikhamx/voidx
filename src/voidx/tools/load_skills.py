"""Load enabled skill bodies by name."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from voidx.skills.context import render_skill_tool_context
from voidx.skills.registry import SkillRegistry, normalize_skill_name
from voidx.skills.schema import SkillSelectionConfig
from voidx.skills.service import SkillService
from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema

_MAX_SKILL_NAMES = 5
_MAX_OUTPUT_CHARS = 24_000


class LoadSkillsInput(BaseModel):
    names: list[str] = Field(
        min_length=1,
        max_length=_MAX_SKILL_NAMES,
        description=(
            "Skill names to load. Use normalized skill names only, not paths. "
            f"At most {_MAX_SKILL_NAMES} names."
        ),
    )
    include_bundled: bool = Field(
        default=False,
        description=(
            "Legacy compatibility flag. Built-in workflows are structured "
            "runtime nodes and are not loaded by this tool."
        ),
    )


class LoadSkillsTool(BaseTool):
    id = "load_skills"
    description = (
        "Load enabled skill instructions by normalized skill name. Use this when "
        "a project/global skill listed in Available Skills or explicitly named "
        "by the user is relevant and you need its full instructions. This is "
        "read-only and does not accept file paths."
    )

    def __init__(self, settings=None) -> None:
        super().__init__()
        self._settings = settings
        self._skill_service: SkillService | None = None
        self._skill_service_signature: tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]] | None = None

    def parameters_schema(self) -> dict:
        return model_to_json_schema(LoadSkillsInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = LoadSkillsInput.model_validate(args)
        service = self._skill_service_for(ctx.workspace)

        names = _normalized_unique(inp.names)
        if not names:
            return _error_result(
                invalid=["<empty>"],
                missing=[],
                disabled=[],
                bundled_blocked=[],
            )
        invalid = [name for name in names if _looks_like_path(name)]
        missing: list[str] = []
        disabled: list[str] = []
        bundled_blocked: list[str] = []
        instructions: list[str] = []
        loaded: list[dict[str, str]] = []

        if invalid:
            return _error_result(
                invalid=invalid,
                missing=[],
                disabled=[],
                bundled_blocked=[],
            )

        for name in names:
            skill = service.get(name)
            if skill is None:
                missing.append(name)
                continue
            if not service.is_enabled(skill):
                disabled.append(name)
                continue
            if skill.meta.scope == "bundled" and not inp.include_bundled:
                bundled_blocked.append(name)
                continue
            instructions.append(service.render_instruction(skill))
            loaded.append({
                "name": skill.name,
                "scope": skill.meta.scope,
                "path": str(skill.path),
            })

        if missing or disabled or bundled_blocked:
            return _error_result(
                invalid=[],
                missing=missing,
                disabled=disabled,
                bundled_blocked=bundled_blocked,
                loaded=loaded,
            )

        output = render_skill_tool_context(instructions)
        truncated = False
        if len(output) > _MAX_OUTPUT_CHARS:
            output = (
                output[:_MAX_OUTPUT_CHARS].rstrip()
                + "\n\n[load_skills output truncated: total skill body limit reached]"
            )
            truncated = True

        return ToolResult(
            title=f"Loaded skills: {', '.join(item['name'] for item in loaded)}",
            output=output,
            summary=f"loaded {len(loaded)} skills",
            metadata={
                "loaded_skills": loaded,
                "count": len(loaded),
                "truncated": truncated,
            },
        )

    def _skill_service_for(self, workspace: str) -> SkillService:
        selection = self._settings.get_skill_selection() if self._settings is not None else None
        signature = _skill_service_signature(workspace, selection)
        if self._skill_service is None or self._skill_service_signature != signature:
            self._skill_service = SkillService(
                SkillRegistry(signature[0]),
                selection=selection,
            )
            self._skill_service_signature = signature
        return self._skill_service


def _normalized_unique(names: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in names:
        name = normalize_skill_name(raw)
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(name)
    return result


def _looks_like_path(name: str) -> bool:
    return (
        "/" in name
        or "\\" in name
        or name in {".", ".."}
        or name.startswith(".")
        or name.endswith(".md")
        or name.endswith(".markdown")
    )


def _skill_service_signature(
    workspace: str,
    selection: SkillSelectionConfig | None,
) -> tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    resolved = str(Path(workspace).resolve())
    if selection is None:
        return resolved, (), (), ()
    return resolved, tuple(sorted(selection.enabled)), tuple(sorted(selection.disabled)), tuple(sorted(selection.auto))


def _error_result(
    *,
    invalid: list[str],
    missing: list[str],
    disabled: list[str],
    bundled_blocked: list[str],
    loaded: list[dict[str, str]] | None = None,
) -> ToolResult:
    parts = ["Could not load one or more skills."]
    if invalid:
        parts.append(f"Invalid skill names: {', '.join(invalid)}")
    if missing:
        parts.append(f"Missing skills: {', '.join(missing)}")
    if disabled:
        parts.append(f"Disabled skills: {', '.join(disabled)}")
    if bundled_blocked:
        parts.append(
            "Bundled skills are not part of workflow runtime loading: "
            + ", ".join(bundled_blocked)
        )
    return ToolResult(
        title="load_skills failed",
        output="\n".join(parts),
        metadata={
            "error": True,
            "invalid": invalid,
            "missing": missing,
            "disabled": disabled,
            "bundled_blocked": bundled_blocked,
            "loaded_skills": loaded or [],
        },
    )
