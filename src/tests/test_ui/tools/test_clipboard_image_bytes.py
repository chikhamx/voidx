"""save_clipboard_image_bytes — desktop/web client-supplied clipboard images."""

from __future__ import annotations

import base64

import pytest

from voidx.ui.gateway.session.core import GatewaySession
from voidx.ui.output.dock import BottomInputDock
from voidx.ui.protocol.v2.envelope import JsonRpcRequest, JsonRpcResult
from voidx.ui.tools.clipboard_image import KEEP_ORIGINAL_BYTES, save_clipboard_image_bytes


def test_small_image_bytes_kept_as_png(tmp_path):
    result = save_clipboard_image_bytes(
        str(tmp_path),
        b"x" * 1024,
        name_factory=lambda: "clip",
    )

    assert result.ok
    assert result.rel_path == ".voidx/attachments/clip.png"
    assert result.compressed is False
    assert (tmp_path / result.rel_path).read_bytes() == b"x" * 1024


def test_large_image_bytes_uses_compressed_jpeg(tmp_path):
    def compress(source, destination) -> bool:
        destination.write_bytes(b"j" * 2048)
        return True

    result = save_clipboard_image_bytes(
        str(tmp_path),
        b"x" * (KEEP_ORIGINAL_BYTES + 1),
        compress_image=compress,
        name_factory=lambda: "clip",
    )

    assert result.ok
    assert result.rel_path == ".voidx/attachments/clip.jpg"
    assert result.compressed is True
    assert not (tmp_path / ".voidx/attachments/clip.png").exists()


def test_empty_image_bytes_rejected(tmp_path):
    result = save_clipboard_image_bytes(str(tmp_path), b"", name_factory=lambda: "clip")

    assert not result.ok
    assert not (tmp_path / ".voidx/attachments").exists() or not list(
        (tmp_path / ".voidx/attachments").iterdir()
    )


@pytest.mark.asyncio
async def test_attachments_save_image_rpc(tmp_path):
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1", workspace=str(tmp_path))

    payload = base64.b64encode(b"png-bytes" * 100).decode("ascii")
    result = await session.dispatch_request(
        JsonRpcRequest(id=1, method="attachments.saveImage", params={"data_base64": payload}),
    )

    assert isinstance(result, JsonRpcResult)
    assert result.result["ok"] is True
    stem = result.result["stem"]
    saved = list((tmp_path / ".voidx/attachments").glob(f"{stem}.*"))
    assert saved and saved[0].read_bytes() == b"png-bytes" * 100


@pytest.mark.asyncio
async def test_attachments_save_image_rejects_invalid_base64(tmp_path):
    dock = BottomInputDock()
    session = GatewaySession(lambda: dock.tree, thread_id="t1", workspace=str(tmp_path))

    result = await session.dispatch_request(
        JsonRpcRequest(id=2, method="attachments.saveImage", params={"data_base64": "!!!"}),
    )

    assert hasattr(result, "error") or result.result.get("ok") is False
