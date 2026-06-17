"""Status bar rendering helpers."""

from __future__ import annotations

from dataclasses import dataclass

from rich.cells import cell_len
from rich.text import Text

from voidx.llm.usage import format_cache_hit_rate, format_token_count
from voidx.ui.tui.activity import BUSY_ACTIVITY_STYLE
from voidx.ui.tui.helpers import (
    _call_bool,
    _call_int,
    _call_status,
    _clip_cells,
    _safe_status_value,
)
from voidx.ui.tui.state import StatusSummaryCache


@dataclass(frozen=True)
class StatusSegment:
    kind: str
    text: str


_STATUS_STYLES = {
    "model": "#6CB6FF",
    "policy": "#57AB5A",
    "state": BUSY_ACTIVITY_STYLE,
    "workflow": "#8BD5FF",
    "usage": "#56D4DD",
    "goal": "#C698F0",
    "separator": "#4B5563",
}
_STATUS_VARIANTS = (
    ("model", "policy", "state", "workflow", "usage", "goal"),
    ("model", "policy", "state", "workflow", "goal"),
    ("model", "policy", "workflow", "goal"),
    ("model", "policy", "workflow"),
    ("model", "policy", "state", "usage", "goal"),
    ("model", "policy", "usage", "goal"),
    ("model", "policy", "usage"),
    ("model", "policy"),
    ("model",),
)

_WORKFLOW_RAINBOW = ("#FF6B6B", "#FFD93D", "#6BCB77", "#4D96FF", "#B983FF")


