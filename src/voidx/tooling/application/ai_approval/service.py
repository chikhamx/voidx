"""Stateless, fail-closed AI reviewer for approvable permission decisions."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Literal

from voidx.tooling.domain.ai_approval import AiApprovalModelConfig, AiApprovalRequestItem, AiApprovalResponse, AiApprovalResult
from .parsing import validate_ai_approval_response
from .prompt import ai_approval_system_prompt
from voidx.tooling.policy.ai_approval_redaction import project_tool_args


def _structured_accepts_keyword(callable_obj, keyword: str) -> bool:
    import inspect
    try:
        parameters = inspect.signature(callable_obj).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(
        parameter.name == keyword or parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


class _AiApprovalTransientError(Exception):
    def __init__(self, reason: Literal["timeout", "connection_error"]):
        self.reason = reason
        super().__init__(reason)


def _classify_ai_approval_failure(exc: Exception) -> Literal["timeout", "connection_error", "error"]:
    class_names = {base.__name__.lower() for base in type(exc).__mro__}
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or any(
        "timeout" in name or name == "deadlineexceeded"
        for name in class_names
    ):
        return "timeout"
    if isinstance(exc, (ConnectionError, OSError)) or any(
        "connection" in name
        or name in {"connecterror", "networkerror", "transporterror", "requesterror", "serviceunavailable"}
        for name in class_names
    ):
        return "connection_error"
    return "error"


def is_ai_approval_candidate(decision) -> bool:
    raw_action = getattr(decision, "action", None)
    action = getattr(raw_action, "value", raw_action)
    if action != "ask":
        return False
    risk = getattr(decision, "risk", None)
    if risk is None:
        return False
    raw_level = getattr(risk, "level", None)
    level = getattr(raw_level, "value", raw_level)
    return level in {"dangerous", "extreme"}



class AiApprovalService:
    """Stateless, fail-closed reviewer for approvable permission decisions."""

    def __init__(self, model_factory=None, resolver_model_factory=None) -> None:
        self._model_factory = model_factory
        self._resolver_model_factory = resolver_model_factory

    async def review(self, decisions, settings) -> AiApprovalResult:
        if settings is None:
            return AiApprovalResult(reason="unavailable")
        skipped_reasons: dict[str, str] = {}
        try:
            if self._model_factory is None or self._resolver_model_factory is None:
                return AiApprovalResult(reason="unavailable")

            config = settings.get_ai_approval_config()
            profiles = await settings.list_profiles()
            profile = None
            if config.profile_name:
                profile = next((item for item in profiles if item.name == config.profile_name), None)
            else:
                profile = await settings.resolve_profile()
            if profile is None or not profile.api_key:
                return AiApprovalResult(reason="unavailable")

            items = []
            for decision in decisions:
                risk = getattr(decision, "risk", None)
                tool_call = getattr(decision, "tool_call", decision)
                if not is_ai_approval_candidate(decision):
                    continue
                call_id = str(tool_call.get("id", ""))
                if not call_id:
                    return AiApprovalResult(reason="invalid_response")
                args = tool_call.get("args", {})
                try:
                    projected, digest = project_tool_args(args, tool_name=tool_call.get("name", ""))
                except (TypeError, ValueError):
                    skipped_reasons[call_id] = "tool arguments could not be safely projected"
                    continue
                if projected is None:
                    skipped_reasons[call_id] = "tool is not supported by AI approval"
                    continue
                items.append(AiApprovalRequestItem(
                    id=call_id,
                    tool_name=str(tool_call.get("name", "")),
                    pattern=(
                        str(projected.get("command", ""))
                        if tool_call.get("name") in {"bash", "powershell"}
                        else risk.pattern
                    ),
                    risk_level=getattr(risk.level, "value", risk.level),
                    risk_tags=tuple(getattr(tag, "value", str(tag)) for tag in risk.tags),
                    risk_reason=risk.reason,
                    args=projected,
                    args_sha256=digest,
                ))
            ids = [item.id for item in items]
            if not ids:
                reason = "skipped" if skipped_reasons else "invalid_response"
                return AiApprovalResult(reason=reason, skipped_reasons=skipped_reasons)
            if len(set(ids)) != len(ids):
                return AiApprovalResult(reason="invalid_response", skipped_reasons=skipped_reasons)

            model_config = AiApprovalModelConfig(
                provider=profile.provider,
                model=profile.model,
                base_url=profile.base_url,
                protocol=profile.protocol,
            )
            model = self._model_factory(profile.api_key, model_config)
            resolver = self._resolver_model_factory(model, model_config)
            from voidx.llm.structured import ainvoke_structured
            if not callable(getattr(resolver, "with_structured_output", None)):
                return AiApprovalResult(reason="unavailable", skipped_reasons=skipped_reasons)
            from langchain_core.messages import HumanMessage, SystemMessage
            payload = json.dumps([item.model_dump(mode="json") for item in items], ensure_ascii=False, sort_keys=True)
            if len(payload.encode("utf-8")) > 48 * 1024:
                for call_id in ids:
                    skipped_reasons[call_id] = "approval batch exceeds the 48 KiB limit"
                return AiApprovalResult(reason="skipped", skipped_reasons=skipped_reasons)
            from voidx.platform.retry import retry_async

            base_method = getattr(resolver, "resolver_structured_output_method", None)
            if base_method not in {"json_mode", "function_calling"}:
                base_method = "function_calling"
            messages = [
                SystemMessage(content=ai_approval_system_prompt()),
                HumanMessage(content=payload),
            ]

            async def _invoke_with_method(method: str) -> AiApprovalResult:
                async def invoke_once():
                    try:
                        response = await ainvoke_structured(
                            model=resolver,
                            schema=AiApprovalResponse,
                            messages=messages,
                            method=method,
                            include_raw=True,
                            timeout=config.timeout_seconds,
                        )
                        return validate_ai_approval_response(response, set(ids))
                    except Exception as exc:
                        reason = _classify_ai_approval_failure(exc)
                        if reason in {"timeout", "connection_error"}:
                            raise _AiApprovalTransientError(reason) from exc
                        raise

                return await retry_async(
                    invoke_once,
                    max_attempts=2,
                    base_delay=0.1,
                    max_delay=0.5,
                    jitter=False,
                    label="ai_approval",
                    retry_on=_AiApprovalTransientError,
                )

            result = await _invoke_with_method(base_method)
            if (
                result.reason == "invalid_response"
                and base_method != "json_mode"
            ):
                result = await _invoke_with_method("json_mode")
            return result.model_copy(update={"skipped_reasons": skipped_reasons})
        except _AiApprovalTransientError as exc:
            return AiApprovalResult(reason=exc.reason, skipped_reasons=skipped_reasons)
        except Exception as exc:
            return AiApprovalResult(
                reason=_classify_ai_approval_failure(exc),
                skipped_reasons=skipped_reasons,
            )
