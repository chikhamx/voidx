from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from voidx.persistence.jsonl import session_dir
from voidx.presentation.transcript_adapter import TranscriptSnapshotAdapter


@pytest.mark.asyncio
async def test_clear_writes_transcript_reset_record(tmp_path, monkeypatch):
    monkeypatch.setenv("VOIDX_HOME", str(tmp_path / ".voidx"))
    adapter = TranscriptSnapshotAdapter(SimpleNamespace(get_dock=lambda: None))

    await adapter.clear("session-1")

    path = session_dir("session-1") / "transcript.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert records[-1]["type"] == "transcript_reset"
    assert records[-1]["reason"] == "clear_messages"
