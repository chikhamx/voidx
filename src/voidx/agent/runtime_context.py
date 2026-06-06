"""Structured runtime context assembly for LLM calls."""

from __future__ import annotations

from datetime import datetime
import platform
from typing import Iterable

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from voidx.config import ApprovalReviewer, Config, UserProfile
from voidx.runtime.intent import InteractionMode, TaskIntent, infer_task_intent
from voidx.skills.runtime import SkillRunState


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
    sections: list[ContextSection]
    task_sections: list[ContextSection] = Field(default_factory=list)

    def section_names(self) -> list[str]:
        names = [section.name for section in self.sections]
        names.extend(section.name for section in self.task_sections)
        return names

    def render_system(self) -> str:
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
        semantic_messages = [message for message in messages if not isinstance(message, SystemMessage)]
        current_user_index = _last_user_index(semantic_messages)

        prefix = SystemMessage(content=self.context.render_system())
        task_context = self.context.render_task_context()
        if task_context:
            if current_user_index is None:
                semantic_messages.append(HumanMessage(content=task_context))
            else:
                current = semantic_messages[current_user_index]
                semantic_messages[current_user_index] = _prepend_task_context(current, task_context)

        return [prefix, *semantic_messages]

    def apply_to_messages(self, messages: list[BaseMessage]) -> None:
        messages[:] = self.compile_messages(messages)


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
        skill_instructions: Iterable[str] = (),
        skill_runs: Iterable[SkillRunState] = (),
        active_skill_summaries: Iterable[str] = (),
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
        self.skill_instructions = [item for item in skill_instructions if item.strip()]
        self.skill_runs = list(skill_runs)
        self.active_skill_summaries = [item for item in active_skill_summaries if item.strip()]
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

        sections = [
            ContextSection(name="Base System", content=self.base_system_prompt),
        ]
        if self.role_prompt:
            sections.append(ContextSection(name="Role Prompt", content=self.role_prompt))
        if self.mode_prompt:
            sections.append(ContextSection(name="Mode Prompt", content=self.mode_prompt))
        if self.tool_contract:
            sections.append(ContextSection(name="Tool Contract", content=self.tool_contract))
        sections.extend([
            ContextSection(
                name="Workspace Facts",
                content=f"- Current workspace: {self.workspace}\n- Platform: {_platform_info()}",
            ),
        ])

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

        task_sections = [
            ContextSection(name="Runtime State", content=_render_envelope(envelope)),
            ContextSection(name="Current DateTime", content=self.current_datetime),
        ]
        if self.skill_instructions:
            task_sections.append(ContextSection(
                name="Active Skills",
                content="\n\n".join(self.skill_instructions),
            ))
        task_sections.append(ContextSection(
            name="Current Task State",
            content=self._current_task_state(),
        ))

        return RuntimeContext(sections=sections, task_sections=task_sections)

    def _current_task_state(self) -> str:
        lines = [
            f"- Mode: {self.interaction_mode.value}",
            f"- Intent: {self.task_intent.value}",
            f"- Agent: {self.agent}",
            f"- Agent ID: {self.agent_id}",
        ]
        if self.active_skill_summaries:
            lines.append(f"- Active workflow skills: {'; '.join(self.active_skill_summaries)}")
        if self.skill_runs:
            lines.append(f"- Skill run state: {'; '.join(run.state_summary() for run in self.skill_runs)}")
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


def _render_sections(sections: list[ContextSection]) -> str:
    parts = ["VOIDX_RUNTIME_CONTEXT"]
    for section in sections:
        if not section.content.strip():
            continue
        parts.append(f"## {section.name}\n{section.content.strip()}")
    return "\n\n".join(parts)


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
