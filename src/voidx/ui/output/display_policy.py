"""Tool display policy — show / summary / hidden control for UI rendering."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel


class ToolDisplayMode(str, Enum):
    SHOW = "show"
    SUMMARY = "summary"
    HIDDEN = "hidden"


class ToolDisplayRule(BaseModel):
    tool_name: str
    mode: ToolDisplayMode = ToolDisplayMode.SHOW
    summary_max_lines: int = 3
    auto_summary_lines: int = 50
    auto_summary_chars: int = 5000
    replay_sanitize: bool = False


class ToolDisplayPolicy(BaseModel):
    rules: dict[str, ToolDisplayRule] = {}
    default_mode: ToolDisplayMode = ToolDisplayMode.SHOW
    default_summary_max_lines: int = 3
    default_auto_summary_lines: int = 50
    default_auto_summary_chars: int = 5000

    def rule_for(self, tool_name: str) -> ToolDisplayRule:
        return self.rules.get(
            tool_name,
            ToolDisplayRule(
                tool_name=tool_name,
                mode=self.default_mode,
                summary_max_lines=self.default_summary_max_lines,
                auto_summary_lines=self.default_auto_summary_lines,
                auto_summary_chars=self.default_auto_summary_chars,
            ),
        )

    def resolve_display_mode(
        self,
        tool_name: str,
        result_output: str,
        result_ok: bool = True,
    ) -> tuple[ToolDisplayMode, int]:
        rule = self.rule_for(tool_name)
        mode = rule.mode
        summary_lines = rule.summary_max_lines
        auto_lines = rule.auto_summary_lines
        auto_chars = rule.auto_summary_chars

        if mode == ToolDisplayMode.HIDDEN:
            return mode, summary_lines

        if not result_ok:
            return ToolDisplayMode.SHOW, summary_lines

        if mode == ToolDisplayMode.SHOW:
            output_lines = result_output.count("\n") + 1
            if output_lines > auto_lines or len(result_output) > auto_chars:
                mode = ToolDisplayMode.SUMMARY

        return mode, summary_lines

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any],
        defaults: dict[str, ToolDisplayRule] | None = None,
    ) -> ToolDisplayPolicy:
        rules: dict[str, ToolDisplayRule] = {}
        if defaults:
            rules.update(defaults)

        default_mode = ToolDisplayMode(config.get("default_mode", "show"))
        default_summary_max_lines = int(config.get("default_summary_max_lines", 3))
        default_auto_summary_lines = int(config.get("default_auto_summary_lines", 50))
        default_auto_summary_chars = int(config.get("default_auto_summary_chars", 5000))

        for name, rule_cfg in config.get("rules", {}).items():
            if not isinstance(rule_cfg, dict):
                continue
            try:
                rules[name] = ToolDisplayRule(
                    tool_name=name,
                    mode=ToolDisplayMode(rule_cfg.get("mode", default_mode.value)),
                    summary_max_lines=rule_cfg.get("summary_max_lines", default_summary_max_lines),
                    auto_summary_lines=rule_cfg.get("auto_summary_lines", default_auto_summary_lines),
                    auto_summary_chars=rule_cfg.get("auto_summary_chars", default_auto_summary_chars),
                )
            except (ValueError, TypeError):
                continue

        return cls(
            rules=rules,
            default_mode=default_mode,
            default_summary_max_lines=default_summary_max_lines,
            default_auto_summary_lines=default_auto_summary_lines,
            default_auto_summary_chars=default_auto_summary_chars,
        )


DEFAULT_DISPLAY_RULES: dict[str, ToolDisplayRule] = {
    # ── Hidden：runtime-only / barrier / 状态工具 ──
    # These tools are hidden in UI output, but their ToolMessage stays in replay context.
    "todo": ToolDisplayRule(tool_name="todo", mode=ToolDisplayMode.HIDDEN),
    "task_status": ToolDisplayRule(tool_name="task_status", mode=ToolDisplayMode.HIDDEN),
    "document": ToolDisplayRule(tool_name="document", mode=ToolDisplayMode.HIDDEN),
    "checkpoint": ToolDisplayRule(tool_name="checkpoint", mode=ToolDisplayMode.HIDDEN),
    "compact": ToolDisplayRule(tool_name="compact", mode=ToolDisplayMode.HIDDEN),
    "workflow": ToolDisplayRule(tool_name="workflow", mode=ToolDisplayMode.HIDDEN),
    "skill": ToolDisplayRule(tool_name="skill", mode=ToolDisplayMode.HIDDEN),
    "loop": ToolDisplayRule(tool_name="loop", mode=ToolDisplayMode.HIDDEN),
    "goal": ToolDisplayRule(tool_name="goal", mode=ToolDisplayMode.HIDDEN),
    "clarify": ToolDisplayRule(tool_name="clarify", mode=ToolDisplayMode.HIDDEN),
    # ── Summary：搜索/查询类 ──
    "search": ToolDisplayRule(tool_name="search", mode=ToolDisplayMode.SUMMARY, summary_max_lines=5),
    "find": ToolDisplayRule(tool_name="find", mode=ToolDisplayMode.SUMMARY, summary_max_lines=5),
    "websearch": ToolDisplayRule(tool_name="websearch", mode=ToolDisplayMode.SUMMARY, summary_max_lines=5),
    "lsp": ToolDisplayRule(tool_name="lsp", mode=ToolDisplayMode.SUMMARY, summary_max_lines=5),
    # ── Show + 自适应 ──
    "bash": ToolDisplayRule(tool_name="bash", mode=ToolDisplayMode.SHOW, auto_summary_lines=50, auto_summary_chars=10000),
    "powershell": ToolDisplayRule(tool_name="powershell", mode=ToolDisplayMode.SHOW, auto_summary_lines=50, auto_summary_chars=10000),
    "read": ToolDisplayRule(tool_name="read", mode=ToolDisplayMode.SHOW, auto_summary_lines=100),
    "webfetch": ToolDisplayRule(tool_name="webfetch", mode=ToolDisplayMode.SHOW, auto_summary_lines=50, auto_summary_chars=10000),
    # ── Show ──
    "manage": ToolDisplayRule(tool_name="manage", mode=ToolDisplayMode.SHOW),
    "write": ToolDisplayRule(tool_name="write", mode=ToolDisplayMode.SHOW),
    "replace": ToolDisplayRule(tool_name="replace", mode=ToolDisplayMode.SHOW),
    "agent": ToolDisplayRule(tool_name="agent", mode=ToolDisplayMode.SHOW),
    "git": ToolDisplayRule(tool_name="git", mode=ToolDisplayMode.SHOW),
}
