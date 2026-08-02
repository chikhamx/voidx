"""Smoke tests for tool system — types, execution, error handling."""

import asyncio
import json
import logging
import shlex
import sys
from pathlib import Path


import pytest

from langchain_core.messages import ToolMessage

from voidx.agent.application.tool_messages import DEFAULT_TOOL_MESSAGE_MAX_CHARS
from voidx.tools.base import ToolContext, ToolResult, BaseTool, UserInteraction, UserResponse
from voidx.tools.file import FileReadInput, FileReplaceInput, WriteInput, FileReadTool, WriteTool, FileReplaceTool, ManageTool
from voidx.tools.file.state import save_file_version
import voidx.tools.file.state as file_state
from voidx.tools.search import FindInput, SearchInput, FindTool, SearchTool
from voidx.tools.bash import BashInput, BashTool
from voidx.tools.powershell import PowerShellTool
from voidx.tools.git import GitTool
from voidx.tools.lsp import LspTool, LspFormatTool
from voidx.tools.agent import AgentInput, AgentTool
from voidx.tools.task_tracker import TaskTracker
from voidx.tools.todo import TodoInput, TodoWriteTool
from voidx.tools.registry import ToolRegistry
from voidx.tools.clarify import ClarifyTool, ClarifyInput, _infer_state_patch
from voidx.tools.skills import SkillsTool
from voidx.tools.document import DocumentTool, DocumentInput
from voidx.tools.checkpoint import PlanCheckpointTool
from voidx.runtime.task_state import GoalSpec, GoalResolution, IntentResolution, PlanResolution, ToolStatePatch
from voidx.agent.application.runtime_context import TaskIntent
from voidx.skills.context import SKILL_TOOL_CONTEXT_MARKER
from voidx.workflow.runtime import WorkflowRunState, WorkflowRunStatus
from voidx.workflow.types import WorkflowStateEventKind
import voidx.memory.store as store


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

    def test_model_to_json_schema_flattens_anyof(self):
        from voidx.tools.base import model_to_json_schema
        from voidx.tools.file.manage import ManageInput

        schema = model_to_json_schema(ManageInput)
        for name, prop in schema["properties"].items():
            assert "anyOf" not in prop, (
                f"Property '{name}' has anyOf — OpenAI strict mode prefers "
                f"multi-type 'type' arrays over anyOf for optional fields"
            )

    def test_replace_anyof_preserves_null_and_array_types(self):
        from voidx.tools.base import _replace_anyof

        prop: dict = {"anyOf": [
            {"type": "string"},
            {"type": "array", "items": {"type": "string"}},
            {"type": "null"},
        ]}
        _replace_anyof(prop)
        assert prop["type"] == ["string", "array", "null"]
        assert prop["items"] == {"type": "string"}

    def test_replace_anyof_skips_ref_branch_without_type(self):
        from voidx.tools.base import _replace_anyof

        prop: dict = {"anyOf": [
            {"type": "string"},
            {"$ref": "#/$defs/Foo"},
        ]}
        _replace_anyof(prop)
        assert prop["type"] == "string"

    def test_replace_anyof_no_type_branches_returns_unchanged(self):
        from voidx.tools.base import _replace_anyof

        original = {"anyOf": [{"$ref": "#/$defs/A"}, {"$ref": "#/$defs/B"}]}
        prop = dict(original)
        _replace_anyof(prop)
        assert "anyOf" not in prop
        assert "type" not in prop

    def test_read_input_validates(self):
        inp = FileReadInput(file_path="foo.py")
        assert inp.file_path == "foo.py"
        assert inp.offset is None
        assert inp.limit is None

    def test_read_input_with_offset(self):
        inp = FileReadInput(file_path="foo.py", offset=10, limit=5)
        assert inp.offset == 10
        assert inp.limit == 5

    def test_read_input_normalizes_zero_offset_to_first_line(self):
        inp = FileReadInput(file_path="foo.py", offset=0)
        assert inp.offset == 1

    def test_read_input_rejects_negative_values_and_zero_limit(self):
        with pytest.raises(ValueError):
            FileReadInput(file_path="foo.py", limit=0)
        with pytest.raises(ValueError):
            FileReadInput(file_path="foo.py", offset=-1)
        with pytest.raises(ValueError):
            FileReadInput(file_path="foo.py", limit=-2)


    def test_write_input_supports_insert_and_append(self):
        insert = WriteInput(file_path="x.py", op="insert", lineno=3, new_string="added\n")
        append = WriteInput(file_path="x.py", op="append", new_string="appended\n")
        assert insert.lineno == 3
        assert append.op == "append"

    def test_write_insert_requires_lineno(self):
        with pytest.raises(ValueError):
            WriteInput(file_path="x.py", op="insert", new_string="nope\n")

    def test_write_append_ignores_lineno(self):
        inp = WriteInput(file_path="x.py", op="append", lineno=3, new_string="nope\n")
        assert inp.op == "append"
        assert inp.lineno == 3

    def test_write_schema_has_insert_append_fields(self):
        schema = WriteTool().parameters_schema()

        assert set(schema["properties"]) == {"file_path", "op", "lineno", "new_string"}
        assert "insert" in schema["properties"]["op"]["description"]
        assert "append" in schema["properties"]["op"]["description"]
        assert "op=write" in schema["properties"]["new_string"]["description"]
        assert "up to three non-empty" in schema["properties"]["new_string"]["description"]
        assert "treated as overlap" in schema["properties"]["new_string"]["description"]
        assert "creates or fully overwrites" in WriteTool.description

    def test_file_tool_descriptions_are_precise_for_llms(self):
        read_schema = FileReadTool().parameters_schema()
        write_schema = WriteTool().parameters_schema()
        manage_schema = ManageTool().parameters_schema()

        assert "numbered lines" in FileReadTool.description
        assert "1-based" in read_schema["properties"]["offset"]["description"]
        assert "maximum number of lines" in read_schema["properties"]["limit"]["description"].lower()
        assert "complete file content" in write_schema["properties"]["new_string"]["description"]
        assert "No file content is written" in ManageTool.description
        assert "paths is required" in manage_schema["properties"]["paths"]["description"]
        assert "per-move" in manage_schema["properties"]["moves"]["description"]

    def test_replace_input_uses_bounds_without_operation(self):
        inp = FileReplaceInput(file_path="x.py", bounds=[{"line_no": 3, "anchor": "old"}, {"line_no": 5, "anchor": "tail"}], new_string="new")
        schema = FileReplaceTool().parameters_schema()

        assert inp.resolved_start_anchor == "old"
        assert inp.resolved_end_no == 5
        assert set(schema["properties"]) == {"file_path", "bounds", "new_string"}
        assert "One or two boundary locators" in schema["properties"]["bounds"]["description"]
        assert "complete lines" in FileReplaceTool().description.lower()
        assert "operation" not in schema["properties"]
        assert "edits" not in schema["properties"]
        assert "old_text" not in schema["properties"]

    def test_replace_input_rejects_duplicate_two_bound_line_numbers(self):
        with pytest.raises(ValueError):
            FileReplaceInput(file_path="x.py", bounds=[{"line_no": 5, "anchor": "old"}, {"line_no": 5, "anchor": "tail"}], new_string="new")
        with pytest.raises(ValueError):
            FileReplaceInput(file_path="x.py", bounds=[{"line_no": 5, "anchor": ""}, {"line_no": 3, "anchor": "tail"}], new_string="new")

    def test_find_input(self):
        inp = FindInput(query="src", extensions=["py"])
        assert inp.query == "src"
        assert inp.extensions == ["py"]

    def test_search_input(self):
        inp = SearchInput(query="TODO", extensions=["py"])
        assert inp.query == "TODO"

    def test_bash_input(self):
        inp = BashInput(command="ls")
        assert inp.command == "ls"
        assert inp.timeout == 120

    def test_execution_and_discovery_tool_descriptions_are_precise_for_llms(self):
        git_schema = GitTool().parameters_schema()
        bash_schema = BashTool().parameters_schema()
        powershell_schema = PowerShellTool().parameters_schema()
        find_schema = FindTool().parameters_schema()
        search_schema = SearchTool().parameters_schema()
        lsp_schema = LspTool().parameters_schema()
        lsp_format_schema = LspFormatTool().parameters_schema()
        skill_schema = SkillsTool().parameters_schema()

        assert "do not include the git executable" in git_schema["properties"]["args"]["description"]
        assert "read-only commands return structured JSON" in GitTool.description
        assert "working directory is the workspace root" in BashTool.description
        assert "non-interactive" in bash_schema["properties"]["command"]["description"]
        assert "terminated" in bash_schema["properties"]["timeout"]["description"]
        assert "Windows only" in PowerShellTool.description
        assert "PowerShell command string" in powershell_schema["properties"]["command"]["description"]
        assert "Filename" in find_schema["properties"]["query"]["description"]
        assert "stable structured" in FindTool.description
        assert "Text or regular expression" in search_schema["properties"]["query"]["description"]
        assert "File or directory scope" in search_schema["properties"]["path"]["description"]
        assert "literal" in SearchTool.description
        assert "definition and references" in lsp_schema["properties"]["line"]["description"]
        assert "0-based" in lsp_schema["properties"]["character"]["description"]
        assert "range" in LspFormatTool.description.lower()
        assert set(lsp_format_schema["required"]) == {
            "file_path", "start_line", "start_character", "end_line", "end_character"
        }
        assert "1-based" in lsp_format_schema["properties"]["start_line"]["description"]
        assert "UTF-16" in lsp_format_schema["properties"]["end_character"]["description"]
        assert "Load/list are read-only" in SkillsTool.description
        assert "Required for op=load and op=create" in skill_schema["properties"]["name"]["description"]

    def test_agent_input_uses_child_agent_schema(self):
        inp = AgentInput.model_validate({
            "name": "voidx",
            "mode": "inspect",
            "task": "Inspect auth flow",
            "target": "src/voidx/auth.py",
        })
        assert inp.name == "voidx"
        assert inp.mode == "inspect"
        assert inp.task == "Inspect auth flow"
        assert inp.target == "src/voidx/auth.py"
        assert inp.result_preset == "auto"
        schema = AgentInput.model_json_schema()
        assert "name" in schema["properties"]
        assert "agent" not in schema["properties"]
        assert "sub-voidx" not in str(schema)
        assert "name" not in schema.get("required", [])
        assert "mode" not in schema.get("required", [])
        assert "task" not in schema.get("required", [])
        assert "target" not in schema.get("required", [])
        wait_inp = AgentInput.model_validate({
            "action": "wait",
            "target_run_id": "run_123",
            "timeout": 1,
        })
        assert wait_inp.action == "wait"
        assert wait_inp.name is None
        assert wait_inp.mode is None
        assert wait_inp.task is None
        assert wait_inp.target is None
        required = schema.get("required", [])
        assert "description" not in required
        assert "goal_resolution" not in required
        assert "result" not in required
        assert "persona" not in required
        assert "max_steps" not in required
        assert "delegation_reason" not in required
        assert "expected_output" not in required
        assert "parent_evidence" not in required
        assert "subagent_type" not in schema["properties"]

    def test_interaction_tool_descriptions_are_precise_for_llms(self):
        agent_schema = AgentTool(runner=None).parameters_schema()
        document_schema = DocumentTool().parameters_schema()
        clarify_schema = ClarifyTool().parameters_schema()
        checkpoint_schema = PlanCheckpointTool().parameters_schema()

        assert "independent delegated task" in AgentTool(runner=None).description
        assert "return its run_id immediately" in AgentTool(runner=None).description
        assert "self-contained" in agent_schema["properties"]["task"]["description"]
        assert "Use empty string if not needed" in agent_schema["properties"]["success_criteria"]["description"]
        assert "built-in document" in DocumentTool.description
        assert "does not read workspace files" in DocumentTool.description
        assert "list reads a directory README index" in document_schema["properties"]["action"]["description"]
        assert "one question" in ClarifyTool.description
        assert "Do not use for progress updates" in ClarifyTool.description
        assert "mutually exclusive" in clarify_schema["properties"]["options"]["description"]
        assert "approval gate" in PlanCheckpointTool.description
        assert "no code changes" in PlanCheckpointTool.description
        assert "one small action" in checkpoint_schema["properties"]["steps"]["description"]

    def test_agent_input_allows_control_actions_without_spawn_fields(self):
        wait_inp = AgentInput.model_validate({"action": "wait", "target_run_id": "run_123", "timeout": 1})
        cancel_inp = AgentInput.model_validate({"action": "cancel", "target_run_id": "run_123"})

        assert wait_inp.name is None
        assert wait_inp.mode is None
        assert wait_inp.task is None
        assert wait_inp.target is None
        assert cancel_inp.name is None
        assert cancel_inp.mode is None
        assert cancel_inp.task is None
        assert cancel_inp.target is None
