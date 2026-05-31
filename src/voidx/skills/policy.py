"""Workflow skill activation policy."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkflowSkillActivation:
    name: str
    reason: str


WORKFLOW_SKILL_PRIORITY = {
    "systematic-debugging": 10,
    "receiving-code-review": 20,
    "writing-plans": 30,
    "test-driven-development": 40,
    "verification-before-completion": 50,
    "requesting-code-review": 60,
}


def workflow_skill_activations(
    user_text: str,
    *,
    agent: str = "",
    task_intent: str | None = None,
    interaction_mode: str | None = None,
) -> list[WorkflowSkillActivation]:
    text = user_text.strip().lower()
    agent_name = (agent or "").strip().lower()
    intent = (task_intent or "").strip().lower()
    mode = (interaction_mode or "").strip().lower()
    activations: dict[str, WorkflowSkillActivation] = {}

    def add(name: str, reason: str) -> None:
        activations.setdefault(name, WorkflowSkillActivation(name=name, reason=reason))

    if intent == "debug":
        add("systematic-debugging", "debug intent")
        add("verification-before-completion", "debug lifecycle")

    if agent_name == "implement":
        add("test-driven-development", "implement role")
        add("verification-before-completion", "implement lifecycle")
    elif intent == "implement":
        add("test-driven-development", "implement intent")
        add("verification-before-completion", "implement lifecycle")

    if agent_name == "plan":
        add("writing-plans", "plan role")

    if intent == "review" and _contains_any(text, _REVIEW_FEEDBACK_TERMS):
        add("receiving-code-review", "review feedback")

    if intent == "design" and _contains_any(text, _PLAN_TERMS):
        add("writing-plans", "planning intent")

    if mode == "plan":
        add("writing-plans", "plan mode")

    return sorted(
        activations.values(),
        key=lambda item: (WORKFLOW_SKILL_PRIORITY.get(item.name, 999), item.name),
    )


def workflow_skill_sort_key(name: str) -> tuple[int, str]:
    return (WORKFLOW_SKILL_PRIORITY.get(name, 999), name)


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


_REVIEW_FEEDBACK_TERMS = (
    "review feedback",
    "code review feedback",
    "review comment",
    "reviewer says",
    "feedback says",
    "优化点",
    "审查意见",
    "评审意见",
)

_PLAN_TERMS = (
    "implementation plan",
    "write a plan",
    "planning",
    "spec",
    "requirements",
    "计划",
    "实施方案",
    "需求",
)
