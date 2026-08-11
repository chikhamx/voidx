from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from voidx.agent.adapters.langgraph.runtime.core.context import rebuild_llm_messages


def _rebuild(messages):
    rebuilt, convergence_messages, convergence_forced = rebuild_llm_messages(
        messages,
        [],
        allow_inline_compaction=False,
        compaction_happened=False,
        inline_compaction_guide_for=lambda _messages: None,
    )
    assert convergence_messages == []
    assert convergence_forced is False
    return rebuilt


def _image_message(text: str = "describe this") -> HumanMessage:
    return HumanMessage(content=[
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,image"}},
        {"type": "image", "source": {"type": "base64", "data": "image"}},
    ])


def test_rebuild_keeps_images_until_the_message_is_consumed():
    image_message = _image_message()

    rebuilt = _rebuild([image_message])

    assert rebuilt[0].content == image_message.content


def test_rebuild_removes_consumed_images_and_preserves_text():
    image_message = _image_message()

    rebuilt = _rebuild([
        image_message,
        AIMessage(
            content="",
            tool_calls=[{
                "id": "read-1",
                "name": "read",
                "args": {"file_path": "f.py"},
                "type": "tool_call",
            }],
        ),
        ToolMessage(content="contents", tool_call_id="read-1"),
    ])

    assert rebuilt[0].content == [{"type": "text", "text": "describe this"}]
    assert image_message.content[1]["type"] == "image_url"
    assert image_message.content[2]["type"] == "image"


def test_rebuild_removes_old_images_but_keeps_new_unconsumed_images():
    old_image = _image_message("old")
    new_image = _image_message("new")

    rebuilt = _rebuild([
        old_image,
        AIMessage(content="done"),
        new_image,
    ])

    assert rebuilt[0].content == [{"type": "text", "text": "old"}]
    assert rebuilt[2].content == new_image.content


def test_rebuild_uses_empty_text_when_consumed_message_only_had_images():
    image_message = HumanMessage(content=[
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,image"}},
    ])

    rebuilt = _rebuild([image_message, AIMessage(content="done")])

    assert rebuilt[0].content == ""


def test_rebuild_does_not_revive_images_when_old_file_tool_group_is_trimmed():
    image_message = _image_message()
    old_read = AIMessage(
        content="",
        tool_calls=[{
            "id": "old-read",
            "name": "read",
            "args": {"file_path": "f.py"},
            "type": "tool_call",
        }],
    )
    new_read = AIMessage(
        content="",
        tool_calls=[{
            "id": "new-read",
            "name": "read",
            "args": {"file_path": "f.py"},
            "type": "tool_call",
        }],
    )

    rebuilt = _rebuild([
        image_message,
        old_read,
        ToolMessage(content="1\told", tool_call_id="old-read"),
        new_read,
        ToolMessage(content="1\tnew", tool_call_id="new-read"),
    ])

    assert rebuilt[0].content == [{"type": "text", "text": "describe this"}]
    assert not any(
        isinstance(message, ToolMessage) and message.tool_call_id == "old-read"
        for message in rebuilt
    )
    assert image_message.content[1]["type"] == "image_url"
