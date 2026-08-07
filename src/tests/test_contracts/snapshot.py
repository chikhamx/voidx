from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "contracts"


def assert_snapshot(name: str, actual: Any) -> None:
    expected = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    if actual == expected:
        return
    expected_text = json.dumps(expected, indent=2, ensure_ascii=False, sort_keys=True).splitlines()
    actual_text = json.dumps(actual, indent=2, ensure_ascii=False, sort_keys=True).splitlines()
    diff = "\n".join(
        difflib.unified_diff(expected_text, actual_text, fromfile=name, tofile="actual", n=3)
    )
    raise AssertionError(f"contract snapshot mismatch:\n{diff}")
