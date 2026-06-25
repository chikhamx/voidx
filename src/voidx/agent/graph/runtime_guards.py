"""Runtime guard state for detecting unproductive agent loops."""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, Field


LOW_VALUE_REPETITIVE_TOOLS = frozenset({"todo", "workflow", "checkpoint"})
REPETITIVE_TOOL_EXEMPTIONS = frozenset({"bash", "read", "grep"})
EVIDENCE_TEXT_LIMIT = 500


class GuardGuidance(BaseModel):
    kind: str
    level: Literal["light", "stern"]
    message: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class GuardDecision(BaseModel):
    action: Literal["allow", "skip", "terminate"] = "allow"
    tool_name: str = ""
    message: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class FailureKey(BaseModel):
    tool_name: str
    normalized_args: str
    error_kind: str

    @property
    def stable_key(self) -> str:
        return "\x1f".join((self.tool_name, self.normalized_args, self.error_kind))

    @property
    def call_key(self) -> str:
        return "\x1f".join((self.tool_name, self.normalized_args))


class ToolFailureLoopState(BaseModel):
    last_key: str = ""
    last_call_key: str = ""
    count: int = 0
    last_error: str = ""
    warned_count: int = 0
    blocked_call_keys: set[str] = Field(default_factory=set)

    def record_success(self, tool_call: dict[str, Any]) -> None:
        tool_name = str(tool_call.get("name") or "")
        self.blocked_call_keys = {
            key for key in self.blocked_call_keys if not key.startswith(f"{tool_name}\x1f")
        }
        if self.last_call_key == tool_call_key(tool_call) or self.last_call_key.startswith(f"{tool_name}\x1f"):
            self.last_key = ""
            self.last_call_key = ""
            self.count = 0
            self.last_error = ""
            self.warned_count = 0

    def record_failure(self, key: FailureKey, error_summary: str) -> GuardGuidance | None:
        stable = key.stable_key
        if stable == self.last_key:
            self.count += 1
        else:
            self.blocked_call_keys.clear()
            self.last_key = stable
            self.last_call_key = key.call_key
            self.count = 1
            self.warned_count = 0
        self.last_error = error_summary

        if self.count == 2 and self.warned_count < 2:
            self.warned_count = 2
            return GuardGuidance(
                kind="tool_failure_loop",
                level="light",
                message=(
                    "The same tool call has failed twice. Do not keep retrying it unchanged.\n"
                    "Either change the approach, summarize the blocker, or ask for the missing input."
                ),
                metadata={"count": self.count, "tool_name": key.tool_name, "error_kind": key.error_kind},
            )
        if self.count == 3 and self.warned_count < 3:
            self.warned_count = 3
            self.blocked_call_keys.add(key.call_key)
            return GuardGuidance(
                kind="tool_failure_loop",
                level="stern",
                message=(
                    "The same tool call has failed 3 times. Stop retrying it now.\n"
                    "Do not call this tool again with the same arguments.\n"
                    "Summarize the failure, choose a materially different approach if one exists,\n"
                    "or explain the blocker and the exact input needed from the user."
                ),
                metadata={"count": self.count, "tool_name": key.tool_name, "error_kind": key.error_kind},
            )
        return None

    def should_block(self, tool_call: dict[str, Any]) -> bool:
        return tool_call_key(tool_call) in self.blocked_call_keys


class ToolCycleSummary(BaseModel):
    tool_names: list[str] = Field(default_factory=list)
    only_tool: str = ""
    call_count: int = 0
    has_progress: bool = False
    evidence_keys: list[str] = Field(default_factory=list)


