from __future__ import annotations

from types import SimpleNamespace

import pytest

from voidx.presentation.slash import SlashHandler
from tests.test_slash.context import command_context


@pytest.mark.asyncio
async def test_log_command_uses_narrow_mode_log_configuration() -> None:
    graph = command_context()
    graph.config = SimpleNamespace(log_llm_exchange=False, log_llm_diagnostic=False)

    handler = SlashHandler(graph)
    assert await handler.dispatch("/log exchange on") is True

    assert graph.config.log_llm_exchange is True
    assert graph.config.log_llm_diagnostic is False
