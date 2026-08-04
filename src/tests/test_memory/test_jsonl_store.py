from __future__ import annotations

import asyncio

import pytest

import voidx.memory.jsonl_store as jsonl_store
import voidx.memory.store as store


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

