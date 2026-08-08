"""Composition component for graph context compaction."""

from __future__ import annotations

StreamingRenderer = None

from voidx.agent.adapters.langgraph.ui_events import StatusFinished, StatusUpdated
from voidx.agent.ports.ui import NullAgentUiPort

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from voidx.agent.domain.compaction import CompactionResult, PreflightCompactionResult
from voidx.agent.adapters.langgraph.runtime.streaming import extract_text, stream_llm
from voidx.agent.adapters.persistence.message_rows import messages_from_rows
from voidx.agent.application.runtime_context import raw_semantic_messages
from voidx.llm.compaction import (
    COMPACTION_MAX_RETRIES,
    COMPACTION_REQUEST,
    SUMMARY_TEMPLATE,
    CompactionService,
)
from voidx.llm.domain.provider import resolve_protocol
from voidx.observability.tool_log import log_tool_event
from voidx.llm.usage import estimate_context_tokens, estimate_message_tokens, extract_token_usage
from voidx.agent.adapters.persistence.context_frame_repository import save_context_frame_from_messages
from voidx.agent.application.automation.workflow.service import is_workflow_context_content

RunCompactionAgent = Callable[[list, str | None], Awaitable[str | None]]
PersistCompaction = Callable[[list], Awaitable[None]]
COMPACTION_REQUEST_HEADROOM = 2_000
IN_TURN_SUMMARY_PREFIX = "## Long Summary\n"




