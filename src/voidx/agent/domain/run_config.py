"""Fixed RunConfig preset catalog for agent profiles.

``run_mode`` is an internal fixed catalog, not a freely composable YAML field:
each preset derives a fixed control protocol, phase set, and lifecycle tool.
New combinations enter the catalog as new preset ids only.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

RunMode = Literal["single", "goal_eval", "loop_fixed", "loop_dynamic"]


class RunConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_mode: RunMode
    protocol: str
    phases: tuple[str, ...]
    lifecycle_tool: str


RUN_CONFIG_PRESETS: dict[str, RunConfig] = {
    preset.run_mode: preset
    for preset in (
        RunConfig(
            run_mode="single",
            protocol="turn",
            phases=("turn",),
            lifecycle_tool="turn",
        ),
        RunConfig(
            run_mode="goal_eval",
            protocol="goal",
            phases=("idle", "intake", "work", "evaluator"),
            lifecycle_tool="goal",
        ),
        RunConfig(
            run_mode="loop_fixed",
            protocol="loop",
            phases=("idle", "work"),
            lifecycle_tool="loop",
        ),
        RunConfig(
            run_mode="loop_dynamic",
            protocol="loop",
            phases=("idle", "work"),
            lifecycle_tool="loop",
        ),
    )
}


def resolve_run_config(run_mode: str) -> RunConfig:
    preset = RUN_CONFIG_PRESETS.get(run_mode.strip().lower())
    if preset is None:
        raise ValueError(f"unknown run_mode preset: {run_mode}")
    return preset
