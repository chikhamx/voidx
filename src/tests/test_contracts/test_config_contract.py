from __future__ import annotations

from voidx.config import Config
from voidx.config.settings import GLOBAL_KEYS, WORKSPACE_ONLY_KEYS

from .snapshot import assert_snapshot


def test_config_contract() -> None:
    assert_snapshot(
        "config.json",
        {
            "default": Config().model_dump(mode="json"),
            "global_keys": sorted(GLOBAL_KEYS),
            "workspace_only_keys": sorted(WORKSPACE_ONLY_KEYS),
        },
    )
