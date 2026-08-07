"""Tests for repair_tool_name append mapping."""

import sys
from pathlib import Path


from voidx.tooling.policy.permission.rules import repair_tool_name


class TestRepairToolNameAppend:
    def test_append_maps_to_line(self):
        assert repair_tool_name("append") == "write"

    def test_insert_still_maps_to_line(self):
        assert repair_tool_name("insert") == "write"

    def test_delete_still_maps_to_line(self):
        assert repair_tool_name("delete") == "replace"

    def test_unknown_tool_unchanged(self):
        assert repair_tool_name("foobar") == "foobar"
