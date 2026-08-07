"""Agent-owned display labels for agent identities."""

from __future__ import annotations

import hashlib

SUBAGENT_DISPLAY_NAMES: tuple[str, ...] = (
    "Athena",
    "Orion",
    "Nova",
    "Lyra",
    "Vega",
    "Quill",
    "Pixel",
    "Cipher",
    "Echo",
    "Flux",
    "Helix",
    "Iris",
    "Juno",
    "Kai",
    "Lumen",
    "Mira",
    "Nexus",
    "Onyx",
    "Prism",
    "Rune",
    "Sol",
    "Tess",
    "Umbra",
    "Vesper",
)


def agent_display_name(agent: object) -> str:
    raw = str(agent or "").strip()
    return raw or "Agent"


def subagent_display_name(seed: object) -> str:
    raw = str(seed or "").strip() or "subagent"
    digest = hashlib.sha1(raw.encode("utf-8")).digest()
    index = int.from_bytes(digest[:4], "big") % len(SUBAGENT_DISPLAY_NAMES)
    return SUBAGENT_DISPLAY_NAMES[index]
