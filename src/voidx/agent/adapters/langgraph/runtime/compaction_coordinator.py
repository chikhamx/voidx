"""Composition component for graph context compaction."""

from __future__ import annotations

import asyncio

StreamingRenderer = None

from voidx.agent.domain.ui_events import StatusFinished, StatusUpdated
from voidx.agent.ports.ui import NullAgentUiPort

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

import uuid
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from voidx.agent.domain.compaction import (
    CompactionMessageMetadata,
    CompactionResult,
    ContextBudgetExhausted,
    PreflightCompactionResult,
)
from voidx.agent.adapters.persistence.message_rows import (
    compute_source_range_hash,
    is_compaction_row,
)
from voidx.agent.adapters.persistence.session_models import MessageRow
from voidx.agent.adapters.persistence.session_repository import (
    load_messages,
    replace_effective_message_range,
)
from voidx.llm.message_markers import (
    COMPACTION_MESSAGE_MARKER,
    create_compaction_user_message,
    create_continuation_message,
    is_compaction_message,
    is_continuation_message,
    is_context_pressure_message,
    is_step_hint_message,
)
from voidx.llm.compaction.service import (
    select_closed_tool_tail,
    validate_closed_tool_batches,
)
from voidx.llm.compaction.fallback_summary import fallback_summary
from voidx.agent.adapters.langgraph.runtime.prepared_request import (
    PreparedMainRequest,
    prepare_main_request,
)
from voidx.agent.adapters.langgraph.runtime.thread_context import (
    current_thread_execution_state,
    save_turn_message,
)
from voidx.agent.adapters.langgraph.runtime.streaming import extract_text, stream_llm
from voidx.agent.adapters.persistence.message_rows import messages_from_rows
from voidx.agent.application.runtime_context import raw_semantic_messages
from voidx.llm.compaction import (
    COMPACTION_MAX_RETRIES,
    COMPACTION_REQUEST,
    SUMMARY_TEMPLATE,
    CompactionService,
    compaction_summary_messages,
    fallback_summary_with_previous,
)
from voidx.llm.domain.model import ModelConfig, ReasoningEffort
from voidx.llm.domain.provider import resolve_protocol
from voidx.observability.tool_log import log_tool_event
from voidx.observability.request_log import log_llm_exchange
from voidx.llm.cache_key import bind_prompt_cache_key, prompt_cache_key_for
from voidx.llm.usage import estimate_context_tokens, estimate_message_tokens, extract_token_usage
from voidx.agent.adapters.persistence.context_frame_repository import gc_context_frames, save_context_frame_from_messages
from voidx.agent.application.automation.workflow.service import is_workflow_context_content

RunCompactionAgent = Callable[[list, str | None], Awaitable[str | None]]
PersistCompaction = Callable[[list], Awaitable[None]]
COMPACTION_REQUEST_HEADROOM = 2_000
IN_TURN_SUMMARY_PREFIX = "## Long Summary\n"


@dataclass(frozen=True)
class ResolvedCompactionModel:
    model: Any
    model_config: ModelConfig
    model_source: str
    profile_name: str
    reasoning_source: str
    effective_reasoning_effort: ReasoningEffort
    is_exact_main_instance: bool




@dataclass(frozen=True)
class _DefaultCompactionConfig:
    profile_name: str = ""
    reasoning_effort: ReasoningEffort | None = None
    timeout_seconds: float = 256.0


