"""Public memory service boundary."""

from __future__ import annotations

from voidx.memory.context_frames import save_context_frame_from_messages
from voidx.memory.cleanup import (
    SessionDeleteCandidate,
    SessionDeletePlan,
    apply_session_delete_plan,
    plan_session_delete,
)
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
from voidx.memory.thread_store import (
    CommitResult,
    LoadedThread,
    ThreadStateConflict,
    ThreadStore,
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
    ensure_session,
    get_session,
    list_sessions,
    load_messages,
    save_message,
    touch_session,
    update_session_model,
    update_session_profile,
    update_title,
    update_title_if_current,
)
from voidx.memory.subagents import append_subagent_event
from voidx.memory.transcript import (
    TranscriptNodeRow,
    TranscriptTurnRow,
    append_transcript_summary,
    load_transcript,
    replace_transcript,
)
from voidx.memory import session as _session


def memory_now() -> str:
    return _session._now()

__all__ = [
    "MessageRow",
    "MessageRuntimeSnapshot",
    "ModelProfileRow",
    "RuntimeStateSnapshot",
    "SessionDeleteCandidate",
    "SessionDeletePlan",
    "SessionInfo",
    "TranscriptNodeRow",
    "ThreadStore",
    "ThreadStateConflict",
    "LoadedThread",
    "CommitResult",
    "TranscriptTurnRow",
    "append_subagent_event",
    "append_transcript_summary",
    "apply_session_delete_plan",
    "clear_messages",
    "clear_runtime_state",
    "count_messages",
    "create_session",
    "delete_messages_from",
    "delete_messages_through",
    "delete_model_profile_async",
    "delete_session",
    "ensure_session",
    "get_model_profile_async",
    "get_session",
    "list_sessions",
    "list_model_profiles_async",
    "load_messages",
    "load_runtime_state",
    "load_transcript",
    "memory_now",
    "plan_session_delete",
    "replace_transcript",
    "save_context_frame_from_messages",
    "save_message",
    "save_message_runtime_snapshot",
    "save_model_profile_async",
    "save_runtime_state",
    "touch_session",
    "update_session_model",
    "update_session_profile",
    "update_title",
    "update_title_if_current",
]
