"""Legacy path permission helpers must not remain public APIs."""


def test_legacy_tool_modules_removed():
    import importlib.util

    for module in ("voidx.tools.base", "voidx.tools.service"):
        try:
            spec = importlib.util.find_spec(module)
        except ModuleNotFoundError:
            spec = None
        assert spec is None


def test_tool_context_legacy_path_callbacks_removed(tmp_path):
    from voidx.tooling.domain.context import ToolExecutionContext as ToolContext

    ctx = ToolContext(workspace=str(tmp_path))

    assert not hasattr(ctx, "sandbox_extra_paths")
    assert not hasattr(ctx, "add_extra_path")
