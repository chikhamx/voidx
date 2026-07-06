"""Terminal rendering mixin composition."""

from __future__ import annotations

from voidx_tui.overlays import _OverlayRendererMixin
from voidx_tui.render_activity import _ActivityRendererMixin
from voidx_tui.render_frame import _FrameRendererMixin
from voidx_tui.render_input import _InputRendererMixin
from voidx_tui.render_status import StatusSegment, _StatusRendererMixin
from voidx_tui.render_todo import _TodoRendererMixin


class _TerminalRendererMixin(
    _FrameRendererMixin,
    _InputRendererMixin,
    _ActivityRendererMixin,
    _TodoRendererMixin,
    _StatusRendererMixin,
    _OverlayRendererMixin,
):
    """Terminal rendering methods grouped by focused helper mixins."""


__all__ = ["StatusSegment", "_TerminalRendererMixin"]
