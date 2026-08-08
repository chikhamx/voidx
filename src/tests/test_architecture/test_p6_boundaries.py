"""Architecture boundaries introduced by phase P6."""

from __future__ import annotations

import ast
from pathlib import Path

from voidx.agent.domain.user_profile import UserProfile
from voidx.llm.domain.model import ModelConfig, ReasoningEffort
from voidx.platform.code_ide import CodeIde
from voidx.skills.domain.selection import SkillSelectionConfig
from voidx.tooling.domain.web import WebToolRoute

ROOT = Path(__file__).resolve().parents[3]


def _defined_classes(path: str) -> set[str]:
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.AsyncFunctionDef, ast.FunctionDef))
    }


def test_feature_dtos_have_single_domain_owner() -> None:
    assert ModelConfig.__module__ == "voidx.llm.domain.model"
    assert ReasoningEffort.__module__ == "voidx.llm.domain.model"
    assert SkillSelectionConfig.__module__ == "voidx.skills.domain.selection"
    assert UserProfile.__module__ == "voidx.agent.domain.user_profile"
    assert CodeIde.__module__ == "voidx.platform.code_ide"
    assert WebToolRoute.__module__ == "voidx.tooling.domain.web"


def test_config_does_not_define_or_reexport_feature_dtos() -> None:
    config_models = _defined_classes("src/voidx/config/models.py")
    config_enums = _defined_classes("src/voidx/config/enums.py")
    assert config_models.isdisjoint(
        {"ModelConfig", "SkillSelectionConfig", "UserProfile", "WebToolRoute"}
    )
    assert config_enums.isdisjoint({"ReasoningEffort", "CodeIde"})

    import voidx.config as config

    for name in (
        "ModelConfig",
        "ReasoningEffort",
        "SkillSelectionConfig",
        "UserProfile",
        "CodeIde",
        "WebToolRoute",
    ):
        assert not hasattr(config, name), f"config reexports feature DTO {name}"


def test_moved_dto_defaults_and_schema_are_stable() -> None:
    model = ModelConfig()
    assert model.provider == "anthropic"
    assert model.model == "claude-sonnet-4-6"
    assert model.reasoning_effort is ReasoningEffort.XHIGH
    assert [effort.value for effort in ReasoningEffort] == [
        "none",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    assert SkillSelectionConfig().model_dump(mode="json") == {
        "enabled": [],
        "disabled": [],
        "auto": [],
    }
