"""Strict YAML loader for agent profiles: validation, ref expansion, hashing."""

import pytest

from voidx.agent.application.agent_profile_loader import (
    ProfileLoadError,
    ProfileLoaderContext,
    load_profile,
)


KNOWN_TOOLS = frozenset({
    "read", "search", "find", "write", "bash", "clarify", "checkpoint",
    "todo", "workflow", "skill", "mcp", "agent", "ls", "lsp", "document",
    "websearch", "webfetch", "git", "compact",
})


def _context() -> ProfileLoaderContext:
    return ProfileLoaderContext(
        known_tools=KNOWN_TOOLS,
        available_skills=frozenset({"code-review"}),
        available_mcp_servers=frozenset({"github"}),
    )


def _load(text: str, **kwargs):
    return load_profile(text, source="project", context=_context(), **kwargs)


def _error_codes(excinfo) -> list[str]:
    return [d.code for d in excinfo.value.diagnostics]


MINIMAL = """
name: my-reviewer
revision: 1
"""

FULL = """
name: my-reviewer
revision: 2
display_name: "代码评审员"
prompt_policy: coding
identity: |
  你是一个严格的代码评审员。
style_rules:
  - "评审输出按严重度分级"
extra_rules:
  - "不修改代码，只输出评审意见"
persona: review
suppress_sections:
  - "Workflow Runtime"
workflow:
  nodes:
    - ref: review
    - name: summarize
      goal: "汇总评审发现"
      description: "汇总"
      io: {input: {findings: "评审发现"}, output: {report: "汇总报告"}}
      persona: review
      gate: {required_before_transition: "所有 blocker 已列出"}
      workflow:
        - {order: 1, action: "汇总"}
      rules:
        - "按严重度排序"
  edges:
    - {source: review, target: summarize, condition: completed}
run_mode: single
hitl_mode: interactive
tools:
  allow: [read, search, find]
skills: [code-review]
mcp_servers: [github]
"""


def test_minimal_profile_resolves_with_defaults() -> None:
    resolved, warnings = _load(MINIMAL)

    assert resolved.snapshot.profile_id == "my-reviewer"
    assert resolved.snapshot.revision == 1
    assert resolved.snapshot.source == "project"
    assert resolved.run_config.run_mode == "single"
    assert resolved.run_config.protocol == "turn"
    assert resolved.resource_policy.hitl_mode == "interactive"
    assert resolved.resource_policy.tools_allow is None
    assert resolved.workflow_context is None
    assert resolved.runtime_profile.prompt_policy is not None
    assert warnings == ()


def test_full_profile_resolves_all_layers() -> None:
    resolved, _ = _load(FULL)

    profile = resolved.runtime_profile
    assert profile.name == "代码评审员"
    assert profile.persona == "review"
    assert profile.system_prompt.startswith("你是一个严格的代码评审员")
    assert profile.constraints == ("不修改代码，只输出评审意见",)

    policy = resolved.resource_policy
    assert policy.tools_allow == frozenset({"read", "search", "find"})
    assert policy.skills == ("code-review",)
    assert policy.mcp_servers == ("github",)

    payload = resolved.snapshot.canonical_payload
    assert payload["display_name"] == "代码评审员"
    assert payload["suppress_sections"] == ["Workflow Runtime"]


def test_workflow_ref_expands_builtin_node_with_overrides() -> None:
    text = """
name: wf-profile
revision: 1
workflow:
  nodes:
    - ref: review
      rules: ["额外规则"]
    - name: summarize
      goal: g
      description: d
      io: {input: {}, output: {}}
      persona: review
      gate: {required_before_transition: "gate"}
  edges:
    - {source: review, target: summarize, condition: completed}
"""
    resolved, _ = _load(text)

    context = resolved.workflow_context
    assert context is not None
    assert set(context.dag.nodes) == {"review", "summarize"}
    # ref expansion inherits the builtin definition and applies overrides.
    builtin_review = context.dag.nodes["review"]
    assert "额外规则" in builtin_review.rules
    assert builtin_review.goal  # inherited non-empty goal
    # Expanded nodes (not the raw ref) participate in the canonical payload.
    payload_nodes = resolved.snapshot.canonical_payload["workflow"]["nodes"]
    assert isinstance(payload_nodes, dict)
    assert payload_nodes["review"]["goal"] == builtin_review.goal


