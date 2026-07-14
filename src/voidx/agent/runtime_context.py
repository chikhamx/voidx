"""Structured runtime context assembly for LLM calls."""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from datetime import datetime
import hashlib
import json
import platform
from typing import Any, Iterable

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, ConfigDict, Field

from voidx.agent.prompts import BaseSystemPrompt, WorkflowRuntimePrompt
from voidx.agent.todo_state import sanitize_todo_replay_messages
from voidx.agent.message_rows import RowMessageCacheEntry
from voidx.agent.task_state import GoalSpec, TodoRunState
from voidx.config import Config, UserProfile
from voidx.runtime.intent import InteractionMode, TaskIntent
from voidx.skills.service import (
    has_skill_tool_context,
    strip_skill_tool_context,
)
from voidx.workflow.service import (
    is_workflow_context_content,
    workflow_exit_summaries,
)
from voidx.workflow.types import WorkflowRunState, WorkflowRunStatus

_CONTEXT_MARKER = "VOIDX_RUNTIME_CONTEXT"
# Retained only to strip persisted overlays from sessions created before the
# Goal Resolution Guide was removed from the message stream.
_GOAL_RESOLUTION_GUIDE_MARKER = "VOIDX_GOAL_RESOLUTION_GUIDE"
COMPACTION_GUIDE_MARKER = "VOIDX_COMPACTION_GUIDE"
_TASK_CONTEXT_DELIMITER = "\n\n## Task Context\n"
_LEGACY_USER_MESSAGE_DELIMITER = "\n\n## User Message\n"


@dataclass
class ContextCompilerCache:
    stable_prefix_key: str = ""
    stable_system_content: str = ""
    stable_system_message: SystemMessage | None = None
    row_messages: dict[int, RowMessageCacheEntry] = dataclass_field(default_factory=dict)


class ExecutionPolicy(BaseModel):
    permission_preset: str
    extra_write_paths: list[str] = Field(default_factory=list)

    @property
    def sandbox_mode(self) -> str:
        from voidx.config import PermissionPreset
        try:
            return PermissionPreset(self.permission_preset).sandbox_mode
        except ValueError:
            return "workspace-write"

    @property
    def approval_policy(self) -> str:
        from voidx.config import PermissionPreset
        try:
            return PermissionPreset(self.permission_preset).approval_policy
        except ValueError:
            return "untrusted"

    @classmethod
    def from_config(cls, config: Config) -> "ExecutionPolicy":
        from voidx.memory.store import DATA_DIR

        extra = [
            *config.sandbox_writable_files,
            *config.sandbox_writable_dirs,
        ]
        data_dir = str(DATA_DIR.resolve())
        if data_dir not in extra:
            extra.append(data_dir)
        return cls(
            permission_preset=config.permission_preset.value,
            extra_write_paths=extra,
        )


class RuntimeEnvelope(BaseModel):
    workspace: str
    platform: str
    execution_policy: ExecutionPolicy
    user_profile: UserProfile = Field(default_factory=UserProfile)


class ContextSection(BaseModel):
    name: str
    content: str


class RuntimeContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    sections: list[ContextSection]
    task_sections: list[ContextSection] = Field(default_factory=list)
    system_content: str | None = None
    system_message: SystemMessage | None = Field(default=None, exclude=True)

    def section_names(self) -> list[str]:
        names = [section.name for section in self.sections]
        names.extend(section.name for section in self.task_sections)
        return names

    def render_system(self) -> str:
        if self.system_content is not None:
            return self.system_content
        return _render_sections(self.sections)

    def render_task_context(self) -> str:
        return _render_sections(self.task_sections)

    def apply_to_messages(self, messages: list[BaseMessage]) -> None:
        ContextCompiler(self).apply_to_messages(messages)


