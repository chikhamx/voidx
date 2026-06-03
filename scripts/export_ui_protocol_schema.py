"""Export the UI protocol JSON Schema for frontend type generation."""

from __future__ import annotations

import json
from pathlib import Path

from voidx.ui.protocol import export_protocol_schema


def main() -> None:
    target = Path("frontend/src/protocol.schema.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(export_protocol_schema(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(target)


if __name__ == "__main__":
    main()
