from tui_helpers import _rich_plain, _tui


def test_render_choice_overlay_groups_batch_wait_tools(tmp_path):
    tui = _tui(tmp_path)
    tui._active_choice = [("Yes", "y", ""), ("No", "n", "")]
    tui._choice_selected = 0
    tui._choice_prompt = "Allow tool use?"
    tui._choice_details = [
        {"name": "Wait", "pattern": "xxx"},
        {"name": "Wait", "pattern": "yyy"},
        {"name": "Wait", "pattern": "zzz"},
    ]

    lines = tui._render_choice_overlay(100)

    plain_lines = [_rich_plain(line) for line in lines]
    assert any('Wait("xxx", "yyy", "zzz")' in line for line in plain_lines)
    assert sum('Wait("xxx", "yyy", "zzz")' in line for line in plain_lines) == 1
