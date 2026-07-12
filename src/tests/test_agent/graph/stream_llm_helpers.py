"""Shared helpers for test_stream_llm split files."""

import sys
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from rich.console import Console


from voidx.agent.graph.streaming import stream_llm as _stream_llm
from voidx.agent.graph import VoidXGraph
from voidx.agent.graph.convergence import is_step_hint_message
from voidx.agent.runtime_context import RuntimeContextBuilder
from voidx.agent.task_state import TaskState, TodoRunState
from voidx.config import Config, ModelConfig
from voidx.llm.compaction import CompactionSelection
from voidx.llm.message_markers import is_guidance_message
from voidx.memory.context_frames import load_context_frames
from voidx.memory.session import MessageRow, create_session, delete_session, save_message
from voidx.ui.output.console import StreamingRenderer
from voidx.ui.output.dock import ANSI_LINE_PREFIX, BottomInputDock, set_dock
from voidx.ui.output.events import AnsiAppended, DockEventConsumer, StatusFinished, StatusUpdated, ui_events
from voidx.workflow.runtime import WorkflowRunState, WorkflowRunStatus

def _plain(line: str) -> str:
    return line.replace(ANSI_LINE_PREFIX, "")


class FakeStreamingModel:
    def __init__(self) -> None:
        self.messages = None

    def bind_tools(self, tool_defs):
        return self

    async def astream(self, messages):
        self.messages = messages
        yield AIMessageChunk(content=[{"type": "thinking", "text": "think"}])
        yield AIMessageChunk(content="answer")
        yield AIMessageChunk(
            content="",
            tool_calls=[{"name": "turn", "args": {"operation": "stop", "intent": "", "goal": ""}, "id": "turn-1", "type": "tool_call"}],
        )


class FakeUsageStreamingModel:
    def bind_tools(self, tool_defs):
        return self

    async def astream(self, messages):
        yield AIMessageChunk(
            content="answer",
            usage_metadata={
                "input_tokens": 7,
                "output_tokens": 3,
                "total_tokens": 10,
            },
        )
        yield AIMessageChunk(
            content="",
            tool_calls=[{"name": "turn", "args": {"operation": "stop", "intent": "", "goal": ""}, "id": "turn-1", "type": "tool_call"}],
        )


class FakeDuplicatedReasoningStreamingModel:
    async def astream(self, messages):
        yield AIMessageChunk(
            content="622",
            additional_kwargs={"reasoning_content": "622"},
        )
        yield AIMessageChunk(content="final answer")
        yield AIMessageChunk(
            content="",
            tool_calls=[{"name": "turn", "args": {"operation": "stop", "intent": "", "goal": ""}, "id": "turn-1", "type": "tool_call"}],
        )


class FakeDsmlStreamingModel:
    def __init__(self) -> None:
        self.messages = None

    def bind_tools(self, tool_defs):
        return self

    async def astream(self, messages):
        self.messages = messages
        yield AIMessageChunk(content=(
            '也必须在 commands 列表中注册:\n\n'
            '<｜｜DSML｜｜tool_calls>\n'
            '<｜｜DSML｜｜invoke name="grep">\n'
            '<｜｜DSML｜｜parameter name="path" string="true">src/voidx/ui/commands.py</｜｜DSML｜｜parameter>\n'
            '<｜｜DSML｜｜parameter name="pattern" string="true">permissions</｜｜DSML｜｜parameter>\n'
            '</｜｜DSML｜｜invoke>\n'
            '</｜｜DSML｜｜tool_calls>'
        ))


class FakeMalformedDsmlStreamingModel:
    def bind_tools(self, tool_defs):
        return self

    async def astream(self, messages):
        yield AIMessageChunk(content=(
            '<|||DSML||tool_calls>\n'
            '<||DSML||invoke name="grep">\n'
            '<||DSML||parameter name="path" string="true">src/voidx/ui/commands.py</||DSML||parameter>\n'
            '</||DSML||invoke>\n'
            '</|||DSML||tool_calls>'
        ))


class FakeMalformedLegacyXmlStreamingModel:
    def bind_tools(self, tool_defs):
        return self

    async def astream(self, messages):
        yield AIMessageChunk(content=(
            "<tool_call>"
            "<tool_name>read</tool_name>"
            "<arg_key>file_path</arg_key><arg_value>src/voidx/permission/engine.py</arg_value>"
        ))