class RepetitiveToolCycleState(BaseModel):
    recent_cycles: list[ToolCycleSummary] = Field(default_factory=list)
    window_size: int = 2
    warned_tool: str = ""
    skipped_tool: str = ""

    def record_cycle(self, summary: ToolCycleSummary) -> GuardGuidance | None:
        self.recent_cycles.append(summary)
        if len(self.recent_cycles) > self.window_size * 3:
            self.recent_cycles = self.recent_cycles[-self.window_size * 2:]
        stuck, tool_name, count = self.is_stuck()
        if not stuck:
            self.warned_tool = ""
            self.skipped_tool = ""
            return None
        if self.warned_tool == tool_name:
            return None
        self.warned_tool = tool_name
        return GuardGuidance(
            kind="repetitive_tool_cycle",
            level="light",
            message=(
                f"You have only called {tool_name} for the last {count} tool cycles.\n"
                "Avoid repeating state updates. Take one concrete work action next,\n"
                "or briefly explain what is blocking you."
            ),
            metadata={"tool_name": tool_name, "count": count},
        )

    def is_stuck(self) -> tuple[bool, str, int]:
        if len(self.recent_cycles) < self.window_size:
            return False, "", 0
        window = self.recent_cycles[-self.window_size:]
        tool = window[0].only_tool
        if not tool or tool in REPETITIVE_TOOL_EXEMPTIONS:
            return False, "", 0
        if all(item.only_tool == tool for item in window):
            if tool in LOW_VALUE_REPETITIVE_TOOLS and not any(item.has_progress for item in window):
                return True, tool, len(window)
        return False, "", 0

    def decision_for_pending(self, tool_calls: list[dict[str, Any]]) -> GuardDecision:
        only_tool = only_tool_name(tool_calls)
        if not only_tool or only_tool != self.warned_tool or only_tool not in LOW_VALUE_REPETITIVE_TOOLS:
            return GuardDecision()
        if self.skipped_tool == only_tool:
            return GuardDecision(
                action="terminate",
                tool_name=only_tool,
                message=(
                    f"Runtime guard stopped this turn because {only_tool} was repeated "
                    "after a previous runtime guard skip without meaningful progress."
                ),
                metadata={"tool_name": only_tool, "guard": "repetitive_tool_cycle"},
            )
        self.skipped_tool = only_tool
        return GuardDecision(
            action="skip",
            tool_name=only_tool,
            message=(
                f"Runtime guard skipped repeated {only_tool} call. Avoid repeating state updates. "
                "Take one concrete work action next, or explain what is blocking you."
            ),
            metadata={"tool_name": only_tool, "guard": "repetitive_tool_cycle"},
        )


class NoProgressState(BaseModel):
    consecutive: int = 0
    warn_threshold: int = 3
    terminate_threshold: int = 5
    warned: bool = False
    seen_evidence_keys: set[str] = Field(default_factory=set)

    def record_cycle(self, summary: ToolCycleSummary) -> GuardGuidance | None:
        unseen_evidence = [key for key in summary.evidence_keys if key not in self.seen_evidence_keys]
        if summary.has_progress or unseen_evidence:
            self.consecutive = 0
            self.warned = False
            self.seen_evidence_keys.update(summary.evidence_keys)
            return None

        self.consecutive += 1
        self.seen_evidence_keys.update(summary.evidence_keys)
        if self.consecutive >= self.warn_threshold and not self.warned:
            self.warned = True
            return GuardGuidance(
                kind="no_progress",
                level="light",
                message=(
                    f"No meaningful progress has been detected across the last {self.consecutive} model/tool cycles.\n"
                    "Do not start broad new exploration. Summarize what is known, state the blocker,\n"
                    "and either choose one concrete next action or ask the user for input."
                ),
                metadata={"count": self.consecutive},
            )
        return None

    def decision(self) -> GuardDecision:
        if self.consecutive < self.terminate_threshold:
            return GuardDecision()
        return GuardDecision(
            action="terminate",
            message=(
                f"No meaningful progress has been detected across {self.consecutive} model/tool cycles. "
                "Runtime guard stopped this turn; summarize the current state and ask for the missing input."
            ),
            metadata={"guard": "no_progress", "count": self.consecutive},
        )


