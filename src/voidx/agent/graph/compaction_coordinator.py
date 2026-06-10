"""Composition component for graph context compaction."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from langchain_core.messages import HumanMessage, SystemMessage

from voidx.agent.graph.streaming import extract_text, stream_llm
from voidx.agent.message_rows import messages_from_rows
from voidx.llm.compaction import COMPACTION_MAX_RETRIES, CompactionService
from voidx.llm.provider import resolve_protocol
from voidx.llm.usage import estimate_context_tokens, estimate_message_tokens, extract_token_usage
from voidx.memory.context_frames import save_context_frame_from_messages
from voidx.ui.output.console import StreamingRenderer
from voidx.ui.output.events.schema import StatusFinished, StatusUpdated

if TYPE_CHECKING:
    from voidx.agent.graph.contracts import GraphCompactionHost


logger = logging.getLogger(__name__)

RunCompactionAgent = Callable[[list, str | None], Awaitable[str | None]]
PersistCompaction = Callable[[list], Awaitable[None]]


class GraphCompactionCoordinator:
    """Coordinates context compaction for a graph host."""

    def __init__(self, host: GraphCompactionHost) -> None:
        self.host = host

    async def maybe_compact(
        self,
        messages: list,
        session_msgs: list | None = None,
        *,
        force: bool = False,
        ask: bool = True,
        run_compaction_agent: RunCompactionAgent | None = None,
        persist_compaction: PersistCompaction | None = None,
    ) -> tuple[list | None, str | None]:
        """Check overflow and compact if needed.

        Returns the messages removed from the live context and the persisted
        tail anchor id when compaction removes an older complete turn.
        """
        host = self.host
        run_agent = run_compaction_agent or self.run_compaction_agent
        persist = persist_compaction or self.persist_compaction
        total_tokens = estimate_context_tokens(messages, host.config.model.model)
        tokens = {"total": total_tokens, "input": total_tokens, "output": 0, "reasoning": 0}

        if not force and not host._compaction.is_overflow(tokens):
            return None, None

        if not force and ask and getattr(host.config, "ask_compact", False):
            should_compact = await self.ask_compact(total_tokens)
            if not should_compact:
                if host._ui.via_events():
                    await host._ui.events.emit(StatusFinished(
                        status_id="compaction",
                        label="Compaction skipped",
                        remove=False,
                    ))
                else:
                    host._ui.ui.print("[dim]Compaction skipped[/dim]")
                return None, None

        if host._ui.via_events():
            await host._ui.events.emit(StatusUpdated(
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
            host._ui.ui.print(
                "[yellow]Context overflow — compacting...[/yellow]"
                if not force
                else "[yellow]Compacting context...[/yellow]"
            )

        selection = host._compaction.select_details(messages)
        head_msgs, tail_id = selection.head, selection.tail_id

        if not selection.should_compact:
            if host._ui.via_events():
                await host._ui.events.emit(StatusFinished(
                    status_id="compaction",
                    label="Compaction skipped: no older complete turn to summarize",
                    remove=False,
                ))
            return None, None

        summary = None
        previous_summary = getattr(host, "_compaction_summary", "") or None
        last_error: Exception | None = None
        returned_no_summary = False

        for attempt in range(1, COMPACTION_MAX_RETRIES + 2):
            try:
                if host._ui.via_events():
                    retry_label = f" (attempt {attempt})" if attempt > 1 else ""
                    await host._ui.events.emit(StatusUpdated(
                        status_id="compaction",
                        label="Compacting context",
                        detail=f"summarizing {len(head_msgs)} old messages{retry_label}",
                        stage="compacting",
                    ))
                summary = await run_agent(head_msgs, previous_summary)
                if summary:
                    break
                returned_no_summary = True
                last_error = None
            except Exception as e:
                last_error = e
                returned_no_summary = False
                if attempt <= COMPACTION_MAX_RETRIES:
                    if host._ui.via_events():
                        await host._ui.events.emit(StatusUpdated(
                            status_id="compaction",
                            label="Compaction agent failed",
                            detail=f"{e}; retrying ({attempt}/{COMPACTION_MAX_RETRIES})",
                            stage="compacting",
                        ))
                    else:
                        host._ui.ui.print(f"[dim]Compaction agent failed ({e}) — retrying ({attempt}/{COMPACTION_MAX_RETRIES})[/dim]")

        if not summary:
            if last_error:
                failure_detail = f"{type(last_error).__name__}: {last_error}"
            elif returned_no_summary:
                failure_detail = "compaction agent returned no summary"
            else:
                failure_detail = "compaction agent did not produce a summary"
            if host._ui.via_events():
                await host._ui.events.emit(StatusUpdated(
                    status_id="compaction",
                    label="Compaction agent failed",
                    detail=f"{failure_detail}; using extracted summary",
                    stage="compacting",
                ))
            else:
                err_msg = f" ({failure_detail})"
                host._ui.ui.print(f"[dim]Compaction agent failed{err_msg} — using extracted summary[/dim]")
            fallback = CompactionService.fallback_summary(head_msgs)
            host._pending_summary = fallback
            host._compaction_summary = fallback
            host._compaction.compaction_count += 1
            tail_msgs = messages[selection.keep_from:]
            messages.clear()
            messages.extend(tail_msgs)
            await persist(head_msgs)
            if host._ui.via_events():
                await host._ui.events.emit(StatusFinished(
                    status_id="compaction",
                    label=f"Compaction fallback summarized {len(head_msgs)} messages",
                    detail=f"{failure_detail}; using extracted summary",
                    ok=False,
                    remove=False,
                ))
            return head_msgs, tail_id

        if summary:
            tail_msgs = messages[selection.keep_from:]
            messages.clear()
            messages.extend(tail_msgs)
            host._pending_summary = summary
            host._compaction_summary = summary
            host._compaction.compaction_count += 1
            await persist(head_msgs)
            if host._ui.via_events():
                await host._ui.events.emit(StatusFinished(
                    status_id="compaction",
                    label=f"Compacted {len(head_msgs)} messages into summary",
                    remove=False,
                ))
            else:
                host._ui.ui.print(f"[dim]Compacted: {len(head_msgs)} messages → summary[/dim]")
        elif host._ui.via_events():
            await host._ui.events.emit(StatusFinished(
                status_id="compaction",
                label="Compaction produced no summary",
                ok=False,
                remove=False,
            ))
            return None, None
        else:
            return None, None

        return head_msgs, tail_id

    async def ask_compact(self, total_tokens: int) -> bool:
        host = self.host
        choices = [
            ("Compact", "compact", "Summarize older context and continue"),
            ("Skip once", "skip", "Continue without compacting this turn"),
        ]
        app = getattr(host, "_app", None)
        if app:
            choice = await app.ask_choice("Compact context?", choices)
            return choice == "compact"
        host._ui.ui.print("")
        host._ui.ui.print(f"  [yellow]Context is large ({total_tokens} tokens); compacting automatically.[/yellow]")
        return True

    async def persist_compaction(self, head_messages: list) -> None:
        host = self.host
        if getattr(host, "_session", None) is None:
            return
        if hasattr(host, "_persist_runtime_state"):
            await host._persist_runtime_state()
        last_message_id = _max_persisted_message_id(head_messages)
        if last_message_id is None:
            return
        from voidx.memory.session import delete_messages_through

        await delete_messages_through(host._session.id, last_message_id)

        cache = getattr(host, "_session_msg_cache", None)
        if cache is not None:
            host._session_msg_cache = [r for r in cache if r.id is not None and r.id > last_message_id]
        context_cache = getattr(host, "_context_cache", None)
        if context_cache is not None:
            context_cache.row_messages = {
                row_id: entry
                for row_id, entry in context_cache.row_messages.items()
                if row_id > last_message_id
            }

    async def compact_session_history(
        self,
        *,
        force: bool = True,
        run_compaction_agent: RunCompactionAgent | None = None,
        persist_compaction: PersistCompaction | None = None,
    ) -> bool:
        host = self.host
        if getattr(host, "_session", None) is None:
            host._ui.ui.print("[dim]No active session to compact.[/dim]")
            return False

        cache = getattr(host, "_session_msg_cache", None)
        if cache is not None:
            rows = list(cache)
        else:
            from voidx.memory.session import load_messages
            rows = await load_messages(host._session.id)

        messages = messages_from_rows(rows)
        head, _tail_id = await self.maybe_compact(
            messages,
            rows,
            force=force,
            ask=False,
            run_compaction_agent=run_compaction_agent,
            persist_compaction=persist_compaction,
        )
        return bool(head)

    async def run_compaction_agent(
        self,
        head_messages: list,
        previous_summary: str | None,
    ) -> str | None:
        """Run the compaction agent to generate a structured summary."""
        from voidx.agent.agents import COMPACTION_PROMPT

        host = self.host
        if host.model is None:
            return None

        prompt = host._compaction.build_prompt(head_messages, previous_summary)
        renderer = StreamingRenderer(
            host._ui.console,
            debug=host._debug,
            stream_to_dock=False,
            headless=True,
        )

        messages = [SystemMessage(content=COMPACTION_PROMPT)]
        messages.append(HumanMessage(content=prompt))

        context_tokens = estimate_context_tokens(messages, host.config.model.model)
        host._usage_stats.update_context(context_tokens)
        if host._session is not None:
            await save_context_frame_from_messages(
                session_id=host._session.id,
                frame_kind="compaction",
                agent_role="compaction",
                provider=host.config.model.provider,
                model=host.config.model.model,
                messages=messages,
                token_estimate=context_tokens,
                metadata={
                    "head_message_count": len(head_messages),
                    "has_previous_summary": previous_summary is not None,
                },
            )
        assistant_msg = await stream_llm(host.model, messages, renderer, resolve_protocol(host.config.model))
        host._usage_stats.record_call(
            extract_token_usage(assistant_msg),
            fallback_input_tokens=context_tokens,
            fallback_output_tokens=estimate_message_tokens(assistant_msg, host.config.model.model),
            messages=messages,
            model=host.config.model.model,
            cache_key=f"{host.config.model.provider}/{host.config.model.model}",
        )
        text = extract_text(assistant_msg)
        if text:
            return text
        logger.warning(
            "Compaction agent returned empty text: message_type=%s content_type=%s",
            type(assistant_msg).__name__,
            _content_type_summary(getattr(assistant_msg, "content", None)),
        )
        return None


def _content_type_summary(content: object) -> str:
    if isinstance(content, list):
        return ",".join(type(item).__name__ for item in content) or "list(empty)"
    return type(content).__name__


def _max_persisted_message_id(messages: list) -> int | None:
    ids: list[int] = []
    for message in messages:
        raw = getattr(message, "id", None)
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    return max(ids) if ids else None
