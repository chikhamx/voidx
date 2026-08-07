"""Tests for file edit operations — replace and line insert via registry."""

import sys
from pathlib import Path


import pytest

from voidx.tooling.application.execution import FileToolContext as ToolContext
from voidx.tooling.builtin.file.replace import FileReplaceTool
from voidx.tooling.builtin.file.replace_resolve import _find_text_segment
from voidx.tooling.application.registry import ToolRegistry
import voidx.tooling.application.file_state as file_state


class TestReplaceLLMVisibleMessages:
    def test_tool_description_explains_anchor_search_without_runtime_terms(self):
        description = FileReplaceTool.description

        assert "Read the target lines first" in description
        assert "inclusive line range" in description
        assert "missing or ambiguous" in description
        assert "exact start_no/end_no" not in description
        assert "drift" not in description.lower()

    def test_parameter_descriptions_explain_current_line_numbers_without_drift(self):
        schema = FileReplaceTool().parameters_schema()
        properties = schema["properties"]
        bounds_schema = properties["bounds"]
        bound_properties = bounds_schema["items"]["properties"]

        assert set(properties) == {"file_path", "bounds", "new_string"}
        assert "One or two boundary locators" in bounds_schema["description"]
        assert "inclusive range" in bounds_schema["description"]
        assert "Length must be 1 or 2" in bounds_schema["description"]
        assert "1-based line number hint" in bound_properties["line_no"]["description"]
        assert "Literal, case-sensitive substring" in bound_properties["anchor"]["description"]
        assert "unchanged surrounding lines" in properties["new_string"]["description"]
        visible = "\n".join(prop.get("description", "") for prop in properties.values())
        assert "Exact" not in visible
        assert "drift" not in visible.lower()

    def test_ambiguous_single_line_lists_candidates_without_runtime_terms(self):
        result = _find_text_segment(["target = 1", "other = 0", "target = 2"], 2, 2, "target", "target")

        assert isinstance(result, str)
        assert "single-line match ambiguous" in result
        assert "line 1: target = 1" in result
        assert "line 3: target = 2" in result
        assert "Hint: Provide a longer start_anchor" in result
        assert "drift" not in result.lower()
        assert "read(" not in result

    def test_missing_anchor_points_to_unique_line_without_call_syntax(self):
        lines = ["old = 1", "current = 0", "near = 1", "near = 2", "near = 3", "target = 2"]
        result = _find_text_segment(lines, 2, 2, "target", "target")

        assert isinstance(result, str)
        assert "start_anchor 'target' not found near line 2" in result
        assert "appears on line 6" in result
        assert "Read lines 6-6" in result
        assert "read(" not in result
        assert "ToolResult" not in result
        assert "metadata" not in result

    def test_span_mismatch_avoids_drift_and_internal_terms(self):
        result = _find_text_segment(["start", "body", "body", "body", "end"], 1, 2, "start", "end")

        assert isinstance(result, str)
        assert "No valid replace range found" in result
        assert "You specified lines 1-2" in result
        assert "Read the target block again" in result
        assert "drift" not in result.lower()
        assert "expected span" not in result

    def test_span_mismatch_message_not_redundant(self):
        result = _find_text_segment(["start", "body", "body", "body", "end"], 1, 2, "start", "end")

        assert isinstance(result, str)
        assert "No valid replace range found; no valid replace range" not in result
