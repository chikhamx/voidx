"""Architecture boundaries introduced by phase P6."""

from __future__ import annotations

import ast
from pathlib import Path

from voidx.agent.domain.user_profile import UserProfile
from voidx.llm.domain.model import ModelConfig, ReasoningEffort
from voidx.platform.code_ide import CodeIde
from voidx.skills.domain.selection import SkillSelectionConfig
from voidx.tooling.domain.web import WebToolRoute

from .import_graph import _walk_imports, format_edges, import_edges, top_level

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


def test_llm_catalog_has_no_process_global_binding_or_legacy_module() -> None:
    assert not (ROOT / "src/voidx/llm/catalog.py").exists()

    for relative in (
        "src/voidx/llm/application/model_catalog.py",
        "src/voidx/llm/adapters/http_model_discovery.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = {
            target.id
            for node in tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            if isinstance(target, ast.Name)
        }
        functions = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert names.isdisjoint({"_settings", "_fetchers"})
        assert functions.isdisjoint({"bind_settings", "register_fetcher"})


def test_llm_application_does_not_import_adapters_or_config() -> None:
    offenders: list[str] = []
    for path in (ROOT / "src/voidx/llm/application").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if node.module.startswith((
                "voidx.llm.adapters",
                "voidx.config",
                "voidx.logging",
            )):
                offenders.append(f"{path.name}:{node.lineno}:{node.module}")
    assert offenders == []


def test_llm_provider_factory_is_owned_by_adapter() -> None:
    assert not (ROOT / "src/voidx/llm/provider.py").exists()
    assert not (ROOT / "src/voidx/llm/service.py").exists()

    adapter = ROOT / "src/voidx/llm/adapters/langchain_model_factory.py"
    assert adapter.exists()
    adapter_source = adapter.read_text(encoding="utf-8")
    assert "langchain_anthropic" in adapter_source
    assert "langchain_core" in adapter_source

    offenders: list[str] = []
    for layer in ("domain", "application"):
        for path in (ROOT / f"src/voidx/llm/{layer}").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                module = ""
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                elif isinstance(node, ast.Import):
                    module = ",".join(alias.name for alias in node.names)
                if module.startswith(("langchain", "voidx.llm.adapters")):
                    offenders.append(f"{path.name}:{node.lineno}:{module}")
    assert offenders == []


def _imports_under(root: Path) -> dict[Path, set[str]]:
    imports: dict[Path, set[str]] = {}
    for path in sorted(root.rglob("*.py")):
        modules: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
        imports[path] = modules
    return imports


def test_skills_layers_have_one_way_dependencies() -> None:
    skills_root = ROOT / "src/voidx/skills"
    domain_imports = _imports_under(skills_root / "domain")
    application_imports = _imports_under(skills_root / "application")

    domain_forbidden = ("voidx.config", "voidx.agent", "voidx.skills.service", "voidx.skills.registry")
    application_forbidden = ("voidx.config", "voidx.skills.adapters")
    assert {
        str(path.relative_to(ROOT)): sorted(module for module in modules if module.startswith(domain_forbidden))
        for path, modules in domain_imports.items()
        if any(module.startswith(domain_forbidden) for module in modules)
    } == {}
    assert {
        str(path.relative_to(ROOT)): sorted(module for module in modules if module.startswith(application_forbidden))
        for path, modules in application_imports.items()
        if any(module.startswith(application_forbidden) for module in modules)
    } == {}


def test_skills_legacy_references_and_production_fallbacks_are_gone() -> None:
    assert not (ROOT / "src/voidx/skills/references.py").exists()

    for relative in (
        "src/voidx/agent/infrastructure/langgraph/execution.py",
        "src/voidx/agent/application/instruction.py",
        "src/voidx/agent/slash/commands/skills.py",
        "src/voidx/presentation/tools/skill_picker.py",
        "src/voidx/presentation/gateway/session/method/references.py",
        "src/voidx/presentation/gateway/session/method/integrations.py",
        "src/voidx/tooling/adapters/skills.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "Settings(" not in source, relative
        assert "SkillService(" not in source, relative
        assert "SkillRegistry(" not in source, relative
        assert "SkillService.for_workspace(" not in source, relative


def test_feature_packages_do_not_import_config() -> None:
    features = {"agent", "tooling", "llm", "mcp", "lsp", "skills"}
    violations = [
        edge
        for edge in import_edges()
        if top_level(edge.source) in features and top_level(edge.target) == "config"
    ]
    assert violations == [], "feature packages import config:\n" + format_edges(violations)


def test_import_graph_detects_nested_literal_dynamic_imports() -> None:
    statements = ast.parse(
        'assigned = importlib.import_module("voidx.config")\n'
        'def load():\n    return __import__("voidx.config.models")\n'
    ).body

    refs = _walk_imports("voidx.agent.sample", statements, source_is_package=False)

    assert {(ref.target, ref.dynamic) for ref in refs} == {
        ("voidx.config", True),
        ("voidx.config.models", True),
    }
