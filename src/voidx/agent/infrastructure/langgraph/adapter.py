"""Turn engine backed by LangGraph execution."""

from __future__ import annotations

from collections import Counter
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from voidx.agent.ports.execution_host import ExecutionHost

from voidx.agent.domain.state import SessionRuntimeState
from voidx.agent.domain.turn import TurnPhase
from voidx.agent.infrastructure.langgraph.state_mapper import LangGraphStateMapper


class LangGraphTurnEngine:
    def __init__(
        self,
        execution: ExecutionHost,
        *,
        mapper: LangGraphStateMapper | None = None,
    ) -> None:
        self._execution = execution
        self._mapper = mapper or LangGraphStateMapper()
        self.last_evidence: dict[str, Any] = {}

    @property
    def session_id(self) -> str:
        return getattr(self._execution, "session_id", "") or ""

    async def run(
        self,
        user_text: str,
        runtime: SessionRuntimeState,
        *,
        display_text: str | None = None,
        context: Any | None = None,
        persist_user_input: bool = True,
    ) -> SessionRuntimeState:
        self._mapper.apply_runtime(self._execution, runtime)
        await self._execution.run_turn(
            user_text,
            display_text=display_text,
            context=context,
            persist_user_input=persist_user_input,
        )
        self.last_evidence = _evidence_from_execution(self._execution)
        # Return the post-execution state still in RUNNING phase; the runtime
        # facade owns the COMMITTED transition via advance_turn.
        return self._mapper.runtime_from_execution(
            self._execution,
            turn_phase=TurnPhase.RUNNING,
        )


def _evidence_from_execution(execution: ExecutionHost) -> dict[str, Any]:
    messages = tuple(getattr(execution, "_current_messages", None) or ())
    assistant_messages = tuple(message for message in messages if isinstance(message, AIMessage))
    tool_messages = tuple(message for message in messages if isinstance(message, ToolMessage))
    tool_summaries = _summarize_tool_messages(tool_messages)
    final_assistant_summary = ""
    if assistant_messages:
        final_assistant_summary = str(assistant_messages[-1].content or "")[:4000]
    pending_summary = getattr(execution, "_pending_summary", None)
    if pending_summary:
        final_assistant_summary = str(pending_summary)[:4000]
    return {
        "final_llm_messages": assistant_messages[-4:],
        "final_assistant_summary": final_assistant_summary,
        "tool_result_summaries": tool_summaries,
        "stop_signal": "",
    }


def _summarize_tool_messages(messages: tuple[ToolMessage, ...]) -> tuple[str, ...]:
    if not messages:
        return ()
    names = Counter(_tool_message_name(message) for message in messages)
    ok_true_count = sum(_tool_message_has_ok_true(message) for message in messages)
    summary = [
        f"Observed tool result total: {len(messages)}",
        "Observed tool result names: "
        + ", ".join(f"{name}={count}" for name, count in sorted(names.items())),
        f"Observed tool result success markers: ok_true={ok_true_count}",
    ]
    if len(messages) <= 40:
        return (*summary, *(_summarize_tool_message(message) for message in messages))
    first = tuple(_summarize_tool_message(message) for message in messages[:20])
    last = tuple(_summarize_tool_message(message) for message in messages[-20:])
    omitted = len(messages) - len(first) - len(last)
    return (*summary, f"omitted_middle_tool_results={omitted}", *first, *last)


def _summarize_tool_message(message: ToolMessage) -> str:
    name = _tool_message_name(message)
    content = str(message.content or "")
    return f"{name}: {content[:500]}"


def _tool_message_name(message: ToolMessage) -> str:
    return str(getattr(message, "name", None) or getattr(message, "tool_call_id", "tool"))


def _tool_message_has_ok_true(message: ToolMessage) -> bool:
    content = str(message.content or "").lower()
    return '"ok": true' in content or "'ok': true" in content or "ok: true" in content
