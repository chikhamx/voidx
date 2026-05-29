import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from voidx.agent.attachments import (
    build_user_message_payload,
    parse_structured_content,
    serialize_message_content,
)


def test_text_attachment_is_embedded_in_user_message(tmp_path):
    file_path = tmp_path / "src" / "main.py"
    file_path.parent.mkdir()
    file_path.write_text("print('hi')\n", encoding="utf-8")

    payload = build_user_message_payload("review @src/main.py please", str(tmp_path))

    assert payload.clean_text == "review please"
    assert payload.content_format == "text"
    assert "Attached file: src/main.py" in payload.content
    assert "```python\nprint('hi')" in payload.content
    assert payload.display_text == "review please\n[attachments: src/main.py]"


def test_image_attachment_builds_structured_multimodal_content(tmp_path):
    image_path = tmp_path / "shot.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    payload = build_user_message_payload("describe @shot.png", str(tmp_path))

    assert payload.content_format == "structured"
    assert isinstance(payload.content, list)
    assert payload.content[0]["type"] == "text"
    assert payload.content[1]["type"] == "image_url"
    assert payload.content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_structured_content_round_trips():
    content = [{"type": "text", "text": "hello"}]

    saved, fmt = serialize_message_content(content)

    assert fmt == "structured"
    assert parse_structured_content(saved, fmt) == content


def test_missing_attachment_token_is_preserved(tmp_path):
    payload = build_user_message_payload("explain @dataclass", str(tmp_path))

    assert payload.clean_text == "explain @dataclass"
    assert "Attachment not found: dataclass" in payload.warnings
