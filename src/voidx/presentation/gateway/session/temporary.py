"""Temporary Gateway session identity and profile validation."""

from __future__ import annotations

import uuid

_TEMPORARY_PROFILES = frozenset({"chat", "coding", "goal", "loop"})


def new_temporary_thread_id() -> str:
    return uuid.uuid4().hex[:12]


def validate_temporary_profile(profile: str) -> str:
    if profile not in _TEMPORARY_PROFILES:
        raise ValueError(f"unknown runtime profile: {profile}")
    return profile


def is_work_submission(text: str) -> bool:
    value = text.strip()
    if not value.startswith("/"):
        return True
    command, _, raw_args = value.partition(" ")
    args = raw_args.strip()
    if command == "/init":
        return True
    if command == "/goal":
        return bool(args and args not in {"help", "status", "stop"} and "--accept" in args)
    if command == "/loop":
        return bool(args and args not in {"help", "status", "stop", "resume"})
    return False
