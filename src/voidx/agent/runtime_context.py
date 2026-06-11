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

from voidx.agent.message_rows import RowMessageCacheEntry
from voidx.config import ApprovalReviewer, Config, UserProfile
from voidx.runtime.intent import InteractionMode, TaskIntent, infer_task_intent
from voidx.skills.context import (
    has_skill_tool_context,
    is_skill_context_content,
    skill_context_cache_key,
    strip_skill_tool_context,
)
from voidx.workflow.context import (
    is_workflow_context_content,
    workflow_context_cache_key,
)
from voidx.workflow.dag import DEFAULT_WORKFLOW_DAG
from voidx.workflow.policy import workflow_exit_summaries, workflow_gate
from voidx.workflow.render import render_dag_overview
from voidx.workflow.runtime import WorkflowRunState, WorkflowRunStatus

_CONTEXT_MARKER = "VOIDX_RUNTIME_CONTEXT"
_USER_MESSAGE_DELIMITER = "\n\n## User Message\n"


@dataclass
class ContextCompilerCache:
    stable_prefix_key: str = ""
    stable_system_content: str = ""
    stable_system_message: SystemMessage | None = None
    skill_context_key: str = ""
    skill_context_content: str = ""
    skill_context_message: HumanMessage | None = None
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
    date: str
    workspace: str
    provider: str
    model: str
    interaction_mode: InteractionMode
    permission_profile: str
    execution_policy: ExecutionPolicy
    agent: str
    agent_id: int = -1
    user_profile: UserProfile = Field(default_factory=UserProfile)


class ContextSection(BaseModel):
    name: str
    content: str


class RuntimeContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    sections: list[ContextSection]
    task_sections: list[ContextSection] = Field(default_factory=list)
    skill_context_content: str = ""
    system_content: str | None = None
    system_message: SystemMessage | None = Field(default=None, exclude=True)
    skill_context_message: HumanMessage | None = Field(default=None, exclude=True)

    def section_names(self) -> list[str]:
        names = [section.name for section in self.sections]
        if self.skill_context_content:
            names.append(_context_message_section_name(self.skill_context_content))
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

        skill_context_message = self._skill_context_message()
        if skill_context_message is None:
            return [prefix, *semantic_messages]
        return [prefix, skill_context_message, *semantic_messages]

    def apply_to_messages(self, messages: list[BaseMessage]) -> None:
        messages[:] = self.compile_messages(messages)

    def _skill_context_message(self) -> HumanMessage | None:
        content = self.context.skill_context_content.strip()
        if not content:
            return None
        cached = self.context.skill_context_message
        if cached is not None and cached.content == content:
            return cached
        return HumanMessage(content=content)


