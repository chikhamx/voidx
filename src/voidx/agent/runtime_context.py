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

from voidx.agent.todo_state import sanitize_todo_replay_messages
from voidx.agent.message_rows import RowMessageCacheEntry
from voidx.agent.task_state import Goal, TodoRunState
from voidx.config import ApprovalReviewer, Config, UserProfile
from voidx.runtime.intent import InteractionMode, TaskIntent, infer_task_intent
from voidx.skills.service import (
    has_skill_tool_context,
    is_skill_context_content,
    skill_context_cache_key,
    strip_skill_tool_context,
)
from voidx.workflow.service import (
    is_workflow_context_content,
    workflow_exit_summaries,
    workflow_context_cache_key,
)
from voidx.workflow.types import WorkflowRunState, WorkflowRunStatus

_CONTEXT_MARKER = "VOIDX_RUNTIME_CONTEXT"
_GOAL_RESOLUTION_GUIDE_MARKER = "VOIDX_GOAL_RESOLUTION_GUIDE"
COMPACTION_GUIDE_MARKER = "VOIDX_COMPACTION_GUIDE"
_USER_MESSAGE_DELIMITER = "\n\n## User Message\n"


@dataclass
class ContextCompilerCache:
    stable_prefix_key: str = ""
    stable_system_content: str = ""
    stable_system_message: SystemMessage | None = None
    workflow_context_key: str = ""
    workflow_context_content: str = ""
    workflow_context_message: HumanMessage | None = None
    skill_context_key: str = ""
    skill_context_content: str = ""
    skill_context_message: HumanMessage | None = None
    goal_resolution_guide_key: str = ""
    goal_resolution_guide_content: str = ""
    goal_resolution_guide_message: HumanMessage | None = None
    row_messages: dict[int, RowMessageCacheEntry] = dataclass_field(default_factory=dict)


class ExecutionPolicy(BaseModel):
    sandbox_mode: str
    approval_policy: str
    approval_reviewer: str = ApprovalReviewer.USER.value
    extra_write_paths: list[str] = Field(default_factory=list)

    @classmethod
    def from_config(cls, config: Config) -> "ExecutionPolicy":
        return cls(
            sandbox_mode=config.sandbox_mode.value,
            approval_policy=config.approval_policy.value,
            approval_reviewer=config.approval_reviewer.value,
            extra_write_paths=list(config.sandbox_workspace_write),
        )


class RuntimeEnvelope(BaseModel):
    workspace: str
    provider: str
    model: str
    interaction_mode: InteractionMode
    permission_profile: str
    execution_policy: ExecutionPolicy
    user_profile: UserProfile = Field(default_factory=UserProfile)


class ContextSection(BaseModel):
    name: str
    content: str


class RuntimeContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    sections: list[ContextSection]
    task_sections: list[ContextSection] = Field(default_factory=list)
    workflow_context_content: str = ""
    skill_context_content: str = ""
    goal_resolution_guide_content: str = ""
    system_content: str | None = None
    system_message: SystemMessage | None = Field(default=None, exclude=True)
    workflow_context_message: HumanMessage | None = Field(default=None, exclude=True)
    skill_context_message: HumanMessage | None = Field(default=None, exclude=True)
    goal_resolution_guide_message: HumanMessage | None = Field(default=None, exclude=True)

    def section_names(self) -> list[str]:
        names = [section.name for section in self.sections]
        if self.workflow_context_content:
            names.append("Workflow Context")
        if self.skill_context_content:
            names.append("Skill Context")
        if self.goal_resolution_guide_content:
            names.append("Goal Resolution Guide")
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
        current_user_index = _last_user_index(semantic_messages)
        semantic_messages = _strip_historical_tool_skill_context(
            semantic_messages,
            current_user_index,
        )
        semantic_messages = sanitize_todo_replay_messages(semantic_messages)
        current_user_index = _last_user_index(semantic_messages)

        system_content = self.context.render_system()
        cached_system = self.context.system_message
        prefix = (
            cached_system
            if cached_system is not None and cached_system.content == system_content
            else SystemMessage(content=system_content)
        )
        task_context = self.context.render_task_context()
        if task_context:
            if current_user_index is None:
                semantic_messages.append(HumanMessage(content=task_context))
            else:
                current = semantic_messages[current_user_index]
                semantic_messages[current_user_index] = _prepend_task_context(current, task_context)
        guide_msg = self._context_message(
            self.context.goal_resolution_guide_content,
            self.context.goal_resolution_guide_message,
        )
        if guide_msg is not None:
            current_user_index = _last_user_index(semantic_messages)
            if current_user_index is None:
                semantic_messages.append(guide_msg)
            else:
                semantic_messages.insert(current_user_index, guide_msg)

        # Compile order: SystemMessage, workflow context, skill context, semantic history
        result = [prefix]
        wf_msg = self._context_message(self.context.workflow_context_content, self.context.workflow_context_message)
        if wf_msg is not None:
            result.append(wf_msg)
        sk_msg = self._context_message(self.context.skill_context_content, self.context.skill_context_message)
        if sk_msg is not None:
            result.append(sk_msg)
        result.extend(semantic_messages)
        return result

    def apply_to_messages(self, messages: list[BaseMessage]) -> None:
        messages[:] = self.compile_messages(messages)

    @staticmethod
    def _context_message(content: str, cached: HumanMessage | None) -> HumanMessage | None:
        content = content.strip()
        if not content:
            return None
        if cached is not None and cached.content == content:
            return cached
        return HumanMessage(content=content)


