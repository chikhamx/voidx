"""Session management shim — backed by voidx_core PySessionStore."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SessionInfo:
    id: str
    workspace: str
    model_provider: str | None = None
    model_name: str | None = None
    title: str = ""
    created_at: str = ""
    updated_at: str = ""
    message_count: int = 0


async def create_session(
    workspace: str,
    provider: str | None = None,
    model: str | None = None,
    title: str = "",
) -> SessionInfo:
    """Create a new session using Rust SessionStore."""
    store = _get_store(workspace)
    py_session = store.create_session(workspace, provider, model)
    return SessionInfo(
        id=py_session.id,
        workspace=py_session.workspace,
        model_provider=provider,
        model_name=model,
        title=title or "New session",
    )


async def get_session(session_id: str, workspace: str = ".") -> SessionInfo | None:
    """Get a session by ID."""
    store = _get_store(workspace)
    sessions = store.list_sessions()
    for s in sessions:
        if s.id == session_id:
            return SessionInfo(
                id=s.id,
                workspace=s.workspace,
                title="",
                created_at=s.created_at,
                updated_at=s.updated_at,
            )
    return None


async def list_sessions(workspace: str = ".") -> list[SessionInfo]:
    """List all sessions from Rust store."""
    store = _get_store(workspace)
    sessions = store.list_sessions()
    return [
        SessionInfo(
            id=s.id,
            workspace=s.workspace,
            title=f"Session {s.id[:8]}",
            created_at=s.created_at,
            updated_at=s.updated_at,
        )
        for s in sessions
    ]


async def latest_session_for_workspace(workspace: str) -> SessionInfo | None:
    """Return the most recently updated session."""
    sessions = await list_sessions(workspace)
    if not sessions:
        return None
    return sessions[0]  # Already sorted by updated_at DESC


async def touch_session(session_id: str, workspace: str = ".") -> None:
    """Update session timestamp (no-op in Rust — auto on message append)."""
    pass


async def update_title(session_id: str, title: str, workspace: str = ".") -> None:
    """Title update (stored as metadata in Rust)."""
    pass


async def clear_messages(session_id: str, workspace: str = ".") -> None:
    """Clear messages (not yet implemented in Rust shim)."""
    pass


async def delete_messages_from(message_id: int, workspace: str = ".") -> None:
    """Delete messages from a point onward (not yet implemented)."""
    pass


# ── Session store singleton (per workspace) ────────────────────────────

_stores: dict[str, object] = {}


def _get_store(workspace: str) -> object:
    """Get or create a PySessionStore for a workspace."""
    import voidx_core
    from pathlib import Path

    store_path = str(Path(workspace) / ".voidx" / "sessions.db")
    Path(store_path).parent.mkdir(parents=True, exist_ok=True)

    if store_path not in _stores:
        _stores[store_path] = voidx_core.SessionStore.open(store_path)
    return _stores[store_path]