class CompactionCoordinator:
    """Coordinates context compaction for a graph host."""

    def __init__(self, host: Any) -> None:
        self.host = host

    async def maybe_compact(
        self,
        messages: list,
        session_msgs: list | None = None,
        *,
        force: bool = False,
        ask: bool = True,
        preflight: bool = False,
        run_compaction_agent: RunCompactionAgent | None = None,
        persist_compaction: PersistCompaction | None = None,
    ) -> tuple[list | None, str | None]:
        """Check overflow and compact if needed.

        Returns the messages removed from the live context and the persisted
        tail anchor id when compaction removes an older complete turn.
        """
        result = await self.compact_for_live_state(
            messages,
            session_msgs,
            force=force,
            ask=ask,
            preflight=preflight,
            run_compaction_agent=run_compaction_agent,
            persist_compaction=persist_compaction,
        )
        if result is None:
            return None, None

        messages.clear()
        messages.extend(result.live_messages)
        return result.removed_messages, result.tail_id

    async def preflight_compact_if_needed(
        self,
        messages: list[BaseMessage],
        session_msgs: list | None = None,
        *,
        force: bool = False,
        reason: str = "threshold",
        ask: bool = False,
        run_compaction_agent: RunCompactionAgent | None = None,
        persist_compaction: PersistCompaction | None = None,
    ) -> tuple[CompactionResult | None, PreflightCompactionResult]:
        result = await self.compact_for_live_state(
            messages,
            session_msgs,
            force=force,
            ask=ask,
            preflight=True,
            include_summary_message=False,
            run_compaction_agent=run_compaction_agent,
            persist_compaction=persist_compaction,
        )
        preflight_result = PreflightCompactionResult.from_compaction_result(result)
        if preflight_result.compacted and reason and preflight_result.reason in {"", "threshold", "force"}:
            preflight_result.reason = reason
            if result is not None:
                result.metadata["compaction_reason"] = reason
        return result, preflight_result

    async def compact_for_live_state(
        self,
        messages: list[BaseMessage],
        session_msgs: list | None = None,
        *,
        force: bool = False,
        ask: bool = True,
        preflight: bool = False,
        include_summary_message: bool = False,
        run_compaction_agent: RunCompactionAgent | None = None,
        persist_compaction: PersistCompaction | None = None,
    ) -> CompactionResult | None:
        """Compact without mutating the caller's message list."""
        host = self.host
        run_agent = run_compaction_agent or self.run_compaction_agent
        persist = persist_compaction or self.persist_compaction
        total_tokens = estimate_context_tokens(messages, host.config.model.model)
        tokens = {"total": total_tokens, "input": total_tokens, "output": 0, "reasoning": 0}

        over_hard = host._compaction.is_overflow(tokens)
        over_soft = preflight and host._compaction.is_soft_overflow(tokens)
        if not force and not over_hard and not over_soft:
            return None

        if not force and ask and getattr(host.config, "ask_compact", False):
            should_compact = await self.ask_compact(total_tokens)
            if not should_compact:
                if host._ui.via_events():
                    await host._ui.events.emit(StatusFinished(
                        status_id="compaction",
                        label="Compaction skipped",
                        remove=True,
                    ))
                else:
                    host._ui.ui.print("[dim]Compaction skipped[/dim]")
                return None

        if host._ui.via_events():
            await host._ui.events.emit(StatusUpdated(
                status_id="compaction",
                label="Compacting",
                detail=_compaction_status_detail(total_tokens, force=force, preflight=preflight),
                stage="compacting",
                display="record_only",
            ))
        else:
            host._ui.ui.print(
                "[yellow]Context overflow — compacting...[/yellow]"
                if not force
                else "[yellow]Compacting...[/yellow]"
            )

        runtime_prefix = _runtime_prefix(messages)
        semantic_messages = raw_semantic_messages(messages)
        if preflight:
            selection = host._compaction.select_preflight_details(
                semantic_messages,
                model=host.config.model.model,
            )
            if not selection.should_compact and (force or over_hard):
                selection = host._compaction.select_details(semantic_messages)
        else:
            selection = host._compaction.select_details(semantic_messages)
        head_msgs, tail_id = selection.head, selection.tail_id
        semantic_tail = semantic_messages[selection.keep_from:]

        if not selection.should_compact:
            if host._ui.via_events():
                await host._ui.events.emit(StatusFinished(
                    status_id="compaction",
                    label="Compaction skipped: no older complete turn to summarize",
                    remove=True,
                ))
            return None

        base_metadata = _compaction_metadata(
            host,
            semantic_messages=semantic_messages,
            semantic_tail=semantic_tail,
            total_tokens=total_tokens,
            force=force,
            preflight=preflight,
            over_soft=over_soft,
            over_hard=over_hard,
            removed_message_count=len(head_msgs),
            tail_id=tail_id,
        )
        summary = None
        previous_summary = getattr(host, "_compaction_summary", "") or None
        last_error: Exception | None = None
        returned_no_summary = False

        self._active_compaction_source_messages = list(messages)
        for attempt in range(1, COMPACTION_MAX_RETRIES + 2):
            try:
                if host._ui.via_events():
                    retry_label = f" (attempt {attempt})" if attempt > 1 else ""
                    await host._ui.events.emit(StatusUpdated(
                        status_id="compaction",
                        label="Compacting",
                        detail=f"summarizing {len(head_msgs)} old messages{retry_label}",
                        stage="compacting",
                        display="record_only",
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
                            display="record_only",
                        ))
                    else:
                        host._ui.ui.print(f"[dim]Compaction agent failed ({e}) — retrying ({attempt}/{COMPACTION_MAX_RETRIES})[/dim]")
        self._active_compaction_source_messages = None

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
                    display="record_only",
                ))
            else:
                err_msg = f" ({failure_detail})"
                host._ui.ui.print(f"[dim]Compaction agent failed{err_msg} — using extracted summary[/dim]")
            fallback = CompactionService.fallback_summary(head_msgs)
            host._pending_summary = fallback
            host._compaction_summary = fallback
            host._compaction.compaction_count += 1
            await persist(head_msgs)
            live_messages = _live_messages(
                runtime_prefix,
                semantic_tail,
                fallback,
                include_summary_message=include_summary_message,
            )
            metadata = _finish_compaction_metadata(
                base_metadata,
                live_messages=live_messages,
                model=host.config.model.model,
                fallback=True,
            )
            if host._ui.via_events():
                await host._ui.events.emit(StatusFinished(
                    status_id="compaction",
                    label=f"Compaction fallback summarized {len(head_msgs)} messages",
                    detail=f"{failure_detail}; using extracted summary",
                    ok=False,
                    remove=True,
                ))
            return CompactionResult(
                summary=fallback,
                removed_messages=list(head_msgs),
                live_messages=live_messages,
                tail_id=tail_id,
                fallback=True,
                metadata=metadata,
            )

        if summary:
            host._pending_summary = summary
            host._compaction_summary = summary
            host._compaction.compaction_count += 1
            await persist(head_msgs)
            if host._ui.via_events():
                await host._ui.events.emit(StatusFinished(
                    status_id="compaction",
                    label=f"Compacted {len(head_msgs)} messages into summary",
                    remove=True,
                ))
            else:
                host._ui.ui.print(f"[dim]Compacted: {len(head_msgs)} messages → summary[/dim]")
        elif host._ui.via_events():
            await host._ui.events.emit(StatusFinished(
                status_id="compaction",
                label="Compaction produced no summary",
                ok=False,
                remove=True,
            ))
            return None
        else:
            return None

        live_messages = _live_messages(
            runtime_prefix,
            semantic_tail,
            summary,
            include_summary_message=include_summary_message,
        )
        metadata = _finish_compaction_metadata(
            base_metadata,
            live_messages=live_messages,
            model=host.config.model.model,
            fallback=False,
        )
        return CompactionResult(
            summary=summary,
            removed_messages=list(head_msgs),
            live_messages=live_messages,
            tail_id=tail_id,
            fallback=False,
            metadata=metadata,
        )

    async def ask_compact(self, total_tokens: int) -> bool:
        host = self.host
        choices = [
            ("Compact", "compact", "Summarize older context and continue"),
            ("Skip once", "skip", "Continue without compacting this turn"),
        ]
        choice = await host._ui.ask_choice("Compact context?", choices)
        if choice is not None:
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
        from voidx.agent.adapters.persistence.session_repository import delete_messages_through

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
            from voidx.agent.adapters.persistence.session_repository import load_messages
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
        """Run the compaction behavior to generate a structured summary."""
        host = self.host
        if host.model is None:
            return None

        ui_factories = host._ui if hasattr(host._ui, "streaming_renderer") else NullAgentUiPort()
        renderer_factory = StreamingRenderer or ui_factories.streaming_renderer
        renderer = renderer_factory(
            host._ui.console,
            debug=host._debug,
            stream_to_dock=False,
            headless=True,
        )

        request_message = HumanMessage(content=_compaction_request_text(previous_summary))
        source_messages = (
            getattr(self, "_active_compaction_source_messages", None)
            or getattr(host, "_current_messages", None)
            or []
        )
        pruned_chars = 0
        input_mode = "fallback"
        messages: list[BaseMessage]

        if source_messages:
            candidate = list(source_messages)
            pruned_chars = host._compaction.prune(candidate)
            candidate_messages = [*candidate, request_message]
            candidate_tokens = estimate_context_tokens(candidate_messages, host.config.model.model)
            if candidate_tokens <= host._compaction.context_limit:
                messages = candidate_messages
                context_tokens = candidate_tokens
                input_mode = "main_context"
            else:
                messages = [*head_messages, request_message]
                context_tokens = estimate_context_tokens(messages, host.config.model.model)
        else:
            messages = [*head_messages, request_message]
            context_tokens = estimate_context_tokens(messages, host.config.model.model)

        if context_tokens > host._compaction.context_limit:
            budget = max(
                0,
                host._compaction.context_limit
                - host._compaction.output_token_max
                - COMPACTION_REQUEST_HEADROOM,
            )
            head_messages = host._compaction.truncate_head_to_budget(
                head_messages,
                budget=budget,
                model=host.config.model.model,
            )
            if not head_messages:
                raise ValueError("compaction input exceeds context budget")
            messages = [*head_messages, request_message]
            context_tokens = estimate_context_tokens(messages, host.config.model.model)
            if context_tokens > host._compaction.context_limit:
                raise ValueError("compaction input exceeds context budget")
            input_mode = "fallback"

        host._usage_stats.update_context(context_tokens)
        if host._session is not None:
            await save_context_frame_from_messages(
                session_id=host._session.id,
                frame_kind="compaction",
                agent_persona="compaction-behavior",
                provider=host.config.model.provider,
                model=host.config.model.model,
                messages=messages,
                token_estimate=context_tokens,
                metadata={
                    "head_message_count": len(head_messages),
                    "has_previous_summary": previous_summary is not None,
                    "input_mode": input_mode,
                    "source_message_count": len(source_messages),
                    "pruned_chars": pruned_chars,
                },
            )
        assistant_msg = await stream_llm(host.model, messages, renderer, resolve_protocol(host.config.model), ui_port=host._ui)
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
        log_tool_event(
            "compaction_empty_result",
            message=f"Compaction agent returned empty text: message_type={type(assistant_msg).__name__} content_type={_content_type_summary(getattr(assistant_msg, 'content', None))}",
        )
        return None


