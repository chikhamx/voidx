from voidx.agent.tool_messages import sanitize_tool_message_content


def test_sanitize_tool_message_content_redacts_paths_secrets_and_length(tmp_path):
    text = (
        f"failed at {tmp_path}/service.py with token=abc123 "
        "Authorization: Bearer secret-token "
        + ("x" * 120)
    )

    sanitized = sanitize_tool_message_content(text, workspace=str(tmp_path), max_chars=90)

    assert str(tmp_path) not in sanitized
    assert "<workspace>/service.py" in sanitized
    assert "abc123" not in sanitized
    assert "secret-token" not in sanitized
    assert "token=[redacted]" in sanitized
    assert "Bearer [redacted]" in sanitized
    assert "Tool output truncated" in sanitized
