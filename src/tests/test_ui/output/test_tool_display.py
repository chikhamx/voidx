from voidx.ui.output.tool_display import (
    extract_tool_display_value,
    mcp_tool_display_name,
    strip_rich_markup,
)


def test_extract_tool_display_value_supports_all_shared_branches():
    assert extract_tool_display_value("edit", {"file_path": "src/app.py"}, "") == "src/app.py"
    assert extract_tool_display_value("glob", {"pattern": "src/**/*.py"}, "") == "src/**/*.py"
    assert extract_tool_display_value("agent", {"agent": "implement"}, "") == "implement"
    assert extract_tool_display_value("checkpoint", {"goal": "ship safely"}, "") == "ship safely"
    assert extract_tool_display_value("bash", {"command": "echo one\necho two"}, "") == "echo one; echo two"
    assert extract_tool_display_value("websearch", {"query": "voidx"}, "") == "voidx"


def test_extract_tool_display_value_preserves_nodes_grep_format():
    assert extract_tool_display_value(
        "grep",
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


def test_mcp_tool_display_name_removes_prefix_and_hash():
    assert mcp_tool_display_name("mcp__tavily__tavily_search_943584b9") == "Tavily Search"
    assert mcp_tool_display_name("mcp__github__list_issues_1234abcd") == "Github List Issues"