def _content_type_summary(content: object) -> str:
    if isinstance(content, list):
        return ",".join(type(item).__name__ for item in content) or "list(empty)"
    return type(content).__name__


def _compaction_status_detail(total_tokens: int, *, force: bool, preflight: bool) -> str:
    if force:
        return "manual compaction"
    if preflight:
        return f"{total_tokens} tokens reached the preflight compaction threshold"
    return f"{total_tokens} tokens exceed the active context budget"


def _compaction_metadata(
    host: Any,
    *,
    semantic_messages: list[BaseMessage],
    semantic_tail: list[BaseMessage],
    total_tokens: int,
    force: bool,
    preflight: bool,
    over_soft: bool,
    over_hard: bool,
    removed_message_count: int,
    tail_id: str | None,
) -> dict[str, object]:
    return {
        "compaction_reason": _compaction_reason(
            force=force,
            preflight=preflight,
            over_soft=over_soft,
            over_hard=over_hard,
        ),
        "pre_tokens": total_tokens,
        "soft_threshold": host._compaction.soft_threshold(),
        "hard_threshold": int(host._compaction.context_limit * 0.90),
        "post_compaction_target": host._compaction.post_compaction_target(),
        "removed_message_count": removed_message_count,
        "retained_turn_count": len(host._compaction._turns(semantic_tail)),
        "current_user_preserved": _latest_user_preserved(semantic_messages, semantic_tail),
        "tail_anchor_id": tail_id or "",
        "inline_compaction_enabled": bool(getattr(host.config, "inline_compaction_enabled", False)),
    }


