"""TUI passes mcp_catalog_provider catalog to list_mcp_candidates."""

from pathlib import Path

from voidx_cli.app import PureTui
from tui_helpers import _tui


def test_skill_matches_passes_catalog_provider_to_list_mcp_candidates(tmp_path: Path, monkeypatch):
    import voidx_cli.panels as panels

    captured_catalogs = []

    def fake_list_mcp_candidates(workspace, query, limit=8, *, settings=None, catalog=None):
        captured_catalogs.append(catalog)
        return []

    monkeypatch.setattr(panels, "list_mcp_candidates", fake_list_mcp_candidates)
    monkeypatch.setattr(panels, "list_skill_candidates", lambda *a, **k: [])

    tui = _tui(tmp_path)
    fake_catalog = [{"name": "tavily"}]
    tui.set_mcp_catalog_provider(lambda: fake_catalog)

    tui._input_lines = ["#tav"]
    tui._cursor_col = len("#tav")
    tui._skill_matches()

    assert captured_catalogs == [fake_catalog]


def test_skill_matches_passes_none_when_no_provider(tmp_path: Path, monkeypatch):
    import voidx_cli.panels as panels

    captured_catalogs = []

    def fake_list_mcp_candidates(workspace, query, limit=8, *, settings=None, catalog=None):
        captured_catalogs.append(catalog)
        return []

    monkeypatch.setattr(panels, "list_mcp_candidates", fake_list_mcp_candidates)
    monkeypatch.setattr(panels, "list_skill_candidates", lambda *a, **k: [])

    tui = _tui(tmp_path)

    tui._input_lines = ["#tav"]
    tui._cursor_col = len("#tav")
    tui._skill_matches()

    assert captured_catalogs == [None]
