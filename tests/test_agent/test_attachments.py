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


def test_user_message_prefix_removes_extra_spans_without_changing_display(tmp_path):
    file_path = tmp_path / "src" / "main.py"
    file_path.parent.mkdir()
    file_path.write_text("print('hi')\n", encoding="utf-8")

    payload = build_user_message_payload(
        "use $docs @src/main.py please",
        str(tmp_path),
        text_prefix="用户指定了技能：\n- docs: Write docs",
        extra_removed_spans=[(4, 9)],
    )

    assert payload.clean_text == "use please"
    assert payload.display_text == "use $docs please\n[attachments: src/main.py]"
    assert payload.content.startswith("用户指定了技能：\n- docs: Write docs\n\nuse please")
    assert "Attached file: src/main.py" in payload.content


def test_pasted_text_at_reference_not_parsed_as_attachment(tmp_path):
    """@-references inside <pasted> blocks must NOT be parsed as attachments.

    When a user pastes code containing decorators (e.g. @pytest.mark.asyncio),
    the pasted content is wrapped in <pasted>...</pasted> tags. The attachment
    parser must skip these tags — the @ inside is Python syntax, not a file
    attachment reference.
    """
    pasted_code = "<pasted>\n@pytest.mark.asyncio\nasync def test_foo():\n    pass\n</pasted>"
    payload = build_user_message_payload(pasted_code, str(tmp_path))

    assert payload.warnings == [], f"unexpected warnings: {payload.warnings}"
    assert payload.attachments == []
    # The @pytest.mark.asyncio should remain in the clean text
    assert "@pytest.mark.asyncio" in payload.clean_text


def test_pasted_text_image_token_not_parsed_as_attachment(tmp_path):
    """[image-...] tokens inside <pasted> blocks must NOT be parsed as attachments."""
    pasted_text = "<pasted>\nHere is a screenshot [image-screenshot.png]\n</pasted>"
    payload = build_user_message_payload(pasted_text, str(tmp_path))

    assert payload.warnings == [], f"unexpected warnings: {payload.warnings}"
    assert payload.attachments == []


def test_attachment_outside_pasted_block_still_works(tmp_path):
    """@-references outside <pasted> blocks must still be parsed normally."""
    file_path = tmp_path / "main.py"
    file_path.write_text("print('hi')\n", encoding="utf-8")

    text = "review this @main.py\n<pasted>\n@pytest.mark.asyncio\n</pasted>"
    payload = build_user_message_payload(text, str(tmp_path))

    # @main.py outside pasted block → attachment found
    assert len(payload.attachments) == 1
    assert payload.attachments[0].rel_path == "main.py"
    # @pytest.mark.asyncio inside pasted block → NOT an attachment
    assert payload.warnings == [], f"unexpected warnings: {payload.warnings}"