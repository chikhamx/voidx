from voidx.tooling.policy.permission.rules import (
    PermissionCapability,
    capability_for_tool,
    file_paths_for_tool,
)


def test_lsp_format_is_a_file_format_capability():
    args = {
        "file_path": "src/app.py",
        "start_line": 1,
        "start_character": 0,
        "end_line": 1,
        "end_character": 5,
    }

    assert capability_for_tool("lsp_format", args) == PermissionCapability.FILE_FORMAT
    assert file_paths_for_tool("lsp_format", args) == ["src/app.py"]
