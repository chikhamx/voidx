import pytest

from voidx.memory.session import create_session, get_session


@pytest.mark.asyncio
async def test_chat_session_persists_profile_and_global_workspace_scope(monkeypatch, tmp_path):
    import voidx.memory.store as store

    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    store._conn = None

    session = await create_session(workspace="", directory="", profile="chat")
    loaded = await get_session(session.id)

    assert loaded is not None
    assert loaded.runtime_profile == "chat"
    assert loaded.workspace == ""
    assert loaded.directory == ""


@pytest.mark.asyncio
async def test_update_session_profile_persists(monkeypatch, tmp_path):
    import voidx.memory.store as store
    from voidx.memory.session import update_session_profile

    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    store._conn = None

    session = await create_session(profile="coding")
    await update_session_profile(session.id, "loop")

    loaded = await get_session(session.id)
    assert loaded is not None
    assert loaded.runtime_profile == "loop"
