from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph.message import add_messages

from voidx.agent.adapters.langgraph.runtime.context_pressure import (
    evaluate_context_pressure,
    upsert_context_pressure_hint,
)
from voidx.llm.compaction.service import CompactionService
from voidx.llm.message_markers import CONTEXT_PRESSURE_MARKER, STEP_HINT_MARKER


def _service() -> CompactionService:
    return CompactionService(
        context_limit=100_000,
        output_token_max=0,
        token_counter=lambda messages, model: len(messages),
    )


def _decision(messages: list, tokens: int):
    return evaluate_context_pressure(
        messages,
        tokens,
        compaction_service=_service(),
    )


def test_evaluate_context_pressure_uses_llm_tokens_and_semantic_turn_selection() -> None:
    messages = [HumanMessage(id="turn-1", content="request"), AIMessage(content="working")]

    normal = _decision(messages, 74_999)
    soft = _decision(messages, 75_000)
    hard = _decision(messages, 90_000)

    assert (normal.over_soft, normal.over_hard, normal.pressure_level, normal.should_inject) == (
        False,
        False,
        "none",
        False,
    )
    assert (soft.over_soft, soft.over_hard, soft.pressure_level, soft.should_inject) == (
        True,
        False,
        "soft",
        True,
    )
    assert (hard.over_soft, hard.over_hard, hard.pressure_level, hard.should_inject) == (
        True,
        True,
        "hard",
        True,
    )
    assert hard.turn_id == "turn-1"
    assert hard.turn_count == 1
    assert hard.pre_tokens == 90_000
    assert hard.soft_threshold == 75_000
    assert hard.hard_threshold == 90_000
    assert hard.reason == "hard_threshold"


def test_evaluate_context_pressure_does_not_inject_when_whole_turn_compaction_is_available() -> None:
    messages = [
        HumanMessage(id="turn-1", content="one"),
        AIMessage(content="answer one"),
        HumanMessage(id="turn-2", content="two"),
        AIMessage(content="answer two"),
        HumanMessage(id="turn-3", content="three"),
    ]

    service = _service()
    service.select_preflight_details = lambda _messages: type("Selection", (), {"should_compact": True})()
    decision = evaluate_context_pressure(messages, 90_000, compaction_service=service)

    assert decision.can_compact is True
    assert decision.pressure_level == "hard"
    assert decision.should_inject is False
    assert decision.turn_id == "turn-3"
    assert decision.turn_count == 3


def test_evaluate_context_pressure_skips_synthetic_hints_and_derives_stable_missing_turn_id() -> None:
    messages = [
        HumanMessage(content="real request"),
        HumanMessage(
            id="ordinary-hint",
            content="ordinary convergence hint",
            additional_kwargs={STEP_HINT_MARKER: True},
        ),
        HumanMessage(
            id="old-pressure",
            content="old pressure hint",
            additional_kwargs={STEP_HINT_MARKER: True, CONTEXT_PRESSURE_MARKER: True},
        ),
    ]

    first = _decision(messages, 75_000)
    second = _decision(list(messages), 75_000)

    assert first.turn_count == 1
    assert first.turn_id == "0"
    assert second.turn_id == first.turn_id


def test_upsert_context_pressure_hint_is_immutable_and_same_level_has_empty_delta() -> None:
    original = [HumanMessage(id="turn-1", content="request")]
    snapshot = list(original)
    decision = _decision(original, 75_000)

    injected = upsert_context_pressure_hint(original, decision)

    assert original == snapshot
    assert injected.state_messages is not original
    assert injected.state_messages[:-1] == original
    assert len(injected.message_delta) == 1
    assert injected.pressure_id == "voidx:context-pressure:turn-1"
    assert injected.outcome == "hint_injected"
    hint = injected.state_messages[-1]
    assert hint.id == injected.pressure_id
    assert hint.additional_kwargs == {
        STEP_HINT_MARKER: True,
        CONTEXT_PRESSURE_MARKER: True,
        "pressure_level": "soft",
        "pressure_turn_id": "turn-1",
    }

    repeated = upsert_context_pressure_hint(injected.state_messages, decision)

    assert repeated.state_messages == injected.state_messages
    assert repeated.state_messages is not injected.state_messages
    assert repeated.message_delta == []
    assert repeated.pressure_id == injected.pressure_id
    assert repeated.outcome == "hint_present"