class WallClockGuardState(BaseModel):
    started_at: float = Field(default_factory=time.monotonic)
    limit_seconds: float = 0.0
    terminated: bool = False
    latest_action: str = ""

    @classmethod
    def for_subagent(cls) -> WallClockGuardState:
        return cls(limit_seconds=1800.0)

    def record_check(
        self,
        *,
        now: float | None = None,
        label: str = "",
        latest_action: str = "",
    ) -> GuardDecision:
        if self.limit_seconds <= 0 or self.terminated:
            return GuardDecision()
        current = time.monotonic() if now is None else now
        elapsed = max(0.0, current - self.started_at)
        if latest_action:
            self.latest_action = latest_action
        if elapsed < self.limit_seconds:
            return GuardDecision()
        self.terminated = True
        return GuardDecision(
            action="terminate",
            message=(
                f"This turn has been running for {format_duration(elapsed)}. "
                "Runtime guard stopped at a safe boundary; summarize the current state, "
                "or ask the user whether to continue."
            ),
            metadata={"guard": "wall_clock", "elapsed_seconds": elapsed, "latest_action": latest_action},
        )


class RuntimeGuardState(BaseModel):
    tool_failures: ToolFailureLoopState = Field(default_factory=ToolFailureLoopState)
    repetitive_tools: RepetitiveToolCycleState = Field(default_factory=RepetitiveToolCycleState)
    no_progress: NoProgressState = Field(default_factory=NoProgressState)
    wall_clock: WallClockGuardState = Field(default_factory=WallClockGuardState)


def build_failure_key(tool_call: dict[str, Any], result: Any) -> FailureKey:
    return FailureKey(
        tool_name=str(tool_call.get("name") or ""),
        normalized_args=normalize_tool_args(str(tool_call.get("name") or ""), tool_call.get("args") or {}),
        error_kind=error_kind_from_result(result),
    )


def tool_call_key(tool_call: dict[str, Any]) -> str:
    tool_name = str(tool_call.get("name") or "")
    return "\x1f".join((tool_name, normalize_tool_args(tool_name, tool_call.get("args") or {})))


def normalize_tool_args(tool_name: str, args: dict[str, Any]) -> str:
    if tool_name in {"read", "file", "write", "replace", "lsp_format"}:
        return str(args.get("file_path") or "")
    if tool_name == "lsp":
        return stable_json({
            "operation": args.get("operation"),
            "file_path": args.get("file_path"),
            "line": args.get("line"),
            "character": args.get("character"),
        })
    if tool_name == "grep":
        return stable_json({
            "pattern": args.get("pattern"),
            "path": args.get("path"),
            "include": args.get("include"),
        })
    if tool_name == "bash":
        return " ".join(str(args.get("command") or "").split())
    if tool_name == "agent":
        goal_resolution = args.get("goal_resolution")
        plan = goal_resolution.get("plan") if isinstance(goal_resolution, dict) else {}
        goal = goal_resolution.get("goal") if isinstance(goal_resolution, dict) else {}
        return stable_json({
            "agent": args.get("agent"),
            "mode": args.get("mode"),
            "task": args.get("task"),
            "target": args.get("target"),
            "result_preset": args.get("result_preset"),
            "goal": goal,
            "plan": plan,
        })
    payload = stable_json(args)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def error_kind_from_result(result: Any) -> str:
    metadata = getattr(result, "metadata", {}) or {}
    explicit = metadata.get("error_kind")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    if metadata.get("validation_error"):
        return "validation_error"
    text = f"{getattr(result, 'summary', '')}\n{getattr(result, 'output', '')}".lower()
    if "permission denied" in text:
        return "permission_denied"
    if "sandbox" in text and ("denied" in text or "blocked" in text):
        return "sandbox_denied"
    if "file not found" in text or "path not found" in text:
        return "file_not_found"
    if "stale" in text:
        return "stale_file"
    if "tool execution error" in text:
        return "tool_exception"
    if (
        "exception:" in text
        or "exception occurred" in text
        or ("exception" in text and "traceback" in text)
    ):
        return "tool_exception"
    return "unknown_error"


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def todo_status_signature(todo_state: Any) -> tuple[int, int, int, int]:
    """(done, in_progress, pending, cancelled) count signature."""
    if todo_state is None:
        return (0, 0, 0, 0)
    
    # Try to get counts from TodoRunState
    if hasattr(todo_state, "done"):
        return (
            getattr(todo_state, "done", 0),
            getattr(todo_state, "in_progress", 0),
            getattr(todo_state, "pending", 0),
            getattr(todo_state, "cancelled", 0),
        )
    
    # Fallback for dict representation
    if isinstance(todo_state, dict):
        return (
            todo_state.get("done", 0),
            todo_state.get("in_progress", 0),
            todo_state.get("pending", 0),
            todo_state.get("cancelled", 0),
        )
    
    return (0, 0, 0, 0)