def test_workflow_default_name_derived_from_profile_name() -> None:
    text = """
name: wf-profile
revision: 1
workflow:
  nodes:
    - ref: review
"""
    resolved, _ = _load(text)
    assert resolved.workflow_context is not None
    assert resolved.workflow_context.dag.name == "wf-profile-workflow"


@pytest.mark.parametrize("text,code,path", [
    ("name: x\nrevision: 1\nunknown_field: 1\n", "unknown_field", "unknown_field"),
    ("name: x\nrevision: 1\nrevision: 2\n", "duplicate_key", "revision"),
    ("name: x\nrevision: abc\n", "type_error", "revision"),
    ("name: ''\nrevision: 1\n", "invalid_name", "name"),
    ("name: -Bad\nrevision: 1\n", "invalid_name", "name"),
    ("name: x\nrevision: 0\n", "invalid_revision", "revision"),
    ("name: x\nrevision: 1\nprompt_policy: nope\n", "unknown_prompt_policy", "prompt_policy"),
    ("name: x\nrevision: 1\nrun_mode: nope\n", "unknown_run_mode", "run_mode"),
    ("name: x\nrevision: 1\nhitl_mode: yolo\n", "invalid_hitl_mode", "hitl_mode"),
    ("name: x\nrevision: 1\npersona: yolo\n", "invalid_persona", "persona"),
    ("name: x\nrevision: 1\ntools: {allow: [read], block: [bash]}\n", "tools_conflict", "tools"),
    ("name: x\nrevision: 1\ntools: {allow: [nope]}\n", "unknown_tool", "tools.allow.0"),
    ("name: x\nrevision: 1\ntools: {allow: [turn]}\n", "lifecycle_tool_not_allowed", "tools.allow.0"),
    ("name: x\nrevision: 1\ntools: {block: [goal]}\n", "lifecycle_tool_not_allowed", "tools.block.0"),
    ("name: x\nrevision: 1\nskills: [missing-skill]\n", "unknown_skill", "skills.0"),
    ("name: x\nrevision: 1\nmcp_servers: [missing]\n", "unknown_mcp_server", "mcp_servers.0"),
    ("name: x\nrevision: 1\nsuppress_sections: [\"Base System\"]\n", "reserved_section", "suppress_sections.0"),
    ("name: x\nrevision: 1\nsuppress_sections: [\"Foo\"]\n", "unknown_section", "suppress_sections.0"),
])
def test_hard_errors_carry_stable_path_and_code(text: str, code: str, path: str) -> None:
    with pytest.raises(ProfileLoadError) as excinfo:
        _load(text)

    matches = [d for d in excinfo.value.diagnostics if d.code == code]
    assert matches, f"missing {code}: {[ (d.path, d.code) for d in excinfo.value.diagnostics]}"
    assert matches[0].path == path
    assert matches[0].message


def test_filename_must_match_profile_name() -> None:
    with pytest.raises(ProfileLoadError) as excinfo:
        _load(MINIMAL, expected_name="other-name")
    assert "name_mismatch" in _error_codes(excinfo)

    resolved, _ = _load(MINIMAL, expected_name="my-reviewer")
    assert resolved.snapshot.profile_id == "my-reviewer"


def test_workflow_dag_hard_errors() -> None:
    base = """
name: wf-profile
revision: 1
workflow:
  nodes:
    - name: a
      goal: g
      description: d
      io: {input: {}, output: {}}
      persona: implement
      gate: {required_before_transition: "gate"}
  edges:
    - %s
"""
    cases = {
        "self_loop": "{source: a, target: a, condition: retry}",
        "unknown_edge_node": "{source: a, target: ghost, condition: go}",
        "reserved_condition": "{source: a, target: a, condition: done}",
    }
    for code, edge in cases.items():
        with pytest.raises(ProfileLoadError) as excinfo:
            _load(base % edge)
        assert code in _error_codes(excinfo), code