def test_upsert_soft_to_hard_replaces_same_id_with_reducer_and_never_downgrades() -> None:
    messages = [HumanMessage(id="turn-1", content="request")]
    soft = upsert_context_pressure_hint(messages, _decision(messages, 75_000))
    hard = upsert_context_pressure_hint(soft.state_messages, _decision(soft.state_messages, 90_000))

    assert hard.pressure_id == soft.pressure_id
    assert hard.outcome == "hint_upgraded"
    assert len(hard.message_delta) == 1
    assert hard.message_delta[0].id == soft.pressure_id
    assert hard.message_delta[0].additional_kwargs["pressure_level"] == "hard"
    assert len(hard.state_messages) == 2
    assert hard.state_messages[-1].additional_kwargs["pressure_level"] == "hard"

    reduced = add_messages(soft.state_messages, hard.message_delta)
    assert len(reduced) == 2
    assert reduced[-1].additional_kwargs["pressure_level"] == "hard"

    no_downgrade = upsert_context_pressure_hint(hard.state_messages, _decision(hard.state_messages, 75_000))
    assert no_downgrade.message_delta == []
    assert no_downgrade.outcome == "hint_present"
    assert no_downgrade.state_messages[-1].additional_kwargs["pressure_level"] == "hard"


def test_upsert_new_turn_gets_new_id_and_does_not_touch_ordinary_step_hint() -> None:
    ordinary_hint = HumanMessage(
        id="ordinary-hint",
        content="ordinary convergence hint",
        additional_kwargs={STEP_HINT_MARKER: True},
    )
    first_turn = [HumanMessage(id="turn-1", content="one"), ordinary_hint]
    first = upsert_context_pressure_hint(first_turn, _decision(first_turn, 75_000))
    second_turn = [*first.state_messages, AIMessage(content="done"), HumanMessage(id="turn-2", content="two")]

    second = upsert_context_pressure_hint(second_turn, _decision(second_turn, 75_000))

    assert second.pressure_id == "voidx:context-pressure:turn-2"
    assert second.pressure_id != first.pressure_id
    assert second.outcome == "hint_injected"
    assert ordinary_hint in second.state_messages
    assert [message.id for message in second.state_messages].count("ordinary-hint") == 1
    assert first.pressure_id in [message.id for message in second.state_messages]
    assert second.pressure_id in [message.id for message in second.state_messages]


def test_upsert_no_pressure_returns_copied_state_and_no_delta() -> None:
    messages = [HumanMessage(id="turn-1", content="request")]

    update = upsert_context_pressure_hint(messages, _decision(messages, 1))

    assert update.state_messages == messages
    assert update.state_messages is not messages
    assert update.message_delta == []
    assert update.pressure_id == ""
    assert update.outcome == "none"


def test_can_compact_uses_actual_hard_selection_not_turn_count_shortcut() -> None:
    service = _service()
    messages = [
        HumanMessage(id="turn-1", content="one"),
        HumanMessage(id="turn-2", content="two"),
        HumanMessage(id="turn-3", content="three"),
    ]
    service.select_preflight_details = lambda _messages: type("Selection", (), {"should_compact": False})()
    service.select_details = lambda _messages: type("Selection", (), {"should_compact": False})()

    decision = evaluate_context_pressure(messages, 90_000, compaction_service=service)

    assert decision.can_compact is False
    assert decision.should_inject is True


def test_current_context_pressure_finds_only_current_turn_hint() -> None:
    from voidx.agent.adapters.langgraph.runtime.context_pressure import current_context_pressure

    messages = [
        HumanMessage(
            id="voidx:context-pressure:turn-1",
            content="old hard",
            additional_kwargs={
                STEP_HINT_MARKER: True,
                CONTEXT_PRESSURE_MARKER: True,
                "pressure_level": "hard",
            },
        ),
        HumanMessage(
            id="voidx:context-pressure:turn-2",
            content="current soft",
            additional_kwargs={
                STEP_HINT_MARKER: True,
                CONTEXT_PRESSURE_MARKER: True,
                "pressure_level": "soft",
            },
        ),
    ]

    assert current_context_pressure(messages, "turn-2") == (
        "voidx:context-pressure:turn-2",
        "soft",
    )
    assert current_context_pressure(messages, "turn-3") is None


def test_hard_pressure_decision_forces_provider_overflow_hint() -> None:
    from voidx.agent.adapters.langgraph.runtime.context_pressure import hard_pressure_decision

    normal = _decision([HumanMessage(id="turn-1", content="request")], 1)
    hard = hard_pressure_decision(normal)

    assert hard.turn_id == normal.turn_id
    assert hard.pressure_level == "hard"
    assert hard.over_hard is True
    assert hard.should_inject is True
    assert hard.can_compact is False
