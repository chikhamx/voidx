from __future__ import annotations
import asyncio
from enum import Enum

from voidx.agent.agents import AgentDef
from voidx.agent.runtime_context import COMPACTION_GUIDE_MARKER, InteractionMode
from voidx.llm.compaction import SUMMARY_TEMPLATE
from voidx.agent.task_state import GoalResolution, TaskState
from voidx.workflow import workflow_personas
from voidx.workflow.types import WorkflowRunState, WorkflowRunStatus


def _is_context_overflow_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(
        pattern in msg
        for pattern in (
            "context_length_exceeded",
            "context length",
            "too many tokens",
            "maximum context",
            "token limit",
            "input is too long",
            "request too large",
            "context window",
        )
    )


class LLMErrorKind(str, Enum):
    NETWORK = "network"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    SERVER_ERROR = "server_error"
    CONTEXT_OVERFLOW = "context_overflow"
    NON_RETRYABLE = "non_retryable"
    UNKNOWN = "unknown"


def _is_schema_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(k in msg for k in ("invalid schema", "schema for function", "required is required"))


def _classify_llm_error(exc: Exception) -> LLMErrorKind:
    # 1. Priority: check status_code attribute (OpenAI/Anthropic SDKs set it)
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        if status_code == 429:
            return LLMErrorKind.RATE_LIMIT
        if status_code == 400:
            if _is_context_overflow_error(exc):
                return LLMErrorKind.CONTEXT_OVERFLOW
            return LLMErrorKind.NON_RETRYABLE
        if status_code in (401, 403):
            return LLMErrorKind.NON_RETRYABLE
        if status_code == 404:
            return LLMErrorKind.NON_RETRYABLE
        if status_code in (500, 502, 503):
            if _is_schema_error(exc):
                return LLMErrorKind.NON_RETRYABLE
            return LLMErrorKind.SERVER_ERROR
        return LLMErrorKind.UNKNOWN

    # 2. No status_code — check exception type
    if isinstance(exc, asyncio.TimeoutError):
        return LLMErrorKind.TIMEOUT
    if isinstance(exc, ConnectionError) or "connection" in type(exc).__name__.lower():
        return LLMErrorKind.NETWORK

    # 3. String fallback for context overflow
    if _is_context_overflow_error(exc):
        return LLMErrorKind.CONTEXT_OVERFLOW

    return LLMErrorKind.UNKNOWN


_LLM_MAX_RETRIES = 10
_LLM_TIMEOUT_MAX_RETRIES = 1
_LLM_RETRY_FIXED_PHASE = 2
_LLM_RETRY_FIXED_DELAY = 2.0
_LLM_RETRY_BASE_DELAY = 2.0
_LLM_RETRY_MAX_DELAY = 60.0


def _llm_retry_delay(attempt: int) -> float:
    """Return delay in seconds for the given 1-based retry attempt number."""
    if attempt <= _LLM_RETRY_FIXED_PHASE:
        return _LLM_RETRY_FIXED_DELAY
    exp = attempt - _LLM_RETRY_FIXED_PHASE - 1
    return min(_LLM_RETRY_BASE_DELAY * (2 ** exp), _LLM_RETRY_MAX_DELAY)


def _render_inline_compaction_guide(*, tail_anchor_id: str, head_count: int, previous_summary: str) -> str:

    previous = previous_summary.strip() or "(none)"
    return (
        f"{COMPACTION_GUIDE_MARKER}\n"
        "Scope: inline-context-compaction\n\n"
        "The conversation is large enough to compact older context without a separate compaction request.\n"
        "If you can preserve the durable facts now, call compact before continuing.\n\n"
        "Rules:\n"
        "- Summarize only older context before the tail anchor.\n"
        "- Preserve durable facts, decisions, constraints, changed files, verification results, blockers, and next steps.\n"
        "- Drop transient narration, repeated tool outputs, and stale execution detail.\n"
        "- Do not answer the user through compact; use it only to update runtime memory.\n"
        "- After compact succeeds, continue with the user's request normally.\n\n"
        "Current compaction request:\n"
        f"- tail_anchor_id: {tail_anchor_id}\n"
        f"- older_messages_to_summarize: {head_count}\n"
        f"- previous_summary:\n{previous}\n\n"
        f"{SUMMARY_TEMPLATE}"
    )


