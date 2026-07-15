"""Export the UI protocol JSON Schema for frontend type generation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from voidx.ui.protocol import export_protocol_schema


def main() -> None:
    target = REPO_ROOT / "frontend" / "src" / "rpc" / "protocol.schema.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(export_protocol_schema(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(target.relative_to(REPO_ROOT))


if __name__ == "__main__":
    main()
