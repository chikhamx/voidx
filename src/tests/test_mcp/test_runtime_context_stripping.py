"""Historical `mcp load` tool results are stripped to summaries like skill context."""

from langchain_core.messages import HumanMessage, ToolMessage

from voidx.agent.runtime_context import InteractionMode, RuntimeContextBuilder
from voidx.config import Config
from voidx.mcp.context import (
    MCP_TOOL_CONTEXT_MARKER,
    MCP_TOOL_CONTEXT_STRIPPED_MARKER,
)


def _tool_output() -> str:
    return (
        f"{MCP_TOOL_CONTEXT_MARKER}\nScope: current-turn\n\n"
        "## MCP Server: tavily\n"
        "Status: connected\n\n"
        "Tools:\n"
        "- tavily_search: Search the web.\n"
        "  Required: query\n"
        "  Example:\n"
        '    mcp(op="call", server="tavily", tool="tavily_search", arguments="{\\"query\\": \\"...\\"}")'
    )


def _build_context(tmp_path):
    return RuntimeContextBuilder(
        config=Config(workspace=str(tmp_path)),
        workspace=str(tmp_path),
        base_system_prompt="You are voidx.",
        persona="voidx",
        interaction_mode=InteractionMode.AUTO,
    ).build()


def test_historical_mcp_tool_context_is_stripped(tmp_path):
    messages = [
        HumanMessage(content="old request"),
        ToolMessage(content=_tool_output(), tool_call_id="call_mcp_load"),
        HumanMessage(content="current request"),
    ]
    context = _build_context(tmp_path)

    context.apply_to_messages(messages)

    historical_tool = next(m for m in messages if isinstance(m, ToolMessage))
    assert MCP_TOOL_CONTEXT_STRIPPED_MARKER in historical_tool.content
    assert "tavily" in historical_tool.content
    assert "tavily_search" in historical_tool.content
    assert "Scope: current-turn" not in historical_tool.content
    assert "Required: query" not in historical_tool.content


def test_latest_mcp_tool_context_is_preserved(tmp_path):
    messages = [
        HumanMessage(content="current request"),
        ToolMessage(content=_tool_output(), tool_call_id="call_mcp_load"),
    ]
    context = _build_context(tmp_path)

    context.apply_to_messages(messages)

    latest_tool = next(m for m in messages if isinstance(m, ToolMessage))
    assert MCP_TOOL_CONTEXT_MARKER in latest_tool.content
    assert "Required: query" in latest_tool.content
