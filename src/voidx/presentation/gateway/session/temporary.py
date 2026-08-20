"""Temporary Gateway session identity and profile validation."""

from __future__ import annotations

import uuid


def new_temporary_thread_id() -> str:
    return uuid.uuid4().hex[:12]



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