class _StatusRendererMixin:
    def _mark_status_summary_dirty(self) -> None:
        self._render_state.status_summary_dirty = True

    def _status_summary(self, width: int) -> str:
        cache = self._render_state.status_summary_cache
        if (
            cache is not None
            and cache.width == width
            and not self._render_state.status_summary_dirty
        ):
            return cache.summary

        snapshot, segments = self._status_segments(include_busy=True)
        summary, selected = self._select_status_variant(width, segments)
        self._render_state.status_summary_dirty = False
        self._render_state.status_summary_cache = StatusSummaryCache(
            width,
            snapshot,
            summary,
            tuple((segment.kind, segment.text) for segment in selected),
        )
        return summary

    def _status_summary_text(self, width: int) -> Text:
        summary = self._status_summary(width)
        if not summary:
            return Text()
        cache = self._render_state.status_summary_cache
        selected = ()
        if cache is not None and cache.width == width and cache.summary == summary:
            selected = tuple(
                StatusSegment(kind, text)
                for kind, text in cache.segments
            )
        return self._status_text_from_segments(summary, selected)

    def _status_segments(self, *, include_busy: bool) -> tuple[tuple, tuple[StatusSegment, ...]]:
        provider = _safe_status_value(getattr(self.status, "provider", ""), "")
        model = _safe_status_value(getattr(self.status, "model", ""), "")
        effort = _safe_status_value(getattr(self.status, "reasoning_effort", ""), "")
        permission = _call_status(getattr(self.status, "permission_label", None), "")
        sandbox = _call_status(getattr(self.status, "sandbox_label", None), "")
        approval = _call_status(getattr(self.status, "approval_label", None), "")
        reviewer = _call_status(getattr(self.status, "approval_reviewer_label", None), "")
        mode = _call_status(getattr(self.status, "interaction_mode", None), "")
        plan = _call_bool(getattr(self.status, "plan_mode", None))
        debug = _call_bool(getattr(self.status, "debug", None))
        goal_label = _call_status(getattr(self.status, "goal_label", None), "")
        goal_type = _call_status(getattr(self.status, "goal_type", None), "")
        goal_awaiting = _call_bool(getattr(self.status, "goal_awaiting_approval", None))
        active_workflows = _call_workflows(getattr(self.status, "active_workflows", None))
        stats = getattr(self.status, "usage_stats", None)
        context_limit = getattr(stats, "context_limit", None) or getattr(self.status, "context_limit", 0)
        stats_snapshot = (
            context_limit,
            getattr(stats, "context_tokens", 0) if stats is not None else 0,
            getattr(stats, "total_tokens", 0) if stats is not None else 0,
            getattr(stats, "cache_hit_rate", None) if stats is not None else None,
        )
        snapshot = (
            provider,
            model,
            effort,
            permission,
            sandbox,
            approval,
            reviewer,
            mode,
            plan,
            debug,
            goal_label,
            goal_type,
            goal_awaiting,
            tuple(active_workflows),
            self._busy,
            stats_snapshot,
        )
        model_text = "/".join(part for part in (provider, model) if part)
        if effort:
            model_text = f"{model_text} {effort}"

        policy_parts = [part for part in (permission, sandbox, approval) if part]
        if reviewer and reviewer != "user":
            policy_parts.append(reviewer)
        policy_text = " ".join(policy_parts)

        state_parts = []
        if mode:
            state_parts.append(mode)
        if plan:
            state_parts.append("plan")
        if debug:
            state_parts.append("debug")
        state_text = " ".join(state_parts)

        usage_text = ""
        if stats is not None:
            usage_text = (
                f"ctx {format_token_count(getattr(stats, 'context_tokens', 0))}/"
                f"{format_token_count(context_limit)}"
                f" cache {format_cache_hit_rate(stats)}"
                f" total {format_token_count(getattr(stats, 'total_tokens', 0))}"
            )

        goal_text = ""
        if goal_label or goal_type or goal_awaiting:
            goal_text = "goal"
            if goal_type:
                goal_text += f" {goal_type}"
            if goal_awaiting:
                goal_text += " awaiting-approval"
            if goal_label:
                goal_text += f" {goal_label}"

        workflow_text = ""
        if active_workflows:
            workflow_text = ", ".join(active_workflows)

        segments = tuple(
            segment
            for segment in (
                StatusSegment("model", model_text),
                StatusSegment("policy", policy_text),
                StatusSegment("state", state_text),
                StatusSegment("workflow", workflow_text),
                StatusSegment("usage", usage_text),
                StatusSegment("goal", goal_text),
            )
            if segment.text
        )
        return snapshot, segments

    def _select_status_variant(
        self,
        width: int,
        segments: tuple[StatusSegment, ...],
        *,
        prefix: StatusSegment | None = None,
    ) -> tuple[str, tuple[StatusSegment, ...]]:
        by_kind = {segment.kind: segment for segment in segments}
        prefix_segments = (prefix,) if prefix is not None and prefix.text else ()

        for variant in _STATUS_VARIANTS:
            selected = tuple(
                by_kind[kind]
                for kind in variant
                if kind in by_kind
            )
            candidate = prefix_segments + selected
            if not candidate:
                return "", ()
            summary = self._status_summary_from_segments(candidate)
            if cell_len(summary) <= width:
                return summary, candidate

        fallback = prefix_segments or tuple(
            segment for segment in segments if segment.kind == "model"
        )[:1]
        if not fallback:
            return "", ()
        summary = _clip_cells(self._status_summary_from_segments(fallback), width)
        return summary, ()

    @staticmethod
    def _status_summary_from_segments(segments: tuple[StatusSegment, ...]) -> str:
        return "  " + " | ".join(segment.text for segment in segments if segment.text)

    def _status_text_from_segments(
        self,
        summary: str,
        segments: tuple[StatusSegment, ...],
    ) -> Text:
        if not summary:
            return Text()
        if not segments:
            return Text(summary, style="#8F9BA8")
        if summary != self._status_summary_from_segments(segments):
            return Text(summary, style="#8F9BA8")
        text = Text("  ")
        appended = False
        for segment in segments:
            if not segment.text:
                continue
            if appended:
                text.append(" | ", style=_STATUS_STYLES["separator"])
            if segment.kind == "workflow":
                _append_rainbow(text, segment.text)
            else:
                text.append(segment.text, style=_STATUS_STYLES.get(segment.kind, "#8F9BA8"))
            appended = True
        return text


def _append_rainbow(text: Text, value: str) -> None:
    color_index = 0
    for char in value:
        if char.isspace():
            text.append(char, style=_STATUS_STYLES["workflow"])
            continue
        text.append(char, style=_WORKFLOW_RAINBOW[color_index % len(_WORKFLOW_RAINBOW)])
        color_index += 1


def _call_workflows(func: object) -> list[str]:
    if not callable(func):
        return []
    try:
        value = func()
    except Exception:
        return []
    if not value:
        return []
    if isinstance(value, str):
        items = [value]
    else:
        try:
            items = list(value)
        except TypeError:
            return []
    return [str(item).strip() for item in items if str(item).strip()]