class ContextCompiler:
    """Compile structured context sections into one clean LLM message frame."""

    def __init__(self, context: RuntimeContext) -> None:
        self.context = context

    def compile_messages(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        semantic_messages = raw_semantic_messages(messages)
        skill_context_cutoff = _tool_skill_context_cutoff(semantic_messages)
        semantic_messages = _strip_historical_tool_skill_context(semantic_messages, skill_context_cutoff)
        semantic_messages = sanitize_todo_replay_messages(semantic_messages)

        system_content = self.context.render_system()
        cached_system = self.context.system_message
        prefix = (
            cached_system
            if cached_system is not None and cached_system.content == system_content
            else SystemMessage(content=system_content)
        )
        task_context = self.context.render_task_context()
        if task_context:
            if not semantic_messages:
                semantic_messages.append(HumanMessage(content=task_context))
            else:
                semantic_messages[-1] = _prepend_task_context(semantic_messages[-1], task_context)

        # Compile order: SystemMessage, semantic history.
        result = [prefix]
        result.extend(semantic_messages)
        return result

    def apply_to_messages(self, messages: list[BaseMessage]) -> None:
        messages[:] = self.compile_messages(messages)


class RuntimeContextBuilder:
    def __init__(
        self,
        *,
        config: Config,
        workspace: str,
        base_system_prompt: str | BaseSystemPrompt | None = None,
        workflow_runtime: WorkflowRuntimePrompt | str | None = None,
        persona_prompt: str = "",
        persona: str,
        interaction_mode: str | InteractionMode,
        instructions: Iterable[str] = (),
        workflow_runs: Iterable[WorkflowRunState] = (),
        active_workflow_summaries: Iterable[str] = (),
        summary: str | None = None,
        task_state: "TaskState | None" = None,
        session_date: str | None = None,
        turn_state: str = "initial",
    ) -> None:
        from voidx.runtime.task_state import TaskState as _TaskState

        ts = task_state if isinstance(task_state, _TaskState) else _TaskState()
        self.config = config
        self.workspace = workspace
        self.structured_prompts = isinstance(base_system_prompt, BaseSystemPrompt) or workflow_runtime is not None
        self.base_system_prompt = _render_prompt_input(base_system_prompt)
        self.workflow_runtime = _render_prompt_input(workflow_runtime)
        self.persona_prompt = persona_prompt.strip()
        self.persona = persona.strip()
        self.interaction_mode = InteractionMode.parse(interaction_mode)
        self.instructions = [item for item in instructions if item.strip()]
        self.workflow_runs = list(workflow_runs)
        self.active_workflow_summaries = [item for item in active_workflow_summaries if item.strip()]
        self.summary = summary.strip() if summary else ""
        self.task_intent = ts.current_intent
        self.current_goal = ts.current_goal
        self.workflow_route = ts.workflow_route
        self.todo_state = ts.todo_state
        self.user_profile = config.user_profile
        self.turn_state = turn_state.strip() or "initial"
        now = datetime.now().astimezone()
        self.session_date = (session_date or now.strftime("%Y-%m-%d %Z")).strip()

    def build(self) -> RuntimeContext:
        return RuntimeContext(
            sections=self._build_stable_sections(),
            task_sections=self._build_task_sections(),
        )

    def build_incremental(
        self,
        cache: ContextCompilerCache,
    ) -> tuple[RuntimeContext, ContextCompilerCache]:
        sections = self._build_stable_sections()
        stable_key = _stable_hash([
            {"name": section.name, "content": section.content}
            for section in sections
        ])
        if cache.stable_prefix_key == stable_key and cache.stable_system_content:
            system_content = cache.stable_system_content
            system_message = cache.stable_system_message
        else:
            system_content = _render_sections(sections)
            system_message = SystemMessage(content=system_content)
            cache.stable_prefix_key = stable_key
            cache.stable_system_content = system_content
            cache.stable_system_message = system_message

        return RuntimeContext(
            sections=sections,
            task_sections=self._build_task_sections(),
            system_content=system_content,
            system_message=system_message,
        ), cache

    def _build_stable_sections(self) -> list[ContextSection]:
        sections = [
            ContextSection(name="Base System", content=self.base_system_prompt),
        ]
        if self.persona_prompt:
            persona_section = "Persona" if self.structured_prompts else "Agent Role"
            sections.append(ContextSection(name=persona_section, content=self.persona_prompt))
        if self.workflow_runtime:
            sections.append(ContextSection(
                name="Workflow Runtime",
                content=_strip_section_heading("Workflow Runtime", self.workflow_runtime),
            ))
        sections.append(ContextSection(
            name="Runtime State",
            content=self._runtime_state_content(),
        ))

        if self.instructions:
            sections.append(ContextSection(
                name="Project Facts",
                content="\n\n".join(self.instructions),
            ))
        sections.append(ContextSection(name="Session Time", content=self.session_date))
        if self.summary:
            sections.append(ContextSection(
                name="Long Summary",
                content=self.summary,
            ))
        return sections

    def _build_task_sections(self) -> list[ContextSection]:
        return [
            ContextSection(name="Current Task State", content=self._current_task_state()),
        ]

    def _runtime_state_content(self) -> str:
        envelope = RuntimeEnvelope(
            workspace=self.workspace,
            platform=_platform_info(),
            execution_policy=ExecutionPolicy.from_config(self.config),
            user_profile=self.user_profile,
        )
        return _render_envelope(envelope)

    def _current_task_state(self) -> str:
        lines = [
            f"- Current persona: {self.persona}",
            f"- Intent: {self.task_intent.value}",
            f"- Turn state: {self.turn_state}",
        ]
        if self.current_goal is not None:
            lines.extend([
                f"- Goal: {self.current_goal.desc or 'not set'}",
            ])
        if self.active_workflow_summaries:
            lines.append(f"- Active workflow nodes: {'; '.join(self.active_workflow_summaries)}")
        if self.workflow_route is not None and (self.workflow_route.join or self.workflow_route.leave):
            join = self.workflow_route.join or "not set"
            leave = self.workflow_route.leave or "not set"
            lines.append(f"- Workflow route: {join} -> {leave}")
        active_workflow_names = self._active_workflow_node_names()
        if active_workflow_names:
            lines.append(f"- Active workflows: {'; '.join(active_workflow_names)}")
        for workflow_name in active_workflow_names:
            exits = workflow_exit_summaries(workflow_name)
            if exits:
                lines.append(f"- Workflow transitions [{workflow_name}]: {'; '.join(exits)}")
        todo_lines = _render_task_state_todo_lines(self.todo_state)
        if todo_lines:
            lines.extend(todo_lines)
        if self.interaction_mode == InteractionMode.PLAN:
            lines.append("- Constraint: plan mode blocks write/insert/replace/edit, write-capable bash, and implement delegation.")
        elif self.interaction_mode == InteractionMode.GOAL:
            lines.append("- Constraint: goal mode should keep work scoped to the current user goal and task state.")
        return "\n".join(lines)

    def _active_workflow_node_names(self) -> list[str]:
        names: list[str] = []
        for run in self.workflow_runs:
            if run.status == WorkflowRunStatus.ACTIVE and run.name.strip():
                names.append(run.name.strip())
        if names:
            return names
        for summary in self.active_workflow_summaries:
            name = summary.split(" ", 1)[0].strip()
            if name:
                names.append(name)
        return names


def _render_task_state_todo_lines(todo_state_value: object | None) -> list[str]:
    todo_state = _coerce_todo_run_state(todo_state_value)
    if todo_state is None or not todo_state.items:
        return []

    visible = [item for status in ("active", "pending") for item in todo_state.items if item.status == status]
    if not visible:
        return []

    lines = [f"- Todo: {todo_state.summary}"]
    visible_limit = 3
    for item in visible[:visible_limit]:
        content = _truncate_todo_content(item.content)
        item_id = f" {item.id}" if item.id else ""
        alias = f" ({item.status}: {content})" if item.id else ""
        lines.append(f"  - {item.status}{item_id}: {content}{alias}")

    omitted = len(visible) - visible_limit
    if omitted > 0:
        lines.append(f"  - … {omitted} more active/pending todos")
    return lines


def _truncate_todo_content(content: str, limit: int = 80) -> str:
    if len(content) <= limit:
        return content
    return content[:limit] + "…"



def _render_sections(sections: list[ContextSection]) -> str:
    parts = [_CONTEXT_MARKER]
    for section in sections:
        if not section.content.strip():
            continue
        parts.append(f"## {section.name}\n{section.content.strip()}")
    return "\n\n".join(parts)


def _render_prompt_input(value: object) -> str:
    if value is None:
        return ""
    render = getattr(value, "render", None)
    if callable(render):
        return str(render()).strip()
    return str(value).strip()


def _strip_section_heading(name: str, content: str) -> str:
    heading = f"## {name}"
    stripped = content.strip()
    if stripped == heading:
        return ""
    if stripped.startswith(heading + "\n"):
        return stripped[len(heading):].lstrip()
    return stripped


def _is_runtime_context_overlay(content: object) -> bool:
    return (
        # Back-compat: old sessions may contain standalone workflow context
        # HumanMessages from before workflow runtime joined the SystemMessage.
        is_workflow_context_content(content)
        or is_goal_resolution_guide_content(content)
        or is_compaction_guide_content(content)
        or _is_standalone_runtime_context(content)
    )


def is_goal_resolution_guide_content(content: object) -> bool:
    return _starts_with_marker(content, _GOAL_RESOLUTION_GUIDE_MARKER)


def is_compaction_guide_content(content: object) -> bool:
    return _starts_with_marker(content, COMPACTION_GUIDE_MARKER)


def _starts_with_marker(content: object, marker: str) -> bool:
    if isinstance(content, str):
        return content.lstrip().startswith(marker)
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and first.get("type") == "text":
            text = first.get("text", "")
            return isinstance(text, str) and text.lstrip().startswith(marker)
    return False


def _is_standalone_runtime_context(content: object) -> bool:
    if isinstance(content, str):
        return content.startswith(_CONTEXT_MARKER) and not _is_turn_overlay_text(content)
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and first.get("type") == "text":
            text = first.get("text", "")
            return isinstance(text, str) and text.startswith(_CONTEXT_MARKER) and not _is_turn_overlay_text(text)
    return False


def raw_semantic_messages(messages: list[BaseMessage]) -> list[BaseMessage]:
    raw: list[BaseMessage] = []
    for message in messages:
        if isinstance(message, SystemMessage):
            continue
        if isinstance(message, HumanMessage):
            if _is_runtime_context_overlay(message.content):
                continue
            raw.append(_strip_turn_overlay(message))
        else:
            raw.append(_strip_turn_overlay(message))
    return raw


def _strip_historical_tool_skill_context(
    messages: list[BaseMessage],
    cutoff: int,
) -> list[BaseMessage]:
    stripped: list[BaseMessage] = []
    for index, message in enumerate(messages):
        if index < cutoff and isinstance(message, ToolMessage):
            if not has_skill_tool_context(message.content):
                stripped.append(message)
                continue
            content = strip_skill_tool_context(message.content)
            if content != message.content:
                stripped.append(message.model_copy(update={"content": content}))
                continue
        stripped.append(message)
    return stripped


def _tool_skill_context_cutoff(messages: list[BaseMessage]) -> int:
    if not messages:
        return 0
    if isinstance(messages[-1], ToolMessage):
        index = len(messages) - 1
        while index > 0 and isinstance(messages[index - 1], ToolMessage):
            index -= 1
        return index
    return len(messages) - 1


def _strip_turn_overlay(message: BaseMessage) -> BaseMessage:
    content = message.content
    if isinstance(content, str):
        stripped = _strip_turn_overlay_text(content)
        if stripped != content:
            return message.model_copy(update={"content": stripped})
    elif isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and first.get("type") == "text":
            text = first.get("text", "")
            if isinstance(text, str):
                stripped = _strip_turn_overlay_text(text)
                if stripped != text:
                    if stripped:
                        stripped_first = {**first, "text": stripped}
                        return message.model_copy(update={"content": [stripped_first, *content[1:]]})
                    return message.model_copy(update={"content": list(content[1:])})
    return message


def _strip_turn_overlay_text(content: str) -> str:
    if not _is_turn_overlay_text(content):
        return content
    for delimiter in (_TASK_CONTEXT_DELIMITER, _LEGACY_USER_MESSAGE_DELIMITER):
        if delimiter in content:
            return content.split(delimiter, 1)[1]
    for marker in ("## Task Context", "## User Message"):
        header = f"\n\n{marker}"
        if header in content:
            return content.split(header, 1)[1].lstrip("\r\n")
    return content


def _is_turn_overlay_text(content: str) -> bool:
    return content.startswith(_CONTEXT_MARKER) and (
        "\n\n## Task Context" in content
        or "\n\n## User Message" in content
    )


def _render_envelope(envelope: RuntimeEnvelope) -> str:
    policy = envelope.execution_policy
    lines = [
        f"- Workspace: {envelope.workspace}",
        f"- Platform: {envelope.platform}",
        f"- Sandbox: {policy.sandbox_mode}",
        f"- Approval policy: {policy.approval_policy}",
    ]
    if policy.extra_write_paths:
        lines.append(f"- Extra write paths: {', '.join(policy.extra_write_paths)}")
    tone = envelope.user_profile.tone.strip()
    if tone:
        lines.append(f"- Tone instruction: {_tone_instruction(tone)}")
    return "\n".join(lines)


def _coerce_todo_run_state(value: TodoRunState | dict | None) -> TodoRunState | None:
    if value is None:
        return None
    if isinstance(value, TodoRunState):
        return value
    if isinstance(value, dict):
        try:
            return TodoRunState.model_validate(value)
        except ValueError:
            return None
    return None


def _coerce_goal(value: GoalSpec | dict | None) -> GoalSpec | None:
    if value is None:
        return None
    if isinstance(value, GoalSpec):
        return value
    if isinstance(value, dict):
        try:
            return GoalSpec.model_validate(value)
        except ValueError:
            return None
    return None



_TONE_LABELS: dict[str, tuple[str, str, str]] = {
    "concise": (
        "Concise",
        "short and to the point",
        "Prefer short answers. Remove filler and avoid restating obvious context.",
    ),
    "friendly": (
        "Friendly",
        "warm and approachable",
        "Keep phrasing warm and approachable while staying concrete.",
    ),
    "formal": (
        "Formal",
        "professional and structured",
        "Use polished, structured phrasing and avoid casual wording.",
    ),
    "direct": (
        "Direct",
        "straightforward, no fluff",
        "Be direct and practical. Lead with the answer or action.",
    ),
    "technical": (
        "Technical",
        "precise, uses domain terminology",
        "Use precise domain terminology. Prefer concrete specs and implementation details over broad summaries.",
    ),
    "casual": (
        "Casual",
        "relaxed and conversational",
        "Use relaxed conversational phrasing without losing technical accuracy.",
    ),
}



def _tone_instruction(value: str) -> str:
    text = value.strip()
    label = _TONE_LABELS.get(text.lower())
    if label is None:
        return f"Keep the response tone {text}."
    return label[2]


def _platform_info() -> str:
    system = platform.system() or "Unknown"
    machine = platform.machine() or "unknown"
    if system == "Darwin":
        chip = "Apple Silicon" if machine == "arm64" else "Intel"
        return f"macOS {machine} ({chip})"
    if system == "Windows":
        return f"Windows {machine}"
    return f"{system} {machine}"


def _prepend_task_context(message: BaseMessage, task_context: str) -> BaseMessage:
    content = message.content
    header = f"{task_context}\n\n## Task Context"
    if isinstance(content, str):
        new_content = f"{header}\n{content}"
    elif isinstance(content, list):
        new_content = [{"type": "text", "text": header}, *content]
    else:
        new_content = f"{header}\n{content}"
    return message.model_copy(update={"content": new_content})


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
