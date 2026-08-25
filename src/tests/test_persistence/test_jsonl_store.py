from __future__ import annotations

import asyncio

import pytest

import voidx.persistence.jsonl as jsonl_store
import voidx.persistence.sqlite as store


@pytest.mark.asyncio
async def test_concurrent_session_appends_preserve_records_and_offsets(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    session_id = "session-1"

    offsets = await asyncio.gather(
        *(
            jsonl_store.append_session_record(
                session_id,
                "events.jsonl",
                {"index": index},
            )
            for index in range(40)
        )
    )

    records = await jsonl_store.read_session_records(session_id, "events.jsonl")
    assert records is not None
    assert sorted(record["index"] for record in records) == list(range(40))
    assert len(set(offsets)) == 40


@pytest.mark.asyncio
async def test_delete_session_file_rejects_non_context_digit_jsonl(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    session_id = "session-1"
    session_path = tmp_path / "sessions" / session_id
    (session_path / "context").mkdir(parents=True)
    (session_path / "messages.jsonl").write_text('{"type":"keep"}\n', encoding="utf-8")
    (session_path / "context" / "deletes.jsonl").write_text('{"type":"keep"}\n', encoding="utf-8")
    (session_path / "context" / "12.jsonl").write_text('{"role":"user"}\n', encoding="utf-8")

    for filename in (
        "messages.jsonl",
        "context/deletes.jsonl",
        "context/../messages.jsonl",
        "context/not-digits.jsonl",
        "transcript.jsonl",
    ):
        with pytest.raises(ValueError, match="refusing to delete session file"):
            await jsonl_store.delete_session_file(session_id, filename)

    assert (session_path / "messages.jsonl").exists()
    assert (session_path / "context" / "deletes.jsonl").exists()
    assert await jsonl_store.delete_session_file(session_id, "context/12.jsonl") is True
    assert not (session_path / "context" / "12.jsonl").exists()
    assert await jsonl_store.delete_session_file(session_id, "context/12.jsonl") is False
