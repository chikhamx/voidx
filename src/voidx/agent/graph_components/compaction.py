"""Context compaction methods for the agent graph."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from voidx.agent.graph_components.runtime import console, ui
from voidx.agent.graph_components.streaming import extract_text, stream_llm
from voidx.llm.provider import resolve_protocol
from voidx.llm.usage import estimate_context_tokens, estimate_message_tokens, extract_token_usage
from voidx.memory.context_frames import save_context_frame_from_messages
from voidx.ui.console import StreamingRenderer
from voidx.ui.dock import dock
from voidx.ui.events import StatusFinished, StatusUpdated, ui_events


class GraphCompactionMixin:
    async def _maybe_compact(
        self,
        messages: list,
        session_msgs: list,
        *,
        force: bool = False,
        ask: bool = True,
    ) -> tuple[list | None, str | None]:
        """Check overflow and compact if needed."""
        total_tokens = estimate_context_tokens(messages, self.config.model.model)
        tokens = {"total": total_tokens, "input": total_tokens, "output": 0, "reasoning": 0}

        if not force and not self._compaction.is_overflow(tokens):
            return None, None

        if not force and ask and getattr(self.config, "ask_compact", False):
            should_compact = await self._ask_compact(total_tokens)
            if not should_compact:
                if dock.active and ui_events.is_running:
                    await ui_events.emit(StatusFinished(
                        status_id="compaction",
                        label="Compaction skipped",
                        remove=False,
                    ))
                else:
                    ui.print("[dim]Compaction skipped[/dim]")
                return None, None

        if dock.active and ui_events.is_running:
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

        head_msgs, tail_id = self._compaction.select(messages)

        if not head_msgs or not tail_id:
            # Hard fallback: keep only last 6 messages
            keep = min(6, len(messages))
            if dock.active and ui_events.is_running:
                await ui_events.emit(StatusUpdated(
                    status_id="compaction",
                    label="Compacting context",
                    detail=f"fallback truncation, keeping last {keep} messages",
                    stage="compacting",
                ))
                await ui_events.emit(StatusFinished(
                    status_id="compaction",
                    label=f"Compaction fallback kept last {keep} messages",
                    remove=False,
                ))
            else:
                ui.print(f"[dim]Aggressive truncation: keeping last {keep} messages[/dim]")
            # Remove old messages, keep system + last N
            system_msgs = [m for m in messages if isinstance(m, SystemMessage)]
            other_msgs = [m for m in messages if not isinstance(m, SystemMessage)]
            messages.clear()
            messages.extend(system_msgs)
            messages.extend(other_msgs[-keep:])
            return messages[:max(0, len(messages) - keep)], None

        # Run compaction agent
        try:
            if dock.active and ui_events.is_running:
                await ui_events.emit(StatusUpdated(
                    status_id="compaction",
                    label="Compacting context",
                    detail=f"summarizing {len(head_msgs)} old messages",
                    stage="compacting",
                ))
            previous_summary = getattr(self, "_compaction_summary", "") or None
            summary = await self._run_compaction_agent(head_msgs, previous_summary)
        except Exception as e:
            if dock.active and ui_events.is_running:
                await ui_events.emit(StatusUpdated(
                    status_id="compaction",
                    label="Compaction agent failed",
                    detail=f"{e}; falling back to truncation",
                    stage="compacting",
                ))
            else:
                ui.print(f"[dim]Compaction agent failed ({e}) — aggressive truncation[/dim]")
            keep = min(6, len(messages))
            system_msgs = [m for m in messages if isinstance(m, SystemMessage)]
            other_msgs = [m for m in messages if not isinstance(m, SystemMessage)]
            messages.clear()
            messages.extend(system_msgs)
            messages.extend(other_msgs[-keep:])
            if dock.active and ui_events.is_running:
                await ui_events.emit(StatusFinished(
                    status_id="compaction",
                    label=f"Compaction fallback kept last {keep} messages",
                    ok=False,
                    remove=False,
                ))
            return messages[:max(0, len(messages) - keep)], None

        if summary:
            keep_from = len(head_msgs)
            tail_msgs = messages[keep_from:]
            messages.clear()
            messages.extend(tail_msgs)
            self._pending_summary = summary
            self._compaction_summary = summary
            self._compaction.compaction_count += 1
            await self._persist_compaction(head_msgs)
            if dock.active and ui_events.is_running:
                await ui_events.emit(StatusFinished(
                    status_id="compaction",
                    label=f"Compacted {len(head_msgs)} messages into summary",
                    remove=False,
                ))
            else:
                ui.print(f"[dim]Compacted: {len(head_msgs)} messages → summary[/dim]")
        elif dock.active and ui_events.is_running:
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

    async def _ask_compact(self, total_tokens: int) -> bool:
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

    async def _persist_compaction(self, head_messages: list) -> None:
        if getattr(self, "_session", None) is None:
            return
        if hasattr(self, "_persist_runtime_state"):
            await self._persist_runtime_state()
        last_message_id = _max_persisted_message_id(head_messages)
        if last_message_id is None:
            return
        from voidx.memory.session import delete_messages_through

        await delete_messages_through(self._session.id, last_message_id)

    async def _compact_session_history(self, *, force: bool = True) -> bool:
        if getattr(self, "_session", None) is None:
            ui.print("[dim]No active session to compact.[/dim]")
            return False

        from voidx.agent.attachments import parse_structured_content
        from voidx.memory.session import load_messages

        rows = await load_messages(self._session.id)
        messages = []
        for row in rows:
            msg_id = str(row.id) if row.id is not None else None
            if row.role == "system":
                messages.append(SystemMessage(content=row.content, id=msg_id))
            elif row.role == "user":
                messages.append(HumanMessage(
                    content=parse_structured_content(row.content, row.content_format),
                    id=msg_id,
                ))
            elif row.role == "assistant":
                messages.append(AIMessage(
                    content=parse_structured_content(row.content, row.content_format),
                    tool_calls=row.tool_calls or [],
                    id=msg_id,
                ))
            elif row.role == "tool":
                messages.append(ToolMessage(
                    content=row.content,
                    tool_call_id=row.tool_call_id or "",
                    id=msg_id,
                ))

        head, _tail_id = await self._maybe_compact(messages, rows, force=force, ask=False)
        return bool(head)

    async def _run_compaction_agent(self, head_messages: list, previous_summary: str | None) -> str | None:
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
