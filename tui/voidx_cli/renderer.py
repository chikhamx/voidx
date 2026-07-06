"""Terminal rendering mixin composition."""

from __future__ import annotations

from .overlays import _OverlayRendererMixin
from .render_activity import _ActivityRendererMixin
from .render_frame import _FrameRendererMixin
from .render_input import _InputRendererMixin
from .render_status import StatusSegment, _StatusRendererMixin
from .render_todo import _TodoRendererMixin


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
