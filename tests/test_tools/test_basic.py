"""Smoke tests for tool system — types, execution, error handling."""

import asyncio
import json
import logging
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import pytest

from langchain_core.messages import ToolMessage

from voidx.tools.base import ToolContext, ToolResult, BaseTool, UserInteraction, UserResponse
from voidx.tools.file_ops import FileReadInput, FileWriteInput, FileEditInput, EditEntry
from voidx.tools.search import GlobInput, GrepInput
from voidx.tools.bash import BashInput
from voidx.tools.agent import AgentInput
from voidx.tools.task_tracker import TaskTracker
from voidx.tools.task_status import TaskStatusTool
from voidx.tools.todo import TodoInput, TodoWriteTool
from voidx.tools.registry import ToolRegistry
from voidx.tools.clarify import ClarifyTool, ClarifyInput, ClarifyOption, _infer_state_patch
from voidx.tools.plan_checkpoint import PlanCheckpointTool
from voidx.agent.task_state import ToolStatePatch, PendingApproval
from voidx.agent.runtime_context import TaskIntent


class TestToolSchemas:
    """Every tool has typed, validatable input."""

    def test_base_tool_requires_id_and_description(self):
        with pytest.raises(TypeError, match="must define"):
            class BadTool(BaseTool):
                def parameters_schema(self):
                    return {}
                async def execute(self, args, ctx):
                    pass

    def test_base_tool_subclass_with_id_and_description_ok(self):
        class GoodTool(BaseTool):
            id = "good"
            description = "a good tool"
            def parameters_schema(self):
                return {}
            async def execute(self, args, ctx):
                pass
        assert GoodTool.id == "good"

    def test_read_input_validates(self):
        inp = FileReadInput(file_path="foo.py")
        assert inp.file_path == "foo.py"
        assert inp.offset is None
        assert inp.limit is None

    def test_read_input_with_offset(self):
        inp = FileReadInput(file_path="foo.py", offset=10, limit=5)
        assert inp.offset == 10
        assert inp.limit == 5

    def test_edit_input(self):
        inp = FileEditInput(file_path="x.py", edits=[EditEntry(old_string="a", new_string="b")])
        assert inp.file_path == "x.py"
        assert len(inp.edits) == 1

    def test_glob_input(self):
        inp = GlobInput(pattern="**/*.py")
        assert inp.pattern == "**/*.py"

    def test_grep_input(self):
        inp = GrepInput(pattern="TODO", include="*.py")
        assert inp.pattern == "TODO"

    def test_bash_input(self):
        inp = BashInput(command="ls")
        assert inp.command == "ls"
        assert inp.timeout == 120

    def test_agent_input_uses_child_agent_schema(self):
        assert AgentInput.model_validate({"agent": "explore", "description": "inspect"}).agent == "explore"
        schema = AgentInput.model_json_schema()
        assert "agent" in schema["properties"]
        assert "subagent_type" not in schema["properties"]


class TestToolRegistry:
    """Registry knows all tools."""

    def test_all_tools_registered(self):
        r = ToolRegistry()
        ids = r.ids()
        assert "read" in ids
        assert "write" in ids
        assert "edit" in ids
        assert "apply_patch" in ids
        assert "glob" in ids
        assert "grep" in ids
        assert "git" in ids
        assert "bash" in ids
        assert "repo_map" in ids
        assert "clarify" in ids
        assert "plan_checkpoint" in ids
        assert "lsp_diagnostics" in ids
        assert "lsp_symbols" in ids
        assert "lsp_definition" in ids
        assert "lsp_references" in ids
        assert "lsp_format" in ids

    def test_tools_for_llm(self):
        r = ToolRegistry()
        tools = r.tools_for_llm()
        assert len(tools) == len(r.ids())
        assert len(tools) >= 10
        for t in tools:
            assert t["type"] == "function"
            assert "name" in t["function"]
            assert "description" in t["function"]
            assert "parameters" in t["function"]

    def test_builtin_tools_have_strict_mcp_tools_do_not(self):
        r = ToolRegistry()
        # Register a fake MCP tool
        r.register("mcp__tavily__search_abc12345", object(), "MCP search", {"type": "object", "properties": {}})
        tools = r.tools_for_llm()
        builtin = next(t for t in tools if t["function"]["name"] == "read")
        mcp = next(t for t in tools if t["function"]["name"].startswith("mcp__"))
        assert builtin["function"]["strict"] is True
        assert "strict" not in mcp["function"]

    def test_nested_tool_schemas_keep_defs_and_strict_objects(self):
        r = ToolRegistry()
        clarify = r.get_def("clarify").parameters
        checkpoint = r.get_def("plan_checkpoint").parameters

        assert "$defs" in clarify
        assert "$defs" in checkpoint
        assert clarify["$defs"]["ClarifyOption"]["additionalProperties"] is False
        assert checkpoint["$defs"]["PlanStep"]["additionalProperties"] is False
        assert checkpoint["$defs"]["PlanAlternative"]["additionalProperties"] is False

    def test_unknown_tool(self):
        r = ToolRegistry()
        assert r.get("nonexistent") is None

    def test_filter_tools_retains_only_allowed_tools(self):
        r = ToolRegistry()

        r.filter_tools({"read", "grep"})

        assert set(r.ids()) == {"read", "grep"}
        assert r.get("read") is not None
        assert r.get("write") is None
        names = [tool["function"]["name"] for tool in r.tools_for_llm()]
        assert names == ["read", "grep"]


