"""Context compaction methods for the agent graph."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from voidx.agent.graph_parts.runtime import console, ui
from voidx.agent.graph_parts.streaming import extract_text, stream_llm
from voidx.llm.context import count_messages_tokens
from voidx.llm.provider import resolve_protocol
from voidx.ui.console import StreamingRenderer
from voidx.ui.dock import dock
from voidx.ui.events import StatusFinished, StatusUpdated, ui_events


class GraphCompactionMixin:
    async def _maybe_compact(self, messages: list, session_msgs: list) -> tuple[list | None, str | None]:
        """Check overflow and compact if needed."""
        total_tokens = count_messages_tokens(
            [{"role": "system" if isinstance(m, SystemMessage) else
              "user" if isinstance(m, HumanMessage) else
              "assistant" if isinstance(m, AIMessage) else "tool",
              "content": str(getattr(m, "content", ""))[:500]}
             for m in messages]
        )
        tokens = {"total": total_tokens, "input": total_tokens, "output": 0, "reasoning": 0}

        if not self._compaction.is_overflow(tokens):
            return None, None

        if dock.active and ui_events.is_running:
            await ui_events.emit(StatusUpdated(
                status_id="compaction",
                label="Compacting context",
                detail=f"{total_tokens} tokens exceed the active context budget",
                stage="compacting",
            ))
        else:
            ui.print("[yellow]Context overflow — compacting...[/yellow]")

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
            summary = await self._run_compaction_agent(head_msgs, None)
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
            self._compaction.compaction_count += 1
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

        return head_msgs, tail_id

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
        assistant_msg = await stream_llm(self.model, messages, renderer, resolve_protocol(self.config.model))
        text = extract_text(assistant_msg)
        return text if text else None
