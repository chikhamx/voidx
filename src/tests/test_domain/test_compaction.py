from langchain_core.messages import HumanMessage

from voidx.agent.domain.compaction import CompactionResult, PreflightCompactionResult
from voidx.agent.adapters.langgraph.runtime.compaction_coordinator import (
    CompactionResult as CoordinatorCompactionResult,
)
from voidx.agent.adapters.langgraph.runtime.compaction_coordinator import (
    PreflightCompactionResult as CoordinatorPreflightCompactionResult,
)


def test_compaction_dtos_have_one_domain_source() -> None:
    assert CoordinatorCompactionResult is CompactionResult
    assert CoordinatorPreflightCompactionResult is PreflightCompactionResult


def test_preflight_result_converts_domain_compaction_metadata() -> None:
    removed = [HumanMessage(content="old")]
    result = CompactionResult(
        summary="summary",
        removed_messages=removed,
        live_messages=[HumanMessage(content="new")],
        tail_id="tail-1",
        metadata={
            "removed_message_count": 3,
            "retained_turn_count": 2,
            "pre_tokens": 100,
            "post_tokens": 40,
            "post_compaction_target": 50,
            "compaction_reason": "threshold",
        },
    )

    preflight = PreflightCompactionResult.from_compaction_result(result)

    assert preflight.model_dump() == {
        "compacted": True,
        "summary": "summary",
        "removed_message_count": 3,
        "retained_turn_count": 2,
        "pre_tokens": 100,
        "post_tokens": 40,
        "post_target_tokens": 50,
        "tail_anchor_id": "tail-1",
        "fallback": False,
        "reason": "threshold",
    }


def test_empty_preflight_result_is_not_compacted() -> None:
    assert PreflightCompactionResult.from_compaction_result(None) == PreflightCompactionResult(
        compacted=False
    )