class RuntimeContextBuilder:
    def __init__(
        self,
        *,
        config: Config,
        workspace: str,
        base_system_prompt: str | None = None,
        persona_prompt: str = "",
        mode_prompt: str = "",
        runtime_constraints: str = "",
        tool_contract: str = "",
        persona: str,
        interaction_mode: str | InteractionMode,
        instructions: Iterable[str] = (),
        workflow_context_content: str = "",
        skill_context_content: str = "",
        workflow_runs: Iterable[WorkflowRunState] = (),
        active_workflow_summaries: Iterable[str] = (),
        summary: str | None = None,
        current_user_text: str = "",
        task_state: "TaskState | None" = None,
        session_date: str | None = None,
        include_goal_resolution_guide: bool = False,
    ) -> None:
        from voidx.runtime.task_state import TaskState as _TaskState

        ts = task_state if isinstance(task_state, _TaskState) else _TaskState()
        self.config = config
        self.workspace = workspace
        self.base_system_prompt = (base_system_prompt or "").strip()
        self.persona_prompt = persona_prompt.strip()
        self.mode_prompt = mode_prompt.strip()
        self.runtime_constraints = runtime_constraints.strip()
        self.tool_contract = tool_contract.strip()
        self.persona = persona.strip()
        self.interaction_mode = InteractionMode.parse(interaction_mode)
        self.instructions = [item for item in instructions if item.strip()]
        self.workflow_context_content = workflow_context_content.strip()
        self.skill_context_content = skill_context_content.strip()
        self.workflow_runs = list(workflow_runs)
        self.active_workflow_summaries = [item for item in active_workflow_summaries if item.strip()]
        self.summary = summary.strip() if summary else ""
        self.current_user_text = current_user_text.strip()
        self.task_intent = ts.current_intent
        self.pending_approval = ts.pending_approval
        self.current_goal = ts.current_goal
        self.recent_user_texts = list(ts.recent_user_texts)
        self.user_profile = config.user_profile
        now = datetime.now().astimezone()
        self.session_date = (session_date or now.strftime("%Y-%m-%d %Z")).strip()
        self.include_goal_resolution_guide = include_goal_resolution_guide

    def build(self) -> RuntimeContext:
        goal_guide_content = self._goal_resolution_guide_content()
        return RuntimeContext(
            sections=self._build_stable_sections(),
            task_sections=self._build_task_sections(),
            workflow_context_content=self.workflow_context_content,
            workflow_context_message=(
                HumanMessage(content=self.workflow_context_content)
                if self.workflow_context_content
                else None
            ),
            skill_context_content=self.skill_context_content,
            skill_context_message=(
                HumanMessage(content=self.skill_context_content)
                if self.skill_context_content
                else None
            ),
            goal_resolution_guide_content=goal_guide_content,
            goal_resolution_guide_message=(
                HumanMessage(content=goal_guide_content)
                if goal_guide_content
                else None
            ),
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

        wf_content, wf_message = self._incremental_context_content(
            cache, "workflow", self.workflow_context_content,
        )
        sk_content, sk_message = self._incremental_context_content(
            cache, "skill", self.skill_context_content,
        )
        goal_guide_content, goal_guide_message = self._incremental_context_content(
            cache, "goal_resolution_guide", self._goal_resolution_guide_content(),
        )

        return RuntimeContext(
            sections=sections,
            task_sections=self._build_task_sections(),
            workflow_context_content=wf_content,
            skill_context_content=sk_content,
            goal_resolution_guide_content=goal_guide_content,
            system_content=system_content,
            system_message=system_message,
            workflow_context_message=wf_message,
            skill_context_message=sk_message,
            goal_resolution_guide_message=goal_guide_message,
        ), cache

    @staticmethod
    def _incremental_context_content(
        cache: ContextCompilerCache,
        kind: str,
        content: str,
    ) -> tuple[str, HumanMessage | None]:
        content = content.strip()
        if kind == "workflow":
            key_attr = "workflow_context_key"
            content_attr = "workflow_context_content"
            message_attr = "workflow_context_message"
        elif kind == "goal_resolution_guide":
            key_attr = "goal_resolution_guide_key"
            content_attr = "goal_resolution_guide_content"
            message_attr = "goal_resolution_guide_message"
        else:
            key_attr = "skill_context_key"
            content_attr = "skill_context_content"
            message_attr = "skill_context_message"

        if not content:
            setattr(cache, key_attr, "")
            setattr(cache, content_attr, "")
            setattr(cache, message_attr, None)
            return "", None

        key = _runtime_context_cache_key(content)
        if getattr(cache, key_attr) == key and getattr(cache, content_attr):
            return getattr(cache, content_attr), getattr(cache, message_attr)

        message = HumanMessage(content=content)
        setattr(cache, key_attr, key)
        setattr(cache, content_attr, content)
        setattr(cache, message_attr, message)
        return content, message

    def _build_stable_sections(self) -> list[ContextSection]:
        sections = [
            ContextSection(name="Base System", content=self.base_system_prompt),
        ]
        if self.persona_prompt:
            sections.append(ContextSection(name="Agent Role", content=self.persona_prompt))
        if self.runtime_constraints:
            sections.append(ContextSection(name="Runtime Constraints", content=self.runtime_constraints))
        if self.mode_prompt:
            sections.append(ContextSection(name="Mode", content=self.mode_prompt))
        if self.tool_contract:
            sections.append(ContextSection(name="Tool Contract", content=self.tool_contract))
        sections.append(ContextSection(
            name="Workspace Facts",
            content=f"- Current workspace: {self.workspace}\n- Platform: {_platform_info()}",
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
        envelope = RuntimeEnvelope(
            workspace=self.workspace,
            provider=self.config.model.provider,
            model=self.config.model.model,
            interaction_mode=self.interaction_mode,
            permission_profile=self.config.permission_mode.value,
            execution_policy=ExecutionPolicy.from_config(self.config),
            user_profile=self.user_profile,
        )

        task_sections = [
            ContextSection(name="Runtime State", content=_render_envelope(envelope)),
        ]
        task_sections.append(ContextSection(
            name="Current Task State",
            content=self._current_task_state(),
        ))

        return task_sections

    def _goal_resolution_guide_content(self) -> str:
        if not self.include_goal_resolution_guide:
            return ""
        current_goal = (
            self.current_goal.model_dump(mode="json")
            if self.current_goal is not None
            else None
        )
        pending = (
            self.pending_approval.model_dump(mode="json")
            if self.pending_approval is not None
            else None
        )
        context = {
            "workspace": self.workspace,
            "session_time": self.session_date,
            "interaction_mode": self.interaction_mode.value,
            "current_intent": self.task_intent.value,
            "current_goal": current_goal,
            "pending_approval": pending,
            "recent_user_texts": self.recent_user_texts,
            "latest_user_text": self.current_user_text,
        }
        return (
            f"{_GOAL_RESOLUTION_GUIDE_MARKER}\n"
            "Scope: turn-initial-goal-resolution\n\n"
            "Before responding, determine the user's intent and goal for this turn.\n"
            "Rules:\n"
            "- Use intent=general only for non-code, non-workspace conversation.\n"
            "- Use intent=coding for codebase inspection, design, docs, review, debugging, or edits.\n"
            "- Do not infer write permission from analysis words like look at, inspect, 看看, 分析, or 建议.\n"
            "- If the user's intent clearly indicates which workflow should be active next, call advance_workflow to transition.\n"
            "- Do not call advance_workflow based on vague or ambiguous approval.\n\n"
            f"Current context:\n{json.dumps(context, ensure_ascii=False, indent=2)}"
        )

    def _current_task_state(self) -> str:
        lines = [
            f"- Current persona: {self.persona}",
            f"- Intent: {self.task_intent.value}",
        ]
        if self.current_goal is not None:
            lines.extend([
                f"- Goal type: {self.current_goal.type.value}",
                f"- Goal target: {self.current_goal.target or 'not set'}",
                f"- Goal expected result: {self.current_goal.expected_result or 'not set'}",
                f"- User requested write: {str(self.current_goal.user_requested_write).lower()}",
                f"- Goal needs confirmation: {str(self.current_goal.needs_confirmation).lower()}",
            ])
        if self.active_workflow_summaries:
            lines.append(f"- Active workflow nodes: {'; '.join(self.active_workflow_summaries)}")
        if self.workflow_runs:
            lines.append(f"- Workflow run state: {'; '.join(run.state_summary() for run in self.workflow_runs)}")
        for workflow_name in self._active_workflow_node_names():
            exits = workflow_exit_summaries(workflow_name)
            if exits:
                lines.append(f"- Workflow exits [{workflow_name}]: {'; '.join(exits)}")
        language = self.user_profile.language.strip()
        if language:
            display = _language_display(language)
            target = _language_target(language)
            lines.append(f"- User language preference: {display}")
            lines.append(
                f"- Language instruction: Prefer responding in {target} unless the user explicitly asks otherwise."
            )
        tone = self.user_profile.tone.strip()
        if tone:
            lines.append(f"- User tone preference: {tone}")
            lines.append(f"- Tone instruction: {_tone_instruction(tone)}")
        pending = _render_pending_approval(self.pending_approval)
        if pending:
            lines.append(f"- Pending approval: {pending}")
        if self.current_goal is not None and self.current_goal.type.value == "design" and pending:
            lines.append("- Suggestion: use plan_checkpoint to get explicit approval before implementing.")
        if self.current_user_text:
            first_line = self.current_user_text.splitlines()[0][:160]
            lines.append(f"- Latest user request: {first_line}")
        if self.interaction_mode == InteractionMode.PLAN:
            lines.append("- Constraint: plan mode blocks write/edit/lsp_format, write-capable bash, and implement delegation.")
        elif self.interaction_mode == InteractionMode.GOAL:
            lines.append("- Constraint: goal mode should keep work scoped to the current user goal and task state.")
        lines.append("- Permission gate: tool calls are governed by the current permission mode, sandbox, and interaction mode.")
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


def _render_sections(sections: list[ContextSection]) -> str:
    parts = [_CONTEXT_MARKER]
    for section in sections:
        if not section.content.strip():
            continue
        parts.append(f"## {section.name}\n{section.content.strip()}")
    return "\n\n".join(parts)


def _is_runtime_context_overlay(content: object) -> bool:
    return (
        is_skill_context_content(content)
        or is_workflow_context_content(content)
        or is_goal_resolution_guide_content(content)
        or is_compaction_guide_content(content)
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


def _runtime_context_cache_key(content: str) -> str:
    if is_workflow_context_content(content):
        return workflow_context_cache_key(content)
    return skill_context_cache_key(content)


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
            raw.append(message)
    return raw


def _strip_historical_tool_skill_context(
    messages: list[BaseMessage],
    current_user_index: int | None,
) -> list[BaseMessage]:
    cutoff = current_user_index if current_user_index is not None else len(messages)
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


def _strip_turn_overlay(message: HumanMessage) -> HumanMessage:
    content = message.content
    if isinstance(content, str):
        stripped = _strip_turn_overlay_text(content)
        if stripped != content:
            return message.model_copy(update={"content": stripped})
    elif isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and first.get("type") == "text":
            text = first.get("text", "")
            if isinstance(text, str) and _is_turn_overlay_text(text):
                return message.model_copy(update={"content": list(content[1:])})
    return message


def _strip_turn_overlay_text(content: str) -> str:
    if not _is_turn_overlay_text(content):
        return content
    return content.split(_USER_MESSAGE_DELIMITER, 1)[1]


def _is_turn_overlay_text(content: str) -> bool:
    return content.startswith(_CONTEXT_MARKER) and _USER_MESSAGE_DELIMITER in content


def _render_envelope(envelope: RuntimeEnvelope) -> str:
    policy = envelope.execution_policy
    lines = [
        f"- Workspace: {envelope.workspace}",
        f"- Model: {envelope.provider}/{envelope.model}",
        f"- Interaction mode: {envelope.interaction_mode.value}",
        f"- Permission profile: {envelope.permission_profile}",
        f"- Sandbox: {policy.sandbox_mode}",
        f"- Approval policy: {policy.approval_policy}",
        f"- Approval reviewer: {policy.approval_reviewer}",
    ]
    if policy.extra_write_paths:
        lines.append(f"- Extra write paths: {', '.join(policy.extra_write_paths)}")
    language = envelope.user_profile.language.strip()
    if language:
        lines.append(f"- User language: {_language_display(language)}")
    tone = envelope.user_profile.tone.strip()
    if tone:
        lines.append(f"- User tone: {tone}")
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


def _coerce_goal(value: Goal | dict | None) -> Goal | None:
    if value is None:
        return None
    if isinstance(value, Goal):
        return value
    if isinstance(value, dict):
        try:
            return Goal.model_validate(value)
        except ValueError:
            return None
    return None


def _render_todo_run_state(state: TodoRunState) -> str:
    lines = [state.summary]
    for item in state.items:
        lines.append(f"- {item.status}: {item.content}")
    return "\n".join(line for line in lines if line.strip())


def current_todo_context_message(todo_state: TodoRunState | dict | None) -> HumanMessage | None:
    state = _coerce_todo_run_state(todo_state)
    if state is None or not state.items:
        return None
    return HumanMessage(content=_render_sections([
        ContextSection(name="Current Todo", content=_render_todo_run_state(state)),
    ]))


_LANGUAGE_LABELS = {
    "zh-cn": ("Chinese (Simplified)", "zh-CN"),
    "zh": ("Chinese", "zh"),
    "zh-tw": ("Chinese (Traditional)", "zh-TW"),
    "en": ("English", "en"),
    "en-us": ("English", "en-US"),
    "ja": ("Japanese", "ja"),
    "ko": ("Korean", "ko"),
}


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


def _language_display(value: str) -> str:
    text = value.strip()
    label = _LANGUAGE_LABELS.get(text.lower())
    if label is None:
        return text
    name, tag = label
    return f"{name} [{tag}]"


def _language_target(value: str) -> str:
    text = value.strip()
    label = _LANGUAGE_LABELS.get(text.lower())
    if label is None:
        return text
    return label[0]


def _tone_instruction(value: str) -> str:
    text = value.strip()
    label = _TONE_LABELS.get(text.lower())
    if label is None:
        return f"Keep the response tone {text}."
    return label[2]


def _render_pending_approval(value: object | None) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        kind = str(value.get("kind") or "implementation")
        scope = str(value.get("scope") or "").strip()
    else:
        kind = str(getattr(value, "kind", "implementation") or "implementation")
        scope = str(getattr(value, "scope", "") or "").strip()
    if not scope:
        return kind
    return f"{kind} scope={scope}"


def _platform_info() -> str:
    system = platform.system() or "Unknown"
    machine = platform.machine() or "unknown"
    if system == "Darwin":
        chip = "Apple Silicon" if machine == "arm64" else "Intel"
        return f"macOS {machine} ({chip})"
    if system == "Windows":
        return f"Windows {machine}"
    return f"{system} {machine}"


def _last_user_index(messages: list[BaseMessage]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], HumanMessage):
            return index
    return None


def _prepend_task_context(message: BaseMessage, task_context: str) -> BaseMessage:
    content = message.content
    header = f"{task_context}\n\n## User Message"
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
