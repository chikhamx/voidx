"""Workflow node selection and runtime context support."""

from __future__ import annotations

import re
from collections.abc import Iterable

from pydantic import BaseModel

from voidx.runtime.reference_tokens import EXPLICIT_REF_RE
from voidx.workflow.context import (
    is_workflow_context_content,
    render_workflow_context,
    render_workflow_instruction,
    workflow_context_cache_key,
    workflow_body_hash,
)
from voidx.workflow.dag import DEFAULT_WORKFLOW_DAG
from voidx.workflow.policy import (
    is_workflow_terminal_condition,
    workflow_activations,
    workflow_denied_tools,
    workflow_edges,
    workflow_exit_summaries,
    workflow_gate,
    workflow_sort_key,
    workflow_terminal_condition,
    workflow_terminal_description,
    workflow_transitions,
    workflow_tools,
)
from voidx.workflow.schema import WorkflowNode
from voidx.workflow.types import WorkflowRunState, WorkflowRunStatus, source_from_reason


def advance_workflow_states(*args, **kwargs):
    from voidx.workflow.runtime import advance_workflow_states as _advance_workflow_states

    return _advance_workflow_states(*args, **kwargs)


def auto_advance_events(*args, **kwargs):
    from voidx.workflow.auto_advance import auto_advance_events as _auto_advance_events

    return _auto_advance_events(*args, **kwargs)


def reconcile_workflow_runs_for_turn(*args, **kwargs):
    from voidx.workflow.reconcile import (
        reconcile_workflow_runs_for_turn as _reconcile_workflow_runs_for_turn,
    )

    return _reconcile_workflow_runs_for_turn(*args, **kwargs)


def workflow_run_from_match(
    match: "WorkflowMatch",
    *,
    goal_type: str = "",
    scope: str = "",
    turn_count: int = 0,
    status: WorkflowRunStatus = WorkflowRunStatus.ACTIVE,
    workflow_body: str | None = None,
    body_hash: str = "",
) -> WorkflowRunState:
    body = match.body if workflow_body is None else workflow_body
    return WorkflowRunState(
        name=match.name,
        status=status,
        source=source_from_reason(match.reason),
        reason=match.reason,
        goal_type=goal_type,
        scope=scope,
        personas=[match.node.persona],
        activated_turn=turn_count,
        updated_turn=turn_count,
        body_hash=body_hash or (workflow_body_hash(body) if body else ""),
        transition_to=list(workflow_transitions(match.name)),
    )


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
            if node is None:
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
            workflow_run_from_match(
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
        for trigger in _TEXT_TRIGGERS.get(name, ()):
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


_TEXT_TRIGGERS: dict[str, tuple[str, ...]] = {
    "brainstorm": (
        "create feature",
        "build component",
        "add functionality",
        "new feature",
        "design",
        "brainstorm",
        "refactor",
        "restructure",
        "新功能",
        "实现新功能",
        "设计",
        "头脑风暴",
        "需求澄清",
        "重构",
        "重组",
    ),
    "design-doc": (
        "design doc",
        "technical design",
        "architecture doc",
        "RFC",
        "API doc",
        "README",
        "changelog",
        "release notes",
        "write docs",
        "document this",
        "PRD",
        "product requirements",
        "需求文档",
        "产品需求",
        "技术设计",
        "架构文档",
        "接口文档",
        "写文档",
        "变更日志",
    ),
    "plan": (
        "implementation plan",
        "write a plan",
        "planning",
        "spec",
        "requirements",
        "计划",
        "实施方案",
        "需求",
    ),
    "tdd": (
        "implement",
        "feature",
        "bugfix",
        "refactor",
        "behavior change",
        "add support",
        "fix bug",
        "实现",
        "修复",
        "重构",
        "功能",
    ),
    "verify": (
        "done",
        "complete",
        "fixed",
        "passing",
        "ready",
        "verify",
        "verified",
        "looks good",
        "should work",
        "完成",
        "修好了",
        "通过",
        "验证",
        "好了",
        "没问题了",
    ),
    "review": (
        "request review",
        "ask for review",
        "before merge",
        "pre-merge",
        "review this change",
        "复核一下",
        "合并前",
    ),
    "feedback": (
        "review feedback",
        "code review feedback",
        "reviewer says",
        "feedback says",
        "review comment",
        "优化点",
        "审查意见",
        "评审意见",
    ),
    "debug": (
        "bug",
        "failed",
        "failure",
        "traceback",
        "error",
        "crash",
        "broken",
        "not working",
        "unexpected",
        "test failure",
        "build failure",
        "报错",
        "失败",
        "异常",
        "崩溃",
        "排查",
        "不对",
        "结果不对",
    ),
}
