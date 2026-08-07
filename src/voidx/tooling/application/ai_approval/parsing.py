"""Lenient parsing for AI approval responses."""

from __future__ import annotations

import json

from voidx.tooling.domain.ai_approval import AiApprovalItemResult, AiApprovalResponse, AiApprovalResult


def _extract_decision_from_text(text: str, expected_ids: set[str]) -> AiApprovalResponse | None:
    """Extract allow/deny from free-form model text when structured output fails.

    Looks for the last occurrence of ALLOW/DENY keywords. All expected ids
    receive the same decision. Returns None if no keyword is found.
    """
    lowered = text.lower()
    last_allow = lowered.rfind("allow")
    last_deny = lowered.rfind("deny")
    if last_allow == -1 and last_deny == -1:
        return None
    decision = "allow" if last_allow > last_deny else "deny"
    return AiApprovalResponse(decisions=[
        AiApprovalItemResult(id=call_id, decision=decision)
        for call_id in expected_ids
    ])


_DECISION_KEYS = ("decision", "verdict", "result", "action", "approved", "allow", "allowed", "approve", "outcome")
_BOOL_KEYS = ("approved", "allow", "allowed", "approve")


def _normalize_decision_items(payload: dict) -> dict:
    """Normalize variant field names model outputs use for decisions.

    Maps various field names (``verdict``, ``approved``, ``result``, etc.)
    and value shapes (``true/false``, ``"allow"/"deny"``) to the canonical
    ``decision: "allow"|"deny"`` shape.
    """
    decisions = payload.get("decisions")
    if not isinstance(decisions, list):
        return payload
    normalized = []
    for item in decisions:
        if not isinstance(item, dict):
            normalized.append(item)
            continue
        item = dict(item)
        if "decision" not in item:
            for key in _DECISION_KEYS:
                if key in item:
                    raw_val = item.pop(key)
                    if key in _BOOL_KEYS:
                        item["decision"] = "allow" if _truthy(raw_val) else "deny"
                    else:
                        item["decision"] = _coerce_decision_string(raw_val)
                    break
        normalized.append(item)
    return {**payload, "decisions": normalized}


def _coerce_decision_string(value) -> str:
    if isinstance(value, bool):
        return "allow" if value else "deny"
    text = str(value).lower().strip()
    if text in {"allow", "approve", "approved", "yes", "true", "1", "safe"}:
        return "allow"
    return "deny"


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"true", "yes", "allow", "1"}
    return bool(value)


def _coerce_ai_approval_payload(raw, expected_ids: set[str] | None = None):
    """Normalize varied model outputs into an AiApprovalResponse.

    Handles: AiApprovalResponse, dict with ``decisions``, bare list of
    decisions (common from json_mode), ``include_raw`` wrapper
    (``{"raw": ..., "parsed": ...}``), JSON strings, and free-form text
    extraction as a last resort.
    """
    import json as _json

    payload = raw
    if isinstance(payload, dict) and "parsed" in payload:
        payload = payload["parsed"]
    if isinstance(payload, str):
        try:
            payload = _json.loads(payload)
        except (ValueError, TypeError):
            if expected_ids:
                return _extract_decision_from_text(payload, expected_ids)
            return None
    if isinstance(payload, list):
        payload = {"decisions": payload}
    if isinstance(payload, AiApprovalResponse):
        return payload
    if isinstance(payload, dict):
        payload = _normalize_decision_items(payload)
        return AiApprovalResponse.model_validate(payload)
    if expected_ids and isinstance(raw, dict):
        raw_msg = raw.get("raw")
        text = getattr(raw_msg, "content", None)
        if isinstance(text, list):
            text = " ".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in text
            )
        if isinstance(text, str):
            return _extract_decision_from_text(text, expected_ids)
    return None


def validate_ai_approval_response(raw, expected_ids: set[str] | frozenset[str]) -> AiApprovalResult:
    expected = set(expected_ids)
    if not expected or any(not item for item in expected):
        return AiApprovalResult(reason="invalid_response")
    try:
        response = _coerce_ai_approval_payload(raw, expected)
        if response is None:
            return AiApprovalResult(reason="invalid_response")
        ids = [item.id for item in response.decisions]
        if len(ids) != len(expected) or set(ids) != expected or len(set(ids)) != len(ids):
            return AiApprovalResult(reason="invalid_response")
        return AiApprovalResult(
            allowed_ids=frozenset(item.id for item in response.decisions if item.decision == "allow"),
            reviewed_ids=frozenset(expected),
            denied_reasons={
                item.id: item.reason
                for item in response.decisions
                if item.decision == "deny" and item.reason
            },
            reason="reviewed",
        )
    except Exception:
        return AiApprovalResult(reason="invalid_response")
