from __future__ import annotations

from voidx.diffing import diff_stat
from voidx.logging.tool_log import log_tool_event
from voidx.runtime.ui import (
    FileChangeAppended,
    StatusFinished,
    ToolDisplayMode,
    ToolDisplayPolicy,
    ToolFinished,
    ToolResultAppended,
    ToolStarted,
    _fmt_args,
    _title,
)


async def notify_tool_started(host, tc, display_policy) -> object | None:
    """Emit tool-started notification across all UI channels. Returns tool_node for dock path."""
    tid = tc["name"]
    targs = tc.get("args", {})
    cid = tc.get("id", "")
    tool_event_id = cid or f"{tid}:{id(tc)}"
    rule = display_policy.rule_for(tid)
    initial_display_mode = rule.mode
    initial_summary_max_lines = rule.summary_max_lines
    tool_node = None

    if host._ui.via_events():
        gerund = _title(host._ui.ui._TOOL_GERUND.get(tid, tid + "ing"))
        tool_node = await host._ui.events.request(ToolStarted(
            tool_call_id=tool_event_id,
            tool_name=tid,
            label=gerund,
            args=_fmt_args(targs),
            raw_args=targs,
            display_mode=initial_display_mode,
            summary_max_lines=initial_summary_max_lines,
        ))
        if host._ui.dock.active and host._ui.dock.current_agent is not None:
            host._turn_node = host._ui.dock.current_agent
    elif host._ui.dock.active:
        if initial_display_mode != ToolDisplayMode.HIDDEN:
            gerund = _title(host._ui.ui._TOOL_GERUND.get(tid, tid + "ing"))
            tool_node = host._ui.dock.start_tool(
                gerund,
                _fmt_args(targs),
                tool_call_id=tool_event_id,
                tool_name=tid,
                raw_args=targs,
            )
            if host._ui.dock.current_agent is not None:
                host._turn_node = host._ui.dock.current_agent
    else:
        if initial_display_mode != ToolDisplayMode.HIDDEN:
            host._ui.ui.tool_call(tid, targs)

    return tool_node


async def notify_tool_result(host, tc, result, ok, elapsed, display_policy, tool_node) -> None:
    """Emit tool-finished notification across all UI channels."""
    tid = tc["name"]
    cid = tc.get("id", "")
    tool_event_id = cid or f"{tid}:{id(tc)}"
    rule = display_policy.rule_for(tid)
    initial_display_mode = rule.mode

    if host._ui.via_events():
        if initial_display_mode != ToolDisplayMode.HIDDEN:
            await host._ui.events.emit(ToolFinished(
                tool_call_id=tool_event_id,
                label=_title(tid),
                elapsed=elapsed,
                ok=ok,
                detail=result.summary if result.summary else "",
            ))
    elif tool_node:
        if initial_display_mode != ToolDisplayMode.HIDDEN:
            host._ui.dock.finish_tool_node(tool_node, _title(tid), elapsed, ok)
    else:
        if initial_display_mode != ToolDisplayMode.HIDDEN:
            host._ui.ui.tool_done(tid, elapsed, ok)


async def notify_tool_diff(host, result, tool_event_id, tool_node) -> None:
    """Render diff output across all UI channels."""
    if host._ui.via_events():
        await host._ui.events.emit(FileChangeAppended(
            tool_call_id=tool_event_id,
            diff_text=result.diff,
        ))
    elif tool_node:
        host._ui.dock.append_file_change(
            result.diff,
            parent=tool_node,
            tool_call_id=tool_event_id,
        )
    else:
        added, removed = diff_stat(result.diff)
        host._ui.ui.print(f"  [green]+{added}[/green] [red]−{removed}[/red]")
    if host._debug and not tool_node:
        host._ui.ui.diff(result.diff)


def notify_tool_failure(host, tc, result, display_mode, tool_event_id) -> None:
    """Log hidden-tool failures (non-async, just logging)."""
    tid = tc.get("name", "tool")
    if display_mode == ToolDisplayMode.HIDDEN:
        log_tool_event(
            "hidden_tool_failure",
            tool_name=tid,
            message=result.summary or "unknown error",
            session_id=host._session.id if host._session else None,
        )


async def notify_tool_text_output(host, output, tid, tool_event_id, tool_node, display_policy, ok) -> None:
    """Render non-diff text output across all UI channels."""
    resolved_mode, resolved_max = display_policy.resolve_display_mode(tid, output, result_ok=ok)
    if resolved_mode == ToolDisplayMode.HIDDEN:
        return

    if host._ui.via_events():
        await host._ui.events.emit(ToolResultAppended(
            tool_call_id=tool_event_id,
            text=output,
            display_mode=resolved_mode,
            summary_max_lines=resolved_max,
        ))
    elif tool_node:
        display_output = output
        if resolved_mode == ToolDisplayMode.SUMMARY:
            lines = display_output.splitlines()
            if len(lines) > resolved_max:
                display_output = "\n".join(lines[:resolved_max]) + f"\n… +{len(lines) - resolved_max} more lines"
        host._ui.dock.append_tool_result(
            display_output,
            parent=tool_node,
            tool_call_id=tool_event_id,
        )
    else:
        host._ui.ui.tool_result(output)
