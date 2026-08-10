"""Compact child-agent status snapshots for runtime prompts."""

from __future__ import annotations

from collections.abc import Iterable

from voidx.agent.domain.subagent import AgentRun, TERMINAL_STATUSES

_RECENT_TERMINAL_LIMIT = 3
_DESCRIPTION_LIMIT = 80


def render_child_run_lines(runs: Iterable[AgentRun], *, sampled_at: float) -> list[str]:
    visible, running_count, terminal_count = select_visible_child_runs(runs)
    if not visible:
        return []
    lines = [
        f"- Child agents: {running_count} running · {terminal_count} recent terminal"
    ]
    lines.extend(
        f"  - {_render_child_run(run, sampled_at=sampled_at)}"
        for run in visible
    )
    return lines


def select_visible_child_runs(runs: Iterable[AgentRun]) -> tuple[list[AgentRun], int, int]:
    values = list(runs)
    running = [run for run in values if run.status not in TERMINAL_STATUSES]
    terminal = sorted(
        (run for run in values if run.status in TERMINAL_STATUSES),
        key=lambda run: (run.updated_at, run.run_id),
        reverse=True,
    )[:_RECENT_TERMINAL_LIMIT]
    return [*running, *terminal], len(running), len(terminal)


def render_child_run_metrics(run: AgentRun, *, sampled_at: float) -> str:
    end_at = run.updated_at if run.status in TERMINAL_STATUSES else sampled_at
    parts = [f"elapsed {_duration(max(0.0, end_at - run.created_at))}"]
    activity = _activity_summary(run, sampled_at=sampled_at)
    if activity:
        parts.append(activity)
    return " · ".join(parts)


def _render_child_run(run: AgentRun, *, sampled_at: float) -> str:
    parts = [
        f"{run.run_id} [{run.status}] {_description_summary(run.description)}",
        render_child_run_metrics(run, sampled_at=sampled_at),
    ]
    return " · ".join(parts)


def _activity_summary(run: AgentRun, *, sampled_at: float) -> str:
    if run.active_tools:
        active = ", ".join(
            f"{item.tool_name} ({_duration(max(0.0, sampled_at - item.started_at))})"
            for item in run.active_tools
        )
        return f"active: {active}"
    if run.last_tool is None:
        return ""
    finished_at = run.last_tool.finished_at
    suffix = ""
    if finished_at is not None:
        suffix = f" {_duration(max(0.0, sampled_at - finished_at))} ago"
    return f"last: {run.last_tool.tool_name} {run.last_tool.status}{suffix}"


def _description_summary(description: str) -> str:
    first_line = next(
        (line.strip() for line in description.splitlines() if line.strip()),
        "Goal: not specified",
    )
    if not first_line.lower().startswith("goal:"):
        first_line = f"Goal: {first_line}"
    if len(first_line) <= _DESCRIPTION_LIMIT:
        return first_line
    return f"{first_line[:_DESCRIPTION_LIMIT - 1].rstrip()}…"


def _duration(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes, remainder = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"
