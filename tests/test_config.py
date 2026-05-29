import json
import sys

sys.path.insert(0, "src")

from voidx.config import Settings


def test_settings_lists_mcp_servers_from_voidx_json(tmp_path):
    (tmp_path / "voidx.json").write_text(
        json.dumps({
            "mcpServers": {
                "web-reader": {
                    "command": "npx",
                    "args": ["web-reader"],
                    "tools": ["read_url"],
                },
                "disabled-server": {
                    "command": "node",
                    "disabled": True,
                    "tools": {"inspect": {}},
                },
            }
        }),
        encoding="utf-8",
    )

    servers = Settings(str(tmp_path)).list_mcp_servers()

    assert [server.name for server in servers] == ["web-reader", "disabled-server"]
    assert servers[0].command == "npx"
    assert servers[0].tool_count == 1
    assert servers[1].disabled is True
    assert servers[1].tool_count == 1
