"""Legacy path permission helpers must not remain public APIs."""


def test_resolve_safe_removed():
    import voidx.tools.base as base
    import voidx.tools.service as service

    assert not hasattr(base, "resolve_safe")
    assert not hasattr(service, "resolve_safe")


def test_tool_context_legacy_path_callbacks_removed(tmp_path):
    from voidx.tools.base import ToolContext

    ctx = ToolContext(workspace=str(tmp_path))

    assert not hasattr(ctx, "sandbox_extra_paths")
    assert not hasattr(ctx, "add_extra_path")
