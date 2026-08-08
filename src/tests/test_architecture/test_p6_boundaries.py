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


PROVIDER_ORDER = (
    "anthropic",
    "deepseek",
    "doubao",
    "gemini",
    "kimi",
    "longcat",
    "mimo",
    "mimo-token-plan",
    "minimax",
    "openai",
    "openrouter",
    "qwen",
    "typex",
    "xunfei-coding-plan",
    "zhipu",
)


def test_importing_provider_package_is_lazy_and_side_effect_free() -> None:
    import subprocess
    import sys

    script = """
import json
import sys
before = set(sys.modules)
import voidx.llm.providers
loaded = sorted(
    name for name in set(sys.modules) - before
    if name.startswith('voidx.llm.providers.')
)
print(json.dumps(loaded))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        cwd=ROOT / "src",
    )
    loaded = __import__("json").loads(result.stdout)
    assert loaded in ([], ["voidx.llm.providers.base"]), loaded


def test_provider_modules_are_pure_specs_without_registration() -> None:
    providers_root = ROOT / "src/voidx/llm/providers"
    excluded = {"__init__.py", "base.py", "catalog.py", "common.py"}
    offenders: list[str] = []
    for path in sorted(providers_root.glob("*.py")):
        if path.name in excluded:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "voidx.llm.providers.base" and alias.asname == "base":
                        offenders.append(f"{path.name}:{node.lineno}:import base as base")
            if isinstance(node, ast.Call):
                func = node.func
                if (isinstance(func, ast.Name) and func.id == "register") or (
                    isinstance(func, ast.Attribute) and func.attr == "register"
                ):
                    offenders.append(f"{path.name}:{node.lineno}:register call")
    assert offenders == []


def test_provider_base_has_no_mutable_registry_api() -> None:
    tree = ast.parse(
        (ROOT / "src/voidx/llm/providers/base.py").read_text(encoding="utf-8")
    )
    names = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    assignments = {
        target.id
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (
            node.targets if isinstance(node, ast.Assign) else [node.target]
        )
        if isinstance(target, ast.Name)
    }
    assert "_SPECS" not in assignments
    assert names.isdisjoint({"register", "get", "all_specs", "load_builtins"})


def test_provider_catalog_has_the_only_canonical_order() -> None:
    from voidx.llm.providers.catalog import PROVIDER_SPECS

    names = tuple(spec.name for spec in PROVIDER_SPECS)
    assert names == PROVIDER_ORDER
    assert len(names) == len(set(names))


def test_bootstrap_explicitly_composes_provider_specs() -> None:
    source = (ROOT / "src/voidx/bootstrap/providers.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "voidx.llm.providers.catalog" in imported_modules
    assert "PROVIDER_SPECS" in source