class RepairsMalformedToolCallStreamingModel:
    def __init__(self) -> None:
        self.calls = 0
        self.messages_by_call = []

    def bind_tools(self, tool_defs):
        return self

    async def astream(self, messages):
        self.calls += 1
        self.messages_by_call.append(messages)
        if self.calls == 1:
            yield AIMessageChunk(content=(
                "<tool_call>"
                "<tool_name>read</tool_name>"
                "<arg_key>file_path</arg_key><arg_value>src/voidx/permission/engine.py</arg_value>"
            ))
            return
        yield AIMessageChunk(content="repaired answer")
        yield AIMessageChunk(
            content="",
            tool_calls=[{"name": "turn", "args": {"operation": "stop", "intent": "", "goal": ""}, "id": "turn-1", "type": "tool_call"}],
        )


class AlwaysMalformedToolCallStreamingModel:
    def __init__(self) -> None:
        self.calls = 0

    def bind_tools(self, tool_defs):
        return self

    async def astream(self, messages):
        self.calls += 1
        yield AIMessageChunk(content=(
            "<tool_call>"
            "<tool_name>read</tool_name>"
            "<arg_key>file_path</arg_key><arg_value>src/voidx/permission/engine.py</arg_value>"
        ))


class FakeMalformedProviderJsonToolCallStreamingModel:
    def bind_tools(self, tool_defs):
        return self

    async def astream(self, messages):
        yield AIMessageChunk(content=(
            '{"tool_calls":[{"function":{"name":"read","arguments":"{\\"file_path\\":'
        ))


class FakeLegacyXmlToolCallStreamingModel:
    def bind_tools(self, tool_defs):
        return self

    async def astream(self, messages):
        yield AIMessageChunk(content=(
            "<tool_call>"
            "<tool_name>Read</tool_name>"
            "<arg_key>file_path</arg_key><arg_value>src/voidx/permission/engine.py</arg_value>"
            "<arg_key>offset</arg_key><arg_value>110</arg_value>"
            "<arg_key>limit</arg_key><arg_value>50</arg_value>"
            "</tool_call>"
        ))


class FakeLegacyXmlArgPairToolCallStreamingModel:
    def bind_tools(self, tool_defs):
        return self

    async def astream(self, messages):
        yield AIMessageChunk(content=(
            "<tool_call>"
            "<arg_key>tool_name</arg_key><arg_value>read</arg_value>"
            "<arg_key>file_path</arg_key><arg_value>src/voidx/permission/engine.py</arg_value>"
            "<arg_key>offset</arg_key><arg_value>110</arg_value>"
            "</tool_call>"
        ))


class TrackingStreamingModel(FakeStreamingModel):
    def __init__(self) -> None:
        super().__init__()
        self.bound_tools = None

    def bind_tools(self, tool_defs):
        self.bound_tools = tool_defs
        return self


class FailsOnceStreamingModel(FakeStreamingModel):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def astream(self, messages):
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("Connection error.")
        self.messages = messages
        yield AIMessageChunk(content="answer")
        yield AIMessageChunk(
            content="",
            tool_calls=[{"name": "turn", "args": {"operation": "stop", "intent": "", "goal": ""}, "id": "turn-1", "type": "tool_call"}],
        )


class FakeRenderer:
    def __init__(self, *args, **kwargs) -> None:
        self.text: list[str] = []
        self.thinking: list[str] = []
        self.started = False
        self.done_called = False
        self.discarded = False

    def start(self) -> None:
        self.started = True

    def feed_text(self, text: str) -> None:
        self.text.append(text)

    def feed_thinking(self, text: str) -> None:
        self.thinking.append(text)

    def discard(self) -> None:
        self.discarded = True

    def done(self) -> None:
        self.done_called = True

    def error(self, text: str) -> None:
        pass


class FailsNonRetryableStreamingModel(FakeStreamingModel):
    """Raises an exception with status_code=404 on first call."""
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def astream(self, messages):
        self.calls += 1
        # Must yield first to remain an async generator
        yield AIMessageChunk(content="")
        exc = RuntimeError("404 model_not_found")
        exc.status_code = 404  # type: ignore
        raise exc
