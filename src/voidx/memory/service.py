"""Public memory service boundary."""

from __future__ import annotations

from voidx.memory.context_frames import save_context_frame_from_messages
from voidx.memory.model_profiles import (
    ModelProfileRow,
    delete_model_profile_async,
    get_model_profile_async,
    list_model_profiles_async,
    save_model_profile_async,
)
from voidx.memory.runtime_state import (
    MessageRuntimeSnapshot,
    RuntimeStateSnapshot,
    clear_runtime_state,
    load_runtime_state,
    save_message_runtime_snapshot,
    save_runtime_state,
)
from voidx.memory.session import (
    MessageRow,
    SessionInfo,
    clear_messages,
    count_messages,
    create_session,
    delete_messages_from,
    delete_messages_through,
    delete_session,
    get_session,
    list_sessions,
    load_messages,
    save_message,
    touch_session,
    update_session_model,
    update_title,
    update_title_if_current,
)
from voidx.memory.transcript import TranscriptNodeRow, TranscriptTurnRow, load_transcript, replace_transcript
from voidx.memory import session as _session


def memory_now() -> str:
    return _session._now()

__all__ = [
    "MessageRow",
    "MessageRuntimeSnapshot",
    "ModelProfileRow",
    "RuntimeStateSnapshot",
    "SessionInfo",
    "TranscriptNodeRow",
    "TranscriptTurnRow",
    "clear_messages",
    "clear_runtime_state",
    "count_messages",
    "create_session",
    "delete_messages_from",
    "delete_messages_through",
    "delete_model_profile_async",
    "delete_session",
    "get_model_profile_async",
    "get_session",
    "list_sessions",
    "list_model_profiles_async",
    "load_messages",
    "load_runtime_state",
    "load_transcript",
    "memory_now",
    "replace_transcript",
    "save_context_frame_from_messages",
    "save_message",
    "save_message_runtime_snapshot",
    "save_model_profile_async",
    "save_runtime_state",
    "touch_session",
    "update_session_model",
    "update_title",
    "update_title_if_current",
]
