"""Dependency wiring helpers for the agent graph."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from voidx.config import Config, Settings
from voidx.llm.compaction import CompactionService
from voidx.llm.domain.provider import get_context_limit
from voidx.llm.usage import UsageStats




def build_tool_registry(**kwargs: Any):
    from voidx.bootstrap.tooling import build_tool_registry as build

    return build(**kwargs)


def register_agent_tool(*args: Any, **kwargs: Any) -> None:
    from voidx.bootstrap.tooling import register_agent_tool as register

    register(*args, **kwargs)



def build_compaction_service(config: Config) -> tuple[UsageStats, CompactionService]:
    context_limit = get_context_limit(config.model.provider, config.model.protocol or "", config.model.context_window)
    return (
        UsageStats(context_limit=context_limit),
        CompactionService(
            context_limit=context_limit,
            output_token_max=config.model.max_tokens,
            soft_ratio=config.compaction_soft_ratio,
            post_target_ratio=config.compaction_post_target_ratio,
        ),
    )


