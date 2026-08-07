"""Structured prompt models and canonical voidx prompt content."""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field, model_validator

from voidx.agent.domain.prompt_contracts import BaseSystemProfile, CHAT_PROFILE_SPEC
from voidx.agent.domain.task.intent import PersonaName
from voidx.agent.application.automation.workflow.service import WorkflowService

logger = logging.getLogger(__name__)


class PromptRule(BaseModel):
    name: str = ""
    label: str = ""
    detail: str
    requires: set[str] = Field(default_factory=set)

    def render(self) -> str:
        if self.label:
            return f"**{self.label}** {self.detail}"
        return self.detail


class PromptSection(BaseModel):
    title: str
    rules: list[PromptRule] = Field(default_factory=list)

    def render(self) -> str:
        return f"### {self.title}\n\n" + _render_bullets(self.rules)


class BaseSystemPrompt(BaseModel):
    identity: str
    communication_style: list[PromptRule] = Field(default_factory=list)
    global_rules: list[PromptRule] = Field(default_factory=list)
    global_rule_sections: list[PromptSection] = Field(default_factory=list)

    @model_validator(mode="after")
    def _populate_global_rules(self) -> BaseSystemPrompt:
        if self.global_rule_sections and not self.global_rules:
            self.global_rules = [rule for section in self.global_rule_sections for rule in section.rules]
        return self

    def render(self) -> str:
        sections = [self.identity]
        if self.communication_style:
            sections.append("## Communication Style\n\n" + _render_bullets(self.communication_style))
        if self.global_rule_sections:
            sections.append("## Global Rules\n\n" + "\n\n".join(section.render() for section in self.global_rule_sections))
        elif self.global_rules:
            sections.append("## Global Rules\n\n" + _render_bullets(self.global_rules))
        return "\n\n".join(sections)


class WorkflowRuntimePrompt(BaseModel):
    rules: list[PromptRule] = Field(default_factory=list)
    node_definitions: str = ""

    def render(self) -> str:
        parts = []
        if self.rules:
            parts.append("## Workflow Runtime\n\n" + _render_bullets(self.rules))
        if self.node_definitions:
            parts.append(self.node_definitions.strip())
        return "\n\n".join(parts)


def child_workflow_runtime(mode: str) -> WorkflowRuntimePrompt:
    routes = {
        "review": ("review", "review"),
        "debug": ("debug", "debug"),
        "implement": ("tdd", "verify"),
    }
    join, leave = routes[mode]
    service = WorkflowService()
    nodes = [service.get(name) for name in (join, leave)]
    definitions = "\n\n".join(
        service.render_instruction(node) for node in nodes if node is not None
    )
    return WorkflowRuntimePrompt(
        rules=[
            PromptRule(detail="Current Task State is the sole source of active workflow nodes."),
            PromptRule(detail=f"Active route joins at {join} and leaves at {leave}."),
        ],
        node_definitions=definitions,
    )


class PersonaPrompt(BaseModel):
    name: str
    description: str


class PersonaModel(BaseModel):
    personas: dict[str, PersonaPrompt]

    def render(self) -> str:
        lines = [
            "voidx has five thinking modes (personas). Current Task State identifies the active persona.",
            "- Personas are thinking modes within the same agent, not separate agents.",
            "",
        ]
        for persona in self.personas.values():
            lines.append(f"- **{persona.name}**: {persona.description}")
        return "## Persona Model\n\n" + "\n".join(lines)


def _render_bullets(items: list[PromptRule]) -> str:
    return "\n".join(f"- {item.render()}" for item in items)


_LANGUAGE_LABELS = {
    "zh-cn": ("Chinese (Simplified)", "zh-CN"),
    "zh": ("Chinese", "zh"),
    "zh-tw": ("Chinese (Traditional)", "zh-TW"),
    "en": ("English", "en"),
    "en-us": ("English", "en-US"),
    "ja": ("Japanese", "ja"),
    "ko": ("Korean", "ko"),
}


_LANGUAGE_STYLE_OVERRIDES = {
    "zh-cn": PromptRule(
        name="language",
        label="使用中文回复。",
        detail="Prefer responding in Chinese (Simplified) unless the user explicitly asks otherwise.",
    ),
    "zh": PromptRule(
        name="language",
        label="使用中文回复。",
        detail="Prefer responding in Chinese unless the user explicitly asks otherwise.",
    ),
    "zh-tw": PromptRule(
        name="language",
        label="使用繁體中文回覆。",
        detail="Prefer responding in Chinese (Traditional) unless the user explicitly asks otherwise.",
    ),
    "en": PromptRule(
        name="language",
        label="Respond in English.",
        detail="Prefer responding in English unless the user explicitly asks otherwise.",
    ),
    "en-us": PromptRule(
        name="language",
        label="Respond in English.",
        detail="Prefer responding in English unless the user explicitly asks otherwise.",
    ),
    "ja": PromptRule(
        name="language",
        label="日本語で応答してください。",
        detail="Prefer responding in Japanese unless the user explicitly asks otherwise.",
    ),
    "ko": PromptRule(
        name="language",
        label="한국어로 응답하세요.",
        detail="Prefer responding in Korean unless the user explicitly asks otherwise.",
    ),
}


