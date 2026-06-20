"""Rendering helpers for structured workflow context."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from voidx.workflow.dag import DEFAULT_WORKFLOW_DAG
from voidx.workflow.render import render_node_markdown
from voidx.workflow.schema import WorkflowNode

WORKFLOW_CONTEXT_MARKER = "VOIDX_WORKFLOW_CONTEXT"
WORKFLOW_CONTEXT_SCOPE = "structured-workflow-runtime"

_WORKFLOW_CONTEXT_NOTE = (
    "These are structured workflow definitions owned by the voidx runtime. "
    "The full workflow definitions are kept stable for prompt-cache reuse."
)


def workflow_body_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def workflow_context_cache_key(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def render_workflow_instruction(node: WorkflowNode) -> str:
    return render_node_markdown(node, DEFAULT_WORKFLOW_DAG)


def render_workflow_context(
    nodes: Iterable[WorkflowNode],
    *,
    active_names: Iterable[str] = (),
) -> str:
    del active_names
    rendered: list[str] = []
    for node in nodes:
        rendered.append(render_workflow_instruction(node).strip())
    body = "\n\n".join(item for item in rendered if item)
    if not body:
        return ""
    return (
        f"{WORKFLOW_CONTEXT_MARKER}\n"
        f"Scope: {WORKFLOW_CONTEXT_SCOPE}\n\n"
        f"{_WORKFLOW_CONTEXT_NOTE}\n\n"
        f"{body}"
    )


def is_workflow_context_content(content: object) -> bool:
    if isinstance(content, str):
        return content.lstrip().startswith(WORKFLOW_CONTEXT_MARKER)
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and first.get("type") == "text":
            text = first.get("text", "")
            return isinstance(text, str) and text.lstrip().startswith(WORKFLOW_CONTEXT_MARKER)
    return False