class RuntimeContextBuilder:
    def __init__(
        self,
        *,
        config: Config,
        workspace: str,
        agent_prompt: str | None = None,
        base_system_prompt: str | None = None,
        role_prompt: str = "",
        mode_prompt: str = "",
        tool_contract: str = "",
        agent: str,
        interaction_mode: str | InteractionMode,
        instructions: Iterable[str] = (),
        skill_context_content: str = "",
        workflow_runs: Iterable[WorkflowRunState] = (),
        active_workflow_summaries: Iterable[str] = (),
        summary: str | None = None,
        current_user_text: str = "",
        task_intent: str | TaskIntent | None = None,
        intent_resolution_reason: str = "",
        pending_approval: object | None = None,
        goal: str = "",
        goal_phase: str = "",
        goal_status: str = "",
        goal_turn_count: int = 0,
        available_tool_ids: Iterable[str] = (),
        intent_confidence: float | None = None,
        intent_source: str = "",
        intent_refined: bool = False,
        session_date: str | None = None,
        current_datetime: str | None = None,
        agent_id: int = -1,
    ) -> None:
        self.config = config
        self.workspace = workspace
        self.base_system_prompt = (base_system_prompt or agent_prompt or "").strip()
        self.role_prompt = role_prompt.strip()
        self.mode_prompt = mode_prompt.strip()
        self.tool_contract = tool_contract.strip()
        self.agent = agent
        self.interaction_mode = InteractionMode.parse(interaction_mode)
        self.instructions = [item for item in instructions if item.strip()]
        self.skill_context_content = skill_context_content.strip()
        self.workflow_runs = list(workflow_runs)
        self.active_workflow_summaries = [item for item in active_workflow_summaries if item.strip()]
        self.summary = summary.strip() if summary else ""
        self.current_user_text = current_user_text.strip()
        self.task_intent = (
            TaskIntent(task_intent)
            if task_intent is not None
            else infer_task_intent(self.current_user_text, self.interaction_mode)
        )
        self.intent_resolution_reason = intent_resolution_reason.strip()
        self.pending_approval = pending_approval
        self.goal = goal.strip()
        self.goal_phase = goal_phase.strip()
        self.goal_status = goal_status.strip()
        self.goal_turn_count = goal_turn_count
        self.available_tool_ids = [item for item in available_tool_ids if item.strip()]
        self.intent_confidence = intent_confidence
        self.intent_source = intent_source.strip()
        self.intent_refined = intent_refined
        self.user_profile = config.user_profile
        now = datetime.now().astimezone()
        self.session_date = (session_date or now.strftime("%Y-%m-%d %Z")).strip()
        self.current_datetime = (current_datetime or now.strftime("%Y-%m-%d %H:%M %Z")).strip()
        self.agent_id = agent_id

    def build(self) -> RuntimeContext:
        return RuntimeContext(
            sections=self._build_stable_sections(),
            task_sections=self._build_task_sections(),
            skill_context_content=self.skill_context_content,
            skill_context_message=(
                HumanMessage(content=self.skill_context_content)
                if self.skill_context_content
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

        skill_context_content, skill_context_message = self._incremental_skill_context(cache)

        return RuntimeContext(
            sections=sections,
            task_sections=self._build_task_sections(),
            skill_context_content=skill_context_content,
            system_content=system_content,
            system_message=system_message,
            skill_context_message=skill_context_message,
        ), cache

    def _incremental_skill_context(
        self,
        cache: ContextCompilerCache,
    ) -> tuple[str, HumanMessage | None]:
        content = self.skill_context_content.strip()
        if not content:
            cache.skill_context_key = ""
            cache.skill_context_content = ""
            cache.skill_context_message = None
            return "", None

        key = _runtime_context_cache_key(content)
        if cache.skill_context_key == key and cache.skill_context_content:
            return cache.skill_context_content, cache.skill_context_message

        message = HumanMessage(content=content)
        cache.skill_context_key = key
        cache.skill_context_content = content
        cache.skill_context_message = message
        return content, message

    def _build_stable_sections(self) -> list[ContextSection]:
        sections = [
            ContextSection(name="Base System", content=self.base_system_prompt),
        ]
        if self.role_prompt:
            sections.append(ContextSection(name="Role Prompt", content=self.role_prompt))
        if self.mode_prompt:
            sections.append(ContextSection(name="Mode Prompt", content=self.mode_prompt))
        if self.tool_contract:
            sections.append(ContextSection(name="Tool Contract", content=self.tool_contract))
        sections.append(ContextSection(
            name="Workflow DAG",
            content=render_dag_overview(DEFAULT_WORKFLOW_DAG),
        ))
        sections.append(ContextSection(
            name="Workspace Facts",
            content=f"- Current workspace: {self.workspace}\n- Platform: {_platform_info()}",
        ))

        if self.instructions:
            sections.append(ContextSection(
                name="Project Facts",
                content="\n\n".join(self.instructions),
            ))
        sections.append(ContextSection(name="Session Date", content=self.session_date))
        if self.summary:
            sections.append(ContextSection(
                name="Long Summary",
                content=self.summary,
            ))
        return sections

    def _build_task_sections(self) -> list[ContextSection]:
        envelope = RuntimeEnvelope(
            date=self.session_date,
            workspace=self.workspace,
            provider=self.config.model.provider,
            model=self.config.model.model,
            interaction_mode=self.interaction_mode,
            permission_profile=self.config.permission_mode.value,
            execution_policy=ExecutionPolicy.from_config(self.config),
            agent=self.agent,
            agent_id=self.agent_id,
            user_profile=self.user_profile,
        )

        task_sections = [
            ContextSection(name="Runtime State", content=_render_envelope(envelope)),
            ContextSection(name="Current DateTime", content=self.current_datetime),
        ]
        task_sections.append(ContextSection(
            name="Current Task State",
            content=self._current_task_state(),
        ))

        return task_sections

    def _current_task_state(self) -> str:
        lines = [
            f"- Mode: {self.interaction_mode.value}",
            f"- Intent: {self.task_intent.value}",
            f"- Agent: {self.agent}",
            f"- Agent ID: {self.agent_id}",
        ]
        if self.active_workflow_summaries:
            lines.append(f"- Active workflow nodes: {'; '.join(self.active_workflow_summaries)}")
        if self.workflow_runs:
            lines.append(f"- Workflow run state: {'; '.join(run.state_summary() for run in self.workflow_runs)}")
        for workflow_name in self._active_workflow_node_names():
            gate = workflow_gate(workflow_name)
            if gate:
                if gate.denied_tools:
                    lines.append(
                        f"- Workflow gate [{workflow_name}]: denied tools = {', '.join(gate.denied_tools)}"
                    )
                requirement = gate.required_before_transition or gate.description
                if requirement:
                    lines.append(
                        f"- Workflow gate [{workflow_name}]: must satisfy {requirement} before proceeding"
                    )
            exits = workflow_exit_summaries(workflow_name)
            if exits:
                lines.append(f"- Workflow exits [{workflow_name}]: {'; '.join(exits)}")
        if self.intent_refined:
            confidence = (
                f"{self.intent_confidence:.2f}"
                if self.intent_confidence is not None
                else "unknown"
            )
            source = self.intent_source or "runtime"
            lines.append(f"- Intent refined: true source={source} confidence={confidence}")
        if self.intent_confidence is not None and self.intent_confidence < 0.6:
            lines.append("- Suggestion: use clarify to resolve intent ambiguity before proceeding.")
        if self.available_tool_ids:
            lines.append(f"- Runtime-visible tools: {', '.join(self.available_tool_ids)}")
        if self.intent_resolution_reason:
            lines.append(f"- Intent resolution: {self.intent_resolution_reason}")
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
        if self.task_intent == TaskIntent.DESIGN and pending:
            lines.append("- Suggestion: use plan_checkpoint to get explicit approval before implementing.")
        if self.interaction_mode == InteractionMode.GOAL:
            lines.append("- Goal mode: true")
            lines.append(f"- Goal: {self.goal or 'not set'}")
            lines.append(f"- Goal phase: {self.goal_phase or 'clarify'}")
            lines.append(f"- Goal status: {self.goal_status or 'idle'}")
            lines.append(f"- Goal turn count: {self.goal_turn_count}")
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
    return is_skill_context_content(content) or is_workflow_context_content(content)


def _context_message_section_name(content: object) -> str:
    if is_workflow_context_content(content):
        return "Workflow Context"
    return "Skill Context"


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
        f"- Agent: {envelope.agent}",
        f"- Agent ID: {envelope.agent_id}",
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
