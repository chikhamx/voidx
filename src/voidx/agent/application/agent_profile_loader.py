"""Strict YAML loader for agent profile files.

Produces an immutable ``ResolvedAgentProfile`` from profile YAML text. Every
hard error rejects the load with stable ``path``/``code``/``message``
diagnostics; nothing is silently dropped, downgraded, or replaced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from voidx.agent.domain.agent_profile import (
    PROFILE_NAME_RE,
    AgentProfileSnapshot,
    ProfileDiagnostic,
    ProfileSource,
    ResolvedAgentProfile,
    ResourcePolicy,
    WorkflowRuntimeContext,
    content_hash_of,
    normalize_profile_name,
)
from voidx.agent.domain.automation.workflow_dag import DEFAULT_WORKFLOW_DAG
from voidx.agent.domain.automation.workflow_schema import (
    Edge,
    NodeGate,
    NodeIO,
    NodeSubworkflow,
    TerminalExit,
    WorkflowDAG,
    WorkflowNode,
    WorkflowStep,
)
from voidx.agent.domain.profile import RuntimeProfile
from voidx.agent.application.legacy_runtime_profile import bundled_legacy_runtime_profile
from voidx.agent.domain.prompt_policy import PROMPT_POLICY_REGISTRY, resolve_prompt_policy
from voidx.agent.domain.run_config import resolve_run_config
from voidx.agent.domain.task.intent import PersonaName

LIFECYCLE_TOOL_NAMES = frozenset({"turn", "goal", "loop"})
SUPPRESSIBLE_SECTIONS = frozenset({"Persona", "Agent Role", "Workflow Runtime", "Current Task State"})
RESERVED_SECTIONS = frozenset({
    "Base System", "Runtime State", "ExecutionPolicy", "RuntimeEnvelope",
    "Project Instructions", "Session Time",
})
HITL_MODES = frozenset({"interactive", "autonomous"})
PERSONA_VALUES = frozenset(persona.value for persona in PersonaName)


# ── errors & context ─────────────────────────────────────────────────────


class ProfileLoadError(ValueError):
    def __init__(self, diagnostics: list[ProfileDiagnostic]) -> None:
        self.diagnostics = diagnostics
        summary = "; ".join(f"{d.path}: {d.code}" for d in diagnostics)
        super().__init__(summary or "profile load failed")


@dataclass(frozen=True)
class ProfileLoaderContext:
    """External catalogs the loader validates references against."""

    known_tools: frozenset[str] = frozenset()
    available_skills: frozenset[str] = frozenset()
    available_mcp_servers: frozenset[str] = frozenset()
    builtin_nodes: Mapping[str, WorkflowNode] | None = None

    def nodes(self) -> Mapping[str, WorkflowNode]:
        return self.builtin_nodes if self.builtin_nodes is not None else DEFAULT_WORKFLOW_DAG.nodes


# ── strict YAML parsing ──────────────────────────────────────────────────


class _DuplicateKeyError(ValueError):
    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(key)


class _StrictYAMLLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False):
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in mapping:
            raise _DuplicateKeyError(str(key))
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


_StrictYAMLLoader.add_constructor("tag:yaml.org,2002:map", _construct_mapping)


# ── raw YAML schema (extra=forbid; semantic checks run separately) ───────


class _RawIO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: dict[str, str]
    output: dict[str, str]


class _RawGate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = ""
    required_before_transition: str = ""


class _RawStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order: int
    action: str
    description: str = ""


class _RawSubworkflow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    steps: list[_RawStep]
    exit_condition: str


class _RawNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    ref: str | None = None
    goal: str | None = None
    description: str | None = None
    io: _RawIO | None = None
    persona: str | None = None
    gate: _RawGate | None = None
    workflow: list[_RawStep] | None = None
    subworkflow: _RawSubworkflow | None = None
    rules: list[str] | None = None
    exceptions: list[str] | None = None


class _RawEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    target: str
    condition: str
    label: str = ""
    description: str = ""


class _RawTerminalExit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: str
    label: str = "end"
    description: str = ""


class _RawWorkflow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    nodes: list[_RawNode]
    edges: list[_RawEdge] = []
    terminal_exit: _RawTerminalExit | None = None


class _RawTools(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow: list[str] | None = None
    block: list[str] | None = None


class _RawProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    revision: int
    display_name: str = ""
    prompt_policy: str = "coding"
    identity: str = ""
    style_rules: list[str] = []
    extra_rules: list[str] = []
    persona: str | None = None
    suppress_sections: list[str] = []
    workflow: _RawWorkflow | None = None
    run_mode: str = "single"
    hitl_mode: str = "interactive"
    tools: _RawTools | None = None
    skills: list[str] | None = None
    mcp_servers: list[str] | None = None


# ── diagnostics helpers ──────────────────────────────────────────────────


def _diag(path: str, code: str, message: str, severity: str = "error") -> ProfileDiagnostic:
    return ProfileDiagnostic(path=path, code=code, message=message, severity=severity)  # type: ignore[arg-type]


def _pydantic_diagnostics(error: ValidationError) -> list[ProfileDiagnostic]:
    diagnostics: list[ProfileDiagnostic] = []
    for err in error.errors():
        path = ".".join(str(part) for part in err["loc"])
        err_type = str(err["type"])
        if err_type == "extra_forbidden":
            code = "unknown_field"
        elif err_type == "missing":
            code = "required"
        elif err_type.endswith("_parsing") or err_type.endswith("_type"):
            code = "type_error"
        else:
            code = "value_error"
        diagnostics.append(_diag(path, code, str(err["msg"])))
    return diagnostics




# ── workflow expansion & validation ──────────────────────────────────────


def _expand_node(
    raw: _RawNode,
    index: int,
    context: ProfileLoaderContext,
    diagnostics: list[ProfileDiagnostic],
) -> WorkflowNode | None:
    base_path = f"workflow.nodes.{index}"
    name = raw.name.strip().lower() if raw.name else None
    ref = raw.ref.strip().lower() if raw.ref else None

    if (name is None) == (ref is None):
        diagnostics.append(_diag(
            base_path, "node_identity",
            "workflow node must set exactly one of name (custom) or ref (builtin)",
        ))
        return None

    if ref is not None:
        builtin = context.nodes().get(ref)
        if builtin is None:
            diagnostics.append(_diag(f"{base_path}.ref", "unknown_ref", f"unknown builtin node: {ref}"))
            return None
        node = builtin.model_copy(deep=True)
        if raw.goal is not None:
            node.goal = raw.goal
        if raw.description is not None:
            node.description = raw.description
        if raw.io is not None:
            node.io = NodeIO(input=raw.io.input, output=raw.io.output)
        if raw.persona is not None:
            node.persona = raw.persona.strip().lower()
        if raw.gate is not None:
            node.gate = NodeGate(
                description=raw.gate.description,
                required_before_transition=raw.gate.required_before_transition,
            )
        if raw.workflow is not None:
            node.workflow = [WorkflowStep(**step.model_dump()) for step in raw.workflow]
        if raw.subworkflow is not None:
            node.subworkflow = NodeSubworkflow(**raw.subworkflow.model_dump())
        if raw.rules is not None:
            node.rules = list(raw.rules)
        if raw.exceptions is not None:
            node.exceptions = list(raw.exceptions)
        return node

    assert name is not None
    if not PROFILE_NAME_RE.match(name):
        diagnostics.append(_diag(
            f"{base_path}.name", "invalid_node_name",
            f"invalid node name '{name}': 1-64 chars, lowercase alphanumeric with hyphens",
        ))
        return None

    missing = [
        field_name
        for field_name, value in (
            ("goal", raw.goal), ("description", raw.description),
            ("io", raw.io), ("persona", raw.persona),
        )
        if value is None or (isinstance(value, str) and not value.strip())
    ]
    for field_name in missing:
        diagnostics.append(_diag(f"{base_path}.{field_name}", "required", "custom node field is required"))
    if missing:
        return None

    persona = (raw.persona or "").strip().lower()
    if persona not in PERSONA_VALUES:
        diagnostics.append(_diag(f"{base_path}.persona", "invalid_persona", f"unknown persona: {persona}"))
        return None

    try:
        return WorkflowNode(
            name=name,
            goal=(raw.goal or "").strip(),
            description=(raw.description or "").strip(),
            io=NodeIO(input=raw.io.input, output=raw.io.output),  # type: ignore[union-attr]
            persona=persona,
            gate=NodeGate(**raw.gate.model_dump()) if raw.gate else NodeGate(),
            workflow=[WorkflowStep(**step.model_dump()) for step in raw.workflow or []],
            subworkflow=NodeSubworkflow(**raw.subworkflow.model_dump()) if raw.subworkflow else None,
            rules=list(raw.rules or []),
            exceptions=list(raw.exceptions or []),
        )
    except ValueError as exc:
        diagnostics.append(_diag(base_path, "invalid_node", str(exc)))
        return None


def _build_workflow(
    raw: _RawWorkflow,
    profile_name: str,
    revision: int,
    source: ProfileSource,
    context: ProfileLoaderContext,
    diagnostics: list[ProfileDiagnostic],
) -> WorkflowDAG | None:
    if not raw.nodes:
        diagnostics.append(_diag("workflow.nodes", "required", "workflow must define at least one node"))
        return None

    nodes: dict[str, WorkflowNode] = {}
    for index, raw_node in enumerate(raw.nodes):
        node = _expand_node(raw_node, index, context, diagnostics)
        if node is None:
            continue
        if node.name in nodes:
            diagnostics.append(_diag(
                f"workflow.nodes.{index}", "duplicate_node", f"duplicate node name: {node.name}"
            ))
            continue
        nodes[node.name] = node

    terminal_condition = (
        raw.terminal_exit.condition.strip().lower() if raw.terminal_exit else "done"
    )
    edges: list[Edge] = []
    seen_conditions: dict[str, set[str]] = {}
    for index, raw_edge in enumerate(raw.edges):
        edge_path = f"workflow.edges.{index}"
        try:
            edge = Edge(**raw_edge.model_dump())
        except ValueError as exc:
            diagnostics.append(_diag(edge_path, "invalid_edge", str(exc)))
            continue
        if edge.condition == terminal_condition:
            diagnostics.append(_diag(
                f"{edge_path}.condition", "reserved_condition",
                f"edge condition reserves the terminal condition '{terminal_condition}'",
            ))
        if edge.source == edge.target:
            diagnostics.append(_diag(edge_path, "self_loop", f"self-loop edge on node '{edge.source}'"))
        for endpoint, role in ((edge.source, "source"), (edge.target, "target")):
            if endpoint not in nodes:
                diagnostics.append(_diag(
                    f"{edge_path}.{role}", "unknown_edge_node", f"edge references unknown node: {endpoint}"
                ))
        conditions = seen_conditions.setdefault(edge.source, set())
        if edge.condition in conditions:
            diagnostics.append(_diag(
                f"{edge_path}.condition", "duplicate_condition",
                f"node '{edge.source}' already has an outgoing edge with condition '{edge.condition}'",
            ))
        conditions.add(edge.condition)
        edges.append(edge)

    if any(d.severity == "error" for d in diagnostics):
        return None

    try:
        return WorkflowDAG(
            name=(raw.name or f"{profile_name}-workflow").strip().lower(),
            nodes=nodes,
            edges=edges,
            terminal_exit=(
                TerminalExit(**raw.terminal_exit.model_dump())
                if raw.terminal_exit
                else TerminalExit()
            ),
        )
    except ValueError as exc:
        diagnostics.append(_diag("workflow", "invalid_workflow", str(exc)))
        return None


# ── semantic validation ──────────────────────────────────────────────────


def _validate_tools(
    raw: _RawTools,
    context: ProfileLoaderContext,
    diagnostics: list[ProfileDiagnostic],
) -> None:
    if raw.allow is not None and raw.block is not None:
        diagnostics.append(_diag("tools", "tools_conflict", "tools.allow and tools.block are mutually exclusive"))
    for field_name, values in (("allow", raw.allow), ("block", raw.block)):
        for index, tool in enumerate(values or []):
            normalized = tool.strip().lower()
            path = f"tools.{field_name}.{index}"
            if normalized in LIFECYCLE_TOOL_NAMES:
                diagnostics.append(_diag(
                    path, "lifecycle_tool_not_allowed",
                    f"lifecycle tool '{normalized}' is injected by the control protocol, not the profile",
                ))
            elif normalized not in context.known_tools:
                diagnostics.append(_diag(path, "unknown_tool", f"unknown tool: {normalized}"))


def _validate_semantics(
    raw: _RawProfile,
    context: ProfileLoaderContext,
    expected_name: str | None,
    diagnostics: list[ProfileDiagnostic],
) -> None:
    name = normalize_profile_name(raw.name)
    if not PROFILE_NAME_RE.match(name):
        diagnostics.append(_diag(
            "name", "invalid_name",
            f"invalid profile name '{raw.name}': 1-64 chars, lowercase alphanumeric with hyphens",
        ))
    elif expected_name is not None and name != normalize_profile_name(expected_name):
        diagnostics.append(_diag(
            "name", "name_mismatch", f"profile name '{name}' does not match file name '{expected_name}'",
        ))
    if raw.revision < 1:
        diagnostics.append(_diag("revision", "invalid_revision", "revision must be >= 1"))
    if raw.prompt_policy.strip().lower() not in PROMPT_POLICY_REGISTRY:
        diagnostics.append(_diag(
            "prompt_policy", "unknown_prompt_policy", f"unknown prompt policy: {raw.prompt_policy}",
        ))
    try:
        resolve_run_config(raw.run_mode)
    except ValueError:
        diagnostics.append(_diag("run_mode", "unknown_run_mode", f"unknown run_mode preset: {raw.run_mode}"))
    if raw.hitl_mode.strip().lower() not in HITL_MODES:
        diagnostics.append(_diag("hitl_mode", "invalid_hitl_mode", f"unknown hitl_mode: {raw.hitl_mode}"))
    if raw.persona is not None and raw.persona.strip().lower() not in PERSONA_VALUES:
        diagnostics.append(_diag("persona", "invalid_persona", f"unknown persona: {raw.persona}"))
    for index, section in enumerate(raw.suppress_sections):
        path = f"suppress_sections.{index}"
        if section in RESERVED_SECTIONS:
            diagnostics.append(_diag(path, "reserved_section", f"section '{section}' can never be suppressed"))
        elif section not in SUPPRESSIBLE_SECTIONS:
            diagnostics.append(_diag(path, "unknown_section", f"section '{section}' is not suppressible"))
    if raw.tools is not None:
        _validate_tools(raw.tools, context, diagnostics)
    for index, skill in enumerate(raw.skills or []):
        if skill.strip() not in context.available_skills:
            diagnostics.append(_diag(f"skills.{index}", "unknown_skill", f"unknown skill: {skill}"))
    for index, server in enumerate(raw.mcp_servers or []):
        if server.strip() not in context.available_mcp_servers:
            diagnostics.append(_diag(
                f"mcp_servers.{index}", "unknown_mcp_server",
                f"unknown or disabled MCP server: {server}",
            ))


# ── public entry point ───────────────────────────────────────────────────


def load_profile(
    text: str,
    *,
    source: ProfileSource,
    context: ProfileLoaderContext | None = None,
    expected_name: str | None = None,
) -> tuple[ResolvedAgentProfile, tuple[ProfileDiagnostic, ...]]:
    """Parse, validate, and snapshot one profile YAML document.

    Returns the resolved profile plus non-fatal warnings. Raises
    ``ProfileLoadError`` with stable diagnostics on any hard error.
    """
    context = context or ProfileLoaderContext()

    try:
        data = yaml.load(text, Loader=_StrictYAMLLoader)
    except _DuplicateKeyError as exc:
        raise ProfileLoadError([_diag(exc.key, "duplicate_key", f"duplicate map key: {exc.key}")]) from exc
    except yaml.YAMLError as exc:
        raise ProfileLoadError([_diag("", "invalid_yaml", str(exc))]) from exc

    if not isinstance(data, dict):
        raise ProfileLoadError([_diag("", "invalid_schema", "profile document must be a YAML mapping")])

    try:
        raw = _RawProfile.model_validate(data)
    except ValidationError as exc:
        raise ProfileLoadError(_pydantic_diagnostics(exc)) from exc

    diagnostics: list[ProfileDiagnostic] = []
    _validate_semantics(raw, context, expected_name, diagnostics)

    name = normalize_profile_name(raw.name)
    dag: WorkflowDAG | None = None
    if raw.workflow is not None:
        dag = _build_workflow(raw.workflow, name, raw.revision, source, context, diagnostics)

    errors = [d for d in diagnostics if d.severity == "error"]
    if errors or (raw.workflow is not None and dag is None):
        raise ProfileLoadError(errors or diagnostics)

    payload = raw.model_dump(mode="json")
    if dag is not None:
        payload["workflow"] = dag.model_dump(mode="json")

    content_hash = content_hash_of(payload)
    snapshot_hash = content_hash_of({
        "source": source,
        "profile_id": name,
        "revision": raw.revision,
        "content_hash": content_hash,
    })

    run_config = resolve_run_config(raw.run_mode)
    workflow_context = None
    if dag is not None:
        workflow_context = WorkflowRuntimeContext(
            dag=dag,
            dag_revision=raw.revision,
            dag_hash=content_hash_of(dag.model_dump(mode="json")),
            source=source,
        )

    runtime_profile = bundled_legacy_runtime_profile(
        source=source,
        profile_id=name,
        revision=raw.revision,
    ) or RuntimeProfile(
        profile_id=name,
        revision=raw.revision,
        name=raw.display_name.strip() or name,
        protocol=run_config.protocol,
        system_prompt=raw.identity.strip(),
        constraints=tuple(rule for rule in raw.extra_rules if rule.strip()),
        persona=raw.persona.strip().lower() if raw.persona else None,
        prompt_policy=resolve_prompt_policy(raw.prompt_policy),
    )

    resolved = ResolvedAgentProfile(
        snapshot=AgentProfileSnapshot(
            profile_id=name,
            revision=raw.revision,
            source=source,
            content_hash=content_hash,
            snapshot_hash=snapshot_hash,
            canonical_payload=payload,
        ),
        runtime_profile=runtime_profile,
        workflow_context=workflow_context,
        run_config=run_config,
        resource_policy=ResourcePolicy(
            hitl_mode=raw.hitl_mode.strip().lower(),  # type: ignore[arg-type]
            tools_allow=(
                frozenset(tool.strip().lower() for tool in raw.tools.allow)
                if raw.tools and raw.tools.allow is not None
                else None
            ),
            tools_block=(
                frozenset(tool.strip().lower() for tool in raw.tools.block)
                if raw.tools and raw.tools.block
                else frozenset()
            ),
            skills=tuple(raw.skills) if raw.skills is not None else None,
            mcp_servers=tuple(raw.mcp_servers) if raw.mcp_servers is not None else None,
        ),
    )

    warnings: list[ProfileDiagnostic] = []
    if dag is not None and all(
        not node.gate.required_before_transition.strip() for node in dag.nodes.values()
    ):
        warnings.append(_diag(
            "workflow", "missing_verify_gate",
            "workflow DAG has no gate; consider referencing a gated node such as verify or review",
            severity="warning",
        ))
    return resolved, tuple(warnings)
