"""Machine-readable bootstrap payloads for external UI hosts."""

from __future__ import annotations

import json
import sys
from urllib.parse import parse_qs, urlparse


def emit_web_gateway_bootstrap(url: str) -> None:
    parsed = urlparse(url)
    token = parse_qs(parsed.query).get("token", [""])[0]
    payload = {
        "type": "web_gateway",
        "url": url,
        "token": token,
    }
    sys.stderr.write(f"VOIDX_WEB_GATEWAY{json.dumps(payload, separators=(',', ':'))}\n")
    sys.stderr.flush()
