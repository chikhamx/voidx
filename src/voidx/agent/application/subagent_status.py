"""Compact child-agent status snapshots for runtime prompts."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from voidx.agent.domain.subagent import (
    AgentActivity,
    AgentProgress,
    AgentRun,
    TERMINAL_STATUSES,
)

_RECENT_TERMINAL_LIMIT = 3
_DESCRIPTION_LIMIT = 80
_ACTIVITY_LABELS = {
    "thinking": "thinking",
    "reading": "reading",
    "editing": "editing",
    "running_command": "running command",
    "searching": "searching",
    "other": "other",
}


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


def public_child_run_snapshot(run: AgentRun) -> dict:
    snapshot = {
        "run_id": run.run_id,
        "session_id": run.session_id,
        "parent_run_id": run.parent_run_id,
        "agent_type": run.agent_type,
        "agent_name": run.agent_name,
        "description": run.description,
        "status": run.status,
        "result": run.result,
        "error": run.error,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "progress": run.progress.model_dump(mode="json"),
        "current_activity": _activity_dump(run.current_activity),
        "recent_activity": _activity_dump(run.recent_activity),
        "last_activity_at": run.last_activity_at,
    }
    return snapshot


def render_child_progress(progress: AgentProgress) -> str:
    parts: list[str] = []
    if progress.files_read:
        parts.append(f"read {progress.files_read} {_plural(progress.files_read, 'file')}")
    if progress.files_edited:
        parts.append(f"edited {progress.files_edited} {_plural(progress.files_edited, 'file')}")
    if progress.commands_run:
        parts.append(f"ran {progress.commands_run} {_plural(progress.commands_run, 'command')}")
    if progress.searches:
        parts.append(f"searched {progress.searches} {_plural(progress.searches, 'time')}")
    if progress.other_actions:
        parts.append(f"{progress.other_actions} other {_plural(progress.other_actions, 'action')}")
    return " · ".join(parts)


def render_child_activity(run: AgentRun, *, sampled_at: float) -> list[str]:
    lines: list[str] = []
    if run.current_activity is not None:
        lines.append(
            f"Current: {_activity_label(run.current_activity)} · "
            f"activity {_duration(_activity_age(run.current_activity, sampled_at))} ago"
        )
    if run.recent_activity is not None:
        finished_at = run.recent_activity.finished_at or run.recent_activity.last_observed_at
        lines.append(
            f"Recent: {_activity_label(run.recent_activity)} · "
            f"{run.recent_activity.status} "
            f"{_duration(max(0.0, sampled_at - finished_at))} ago"
        )
    return lines


def activity_recommendation(
    run: AgentRun,
    *,
    wait_started_at: float,
) -> Literal["wait", "cancel"]:
    last_activity_at = run.last_activity_at
    if last_activity_at is not None and last_activity_at >= wait_started_at:
        return "wait"
    return "cancel"


def render_child_elapsed(run: AgentRun, *, sampled_at: float) -> str:
    end_at = run.updated_at if run.status in TERMINAL_STATUSES else sampled_at
    return f"elapsed {_duration(max(0.0, end_at - run.created_at))}"


def activity_label(activity: AgentActivity) -> str:
    return _ACTIVITY_LABELS[activity.category]


def format_duration(seconds: float) -> str:
    return _duration(seconds)


def render_child_run_metrics(run: AgentRun, *, sampled_at: float) -> str:
    parts = [render_child_elapsed(run, sampled_at=sampled_at)]
    if run.current_activity is not None:
        parts.extend([
            f"current: {_activity_label(run.current_activity)}",
            f"activity {_duration(_activity_age(run.current_activity, sampled_at))} ago",
        ])
    elif run.recent_activity is not None:
        finished_at = run.recent_activity.finished_at or run.recent_activity.last_observed_at
        parts.extend([
            f"recent: {_activity_label(run.recent_activity)} {run.recent_activity.status}",
            f"{_duration(max(0.0, sampled_at - finished_at))} ago",
        ])
    return " · ".join(parts)


def _render_child_run(run: AgentRun, *, sampled_at: float) -> str:
    parts = [
        f"{run.run_id} [{run.status}] {_description_summary(run.description)}",
        render_child_run_metrics(run, sampled_at=sampled_at),
    ]
    return " · ".join(parts)


def _activity_dump(activity: AgentActivity | None) -> dict | None:
    return activity.model_dump(mode="json") if activity is not None else None


def _activity_label(activity: AgentActivity) -> str:
    return _ACTIVITY_LABELS[activity.category]


def _activity_age(activity: AgentActivity, sampled_at: float) -> float:
    return max(0.0, sampled_at - activity.last_observed_at)


def _plural(count: int, word: str) -> str:
    return word if count == 1 else f"{word}s"


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