class CompactionCoordinator:
    """Coordinates context compaction for a graph host."""

    def __init__(self, host: Any) -> None:
        self.host = host



    def _compaction_config(self) -> Any:
        settings = getattr(self.host, "_settings", None)
        getter = getattr(settings, "get_compaction_config", None)
        if callable(getter):
            try:
                config = getter()
                if (
                    isinstance(getattr(config, "profile_name", None), str)
                    and hasattr(config, "reasoning_effort")
                    and isinstance(getattr(config, "timeout_seconds", None), (int, float))
                ):
                    return config
            except Exception as exc:
                log_tool_event("compaction_settings_failed", message=str(exc))
        return _DefaultCompactionConfig()
    async def resolve_compaction_models(self) -> list[ResolvedCompactionModel]:
        host = self.host
        main_config = host.config.model
        settings = getattr(host, "_settings", None)
        compaction_config = self._compaction_config()
        profile_name = compaction_config.profile_name
        reasoning_override = compaction_config.reasoning_effort
        effective_reasoning = reasoning_override or main_config.reasoning_effort
        reasoning_source = "compaction" if reasoning_override is not None else "main"

        primary: ResolvedCompactionModel | None = None
        if profile_name and settings is not None:
            try:
                profile = await settings.resolve_profile(profile_name)
                if profile is not None and profile.api_key:
                    model_config = ModelConfig(
                        provider=profile.provider,
                        model=profile.model,
                        base_url=profile.base_url,
                        protocol=profile.protocol,
                        reasoning_effort=effective_reasoning,
                    )
                    model = host._model_factory(profile.api_key, model_config)
                    primary = ResolvedCompactionModel(
                        model=model,
                        model_config=model_config,
                        model_source="profile",
                        profile_name=profile_name,
                        reasoning_source=reasoning_source,
                        effective_reasoning_effort=effective_reasoning,
                        is_exact_main_instance=False,
                    )
                else:
                    log_tool_event("compaction_profile_unavailable", message=f"Compaction profile unavailable: {profile_name}")
            except Exception as exc:
                log_tool_event("compaction_profile_resolution_failed", message=f"{profile_name}: {exc}")

        if primary is None and reasoning_override is not None and getattr(host, "api_key", None):
            try:
                model_config = main_config.model_copy(update={"reasoning_effort": reasoning_override})
                model = host._model_factory(host.api_key, model_config)
                primary = ResolvedCompactionModel(
                    model=model,
                    model_config=model_config,
                    model_source="main",
                    profile_name="",
                    reasoning_source="compaction",
                    effective_reasoning_effort=reasoning_override,
                    is_exact_main_instance=False,
                )
            except Exception as exc:
                log_tool_event("compaction_model_resolution_failed", message=str(exc))

        if primary is None and host.model is not None:
            primary = ResolvedCompactionModel(
                model=host.model,
                model_config=main_config,
                model_source="main",
                profile_name="",
                reasoning_source="main",
                effective_reasoning_effort=main_config.reasoning_effort,
                is_exact_main_instance=True,
            )

        stages = [primary] if primary is not None else []
        if primary is not None and not primary.is_exact_main_instance and host.model is not None:
            stages.append(ResolvedCompactionModel(
                model=host.model,
                model_config=main_config,
                model_source="main",
                profile_name="",
                reasoning_source="main",
                effective_reasoning_effort=main_config.reasoning_effort,
                is_exact_main_instance=True,
            ))
        return stages
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

    async def rollover_for_live_state(
        self,
        messages: list[BaseMessage],
        *,
        prepared_request: PreparedMainRequest | None = None,
        force: bool = False,
        run_compaction_agent: RunCompactionAgent | None = None,
        minimum_net_reclaim: int = 1,
    ) -> CompactionResult | None:
        """Execute a context rollover cycle: flush journal, select tail, create recursive synthetic user summary, replace in persistence, and return live graph messages."""
        host = self.host
        state_context = current_thread_execution_state()
        session = getattr(state_context, "session", None) if state_context else getattr(host, "_session", None)
        session_id = session.id if session is not None else None

        # 1. Flush accepted AI/tool source via execution-local TurnJournal
        journal = getattr(state_context, "turn_journal", None) if state_context else getattr(host, "_turn_journal", None)
        if journal is not None and session_id:
            await journal.flush(messages, session_id=session_id, save_func=save_turn_message)

        model_name = (
            prepared_request.model_name
            if prepared_request is not None
            else host.config.model.model
        )
        context_limit = (
            prepared_request.context_limit
            if prepared_request is not None
            else getattr(host._compaction, "context_limit", 128_000)
        )
        token_counter = (
            prepared_request.metadata.get("token_counter")
            if prepared_request is not None and "token_counter" in prepared_request.metadata
            else lambda msgs, mdl=model_name: estimate_context_tokens(msgs, mdl)
        )

        # 2. Select closed tool tail (<= 10% context limit)
        semantic_messages = [
            m for m in messages
            if not isinstance(m, RemoveMessage)
            and not is_continuation_message(m)
            and not is_context_pressure_message(m)
            and not is_step_hint_message(m)
        ]
        retained_tail, candidate_head = select_closed_tool_tail(
            semantic_messages,
            context_limit=context_limit,
            token_counter=token_counter,
            model=model_name,
        )

        if retained_tail and not validate_closed_tool_batches(retained_tail):
            retained_tail = []

        # 3. Align with effective rows from persistence
        if session_id:
            effective_rows = await load_messages(session_id) or []
            tail_len = len(retained_tail)
            if tail_len >= len(effective_rows):
                retained_tail = []
                tail_len = 0

            source_rows = effective_rows[:len(effective_rows) - tail_len] if tail_len > 0 else list(effective_rows)
            non_compaction_rows = [r for r in source_rows if not is_compaction_row(r)]
            if not non_compaction_rows:
                if tail_len > 0:
                    retained_tail = []
                    source_rows = list(effective_rows)
                    non_compaction_rows = [r for r in source_rows if not is_compaction_row(r)]
                if not non_compaction_rows:
                    return None

            source_message_ids = [r.id for r in source_rows if r.id is not None]
            source_range_hash = compute_source_range_hash(source_rows)
            head_messages = messages_from_rows(source_rows)
        else:
            source_message_ids = []
            source_range_hash = "in_memory"
            head_messages = candidate_head
            if not any(not is_compaction_message(m) for m in head_messages):
                return None

        summary_head = compaction_summary_messages(head_messages)
        if not summary_head:
            return None

        # 4. Determine compaction depth and segment indices
        prev_depth = 0
        prev_opened_seg = 0
        for m in head_messages:
            if is_compaction_message(m):
                d = int(m.additional_kwargs.get("compaction_depth") or 1)
                if d > prev_depth:
                    prev_depth = d
                seg = int(m.additional_kwargs.get("opened_segment_index") or 0)
                if seg > prev_opened_seg:
                    prev_opened_seg = seg

        compaction_depth = prev_depth + 1
        closed_segment_index = prev_opened_seg
        if state_context is not None and getattr(state_context, "segment_index", 0) > closed_segment_index:
            closed_segment_index = state_context.segment_index
        opened_segment_index = closed_segment_index + 1

        # 5. Summary agent invocation (detached)
        summary_text: str | None = None
        used_fallback = False
        agent_fn = run_compaction_agent or getattr(self, "run_compaction_agent", None)
        if agent_fn is not None:
            try:
                summary_text = await agent_fn(summary_head, None)
            except Exception as exc:
                log_tool_event("compaction_agent_failed", message=str(exc))
                summary_text = None

        if not summary_text or not summary_text.strip():
            summary_text = fallback_summary(summary_head)
            used_fallback = True

        # 6. Candidate validation
        operation_id = f"op_rollover_{uuid.uuid4().hex[:12]}"
        compaction_id = f"c_{uuid.uuid4().hex[:8]}"

        meta = CompactionMessageMetadata(
            compaction_id=compaction_id,
            source_range_hash=source_range_hash,
            compaction_depth=compaction_depth,
            closed_segment_index=closed_segment_index,
            opened_segment_index=opened_segment_index,
            replacement_operation_id=operation_id,
        )
        synthetic_user_msg = create_compaction_user_message(
            content=summary_text,
            metadata=meta,
        )

        candidate_messages = [synthetic_user_msg, *retained_tail]
        candidate_prepared = prepare_main_request(
            messages=candidate_messages,
            tool_defs=prepared_request.tool_defs if prepared_request is not None else [],
            model_name=model_name,
            context_limit=context_limit,
            output_token_max=prepared_request.main_output_reserve if prepared_request is not None else 4096,
            safety_margin=prepared_request.safety_margin if prepared_request is not None else COMPACTION_BUFFER,
            token_counter=token_counter,
        )

        if candidate_prepared.should_rollover:
            raise ContextBudgetExhausted(
                f"Candidate request tokens ({candidate_prepared.total_input_tokens}) exceed main request limit ({candidate_prepared.main_request_limit})"
            )

        pre_tokens = (
            prepared_request.total_input_tokens
            if prepared_request is not None
            else token_counter(messages, model_name)
        )
        if pre_tokens - candidate_prepared.total_input_tokens < minimum_net_reclaim:
            return None

        # 7. Commit replacement in persistence
        if session_id:
            replacement_row = MessageRow(
                session_id=session_id,
                role="user",
                content=summary_text,
                content_format="text",
                additional_kwargs=dict(synthetic_user_msg.additional_kwargs),
            )
            repl_result = await replace_effective_message_range(
                session_id=session_id,
                source_message_ids=source_message_ids,
                source_range_hash=source_range_hash,
                replacement=replacement_row,
                operation_id=operation_id,
                closed_segment_index=closed_segment_index,
                opened_segment_index=opened_segment_index,
            )
            if not repl_result.applied and repl_result.winner_operation_id != operation_id:
                return None

            synthetic_user_msg = create_compaction_user_message(
                content=summary_text,
                metadata=meta,
                id=str(repl_result.replacement_message_id),
            )
            if state_context is not None:
                state_context.compaction_summary = summary_text
                state_context.segment_index = opened_segment_index
            host._compaction_summary = summary_text
            if getattr(host, "_session_msg_cache", None) is not None:
                host._session_msg_cache = await load_messages(session_id)
            try:
                await gc_context_frames(session_id)
            except Exception:
                pass

        # 8. Assemble live graph state messages
        continuation_msg = create_continuation_message()
        live_messages: list[BaseMessage] = [
            RemoveMessage(id=REMOVE_ALL_MESSAGES),
            synthetic_user_msg,
            *retained_tail,
            continuation_msg,
        ]

        return CompactionResult(
            summary=summary_text,
            removed_messages=list(head_messages),
            live_messages=live_messages,
            tail_id=str(retained_tail[0].id) if retained_tail and getattr(retained_tail[0], "id", None) else None,
            fallback=used_fallback,
            metadata=meta.model_dump(),
        )

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
        run_agent = run_compaction_agent
        if run_agent is None and "run_compaction_agent" in self.__dict__:
            run_agent = self.__dict__["run_compaction_agent"]
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
        summary_head = compaction_summary_messages(head_msgs)
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

        resolved_stages: list[ResolvedCompactionModel] = []
        if run_agent is None:
            resolved_stages = await self.resolve_compaction_models()
        stages: list[tuple[str, ResolvedCompactionModel | None]] = (
            [("custom", None)] if run_agent is not None else [
                ("summary" if index == 0 else "main_fallback", resolved)
                for index, resolved in enumerate(resolved_stages)
            ]
        )
        timeout_seconds = self._compaction_config().timeout_seconds
        used_stage = ""
        used_resolved: ResolvedCompactionModel | None = None
        used_attempt = 0
        summary_timed_out = False

        for stage_name, resolved in stages:
            for attempt in range(1, COMPACTION_MAX_RETRIES + 2):
                try:
                    if host._ui.via_events():
                        retry_label = f" (attempt {attempt})" if attempt > 1 else ""
                        await host._ui.events.emit(StatusUpdated(
                            status_id="compaction",
                            label="Compacting",
                            detail=f"summarizing {len(summary_head)} old messages{retry_label}",
                            stage="compacting",
                            display="record_only",
                        ))
                    invocation = (
                        run_agent(summary_head, previous_summary)
                        if run_agent is not None
                        else self._run_compaction_attempt(
                            summary_head,
                            previous_summary,
                            resolved,
                            stage=stage_name,
                            attempt=attempt,
                            timeout_seconds=timeout_seconds,
                        )
                    )
                    summary = await asyncio.wait_for(invocation, timeout=timeout_seconds)
                    used_stage = stage_name
                    used_resolved = resolved
                    used_attempt = attempt
                    if summary:
                        break
                    returned_no_summary = True
                    last_error = None
                except Exception as e:
                    used_stage = stage_name
                    used_resolved = resolved
                    used_attempt = attempt
                    summary_timed_out = isinstance(e, TimeoutError)
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
            if summary:
                break

        if not summary:
            if last_error:
                failure_detail = f"{type(last_error).__name__}: {last_error}"
            elif returned_no_summary:
                failure_detail = "compaction agent returned no summary"
            else:
                failure_detail = "compaction agent did not produce a summary"
            log_tool_event(
                "compaction_failed",
                message=(
                    f"{failure_detail} "
                    f"stage={used_stage or 'none'} attempt={used_attempt} "
                    f"model={host.config.model.model}"
                ),
                session_id=host._session.id if host._session is not None else None,
            )
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
            fallback = fallback_summary_with_previous(summary_head, previous_summary)
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
                {
                    **base_metadata,
                    **_summary_attempt_metadata(
                        used_resolved,
                        stage=used_stage,
                        attempt=used_attempt,
                        timeout_seconds=timeout_seconds,
                        fallback_used=used_stage == "main_fallback",
                        input_count=len(summary_head),
                        removed_count=len(head_msgs),
                        timed_out=summary_timed_out,
                    ),
                },
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
            {
                **base_metadata,
                **_summary_attempt_metadata(
                    used_resolved,
                    stage=used_stage,
                    attempt=used_attempt,
                    timeout_seconds=timeout_seconds,
                    fallback_used=used_stage == "main_fallback",
                    input_count=len(summary_head),
                    removed_count=len(head_msgs),
                    timed_out=summary_timed_out,
                ),
            },
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
        try:
            await gc_context_frames(host._session.id)
        except Exception as exc:
            log_tool_event("context_frame_gc_failed", message=str(exc), session_id=host._session.id)

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

    async def _run_compaction_attempt(
        self,
        head_messages: list,
        previous_summary: str | None,
        resolved: ResolvedCompactionModel,
        *,
        stage: str,
        attempt: int,
        timeout_seconds: float,
    ) -> str | None:
        return await self.run_compaction_agent(
            head_messages,
            previous_summary,
            resolved=resolved,
            attempt_metadata={
                "summary_stage": stage,
                "summary_attempt": attempt,
                "summary_timeout_seconds": timeout_seconds,
                "summary_model_source": resolved.model_source,
                "summary_profile_name": resolved.profile_name,
                "summary_reasoning_effort": resolved.effective_reasoning_effort.value,
                "summary_reasoning_source": resolved.reasoning_source,
            },
        )

    async def run_compaction_agent(
        self,
        head_messages: list,
        previous_summary: str | None,
        *,
        resolved: ResolvedCompactionModel | None = None,
        attempt_metadata: dict[str, object] | None = None,
    ) -> str | None:
        """Run the compaction behavior to generate a structured summary."""
        host = self.host
        if resolved is None:
            if host.model is None:
                return None
            resolved = ResolvedCompactionModel(
                model=host.model,
                model_config=host.config.model,
                model_source="main",
                profile_name="",
                reasoning_source="main",
                effective_reasoning_effort=getattr(
                    host.config.model,
                    "reasoning_effort",
                    ReasoningEffort.XHIGH,
                ),
                is_exact_main_instance=True,
            )
        model = resolved.model
        model_config = resolved.model_config
        model_protocol = resolve_protocol(model_config)
        prompt_cache_key = prompt_cache_key_for(
            host._session.id if host._session is not None else None,
            model_config.provider,
            model_config.model,
            scope="compaction",
        )
        model = bind_prompt_cache_key(
            model,
            prompt_cache_key,
            protocol=model_protocol,
        )

        ui_factories = host._ui if hasattr(host._ui, "streaming_renderer") else NullAgentUiPort()
        renderer_factory = StreamingRenderer or ui_factories.streaming_renderer
        renderer = renderer_factory(
            host._ui.console,
            debug=host._debug,
            stream_to_dock=False,
            headless=True,
        )

        request_message = HumanMessage(content=_compaction_request_text(previous_summary))
        messages: list[BaseMessage] = [*head_messages, request_message]
        context_tokens = estimate_context_tokens(messages, model_config.model)
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
                model=model_config.model,
            )
            if not head_messages:
                raise ValueError("compaction input exceeds context budget")
            messages = [*head_messages, request_message]
            context_tokens = estimate_context_tokens(messages, model_config.model)
            if context_tokens > host._compaction.context_limit:
                raise ValueError("compaction input exceeds context budget")

        host._usage_stats.update_context(context_tokens)
        if host._session is not None:
            await save_context_frame_from_messages(
                session_id=host._session.id,
                frame_kind="compaction",
                agent_persona="compaction-behavior",
                provider=model_config.provider,
                model=model_config.model,
                messages=messages,
                token_estimate=context_tokens,
                metadata={
                    "head_message_count": len(head_messages),
                    "has_previous_summary": previous_summary is not None,
                    "input_mode": "removed_history_only",
                    "source_message_count": len(head_messages),
                    "summary_input_scope": "removed_history_only",
                    **(attempt_metadata or {}),
                },
            )
        assistant_msg = await stream_llm(
            model,
            messages,
            renderer,
            model_protocol,
            ui_port=host._ui,
        )
        log_llm_exchange(
            messages,
            assistant_msg,
            model=model_config.model,
            provider=model_config.provider,
            step=0,
            session_id=host._session.id if host._session is not None else None,
            enabled=getattr(host.config, "log_llm_exchange", False),
        )
        host._usage_stats.record_call(
            extract_token_usage(assistant_msg),
            fallback_input_tokens=context_tokens,
            fallback_output_tokens=estimate_message_tokens(assistant_msg, model_config.model),
            messages=messages,
            model=model_config.model,
            cache_key=f"{model_config.provider}/{model_config.model}",
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




def _summary_attempt_metadata(
    resolved: ResolvedCompactionModel | None,
    *,
    stage: str,
    attempt: int,
    timeout_seconds: float,
    fallback_used: bool,
    input_count: int,
    removed_count: int,
    timed_out: bool,
) -> dict[str, object]:
    if resolved is None:
        return {
            "summary_stage": stage,
            "summary_attempt": attempt,
            "summary_timeout_seconds": timeout_seconds,
            "summary_fallback_used": fallback_used,
            "summary_timed_out": timed_out,
            "summary_input_message_count": input_count,
            "summary_removed_message_count": removed_count,
            "summary_input_scope": "removed_history_only",
        }
    return {
        "summary_model_provider": resolved.model_config.provider,
        "summary_model_name": resolved.model_config.model,
        "summary_model_source": resolved.model_source,
        "summary_profile_name": resolved.profile_name,
        "summary_reasoning_effort": resolved.effective_reasoning_effort.value,
        "summary_reasoning_source": resolved.reasoning_source,
        "summary_timeout_seconds": timeout_seconds,
        "summary_stage": stage,
        "summary_attempt": attempt,
        "summary_fallback_used": fallback_used,
        "summary_timed_out": timed_out,
        "summary_input_message_count": input_count,
        "summary_removed_message_count": removed_count,
        "summary_input_scope": "removed_history_only",
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
