"""Context compaction methods for the agent graph."""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.messages import SystemMessage

from voidx.agent.message_rows import messages_from_rows
from voidx.agent.graph.runtime import console, ui
from voidx.agent.graph.streaming import extract_text, stream_llm
from voidx.llm.compaction import COMPACTION_MAX_RETRIES, CompactionService
from voidx.llm.provider import resolve_protocol
from voidx.llm.usage import estimate_context_tokens, estimate_message_tokens, extract_token_usage
from voidx.memory.context_frames import save_context_frame_from_messages
from voidx.runtime.ui import (
    StatusFinished,
    StatusUpdated,
    StreamingRenderer,
    dock,
    ui_events,
    via_events,
)

if TYPE_CHECKING:
    from voidx.agent.graph.contracts import GraphCompactionHost


class GraphCompactionMixin:
    async def _maybe_compact(
        self: GraphCompactionHost,
        messages: list,
        session_msgs: list | None = None,
        *,
        force: bool = False,
        ask: bool = True,
    ) -> tuple[list | None, str | None]:
        """Check overflow and compact if needed.

        Returns the messages removed from the live context and the persisted
        tail anchor id when compaction removes an older complete turn.
        """
        total_tokens = estimate_context_tokens(messages, self.config.model.model)
        tokens = {"total": total_tokens, "input": total_tokens, "output": 0, "reasoning": 0}

        if not force and not self._compaction.is_overflow(tokens):
            return None, None

        if not force and ask and getattr(self.config, "ask_compact", False):
            should_compact = await self._ask_compact(total_tokens)
            if not should_compact:
                if via_events():
                    await ui_events.emit(StatusFinished(
                        status_id="compaction",
                        label="Compaction skipped",
                        remove=False,
                    ))
                else:
                    ui.print("[dim]Compaction skipped[/dim]")
                return None, None

        if via_events():
            await ui_events.emit(StatusUpdated(
                status_id="compaction",
                label="Compacting context",
                detail=(
                    f"{total_tokens} tokens exceed the active context budget"
                    if not force
                    else f"manual compaction of {len(messages)} messages"
                ),
                stage="compacting",
            ))
        else:
            ui.print(
                "[yellow]Context overflow — compacting...[/yellow]"
                if not force
                else "[yellow]Compacting context...[/yellow]"
            )

        selection = self._compaction.select_details(messages)
        head_msgs, tail_id = selection.head, selection.tail_id

        if not selection.should_compact:
            if via_events():
                await ui_events.emit(StatusFinished(
                    status_id="compaction",
                    label="Compaction skipped: no older complete turn to summarize",
                    remove=False,
                ))
            return None, None

        # Run compaction agent with retries
        summary = None
        previous_summary = getattr(self, "_compaction_summary", "") or None
        last_error: Exception | None = None

        for attempt in range(1, COMPACTION_MAX_RETRIES + 2):  # 1 initial + N retries
            try:
                if via_events():
                    retry_label = f" (attempt {attempt})" if attempt > 1 else ""
                    await ui_events.emit(StatusUpdated(
                        status_id="compaction",
                        label="Compacting context",
                        detail=f"summarizing {len(head_msgs)} old messages{retry_label}",
                        stage="compacting",
                    ))
                summary = await self._run_compaction_agent(head_msgs, previous_summary)
                if summary:
                    break
            except Exception as e:
                last_error = e
                if attempt <= COMPACTION_MAX_RETRIES:
                    if via_events():
                        await ui_events.emit(StatusUpdated(
                            status_id="compaction",
                            label="Compaction agent failed",
                            detail=f"{e}; retrying ({attempt}/{COMPACTION_MAX_RETRIES})",
                            stage="compacting",
                        ))
                    else:
                        ui.print(f"[dim]Compaction agent failed ({e}) — retrying ({attempt}/{COMPACTION_MAX_RETRIES})[/dim]")

        if not summary:
            # All retries exhausted — use an extracted summary, but keep the selected tail.
            if via_events():
                err_detail = f"{last_error}; " if last_error else ""
                await ui_events.emit(StatusUpdated(
                    status_id="compaction",
                    label="Compaction agent failed",
                    detail=f"{err_detail}using extracted summary",
                    stage="compacting",
                ))
            else:
                err_msg = f" ({last_error})" if last_error else ""
                ui.print(f"[dim]Compaction agent failed{err_msg} — using extracted summary[/dim]")
            fallback = CompactionService.fallback_summary(head_msgs)
            self._pending_summary = fallback
            self._compaction_summary = fallback
            self._compaction.compaction_count += 1
            tail_msgs = messages[selection.keep_from:]
            messages.clear()
            messages.extend(tail_msgs)
            await self._persist_compaction(head_msgs)
            if via_events():
                await ui_events.emit(StatusFinished(
                    status_id="compaction",
                    label=f"Compaction fallback summarized {len(head_msgs)} messages",
                    ok=False,
                    remove=False,
                ))
            return head_msgs, tail_id

        if summary:
            tail_msgs = messages[selection.keep_from:]
            messages.clear()
            messages.extend(tail_msgs)
            self._pending_summary = summary
            self._compaction_summary = summary
            self._compaction.compaction_count += 1
            await self._persist_compaction(head_msgs)
            if via_events():
                await ui_events.emit(StatusFinished(
                    status_id="compaction",
                    label=f"Compacted {len(head_msgs)} messages into summary",
                    remove=False,
                ))
            else:
                ui.print(f"[dim]Compacted: {len(head_msgs)} messages → summary[/dim]")
        elif via_events():
            await ui_events.emit(StatusFinished(
                status_id="compaction",
                label="Compaction produced no summary",
                ok=False,
                remove=False,
            ))
            return None, None
        else:
            return None, None

        return head_msgs, tail_id

    async def _ask_compact(self: GraphCompactionHost, total_tokens: int) -> bool:
        choices = [
            ("Compact", "compact", "Summarize older context and continue"),
            ("Skip once", "skip", "Continue without compacting this turn"),
        ]
        app = getattr(self, "_app", None)
        if app:
            choice = await app.ask_choice("Compact context?", choices)
            return choice == "compact"
        ui.print("")
        ui.print(f"  [yellow]Context is large ({total_tokens} tokens); compacting automatically.[/yellow]")
        return True

    async def _persist_compaction(self: GraphCompactionHost, head_messages: list) -> None:
        if getattr(self, "_session", None) is None:
            return
        if hasattr(self, "_persist_runtime_state"):
            await self._persist_runtime_state()
        last_message_id = _max_persisted_message_id(head_messages)
        if last_message_id is None:
            return
        from voidx.memory.session import delete_messages_through

        await delete_messages_through(self._session.id, last_message_id)

        # Sync in-memory cache: drop compacted rows
        cache = getattr(self, "_session_msg_cache", None)
        if cache is not None:
            self._session_msg_cache = [r for r in cache if r.id is not None and r.id > last_message_id]

    async def _compact_session_history(self: GraphCompactionHost, *, force: bool = True) -> bool:
        if getattr(self, "_session", None) is None:
            ui.print("[dim]No active session to compact.[/dim]")
            return False

        cache = getattr(self, "_session_msg_cache", None)
        if cache is not None:
            rows = list(cache)
        else:
            from voidx.memory.session import load_messages
            rows = await load_messages(self._session.id)

        messages = messages_from_rows(rows)

        head, _tail_id = await self._maybe_compact(messages, rows, force=force, ask=False)
        return bool(head)

    async def _run_compaction_agent(
        self: GraphCompactionHost,
        head_messages: list,
        previous_summary: str | None,
    ) -> str | None:
        """Run the compaction agent to generate a structured summary."""
        from voidx.agent.agents import COMPACTION_PROMPT

        if self.model is None:
            return None

        prompt = self._compaction.build_prompt(head_messages, previous_summary)
        renderer = StreamingRenderer(console, debug=self._debug, stream_to_dock=False)

        messages = [SystemMessage(content=COMPACTION_PROMPT)]
        messages.append(HumanMessage(content=prompt))

        # Use a cheap/fast call for compaction — no tools
        context_tokens = estimate_context_tokens(messages, self.config.model.model)
        self._usage_stats.update_context(context_tokens)
        if self._session is not None:
            await save_context_frame_from_messages(
                session_id=self._session.id,
                frame_kind="compaction",
                agent_role="compaction",
                provider=self.config.model.provider,
                model=self.config.model.model,
                messages=messages,
                token_estimate=context_tokens,
                metadata={
                    "head_message_count": len(head_messages),
                    "has_previous_summary": previous_summary is not None,
                },
            )
        assistant_msg = await stream_llm(self.model, messages, renderer, resolve_protocol(self.config.model))
        self._usage_stats.record_call(
            extract_token_usage(assistant_msg),
            fallback_input_tokens=context_tokens,
            fallback_output_tokens=estimate_message_tokens(assistant_msg, self.config.model.model),
            messages=messages,
            model=self.config.model.model,
            cache_key=f"{self.config.model.provider}/{self.config.model.model}",
        )
        text = extract_text(assistant_msg)
        return text if text else None


def _max_persisted_message_id(messages: list) -> int | None:
    ids: list[int] = []
    for message in messages:
        raw = getattr(message, "id", None)
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    return max(ids) if ids else None