def test_duplicate_edge_condition_rejected() -> None:
    text = """
name: wf-profile
revision: 1
workflow:
  nodes:
    - ref: review
    - name: summarize
      goal: g
      description: d
      io: {input: {}, output: {}}
      persona: review
  edges:
    - {source: review, target: summarize, condition: completed}
    - {source: review, target: summarize, condition: completed}
"""
    with pytest.raises(ProfileLoadError) as excinfo:
        _load(text)
    assert "duplicate_condition" in _error_codes(excinfo)


@pytest.mark.parametrize("node,code", [
    ("{name: a, ref: review}", "node_identity"),
    ("{goal: g}", "node_identity"),
    ("{ref: ghost}", "unknown_ref"),
    ("{name: Bad_Name, goal: g, description: d, io: {input: {}, output: {}}, persona: implement}", "invalid_node_name"),
    ("{name: a, goal: g, description: d, io: {input: {}, output: {}}, persona: yolo}", "invalid_persona"),
    ("{name: a, goal: g, description: d, persona: implement}", "required"),
])
def test_workflow_node_validation(node: str, code: str) -> None:
    text = f"""
name: wf-profile
revision: 1
workflow:
  nodes:
    - {node}
"""
    with pytest.raises(ProfileLoadError) as excinfo:
        _load(text)
    assert code in _error_codes(excinfo), f"{code}: {_error_codes(excinfo)}"


def test_duplicate_node_names_rejected() -> None:
    text = """
name: wf-profile
revision: 1
workflow:
  nodes:
    - ref: review
    - name: review
      goal: g
      description: d
      io: {input: {}, output: {}}
      persona: review
"""
    with pytest.raises(ProfileLoadError) as excinfo:
        _load(text)
    assert "duplicate_node" in _error_codes(excinfo)


def test_dag_without_gate_produces_warning_not_error() -> None:
    text = """
name: wf-profile
revision: 1
workflow:
  nodes:
    - name: work
      goal: g
      description: d
      io: {input: {}, output: {}}
      persona: implement
"""
    resolved, warnings = _load(text)
    assert resolved.workflow_context is not None
    assert [d.code for d in warnings] == ["missing_verify_gate"]
    assert warnings[0].severity == "warning"


def test_dag_with_gated_ref_node_has_no_warning() -> None:
    text = """
name: wf-profile
revision: 1
workflow:
  nodes:
    - ref: verify
"""
    _, warnings = _load(text)
    assert warnings == ()


def test_hashes_are_deterministic_and_source_sensitive() -> None:
    first, _ = _load(FULL)
    second, _ = _load(FULL)
    assert first.snapshot.content_hash == second.snapshot.content_hash
    assert first.snapshot.snapshot_hash == second.snapshot.snapshot_hash

    bundled, _ = load_profile(FULL, source="bundled", context=_context())
    assert bundled.snapshot.content_hash == first.snapshot.content_hash
    assert bundled.snapshot.snapshot_hash != first.snapshot.snapshot_hash


def test_content_hash_changes_with_content() -> None:
    first, _ = _load(MINIMAL)
    bumped, _ = _load(MINIMAL.replace("revision: 1", "revision: 2"))
    assert first.snapshot.content_hash != bumped.snapshot.content_hash


def test_workflow_context_hash_matches_expanded_dag() -> None:
    text = """
name: wf-profile
revision: 3
workflow:
  nodes:
    - ref: review
"""
    resolved, _ = _load(text)
    context = resolved.workflow_context
    assert context is not None
    assert context.dag_revision == 3
    assert len(context.dag_hash) == 64
    assert context.source == "project"


def test_invalid_yaml_is_a_load_error() -> None:
    with pytest.raises(ProfileLoadError) as excinfo:
        _load("name: [unclosed\n")
    assert "invalid_yaml" in _error_codes(excinfo)


def test_non_mapping_yaml_is_a_load_error() -> None:
    with pytest.raises(ProfileLoadError) as excinfo:
        _load("- just\n- a\n- list\n")
    assert "invalid_schema" in _error_codes(excinfo)