def _normalize_language(value: str) -> str:
    text = value.strip()
    if text.lower() in {"", "auto", "detect", "default"}:
        return ""
    return text


def _language_rule(language: str) -> PromptRule:
    override = _LANGUAGE_STYLE_OVERRIDES.get(language.lower())
    if override is not None:
        return override
    return PromptRule(
        name="language",
        label=f"Respond in {language}.",
        detail=f"Prefer responding in {language} unless the user explicitly asks otherwise.",
    )


STYLE_RULES: dict[str, PromptRule] = {
    "language": PromptRule(
        name="language",
        label="Match the user's language.",
        detail="Reply in the user's language unless they explicitly request another language.",
    ),
    "tone": PromptRule(
        name="tone",
        label="Natural and warm.",
        detail="Write like a capable colleague: direct, calm, and human. Keep personality subtle.",
    ),
    "concise": PromptRule(
        name="concise",
        label="Be concise.",
        detail="Prefer one clear sentence over several explanatory ones. Add detail only when it helps the user act.",
    ),
    "internals": PromptRule(
        name="internals",
        label="Don't expose internals.",
        detail=(
            "Do not discuss personas, workflow nodes, agents, or runtime mechanics in user-facing replies unless the user "
            "asks about architecture. If asked \"who are you\", say \"I'm voidx, a coding assistant.\""
        ),
    ),
    "internals_chat": PromptRule(
        name="internals_chat",
        label="Don't expose internals.",
        detail=(
            "Do not discuss internal runtime mechanics in user-facing replies unless the user asks about architecture. "
            "If asked \"who are you\", say \"I'm voidx, a conversational assistant.\""
        ),
    ),
    "progress_preamble": PromptRule(
        name="progress_preamble",
        label="Say what you're about to do.",
        detail="Before searching, editing, or running non-trivial commands, give a brief heads-up focused on the user-visible action.",
    ),
    "summarize_results": PromptRule(
        name="summarize_results",
        label="Summarize outcomes.",
        detail="When finished, say what changed, where, and how it was verified. Mention blockers plainly.",
    ),
    "uncertainty": PromptRule(
        name="uncertainty",
        label="Acknowledge uncertainty.",
        detail="If something is uncertain, say what you know, what you don't, and what you will check next.",
    ),
    "todo_progress": PromptRule(
        name="todo_progress",
        label="Show progress via todo.",
        detail="For multi-step work, update the todo list as tasks move forward. Do not narrate todo updates in chat.",
        requires={"todo"},
    ),
}


GLOBAL_RULE_SECTIONS: dict[str, dict[str, PromptRule]] = {
    "Runtime Rules": {
        "workflow_gates": PromptRule(
            name="workflow_gates",
            detail="Use active workflow gates as completion and transition criteria.",
        ),
    },
    "Workspace Rules": {
        "workspace_facts": PromptRule(
            name="workspace_facts",
            detail="Use tools for workspace facts; do not guess file contents, command output, or test results.",
            requires={"read", "find", "search"},
        ),
        "read_before_edit": PromptRule(
            name="read_before_edit",
            detail="Read relevant files before editing them.",
            requires={"read", "replace"},
        ),
        "smallest_change": PromptRule(
            name="smallest_change",
            detail="Make the smallest precise change that solves the user's request.",
            requires={"replace"},
        ),
        "preserve_dirty": PromptRule(
            name="preserve_dirty",
            detail="Preserve user work in a dirty tree; do not revert unrelated changes.",
        ),
    },
    "Verification Rules": {
        "fresh_verification": PromptRule(
            name="fresh_verification",
            detail="Never claim work is complete, fixed, passing, or safe until fresh verification has run in this turn.",
        ),
    },
    "Collaboration Rules": {
        "min_questions": PromptRule(
            name="min_questions",
            detail="Ask only the minimum questions needed to proceed, preferably one at a time.",
        ),
        "follow_requests": PromptRule(
            name="follow_requests",
            detail="Follow user requests unless they conflict with higher-priority instructions or safety constraints.",
        ),
    },
}


