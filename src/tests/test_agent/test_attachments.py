import sys
from pathlib import Path


from voidx.agent.attachments import (
    build_user_message_payload,
    parse_structured_content,
    serialize_message_content,
)


def test_text_at_reference_becomes_path_hint(tmp_path):
    file_path = tmp_path / "src" / "main.py"
    file_path.parent.mkdir()
    file_path.write_text("print('hi')\n", encoding="utf-8")

    payload = build_user_message_payload("review @src/main.py please", str(tmp_path))

    assert payload.clean_text == "review please"
    assert payload.content_format == "text"
    assert "Referenced paths:" in payload.content
    assert "- src/main.py" in payload.content
    assert "Attached file: src/main.py" not in payload.content
    assert "```python\nprint('hi')" not in payload.content
    assert payload.display_text == "review please\n[references: src/main.py]"
    assert payload.attachments == []


def test_image_attachment_builds_structured_multimodal_content(tmp_path):
    image_dir = tmp_path / ".voidx" / "attachments"
    image_dir.mkdir(parents=True)
    image_path = image_dir / "shot.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    payload = build_user_message_payload("describe [image-shot]", str(tmp_path))

    assert payload.content_format == "structured"
    assert isinstance(payload.content, list)
    assert payload.content[0]["type"] == "text"
    assert payload.content[1]["type"] == "image_url"
    assert payload.content[1]["image_url"]["url"].startswith("data:image/png;base64,")




def test_missing_clipboard_image_reports_not_found_warning(tmp_path):
    payload = build_user_message_payload("describe [image-missing]", str(tmp_path))

    assert payload.content_format == "text"
    assert payload.attachments == []
    assert payload.warnings == ["Image attachment not found: [image-missing]"]
def test_structured_content_round_trips():
    content = [{"type": "text", "text": "hello"}]

    saved, fmt = serialize_message_content(content)

    assert fmt == "structured"
    assert parse_structured_content(saved, fmt) == content


def test_missing_at_reference_becomes_path_hint_without_warning(tmp_path):
    payload = build_user_message_payload("explain @dataclass", str(tmp_path))

    assert payload.clean_text == "explain"
    assert "Referenced paths:" in payload.content
    assert "- dataclass" in payload.content
    assert payload.warnings == []





def test_image_filename_at_reference_is_only_path_hint(tmp_path):
    image_path = tmp_path / "shot.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    payload = build_user_message_payload("describe @shot.png", str(tmp_path))

    assert payload.content_format == "text"
    assert payload.clean_text == "describe"
    assert "Referenced paths:" in payload.content
    assert "- shot.png" in payload.content
    assert payload.attachments == []
    assert payload.warnings == []


def test_parent_relative_at_reference_is_path_hint_without_warning(tmp_path):
    outside = tmp_path.parent / "outside.py"
    outside.write_text("print('outside')\n", encoding="utf-8")

    payload = build_user_message_payload("review @../outside.py", str(tmp_path))

    assert payload.clean_text == "review"
    assert "Referenced paths:" in payload.content
    assert f"- {outside.resolve().as_posix()}" in payload.content
    assert payload.attachments == []
    assert payload.warnings == []

def test_outside_workspace_at_reference_becomes_path_hint_without_warning(tmp_path):
    outside = tmp_path.parent / "outside-note.txt"
    outside.write_text("outside\n", encoding="utf-8")

    payload = build_user_message_payload(f"review @{outside}", str(tmp_path))

    assert payload.clean_text == "review"
    assert "Referenced paths:" in payload.content
    assert f"- {outside.resolve().as_posix()}" in payload.content
    assert payload.attachments == []
    assert payload.warnings == []


def test_quoted_at_reference_preserves_spaces(tmp_path):
    file_path = tmp_path / "folder with spaces" / "main file.py"
    file_path.parent.mkdir()
    file_path.write_text("print('hi')\n", encoding="utf-8")

    payload = build_user_message_payload('review @"folder with spaces/main file.py" please', str(tmp_path))

    assert payload.clean_text == "review please"
    assert "Referenced paths:" in payload.content
    assert "- folder with spaces/main file.py" in payload.content
    assert payload.display_text == "review please\n[references: folder with spaces/main file.py]"
    assert payload.warnings == []


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
    assert payload.display_text == "use $docs please\n[references: src/main.py]"
    assert payload.content.startswith("用户指定了技能：\n- docs: Write docs\n\nuse please")
    assert "Referenced paths:" in payload.content
    assert "- src/main.py" in payload.content
    assert "Attached file: src/main.py" not in payload.content


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


def test_reference_outside_pasted_block_still_works(tmp_path):
    """@-references outside <pasted> blocks must still be parsed normally."""
    file_path = tmp_path / "main.py"
    file_path.write_text("print('hi')\n", encoding="utf-8")

    text = "review this @main.py\n<pasted>\n@pytest.mark.asyncio\n</pasted>"
    payload = build_user_message_payload(text, str(tmp_path))

    assert payload.clean_text.startswith("review this")
    assert len(payload.attachments) == 0
    assert "Referenced paths:" in payload.content
    assert "- main.py" in payload.content
    assert "@pytest.mark.asyncio" in payload.clean_text
    assert payload.warnings == [], f"unexpected warnings: {payload.warnings}"