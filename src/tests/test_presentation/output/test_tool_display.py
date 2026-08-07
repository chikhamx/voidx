from voidx.presentation.output.tool_display import (
    extract_tool_display_value,
    strip_rich_markup,
)


def test_extract_tool_display_value_supports_all_shared_branches():
    assert extract_tool_display_value("edit", {"file_path": "src/app.py"}, "") == "src/app.py"
    assert extract_tool_display_value("find", {"pattern": "src/**/*.py"}, "") == "src/**/*.py"
    assert extract_tool_display_value("agent", {"name": "voidx"}, "") == "voidx"
    assert extract_tool_display_value("checkpoint", {"goal": "ship safely"}, "") == "ship safely"
    assert extract_tool_display_value("bash", {"command": "echo one\necho two"}, "") == "echo one; echo two"
    assert extract_tool_display_value("websearch", {"query": "voidx"}, "") == "voidx"


def test_extract_tool_display_value_preserves_nodes_grep_format():
    assert extract_tool_display_value(
        "search",
        {"pattern": "needle", "include": "*.py"},
        "",
    ) == "needle in *.py"


def test_extract_tool_display_value_shortens_for_subagent_status():
    value = extract_tool_display_value(
        "unknown_tool",
        {},
        'file_path="src/very/long/path/to/module.py"',
        short_path_limit=24,
    )
    assert value.startswith("src/")
    assert len(value) <= 24


def test_extract_tool_display_value_formats_common_mcp_list_args():
    assert (
        extract_tool_display_value(
            "mcp__tavily__tavily_extract_99a8fac9",
            {"urls": ["https://example.com/a", "https://example.com/b"]},
            "",
        )
        == "https://example.com/a +1 more"
    )


def test_extract_tool_display_value_prefers_mcp_target_over_instruction():
    assert (
        extract_tool_display_value(
            "mcp__tavily__tavily_extract_99a8fac9",
            {"urls": ["https://example.com"], "query": "extract title"},
            "",
        )
        == "https://example.com"
    )


def test_strip_rich_markup_removes_markup_and_key_prefix():
    assert strip_rich_markup('[bold]file_path[/bold]="src/app.py"') == "src/app.py"



def test_extract_tool_display_value_for_agent_wait_and_cancel():
    from voidx.presentation.output.agent_display import subagent_display_name

    run_id = "run_8bf0d23519a843dd9213989e25427944"
    display_name = subagent_display_name(run_id)
    wait_args = {
        "action": "wait",
        "run_id": run_id,
        "wait": "until_complete",
    }
    cancel_args = {
        "action": "cancel",
        "run_id": run_id,
    }
    assert extract_tool_display_value("agent_control", wait_args, "") == display_name
    assert extract_tool_display_value("agent_control", cancel_args, "") == display_name
    assert run_id not in extract_tool_display_value(
        "agent_control",
        wait_args,
        f'action="wait", run_id="{run_id}", wait="until_complete"',
    )


def test_agent_tool_header_for_wait_and_cancel_is_clean():
    from voidx.presentation.output.agent_display import subagent_display_name
    from voidx.presentation.output.dock.nodes import _tool_header
    from voidx.presentation.output.console.formatting import _fmt_args

    run_id = "run_8bf0d23519a843dd9213989e25427944"
    display_name = subagent_display_name(run_id)
    wait_args = {
        "action": "wait",
        "run_id": run_id,
        "wait": "until_complete",
    }
    cancel_args = {
        "action": "cancel",
        "run_id": run_id,
    }
    spawn_args = {
        "mode": "review",
        "goal": "Review gateway",
        "detail": "Review the gateway behavior.",
        "scope": "docs/design/agent-gateway.md",
    }

    wait_header = _tool_header("agent_control", "Agent control", _fmt_args(wait_args), wait_args)
    cancel_header = _tool_header("agent_control", "Agent control", _fmt_args(cancel_args), cancel_args)
    spawn_header = _tool_header("agent", "Agenting", _fmt_args(spawn_args), spawn_args)

    assert "Wait" in wait_header
    assert display_name in wait_header
    assert run_id not in wait_header
    assert 'wait"' not in wait_header
    assert "wait" not in wait_header
    assert "result_preset" not in wait_header
    assert "Cancel" in cancel_header
    assert display_name in cancel_header
    assert run_id not in cancel_header
    assert "Review gateway" in spawn_header

