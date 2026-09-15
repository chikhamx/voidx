"""Bounded display ownership; decisions belong exclusively to the injected port."""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from voidx.presentation.protocol import UiRequest
from voidx.tooling.domain.interaction import InteractionRequest, InteractionResponse


@dataclass
class _Display:
    interaction: InteractionRequest
    request: UiRequest
    submitted: bool = False


class SemanticInteractionRouter:
    def __init__(self, submit: Callable[[str, InteractionResponse], Awaitable[bool]]) -> None:
        self._submit = submit
        self._pending: dict[str, _Display] = {}
        self._closed = False

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def register(self, interaction: InteractionRequest, request: UiRequest) -> None:
        if self._closed:
            raise RuntimeError("Interaction route is closed")
        if len(self._pending) >= 64:
            raise ValueError("At most 64 pending display requests")
        if interaction.interaction_id in self._pending:
            raise ValueError("Duplicate display request")
        if not ((interaction.purpose == "permission" and interaction.input_kind == "permission")
                or (interaction.purpose == "clarify" and interaction.input_kind in {"choice", "text"})
                or (interaction.purpose in {"goal", "loop"} and interaction.input_kind == "choice"
                    and getattr(interaction, interaction.purpose) is not None)
                or (interaction.purpose == "checkpoint" and interaction.checkpoint is not None
                    and ((interaction.checkpoint_stage == "decision" and interaction.input_kind == "choice"
                          and interaction.checkpoint_id == interaction.interaction_id)
                         or (interaction.checkpoint_stage == "scope" and interaction.input_kind == "text"
                             and interaction.checkpoint_id != interaction.interaction_id)))):
            raise ValueError("Only permission, clarify and associated checkpoint interactions are supported by this route")
        if request.request_id != interaction.interaction_id or request.thread_id != interaction.thread_id:
            raise ValueError("Display ownership mismatch")
        self._pending[interaction.interaction_id] = _Display(
            interaction.model_copy(deep=True), request.model_copy(deep=True))

    def requests(self) -> tuple[UiRequest, ...]:
        return tuple(d.request.model_copy(deep=True) for d in self._pending.values() if not d.submitted)

    async def respond(self, request_id: str, value: str | None, *, thread_id: str) -> bool:
        display = self._pending.get(request_id)
        if self._closed or display is None or display.submitted:
            return False
        r = display.interaction
        if thread_id != r.thread_id:
            return False
        # Values are the runtime's authoritative options, not scope encodings.
        values = [c.value for c in r.choices]
        if value is not None and not isinstance(value, str):
            return False
        free_text = value is not None and value not in values and r.purpose in {"clarify", "checkpoint"}
        if len(values) != len(set(values)) or (value is not None and value not in values
                and not (free_text and (r.input_kind == "text" or r.allow_free_text))):
            return False
        display.submitted = True
        accepted = await self._submit(request_id, InteractionResponse(
            session_id=r.session_id, thread_id=r.thread_id, turn_id=r.turn_id,
            value=value or "", cancelled=value is None, free_text=free_text,
        ))
        return accepted

    def resolve(self, request_id: str, *, session_id: str, thread_id: str, turn_id: str) -> None:
        # Closed display ownership is gone; the coordinator still emits its cleanup tail.
        if self._closed:
            return
        display = self._pending.get(request_id)
        if display is None or any(getattr(display.interaction, k) != v for k, v in
            dict(session_id=session_id, thread_id=thread_id, turn_id=turn_id).items()):
            raise ValueError("Resolved display ownership mismatch")
        del self._pending[request_id]

    def close(self) -> None:
        self._closed = True
        self._pending.clear()
