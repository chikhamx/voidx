"""Unified skill management tool: load, create, list."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from voidx.skills.context import render_skill_tool_context
from voidx.skills.registry import SkillRegistry, normalize_skill_name
from voidx.skills.application.api import SkillsApi
from voidx.skills.service import SkillService
from voidx.tooling.domain.context import ToolExecutionContext as ToolContext
from voidx.tooling.domain.result import ToolResult
from voidx.tooling.domain.arguments import (
    drop_nullish_tool_fields,
    keep_tool_args,
)
from voidx.tooling.domain.schema import model_to_json_schema

_MAX_OUTPUT_CHARS = 24_000


class SkillsInput(BaseModel):
    """Unified skill management: load, create, or list skills."""

    op: Literal["load", "create", "list"] = Field(
        description=(
            "Operation: 'load' (fetch a skill's instructions), 'create' "
            "(write a new SKILL.md), 'list' (enumerate discovered skills)."
        )
    )
    name: str | None = Field(
        default=None,
        description=(
            "Skill name. Required for op=load and op=create. Lowercase, "
            "hyphen-separated (e.g. 'react-patterns')."
        ),
    )
    description: str | None = Field(
        default=None,
        description="One-line summary. Required for op=create.",
    )
    body: str | None = Field(
        default=None,
        description="Markdown instruction body. Required for op=create.",
    )
    scope: Literal["project", "global"] = Field(
        default="project",
        description=(
            "Write scope for op=create: project writes .voidx/skills/<name>/SKILL.md; "
            "global writes ~/.voidx/skills/<name>/SKILL.md."
        ),
    )


def _normalize_skill_args(args):
    if not isinstance(args, dict):
        return args
    op = str(args.get("op") or "").strip().lower()
    if op == "list":
        return keep_tool_args(args, {"op"})
    if op == "load":
        normalized = keep_tool_args(args, {"op", "name"})
        return drop_nullish_tool_fields(normalized, "name")
    if op == "create":
        normalized = keep_tool_args(args, {"op", "name", "description", "body", "scope"})
        return drop_nullish_tool_fields(normalized, "scope")
    return args


class SkillsTool:
    id = "skill"
    description = (
        "Load skill instructions, create a new SKILL.md, or list discovered skills. "
        "Load/list are read-only; create writes a SKILL.md file."
    )

    def __init__(self, skills_api_provider: Callable[[str], SkillsApi]) -> None:
        super().__init__()
        self._skills_api_provider = skills_api_provider

    def parameters_schema(self) -> dict:
        return model_to_json_schema(SkillsInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        args = _normalize_skill_args(args)
        try:
            inp = SkillsInput.model_validate(args)
        except Exception as exc:
            return ToolResult(output=f"Invalid arguments: {exc}", metadata={"error": True})
        if inp.op == "load":
            return self._execute_load(inp, ctx)
        if inp.op == "create":
            return self._execute_create(inp, ctx)
        return self._execute_list(inp, ctx)

    def _execute_load(self, inp: SkillsInput, ctx: ToolContext) -> ToolResult:
        raw_name = inp.name or ""
        name = normalize_skill_name(raw_name)
        if _looks_like_path(raw_name):
            return _error_result(invalid=[name])
        if not name:
            return _error_result(missing=[raw_name])

        service = self._skill_service_for(ctx.workspace)
        skill = service.get(name)
        if skill is None:
            available = [s.name for s in service.list_skills()]
            return _error_result(missing=[name], available=available)
        if not service.is_enabled(skill):
            return _error_result(disabled=[name])

        instruction = service.render_instruction(skill)
        output = render_skill_tool_context([instruction])
        truncated = False
        if len(output) > _MAX_OUTPUT_CHARS:
            output = (
                output[:_MAX_OUTPUT_CHARS].rstrip()
                + "\n\n[skill output truncated: total skill body limit reached]"
            )
            truncated = True

        return ToolResult(
            title=f"Loaded skill: {skill.name}",
            output=output,
            summary=f"loaded skill {skill.name}",
            metadata={
                "loaded_skills": [{
                    "name": skill.name,
                    "scope": skill.meta.scope,
                    "path": str(skill.path),
                }],
                "count": 1,
                "truncated": truncated,
            },
        )

    def _execute_create(self, inp: SkillsInput, ctx: ToolContext) -> ToolResult:
        name = inp.name or ""
        description = inp.description or ""
        body = inp.body or ""

        registry = self._skills_api_provider(ctx.workspace).service.registry
        try:
            path = registry.create_skill(name, description, body, scope=inp.scope)
        except ValueError as exc:
            return ToolResult(
                title="skill create failed",
                output=str(exc),
                metadata={"error": True, "name": name},
            )
        except OSError as exc:
            return ToolResult(
                title="skill create failed",
                output=f"Could not write skill '{name}': {exc}",
                metadata={"error": True, "name": name},
            )

        if path is None:
            existing = self._existing_skill_path(registry, name, inp.scope)
            return ToolResult(
                title=f"Skill '{name}' already exists",
                output=(
                    f"Skill '{name}' already exists at {existing}. "
                    f"Use a different name or edit the existing file directly."
                ),
                metadata={"name": name, "scope": inp.scope, "path": str(existing)},
            )

        return ToolResult(
            title=f"Created skill: {name}",
            output=(
                f"Created skill '{name}' at {path}. "
                f"Reference it with ${name} in conversation, "
                f"or run /skills auto {name} to enable automatic selection."
            ),
            summary=f"created skill {name}",
            metadata={"path": str(path), "name": name, "scope": inp.scope},
        )

    def _execute_list(self, inp: SkillsInput, ctx: ToolContext) -> ToolResult:
        service = self._skill_service_for(ctx.workspace)
        skills = service.list_skills()
        rows = []
        structured = []
        for skill in skills:
            enabled = service.is_enabled(skill)
            desc = skill.meta.description.strip()
            rows.append(f"{skill.name}\t{skill.meta.scope}\t{enabled}\t{desc}")
            structured.append({
                "name": skill.name,
                "scope": skill.meta.scope,
                "enabled": enabled,
                "description": desc,
            })

        output = "\n".join(rows) if rows else "(no skills found)"
        return ToolResult(
            title=f"Skills: {len(skills)}",
            output=output,
            summary=f"listed {len(skills)} skills",
            metadata={"skills": structured, "count": len(skills)},
        )

    def _existing_skill_path(self, registry: SkillRegistry, name: str, scope: str) -> Path:
        root = registry.project_dir if scope == "project" else registry.global_dir
        return root / name / "SKILL.md"

    def _skill_service_for(self, workspace: str) -> SkillService:
        return self._skills_api_provider(workspace).service


def _looks_like_path(name: str) -> bool:
    return (
        "/" in name
        or "\\" in name
        or name in {".", ".."}
        or name.startswith(".")
        or name.endswith(".md")
        or name.endswith(".markdown")
    )




def _error_result(
    *,
    invalid: list[str] | None = None,
    missing: list[str] | None = None,
    disabled: list[str] | None = None,
    available: list[str] | None = None,
) -> ToolResult:
    parts = ["Could not load skill."]
    if invalid:
        parts.append(f"Invalid skill name: {', '.join(invalid)}")
    if missing:
        parts.append(f"Missing skill: {', '.join(missing)}")
    if disabled:
        parts.append(f"Disabled skill: {', '.join(disabled)}")
    if available:
        parts.append(f"Available skills: {', '.join(available)}")
    return ToolResult(
        title="skill failed",
        output="\n".join(parts),
        metadata={
            "error": True,
            "invalid": invalid or [],
            "missing": missing or [],
            "disabled": disabled or [],
        },
    )