def _merge_workflow_runs(*groups: list[WorkflowRunState | dict]) -> list[WorkflowRunState]:
    merged: dict[str, WorkflowRunState] = {}
    for group in groups:
        for item in group:
            try:
                run = item if isinstance(item, WorkflowRunState) else WorkflowRunState.model_validate(item)
            except ValueError:
                continue
            existing = merged.get(run.name)
            if existing is not None and existing.status != WorkflowRunStatus.ACTIVE:
                continue
            merged[run.name] = run
    return list(merged.values())


def _workflow_names(group: list[WorkflowRunState | dict]) -> list[str]:
    names: list[str] = []
    for item in group:
        if isinstance(item, WorkflowRunState):
            name = item.name
        elif isinstance(item, dict):
            name = item.get("name", "")
        else:
            name = ""
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def _persona_for_workflow_runs(
    group: list[WorkflowRunState | dict],
    *,
    fallback: str = "coordinate",
) -> str:
    personas: list[str] = []
    for item in group:
        try:
            run = item if isinstance(item, WorkflowRunState) else WorkflowRunState.model_validate(item)
        except (TypeError, ValueError):
            continue
        if run.status != WorkflowRunStatus.ACTIVE:
            continue
        personas.extend(persona.strip() for persona in run.personas if persona.strip())
    if not personas:
        return fallback or "coordinate"
    return ",".join(dict.fromkeys(personas))


def _persona_for_child_workflow(group: list[WorkflowRunState | dict], join: str) -> str:
    persona = _persona_for_workflow_runs(group, fallback="")
    if persona:
        return persona
    personas = [item for item in workflow_personas(join) if item.strip()]
    return ",".join(dict.fromkeys(personas)) or "explore"


def _interaction_mode_for_persona(persona: str) -> str:
    personas = {item.strip() for item in persona.split(",") if item.strip()}
    return InteractionMode.PLAN.value if "plan" in personas else InteractionMode.AUTO.value




def _invalidate_tui(host: object) -> None:
    app = getattr(host, "_app", None)
    invalidate = getattr(app, "invalidate", None)
    if callable(invalidate):
        invalidate()




def _task_state_for_context(value: object, fallback: TaskState | None = None) -> TaskState:
    if isinstance(value, TaskState):
        return value.model_copy(deep=True)
    if isinstance(value, dict):
        try:
            return TaskState.model_validate(value)
        except ValueError:
            pass
    if fallback is not None:
        return fallback.model_copy(deep=True)
    return TaskState()


def _clean_error_message(exc: Exception) -> str:
    import ast
    import json
    import re

    exc_str = str(exc)
    start_idx = exc_str.find('{')
    end_idx = exc_str.rfind('}')
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        dict_str = exc_str[start_idx:end_idx+1]
        prefix = exc_str[:start_idx].strip()
        while prefix and (prefix.endswith("-") or prefix.endswith(":")):
            prefix = prefix[:-1].strip()
        
        message = None
        try:
            data = ast.literal_eval(dict_str)
            if isinstance(data, dict):
                if "error" in data:
                    err = data["error"]
                    if isinstance(err, dict) and "message" in err:
                        message = err["message"]
                    elif isinstance(err, str):
                        message = err
                elif "message" in data:
                    message = data["message"]
        except Exception:
            try:
                data = json.loads(dict_str)
                if isinstance(data, dict):
                    if "error" in data:
                        err = data["error"]
                        if isinstance(err, dict) and "message" in err:
                            message = err["message"]
                        elif isinstance(err, str):
                            message = err
                    elif "message" in data:
                        message = data["message"]
            except Exception:
                pass
        
        if message:
            if prefix:
                return f"{prefix} - {message}"
            return message

    # Fallback to regex
    match = re.search(r"['\"]message['\"]\s*:\s*['\"](.*?)['\"]", exc_str)
    if match:
        msg = match.group(1)
        prefix_match = re.match(r"^(Error code: \d+)", exc_str)
        if prefix_match:
            return f"{prefix_match.group(1)} - {msg}"
        return msg

    return exc_str