class TestInteractiveTools:
    @pytest.mark.asyncio
    async def test_clarify_uses_interaction_callback_and_returns_state_patch(self, tmp_path):
        requests = []

        async def interact(request):
            requests.append(request)
            return UserResponse(value="implement")

        result = await ClarifyTool().execute(
            {
                "question": "What should I do?",
                "options": [
                    {"label": "Implement", "value": "implement", "description": "Make the change"},
                    {"label": "Inspect", "value": "inspect", "description": "Only inspect"},
                ],
            },
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        assert requests
        assert result.metadata["clarify_answer"] == "implement"
        assert result.metadata["state_patch"]["task_intent"] == "implement"
        assert result.metadata["state_patch"]["intent_source"] == "clarify"

    @pytest.mark.asyncio
    async def test_plan_checkpoint_approval_clears_pending_approval(self, tmp_path):
        async def interact(request):
            return UserResponse(value="approved")

        result = await PlanCheckpointTool().execute(
            {"plan_summary": "Update runtime state handling"},
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        assert result.metadata["plan_decision"] == "approved"
        patch = result.metadata["state_patch"]
        assert patch["task_intent"] == "implement"
        assert patch["goal"] == "Update runtime state handling"
        assert patch["pending_approval"] is None

    @pytest.mark.asyncio
    async def test_plan_checkpoint_blocks_without_interaction(self, tmp_path):
        result = await PlanCheckpointTool().execute(
            {"plan_summary": "Edit files"},
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.metadata["blocked"] is True
        assert result.metadata["plan_decision"] == "interaction_unavailable"

    @pytest.mark.asyncio
    async def test_clarify_without_interaction_returns_blocked(self, tmp_path):
        result = await ClarifyTool().execute(
            {"question": "What should I do?"},
            ToolContext(workspace=str(tmp_path)),
        )

        assert result.metadata["clarify_cancelled"] is True
        assert result.metadata["blocked"] is True

    @pytest.mark.asyncio
    async def test_clarify_user_cancels(self, tmp_path):
        async def interact(request):
            return UserResponse(value="", cancelled=True)

        result = await ClarifyTool().execute(
            {"question": "What should I do?"},
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        assert result.metadata["clarify_cancelled"] is True
        assert "skipped" in result.title

    @pytest.mark.asyncio
    async def test_clarify_free_text_without_options(self, tmp_path):
        requests = []

        async def interact(request):
            requests.append(request)
            return UserResponse(value="I want to refactor the auth module")

        result = await ClarifyTool().execute(
            {"question": "What would you like to do?"},
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        assert len(requests) == 1
        assert requests[0].options == []
        assert result.metadata["clarify_answer"] == "I want to refactor the auth module"

    @pytest.mark.asyncio
    async def test_clarify_free_text_with_options_is_not_selected_option(self, tmp_path):
        async def interact(request):
            return UserResponse(value="Audit the auth flow first", free_text=True)

        result = await ClarifyTool().execute(
            {
                "question": "What should I do?",
                "options": [
                    {"label": "Implement", "value": "implement", "description": "Make the change"},
                    {"label": "Inspect", "value": "inspect", "description": "Only inspect"},
                ],
            },
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        payload = json.loads(result.output)
        assert payload["answer"] == "Audit the auth flow first"
        assert payload["selected_option"] is None

    @pytest.mark.asyncio
    async def test_clarify_passes_context_in_prompt(self, tmp_path):
        requests = []

        async def interact(request):
            requests.append(request)
            return UserResponse(value="refactor")

        result = await ClarifyTool().execute(
            {
                "question": "What should I do?",
                "context": "This determines the implementation scope",
            },
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        assert "This determines the implementation scope" in requests[0].prompt

    @pytest.mark.asyncio
    async def test_plan_checkpoint_rejected_stays_in_design(self, tmp_path):
        async def interact(request):
            return UserResponse(value="rejected")

        result = await PlanCheckpointTool().execute(
            {"plan_summary": "Refactor auth module"},
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        assert result.metadata["plan_decision"] == "rejected"
        patch = result.metadata["state_patch"]
        assert patch["task_intent"] == "design"
        assert patch["goal_phase"] == "design"

    @pytest.mark.asyncio
    async def test_plan_checkpoint_modified_updates_scope(self, tmp_path):
        interact_calls = []

        async def interact(request):
            interact_calls.append(request)
            if len(interact_calls) == 1:
                return UserResponse(value="modified")
            return UserResponse(value="Only refactor the login function")

        result = await PlanCheckpointTool().execute(
            {"plan_summary": "Refactor auth module"},
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        assert result.metadata["plan_decision"] == "modified"
        patch = result.metadata["state_patch"]
        assert patch["task_intent"] == "implement"
        assert patch["goal"] == "Only refactor the login function"
        assert len(interact_calls) == 2
        assert "Describe the modified scope" in interact_calls[1].prompt

    @pytest.mark.asyncio
    async def test_plan_checkpoint_free_text_is_modified_not_approved(self, tmp_path):
        async def interact(request):
            return UserResponse(value="Only update the login form", free_text=True)

        result = await PlanCheckpointTool().execute(
            {"plan_summary": "Refactor auth module"},
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        assert result.metadata["plan_decision"] == "modified"
        payload = json.loads(result.output)
        assert payload["decision"] == "modified"
        assert payload["modified_scope"] == "Only update the login form"
        patch = result.metadata["state_patch"]
        assert patch["task_intent"] == "implement"
        assert patch["goal"] == "Only update the login form"

    @pytest.mark.asyncio
    async def test_plan_checkpoint_modified_scope_cancelled_falls_back_to_summary(self, tmp_path):
        async def interact(request):
            if request.options:
                return UserResponse(value="modified")
            return UserResponse(value="", cancelled=True)

        result = await PlanCheckpointTool().execute(
            {"plan_summary": "Refactor auth module"},
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        assert result.metadata["plan_decision"] == "modified"
        patch = result.metadata["state_patch"]
        assert patch["goal"] == "Refactor auth module"

    @pytest.mark.asyncio
    async def test_plan_checkpoint_user_cancels_treated_as_rejected(self, tmp_path):
        async def interact(request):
            return UserResponse(value="", cancelled=True)

        result = await PlanCheckpointTool().execute(
            {"plan_summary": "Refactor auth module"},
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        assert result.metadata["plan_decision"] == "rejected"

    @pytest.mark.asyncio
    async def test_clarify_sets_default_timeout_on_interaction(self, tmp_path):
        requests = []

        async def interact(request):
            requests.append(request)
            return UserResponse(value="chat")

        result = await ClarifyTool().execute(
            {"question": "What should I do?"},
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        assert requests[0].timeout == 120.0

    @pytest.mark.asyncio
    async def test_plan_checkpoint_sets_default_timeout_on_interaction(self, tmp_path):
        requests = []

        async def interact(request):
            requests.append(request)
            return UserResponse(value="approved")

        result = await PlanCheckpointTool().execute(
            {"plan_summary": "Refactor auth"},
            ToolContext(workspace=str(tmp_path), interact=interact),
        )

        assert requests[0].timeout == 120.0


class TestInferStatePatch:
    def test_intent_match_from_option_value(self):
        inp = ClarifyInput(question="What?", options=[
            ClarifyOption(label="Implement", value="implement"),
        ])
        response = UserResponse(value="implement")
        patch = _infer_state_patch(inp, response)

        assert patch is not None
        assert patch.task_intent == TaskIntent.IMPLEMENT
        assert patch.intent_source == "clarify"
        assert patch.intent_refined is True

    def test_intent_match_case_insensitive(self):
        inp = ClarifyInput(question="What?")
        response = UserResponse(value="Implement")
        patch = _infer_state_patch(inp, response)

        assert patch is not None
        assert patch.task_intent == TaskIntent.IMPLEMENT

    def test_scope_context_updates_goal(self):
        inp = ClarifyInput(question="Which files?", context="This determines the scope of changes")
        response = UserResponse(value="Only auth.py and tests")
        patch = _infer_state_patch(inp, response)

        assert patch is not None
        assert patch.goal == "Only auth.py and tests"
        assert patch.intent_source == "clarify"

    def test_no_match_returns_none(self):
        inp = ClarifyInput(question="What color?")
        response = UserResponse(value="blue")
        patch = _infer_state_patch(inp, response)

        assert patch is None

    def test_empty_answer_returns_none(self):
        inp = ClarifyInput(question="What?")
        response = UserResponse(value="  ")
        patch = _infer_state_patch(inp, response)

        assert patch is None


class TestToolStatePatch:
    def test_model_fields_set_tracks_explicit_fields(self):
        patch = ToolStatePatch(task_intent=TaskIntent.IMPLEMENT, intent_source="clarify")
        assert "task_intent" in patch.model_fields_set
        assert "intent_source" in patch.model_fields_set
        assert "goal" not in patch.model_fields_set

    def test_none_pending_approval_is_explicit(self):
        patch = ToolStatePatch(pending_approval=None)
        assert "pending_approval" in patch.model_fields_set
        data = patch.model_dump(mode="json", exclude_unset=True)
        assert data["pending_approval"] is None

    def test_full_patch_round_trips(self):
        patch = ToolStatePatch(
            task_intent=TaskIntent.IMPLEMENT,
            intent_resolution_reason="plan_checkpoint: approved",
            goal="Refactor auth",
            goal_phase="implement",
            pending_approval=None,
            intent_source="plan_checkpoint",
            intent_refined=True,
        )
        data = patch.model_dump(mode="json")
        restored = ToolStatePatch.model_validate(data)
        assert restored.task_intent == TaskIntent.IMPLEMENT
        assert restored.goal == "Refactor auth"
        assert restored.pending_approval is None


class TestPendingApproval:
    def test_default_kind_is_implementation(self):
        pa = PendingApproval(scope="Refactor auth")
        assert pa.kind == "implementation"
        assert pa.source_intent == TaskIntent.DESIGN

    def test_round_trip(self):
        pa = PendingApproval(scope="Fix bug", source_intent=TaskIntent.DESIGN, created_turn=3)
        data = pa.model_dump(mode="json")
        restored = PendingApproval.model_validate(data)
        assert restored.scope == "Fix bug"
        assert restored.created_turn == 3


class TestUserInteractionModels:
    def test_user_interaction_defaults(self):
        ui = UserInteraction(prompt="What?")
        assert ui.options == []
        assert ui.blocking is True
        assert ui.timeout is None

    def test_user_response_cancelled(self):
        resp = UserResponse(value="", cancelled=True)
        assert resp.cancelled is True

    def test_user_interaction_with_options(self):
        ui = UserInteraction(
            prompt="Choose",
            options=[("A", "a", "Option A"), ("B", "b", "Option B")],
            timeout=60.0,
        )
        assert len(ui.options) == 2
        assert ui.timeout == 60.0


class TestMakeInteractCallback:
    @pytest.mark.asyncio
    async def test_returns_none_when_app_is_none(self):
        from voidx.agent.graph.tool_execution import _make_interact_callback
        assert _make_interact_callback(None) is None

    @pytest.mark.asyncio
    async def test_ask_choice_with_options(self):
        from voidx.agent.graph.tool_execution import _make_interact_callback

        class FakeApp:
            async def ask_choice(self, prompt, choices, **kwargs):
                return choices[1][1]

            async def ask_text(self, prompt, **kwargs):
                return "text"

        callback = _make_interact_callback(FakeApp())
        response = await callback(UserInteraction(
            prompt="Choose",
            options=[("A", "a", "desc a"), ("B", "b", "desc b")],
        ))
        assert response.value == "b"
        assert not response.cancelled
        assert not response.free_text

    @pytest.mark.asyncio
    async def test_ask_choice_appends_other_option(self):
        from voidx.agent.graph.tool_execution import _make_interact_callback

        captured_choices = []

        class FakeApp:
            async def ask_choice(self, prompt, choices, **kwargs):
                captured_choices.extend(choices)
                return choices[0][1]

            async def ask_text(self, prompt, **kwargs):
                return "text"

        callback = _make_interact_callback(FakeApp())
        response = await callback(UserInteraction(
            prompt="Choose",
            options=[("A", "a", "desc a")],
        ))

        assert response.value == "a"
        assert captured_choices[-1][0] == "Other (type your answer)"
        assert captured_choices[-1][2] == ""

    @pytest.mark.asyncio
    async def test_ask_choice_other_invokes_text_and_marks_free_text(self):
        from voidx.agent.graph.tool_execution import _make_interact_callback

        calls = []

        class FakeApp:
            async def ask_choice(self, prompt, choices, **kwargs):
                calls.append(("choice", prompt, choices))
                return choices[-1][1]

            async def ask_text(self, prompt, **kwargs):
                calls.append(("text", prompt, kwargs))
                return "custom answer"

        callback = _make_interact_callback(FakeApp())
        response = await callback(UserInteraction(
            prompt="Choose",
            options=[("A", "a", "desc a")],
        ))

        assert response.value == "custom answer"
        assert response.free_text is True
        assert [call[0] for call in calls] == ["choice", "text"]

    @pytest.mark.asyncio
    async def test_ask_choice_other_sentinel_collision_preserves_real_option(self):
        from voidx.agent.graph.tool_execution import _make_interact_callback

        text_called = False
        captured_choices = []

        class FakeApp:
            async def ask_choice(self, prompt, choices, **kwargs):
                captured_choices.extend(choices)
                return "__voidx_choice_prompt_other__"

            async def ask_text(self, prompt, **kwargs):
                nonlocal text_called
                text_called = True
                return "text"

        callback = _make_interact_callback(FakeApp())
        response = await callback(UserInteraction(
            prompt="Choose",
            options=[("Real Other", "__voidx_choice_prompt_other__", "desc")],
        ))

        assert response.value == "__voidx_choice_prompt_other__"
        assert response.free_text is False
        assert text_called is False
        assert captured_choices[-1][1] == "__voidx_choice_prompt_other___1"

    @pytest.mark.asyncio
    async def test_ask_text_without_options(self):
        from voidx.agent.graph.tool_execution import _make_interact_callback

        class FakeApp:
            async def ask_choice(self, prompt, choices, **kwargs):
                return "choice"

            async def ask_text(self, prompt, **kwargs):
                return "user input"

        callback = _make_interact_callback(FakeApp())
        response = await callback(UserInteraction(prompt="Enter text:"))
        assert response.value == "user input"

    @pytest.mark.asyncio
    async def test_cancelled_when_app_returns_none(self):
        from voidx.agent.graph.tool_execution import _make_interact_callback

        class FakeApp:
            async def ask_choice(self, prompt, choices, **kwargs):
                return None

            async def ask_text(self, prompt, **kwargs):
                return None

        callback = _make_interact_callback(FakeApp())
        response = await callback(UserInteraction(
            prompt="Choose",
            options=[("A", "a", "desc")],
        ))
        assert response.cancelled is True
        assert response.value == ""


class TestDumpPendingApproval:
    def test_none_returns_none(self):
        from voidx.agent.graph.tool_execution import _dump_pending_approval
        assert _dump_pending_approval(None) is None

    def test_dict_passthrough(self):
        from voidx.agent.graph.tool_execution import _dump_pending_approval
        d = {"kind": "implementation", "scope": "Fix bug"}
        assert _dump_pending_approval(d) is d

    def test_pydantic_model_dump(self):
        from voidx.agent.graph.tool_execution import _dump_pending_approval
        pa = PendingApproval(scope="Refactor auth")
        result = _dump_pending_approval(pa)
        assert result["kind"] == "implementation"
        assert result["scope"] == "Refactor auth"

    def test_other_type_returns_none(self):
        from voidx.agent.graph.tool_execution import _dump_pending_approval
        assert _dump_pending_approval(42) is None


class TestStateUpdateFromExecutedTools:
    def test_merges_state_patches(self):
        from voidx.agent.graph.tool_execution import _state_update_from_executed_tools, _ExecutedTool

        patch1 = ToolStatePatch(task_intent=TaskIntent.IMPLEMENT, intent_source="on_intent")
        patch2 = ToolStatePatch(goal="Refactor auth", goal_phase="implement")

        msg1 = ToolMessage(content="result1", tool_call_id="c1")
        msg2 = ToolMessage(content="result2", tool_call_id="c2")

        result1 = ToolResult(output="r1", metadata={"state_patch": patch1.model_dump(mode="json", exclude_unset=True)})
        result2 = ToolResult(output="r2", metadata={"state_patch": patch2.model_dump(mode="json", exclude_unset=True)})

        executed = [
            _ExecutedTool(message=msg1, result=result1, tool_call={"name": "on_intent"}),
            _ExecutedTool(message=msg2, result=result2, tool_call={"name": "clarify"}),
        ]

        update = _state_update_from_executed_tools(executed)
        assert update["task_intent"] == "implement"
        assert update["goal"] == "Refactor auth"
        assert update["goal_phase"] == "implement"

    def test_later_patch_overrides_earlier(self):
        from voidx.agent.graph.tool_execution import _state_update_from_executed_tools, _ExecutedTool

        patch1 = ToolStatePatch(task_intent=TaskIntent.DESIGN, intent_source="on_intent")
        patch2 = ToolStatePatch(task_intent=TaskIntent.IMPLEMENT, intent_source="clarify")

        msg1 = ToolMessage(content="r1", tool_call_id="c1")
        msg2 = ToolMessage(content="r2", tool_call_id="c2")

        result1 = ToolResult(output="r1", metadata={"state_patch": patch1.model_dump(mode="json", exclude_unset=True)})
        result2 = ToolResult(output="r2", metadata={"state_patch": patch2.model_dump(mode="json", exclude_unset=True)})

        executed = [
            _ExecutedTool(message=msg1, result=result1, tool_call={"name": "on_intent"}),
            _ExecutedTool(message=msg2, result=result2, tool_call={"name": "clarify"}),
        ]

        update = _state_update_from_executed_tools(executed)
        assert update["task_intent"] == "implement"

    def test_none_pending_approval_clears_state(self):
        from voidx.agent.graph.tool_execution import _state_update_from_executed_tools, _ExecutedTool

        patch = ToolStatePatch(pending_approval=None)
        msg = ToolMessage(content="r", tool_call_id="c1")
        result = ToolResult(output="r", metadata={"state_patch": patch.model_dump(mode="json", exclude_unset=True)})

        executed = [_ExecutedTool(message=msg, result=result, tool_call={"name": "plan_checkpoint"})]
        update = _state_update_from_executed_tools(executed)
        assert update["pending_approval"] is None

    def test_no_patch_returns_empty(self):
        from voidx.agent.graph.tool_execution import _state_update_from_executed_tools, _ExecutedTool

        msg = ToolMessage(content="r", tool_call_id="c1")
        result = ToolResult(output="r", metadata={})
        executed = [_ExecutedTool(message=msg, result=result, tool_call={"name": "read"})]
        update = _state_update_from_executed_tools(executed)
        assert update == {}


class TestFileOps:
    """File operations work on real files."""

    @pytest.mark.asyncio
    async def test_read(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("line1\nline2\nline3\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("read", {"file_path": "test.txt"}, ctx)
        expected = "1\tline1\n2\tline2\n3\tline3"
        assert result.output.strip() == expected
        assert result.metadata["lines"] == 3

    @pytest.mark.asyncio
    async def test_write(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("write", {"file_path": "out.txt", "content": "hello"}, ctx)
        assert "File written" in result.output
        assert (tmp_path / "out.txt").read_text() == "hello"

    @pytest.mark.asyncio
    async def test_edit(self, tmp_path):
        f = tmp_path / "edit.txt"
        f.write_text("hello world")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool(
            "edit",
            {"file_path": "edit.txt", "edits": [{"old_string": "hello", "new_string": "hi"}]},
            ctx,
        )
        assert "File edited" in result.output
        assert (tmp_path / "edit.txt").read_text() == "hi world"

    @pytest.mark.asyncio
    async def test_edit_output_contains_diff(self, tmp_path):
        f = tmp_path / "edit.txt"
        f.write_text("hello world")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool(
            "edit",
            {"file_path": "edit.txt", "edits": [{"old_string": "hello", "new_string": "hi"}]},
            ctx,
        )
        assert "File edited" in result.output
        assert result.diff is not None
        assert "-hello" in result.diff
        assert "+hi" in result.diff
        # output should also contain the diff text
        assert "-hello" in result.output or "diff" in result.output.lower()

    @pytest.mark.asyncio
    async def test_edit_rejects_multiple_matches(self, tmp_path):
        f = tmp_path / "multi.txt"
        f.write_text("foo bar foo baz")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool(
            "edit",
            {"file_path": "multi.txt", "edits": [{"old_string": "foo", "new_string": "qux"}]},
            ctx,
        )
        assert "2 times" in result.output or "matches" in result.output
        assert result.metadata.get("error")
        assert (tmp_path / "multi.txt").read_text() == "foo bar foo baz"

    @pytest.mark.asyncio
    async def test_read_nonexistent(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("read", {"file_path": "nope.txt"}, ctx)
        assert "File not found" in result.output

    @pytest.mark.asyncio
    async def test_read_offset_beyond_file(self, tmp_path):
        f = tmp_path / "short.txt"
        f.write_text("line1\nline2\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("read", {"file_path": "short.txt", "offset": 100}, ctx)
        assert result.metadata["lines"] == 0
        assert "beyond" in result.output.lower() or "offset" in result.output.lower()

    @pytest.mark.asyncio
    async def test_apply_patch_single_file(self, tmp_path):
        (tmp_path / "a.txt").write_text("one\ntwo\nthree\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool("apply_patch", {"patch": """--- a/a.txt
+++ b/a.txt
@@ -1,3 +1,3 @@
 one
-two
+TWO
 three
"""}, ctx)

        assert result.metadata["changed_files"] == 1
        assert (tmp_path / "a.txt").read_text() == "one\nTWO\nthree\n"
        assert "-two" in result.diff
        assert "+TWO" in result.diff

    @pytest.mark.asyncio
    async def test_apply_patch_multi_file(self, tmp_path):
        for name in ("a.txt", "b.txt", "c.txt"):
            (tmp_path / name).write_text(f"{name}\nold\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool("apply_patch", {"patch": """--- a/a.txt
+++ b/a.txt
@@ -1,2 +1,2 @@
 a.txt
-old
+new-a
--- a/b.txt
+++ b/b.txt
@@ -1,2 +1,2 @@
 b.txt
-old
+new-b
--- a/c.txt
+++ b/c.txt
@@ -1,2 +1,2 @@
 c.txt
-old
+new-c
"""}, ctx)

        assert result.metadata["changed_files"] == 3
        assert (tmp_path / "a.txt").read_text() == "a.txt\nnew-a\n"
        assert (tmp_path / "b.txt").read_text() == "b.txt\nnew-b\n"
        assert (tmp_path / "c.txt").read_text() == "c.txt\nnew-c\n"

    @pytest.mark.asyncio
    async def test_apply_patch_returns_combined_diff(self, tmp_path):
        (tmp_path / "a.txt").write_text("a\nold\n")
        (tmp_path / "b.txt").write_text("b\nold\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool("apply_patch", {"patch": """--- a/a.txt
+++ b/a.txt
@@ -1,2 +1,2 @@
 a
-old
+new-a
--- a/b.txt
+++ b/b.txt
@@ -1,2 +1,2 @@
 b
-old
+new-b
"""}, ctx)

        assert result.metadata["changed_files"] == 2
        assert "--- a/a.txt" in result.diff
        assert "+++ b/a.txt" in result.diff
        assert "-old" in result.diff
        assert "+new-a" in result.diff
        assert "--- a/b.txt" in result.diff
        assert "+++ b/b.txt" in result.diff
        assert "+new-b" in result.diff

    @pytest.mark.asyncio
    async def test_apply_patch_create_file(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool("apply_patch", {"patch": """--- /dev/null
+++ b/new.txt
@@ -0,0 +1,2 @@
+hello
+world
"""}, ctx)

        assert result.metadata["files"][0]["status"] == "create"
        assert (tmp_path / "new.txt").read_text() == "hello\nworld\n"

    @pytest.mark.asyncio
    async def test_apply_patch_delete_file(self, tmp_path):
        (tmp_path / "gone.txt").write_text("remove\nme\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool("apply_patch", {"patch": """--- a/gone.txt
+++ /dev/null
@@ -1,2 +0,0 @@
-remove
-me
"""}, ctx)

        assert result.metadata["files"][0]["status"] == "delete"
        assert not (tmp_path / "gone.txt").exists()

    @pytest.mark.asyncio
    async def test_apply_patch_atomic_validation(self, tmp_path):
        (tmp_path / "ok.txt").write_text("before\n")
        (tmp_path / "bad.txt").write_text("actual\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool("apply_patch", {"patch": """--- a/ok.txt
+++ b/ok.txt
@@ -1,1 +1,1 @@
-before
+after
--- a/bad.txt
+++ b/bad.txt
@@ -1,1 +1,1 @@
-expected
+changed
"""}, ctx)

        assert result.metadata["error"] is True
        assert (tmp_path / "ok.txt").read_text() == "before\n"
        assert (tmp_path / "bad.txt").read_text() == "actual\n"

    @pytest.mark.asyncio
    async def test_apply_patch_fuzzy_match(self, tmp_path):
        (tmp_path / "fuzzy.txt").write_text("header\none\ntwo\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool("apply_patch", {"patch": """--- a/fuzzy.txt
+++ b/fuzzy.txt
@@ -1,2 +1,2 @@
-one
+ONE
 two
"""}, ctx)

        assert result.metadata.get("error") is not True
        assert (tmp_path / "fuzzy.txt").read_text() == "header\nONE\ntwo\n"

    @pytest.mark.asyncio
    async def test_apply_patch_dry_run(self, tmp_path):
        (tmp_path / "dry.txt").write_text("old\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool("apply_patch", {"patch": """--- a/dry.txt
+++ b/dry.txt
@@ -1,1 +1,1 @@
-old
+new
""", "dry_run": True}, ctx)

        assert result.metadata["dry_run"] is True
        assert (tmp_path / "dry.txt").read_text() == "old\n"
        assert "+new" in result.diff

    @pytest.mark.asyncio
    async def test_apply_patch_staleness(self, tmp_path):
        target = tmp_path / "stale.txt"
        target.write_text("old\n")
        ctx = ToolContext(workspace=str(tmp_path))
        ctx.file_mtimes[str(target.resolve())] = 0
        r = ToolRegistry()

        result = await r.execute_tool("apply_patch", {"patch": """--- a/stale.txt
+++ b/stale.txt
@@ -1,1 +1,1 @@
-old
+new
"""}, ctx)

        assert result.metadata["error"] is True
        assert "modified since last read" in result.output
        assert target.read_text() == "old\n"

    @pytest.mark.asyncio
    async def test_apply_patch_blocks_path_traversal(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool("apply_patch", {"patch": """--- /dev/null
+++ b/../outside.txt
@@ -0,0 +1,1 @@
+nope
"""}, ctx)

        assert result.metadata["error"] is True
        assert "Path traversal blocked" in result.output
        assert not (tmp_path.parent / "outside.txt").exists()

    @pytest.mark.asyncio
    async def test_apply_patch_rejects_rename(self, tmp_path):
        (tmp_path / "old.txt").write_text("old\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool("apply_patch", {"patch": """--- a/old.txt
+++ b/new.txt
@@ -1,1 +1,1 @@
-old
+new
"""}, ctx)

        assert result.metadata["error"] is True
        assert "Rename patches are not supported" in result.output
        assert (tmp_path / "old.txt").read_text() == "old\n"


class TestSearch:
    """Search tools find files deterministically."""

    @pytest.mark.asyncio
    async def test_glob(self, tmp_path):
        (tmp_path / "a.py").touch()
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.py").touch()
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("glob", {"pattern": "**/*.py"}, ctx)
        assert "a.py" in result.output
        assert "sub/b.py" in result.output.replace("\\", "/")

    @pytest.mark.asyncio
    async def test_grep(self, tmp_path):
        (tmp_path / "code.py").write_text("TODO: fix this\nprint('ok')\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "TODO"}, ctx)
        assert "code.py" in result.output
        assert "TODO" in result.output

    @pytest.mark.asyncio
    async def test_grep_no_match(self, tmp_path):
        (tmp_path / "code.py").write_text("nothing here\n")
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("grep", {"pattern": "XYZNOTFOUND"}, ctx)
        assert "No matches" in result.output

    @pytest.mark.asyncio
    async def test_grep_logs_unreadable_file_and_continues(self, tmp_path, monkeypatch, caplog):
        bad = tmp_path / "bad.py"
        good = tmp_path / "good.py"
        bad.write_text("TODO hidden\n")
        good.write_text("TODO visible\n")
        original_read_text = Path.read_text

        def fake_read_text(self, *args, **kwargs):
            if self == bad:
                raise OSError("cannot read")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fake_read_text)
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        with caplog.at_level(logging.DEBUG, logger="voidx.tools.search"):
            result = await r.execute_tool("grep", {"pattern": "TODO", "include": "*.py"}, ctx)

        assert "good.py" in result.output
        assert "TODO visible" in result.output
        assert "Failed to read file during grep" in caplog.text
        assert "bad.py" in caplog.text

    def test_repomap_logs_python_symbol_extraction_failure(self, tmp_path, monkeypatch, caplog):
        from voidx.tools import repomap as repomap_module

        target = tmp_path / "broken.py"
        target.write_text("def ok():\n    pass\n")
        original_read_text = Path.read_text

        def fake_read_text(self, *args, **kwargs):
            if self == target:
                raise OSError("cannot read")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fake_read_text)

        with caplog.at_level(logging.DEBUG, logger="voidx.tools.repomap"):
            symbols = repomap_module._extract_python_symbols(target)

        assert symbols == []
        assert "Failed to extract Python symbols" in caplog.text
        assert "broken.py" in caplog.text


class TestBash:
    """Bash commands execute and capture output."""

    @pytest.mark.asyncio
    async def test_bash_echo(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()
        result = await r.execute_tool("bash", {"command": "echo hello"}, ctx)
        assert "hello" in result.output
        assert result.metadata["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_bash_blocks_workspace_escape_in_tool_layer(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside = tmp_path / "outside.txt"
        ctx = ToolContext(workspace=str(workspace))
        r = ToolRegistry()

        result = await r.execute_tool(
            "bash",
            {"command": f"printf nope > {shlex.quote(str(outside))}"},
            ctx,
        )

        assert result.metadata["blocked"] is True
        assert not outside.exists()

    @pytest.mark.asyncio
    async def test_bash_timeout_terminates_process(self, tmp_path):
        ctx = ToolContext(workspace=str(tmp_path))
        r = ToolRegistry()

        result = await r.execute_tool(
            "bash",
            {"command": "sleep 2; printf late > late.txt", "timeout": 1},
            ctx,
        )
        await asyncio.sleep(2.2)

        assert result.metadata["timeout"] is True
        assert not (tmp_path / "late.txt").exists()


class TestTaskTracker:
    """TaskTracker reports worker-role progress."""

    def test_start_and_update(self):
        tracker = TaskTracker()
        tracker.start("t1", "implement", "write foo.py", max_steps=5)
        t = tracker.get("t1")
        assert t is not None
        assert t.status == "running"
        assert t.agent == "implement"

        tracker.update("t1", step=3, last_output="writing file...")
        t = tracker.get("t1")
        assert t.step == 3
        assert "writing file" in t.last_output

    def test_finish(self):
        tracker = TaskTracker()
        tracker.start("t2", "explore", "search")
        tracker.finish("t2", "completed")
        assert tracker.get("t2").status == "completed"

    def test_list_running(self):
        tracker = TaskTracker()
        tracker.start("a", "explore", "x")
        tracker.start("b", "implement", "y")
        tracker.finish("a", "completed")
        running = tracker.list_running()
        assert len(running) == 1
        assert running[0].id == "b"

    def test_format_status(self):
        tracker = TaskTracker()
        tracker.start("t1", "implement", "write foo.py", max_steps=5)
        tracker.update("t1", step=2, last_output="found target")
        output = tracker.format_status()
        assert "implement" in output
        assert "running" in output

    def test_todo_state_is_managed_through_public_api(self):
        tracker = TaskTracker()
        todos = [{"content": "ship fix", "status": "pending"}]

        tracker.set_todos(todos)
        todos.clear()

        assert tracker.list_todos() == [{"content": "ship fix", "status": "pending"}]
        tracker.clear_todos()
        assert tracker.list_todos() == []

    @pytest.mark.asyncio
    async def test_todo_tool_returns_structured_metadata(self, tmp_path):
        tracker = TaskTracker()
        tool = TodoWriteTool(tracker=tracker)
        ctx = ToolContext(workspace=str(tmp_path))

        result = await tool.execute({
            "todos": [
                {"content": "implement event", "status": "in_progress"},
                {"content": "write tests", "status": "pending"},
                {"content": "update docs", "status": "completed"},
            ],
        }, ctx)

        assert result.metadata["todo_summary"] == "1/3 done · 1 active · 1 pending"
        assert result.metadata["todo_items"] == [
            {"content": "implement event", "status": "in_progress"},
            {"content": "write tests", "status": "pending"},
            {"content": "update docs", "status": "completed"},
        ]
        assert tracker.list_todos()[0].content == "implement event"

    def test_todo_input_rejects_unknown_status(self):
        with pytest.raises(ValueError):
            TodoInput.model_validate({
                "todos": [{"content": "bad status", "status": "blocked"}],
            })

    @pytest.mark.asyncio
    async def test_task_status_tool(self, tmp_path):
        tracker = TaskTracker()
        tracker.start("t1", "explore", "scan directory")
        tool = TaskStatusTool(tracker=tracker)
        ctx = ToolContext(workspace=str(tmp_path))

        result = await tool.execute({}, ctx)
        assert "explore" in result.output
        assert "running" in result.output

        result2 = await tool.execute({"task_id": "t1"}, ctx)
        assert "t1" in result2.output