def _finish_compaction_metadata(
    metadata: dict[str, object],
    *,
    live_messages: list[BaseMessage],
    model: str,
    fallback: bool,
) -> dict[str, object]:
    return {
        **metadata,
        "post_tokens": estimate_context_tokens(live_messages, model),
        "fallback": fallback,
    }


def _compaction_reason(*, force: bool, preflight: bool, over_soft: bool, over_hard: bool) -> str:
    if force:
        return "force"
    if over_hard:
        return "hard_threshold"
    if preflight and over_soft:
        return "soft_threshold"
    return "threshold"


def _latest_user_preserved(
    semantic_messages: list[BaseMessage],
    semantic_tail: list[BaseMessage],
) -> bool:
    latest_user = next(
        (message for message in reversed(semantic_messages) if isinstance(message, HumanMessage)),
        None,
    )
    return latest_user is None or any(message is latest_user for message in semantic_tail)


def _compaction_request_text(previous_summary: str | None) -> str:
    previous_summary_section = ""
    if previous_summary:
        previous_summary_section = (
            "Below is the previous anchored summary of earlier conversation. "
            "Preserve still-true details, remove stale details, and merge in new facts.\n\n"
            f"<previous-summary>\n{previous_summary}\n</previous-summary>"
        )
    return COMPACTION_REQUEST.format(
        previous_summary_section=previous_summary_section,
        template=SUMMARY_TEMPLATE,
    ).strip()


def _runtime_prefix(messages: list[BaseMessage]) -> list[BaseMessage]:
    prefix: list[BaseMessage] = []
    for message in messages:
        if isinstance(message, SystemMessage):
            if isinstance(message.content, str) and message.content.startswith(IN_TURN_SUMMARY_PREFIX):
                continue
            prefix.append(message)
            continue
        # Back-compat only: sessions created before workflow runtime moved into
        # the stable SystemMessage may still have persisted workflow prefixes.
        if isinstance(message, HumanMessage) and is_workflow_context_content(message.content):
            prefix.append(message)
            continue
        break
    return prefix


def _live_messages(
    runtime_prefix: list[BaseMessage],
    semantic_tail: list[BaseMessage],
    summary: str,
    *,
    include_summary_message: bool,
) -> list[BaseMessage]:
    if not include_summary_message:
        return [*runtime_prefix, *semantic_tail]
    summary_message = SystemMessage(content=f"{IN_TURN_SUMMARY_PREFIX}{summary}")
    return [*runtime_prefix, summary_message, *semantic_tail]


def _max_persisted_message_id(messages: list) -> int | None:
    ids: list[int] = []
    for message in messages:
        raw = getattr(message, "id", None)
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    return max(ids) if ids else None