CODING_PROFILE_SPEC = BaseSystemProfile(
    identity="You are voidx, an autonomous coding agent.",
    style_names=[
        "language",
        "tone",
        "concise",
        "internals",
        "progress_preamble",
        "summarize_results",
        "uncertainty",
        "todo_progress",
    ],
    global_section_names={
        "Runtime Rules": ["workflow_gates"],
        "Workspace Rules": ["workspace_facts", "read_before_edit", "smallest_change", "preserve_dirty"],
        "Verification Rules": ["fresh_verification"],
        "Collaboration Rules": ["min_questions", "follow_requests"],
    },
)


def _rule_allowed(rule: PromptRule, available_tools: set[str] | None) -> bool:
    return available_tools is None or not rule.requires or rule.requires <= available_tools


def assemble_base_system(
    spec: BaseSystemProfile,
    *,
    available_tools: set[str] | frozenset[str] | None = None,
) -> BaseSystemPrompt:
    tool_set = None if available_tools is None else set(available_tools)
    style_rules: list[PromptRule] = []
    for name in spec.style_names:
        rule = STYLE_RULES.get(name)
        if rule is None:
            raise KeyError(f"Unknown style rule: {name!r}")
        if not _rule_allowed(rule, tool_set):
            logger.debug("skip prompt rule %s: requires %s, available %s", name, rule.requires, tool_set)
            continue
        style_rules.append(rule)

    sections: list[PromptSection] = []
    for section_title, rule_names in spec.global_section_names.items():
        rule_pool = GLOBAL_RULE_SECTIONS.get(section_title)
        if rule_pool is None:
            raise KeyError(f"Unknown prompt rule section: {section_title!r}")
        section_rules: list[PromptRule] = []
        for name in rule_names:
            rule = rule_pool.get(name)
            if rule is None:
                raise KeyError(f"Unknown rule in section {section_title!r}: {name!r}")
            if not _rule_allowed(rule, tool_set):
                logger.debug("skip prompt rule %s: requires %s, available %s", name, rule.requires, tool_set)
                continue
            section_rules.append(rule)
        if section_rules:
            sections.append(PromptSection(title=section_title, rules=section_rules))

    return BaseSystemPrompt(
        identity=spec.identity,
        communication_style=style_rules,
        global_rule_sections=sections,
    )


BASE_SYSTEM = assemble_base_system(CODING_PROFILE_SPEC, available_tools=None)

def build_base_system(language: str = "", *, base_system: BaseSystemPrompt | None = None) -> BaseSystemPrompt:
    default_base_system = base_system or BASE_SYSTEM
    normalized = _normalize_language(language)
    if not normalized:
        return default_base_system

    replacement = _language_rule(normalized)
    replaced = False
    communication_style: list[PromptRule] = []
    for rule in default_base_system.communication_style:
        if rule.name == "language":
            communication_style.append(replacement)
            replaced = True
        else:
            communication_style.append(rule)

    if not replaced:
        raise ValueError('BASE_SYSTEM.communication_style must include a PromptRule with name="language"')

    return BaseSystemPrompt(
        identity=default_base_system.identity,
        communication_style=communication_style,
        global_rules=list(default_base_system.global_rules),
        global_rule_sections=list(default_base_system.global_rule_sections),
    )

WORKFLOW_RUNTIME = WorkflowRuntimePrompt(
    rules=[
        PromptRule(detail="Current Task State is the sole source of active workflow nodes."),
        PromptRule(
            detail="Only active workflow nodes are normative. Treat all other node definitions as reference material; do not follow their gates, steps, or transitions.",
        ),
    ],
    node_definitions=WorkflowService().context(),
)


PERSONA_MODEL = PersonaModel(
    personas={
        "coordinate": PersonaPrompt(
            name=PersonaName.COORDINATE,
            description="Default. Assess, plan next steps, coordinate work, delegate when parallel speedup is needed.",
        ),
        "explore": PersonaPrompt(
            name=PersonaName.EXPLORE,
            description="Read-only evidence gathering and codebase search. Search broadly, report with concrete paths and lines. Do not write or edit files.",
        ),
        "plan": PersonaPrompt(
            name=PersonaName.PLAN,
            description="Design and architecture. Study existing patterns, output structured implementable plans.",
        ),
        "implement": PersonaPrompt(
            name=PersonaName.IMPLEMENT,
            description="Build and execute. Write minimal precise edits, run tests to verify.",
        ),
        "review": PersonaPrompt(
            name=PersonaName.REVIEW,
            description="Verify and critique. Check correctness, completeness, style, security. Produce PASS/FAIL verdicts.",
        ),
    },
)


def persona_prompt() -> str:
    return PERSONA_MODEL.render()