def cycle_summary_from_tools(
    executed: list[Any],
    *,
    previous_todo_state: Any = None,
    next_todo_state: Any = None,
    workflow_changed: bool = False,
    result_ok=None,
) -> ToolCycleSummary:
    calls = [_item_tool_call(item) for item in executed]
    calls = [call for call in calls if call]
    tool_names = [str(call.get("name") or "") for call in calls]
    only = only_tool_name(calls)
    ok = result_ok or (lambda result: not (getattr(result, "metadata", {}) or {}).get("error"))
    has_progress = False
    evidence_keys: list[str] = []
    if workflow_changed:
        has_progress = True
    if todo_status_signature(previous_todo_state) != todo_status_signature(next_todo_state):
        has_progress = True
    for item in executed:
        tool_call = _item_tool_call(item)
        if not tool_call:
            continue
        tool_name = str(tool_call.get("name") or "")
        result = _item_result(item)
        if getattr(result, "diff", None) and ok(result):
            has_progress = True
            break
        if tool_name and tool_name not in LOW_VALUE_REPETITIVE_TOOLS and ok(result):
            evidence_key = _tool_evidence_key(tool_call, result)
            if evidence_key:
                evidence_keys.append(evidence_key)
    return ToolCycleSummary(
        tool_names=tool_names,
        only_tool=only,
        call_count=len(tool_names),
        has_progress=has_progress,
        evidence_keys=sorted(set(evidence_keys)),
    )


def _item_tool_call(item: Any) -> dict[str, Any] | None:
    if isinstance(item, dict):
        value = item.get("tool_call")
    else:
        value = getattr(item, "tool_call", None)
    return value if isinstance(value, dict) else None


def _item_result(item: Any) -> Any:
    if isinstance(item, dict):
        return item.get("result")
    return getattr(item, "result", None)


def only_tool_name(tool_calls: list[dict[str, Any]]) -> str:
    names = [str(call.get("name") or "") for call in tool_calls if call.get("name")]
    unique = set(names)
    if len(unique) == 1:
        return names[0]
    return ""


def _tool_evidence_key(tool_call: dict[str, Any], result: Any) -> str:
    tool_name = str(tool_call.get("name") or "")
    normalized_args = normalize_tool_args(tool_name, tool_call.get("args") or {})
    payload = stable_json(
        {
            "tool": tool_name,
            "args": normalized_args,
            "summary": _truncate_evidence_text(getattr(result, "summary", "") or ""),
            "output": _truncate_evidence_text(getattr(result, "output", "") or ""),
            "diff": _truncate_evidence_text(getattr(result, "diff", "") or ""),
        }
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _truncate_evidence_text(value: str) -> str:
    return str(value)[:EVIDENCE_TEXT_LIMIT]


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, remainder = divmod(total, 60)
    if minutes:
        return f"{minutes}m{remainder:02d}s"
    return f"{remainder}s"
