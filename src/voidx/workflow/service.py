"""Workflow node selection and runtime context support."""

from __future__ import annotations

import re
from collections.abc import Iterable

from pydantic import BaseModel

from voidx.skills.schema import EXPLICIT_REF_RE
from voidx.workflow.context import render_workflow_context, render_workflow_instruction
from voidx.workflow.dag import DEFAULT_WORKFLOW_DAG
from voidx.workflow.policy import workflow_activations, workflow_sort_key
from voidx.workflow.runtime import WorkflowRunState
from voidx.workflow.schema import WorkflowNode


class WorkflowMatch(BaseModel):
    node: WorkflowNode
    reason: str

    @property
    def name(self) -> str:
        return self.node.name

    @property
    def body(self) -> str:
        return render_workflow_instruction(self.node)


class WorkflowService:
    def __init__(self) -> None:
        self._dag = DEFAULT_WORKFLOW_DAG

    def nodes(self) -> list[WorkflowNode]:
        return sorted(self._dag.nodes.values(), key=lambda node: workflow_sort_key(node.name))

    def get(self, name: str) -> WorkflowNode | None:
        return self._dag.nodes.get(_normalize(name))

    def select(
        self,
        user_text: str,
        *,
        agent: str = "",
        task_intent: str | None = None,
        goal_type: str | None = None,
        interaction_mode: str | None = None,
        runtime_trigger: str | None = None,
        limit: int = 5,
        scopes: Iterable[str] | None = None,
        exclude_names: Iterable[str] = (),
    ) -> list[WorkflowMatch]:
        del scopes
        text = user_text.strip()
        has_context = bool(agent or task_intent or goal_type or interaction_mode or runtime_trigger)
        if not text and not has_context:
            return []

        excluded = {_normalize(name) for name in exclude_names}
        matches: list[WorkflowMatch] = []
        seen: set[str] = set()

        def add_match(node: WorkflowNode | None, reason: str) -> None:
            if node is None or not node.enabled:
                return
            name = _normalize(node.name)
            if name in seen or name in excluded:
                return
            seen.add(name)
            matches.append(WorkflowMatch(node=node, reason=reason))

        explicit = self._explicit_refs(text)
        if explicit:
            for name in sorted(explicit, key=workflow_sort_key):
                add_match(self.get(name), "explicit")

        for activation in workflow_activations(
            text,
            agent=agent,
            task_intent=task_intent,
            goal_type=goal_type,
            interaction_mode=interaction_mode,
            runtime_trigger=runtime_trigger,
        ):
            add_match(self.get(activation.name), activation.reason)

        if not goal_type:
            lowered = text.lower()
            text_matches: list[WorkflowMatch] = []
            for node in self.nodes():
                if _normalize(node.name) in seen or _normalize(node.name) in excluded:
                    continue
                reason = self._match_reason(node, lowered)
                if reason:
                    text_matches.append(WorkflowMatch(node=node, reason=reason))
            text_matches.sort(key=lambda match: workflow_sort_key(match.name))
            matches.extend(text_matches)
        return matches[:limit]

    def activation_summaries(
        self,
        user_text: str,
        *,
        agent: str = "",
        task_intent: str | None = None,
        goal_type: str | None = None,
        interaction_mode: str | None = None,
        runtime_trigger: str | None = None,
        limit: int = 5,
        scopes: Iterable[str] | None = None,
        exclude_names: Iterable[str] = (),
    ) -> list[str]:
        return [
            f"{match.name} ({match.reason})"
            for match in self.select(
                user_text,
                agent=agent,
                task_intent=task_intent,
                goal_type=goal_type,
                interaction_mode=interaction_mode,
                runtime_trigger=runtime_trigger,
                limit=limit,
                scopes=scopes,
                exclude_names=exclude_names,
            )
        ]

    def runs_from_matches(
        self,
        matches: list[WorkflowMatch],
        *,
        goal_type: str | None = None,
        scope: str = "",
    ) -> list[WorkflowRunState]:
        return [
            WorkflowRunState.from_match(
                match,
                goal_type=goal_type or _goal_type_from_reason(match.reason),
                scope=scope,
            )
            for match in matches
        ]

    def context(self, *, active_names: Iterable[str] = ()) -> str:
        return render_workflow_context(self.nodes(), active_names=active_names)

    @staticmethod
    def render_instruction(node: WorkflowNode) -> str:
        return render_workflow_instruction(node)

    @staticmethod
    def _explicit_refs(text: str) -> set[str]:
        return {_normalize(match.group(1)) for match in EXPLICIT_REF_RE.finditer(text)}

    @staticmethod
    def _match_reason(node: WorkflowNode, lowered_text: str) -> str:
        name = _normalize(node.name)
        if _contains_phrase(lowered_text, name):
            return "name"
        for trigger in node.triggers:
            normalized = trigger.strip().lower()
            if normalized and _contains_phrase(lowered_text, normalized):
                return f"trigger:{trigger}"
        description_terms = _significant_terms(node.description)
        if description_terms and sum(1 for term in description_terms if _contains_phrase(lowered_text, term)) >= 2:
            return "description"
        return ""


def _normalize(name: str) -> str:
    return name.strip().lower()


def _contains_phrase(text: str, phrase: str) -> bool:
    if not phrase:
        return False
    if _is_cjk_phrase(phrase):
        return phrase in text
    return re.search(rf"(?<![\w.-]){re.escape(phrase)}(?![\w.-])", text) is not None


def _is_cjk_phrase(text: str) -> bool:
    return any("\u3400" <= ch <= "\u9fff" for ch in text)


def _significant_terms(description: str) -> list[str]:
    terms = re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", description.lower())
    stop = {"when", "with", "this", "that", "from", "into", "before", "after", "your"}
    return [term for term in terms if term not in stop][:8]


def _goal_type_from_reason(reason: str) -> str:
    if reason.startswith("goal:"):
        return reason.removeprefix("goal:")
    return ""
