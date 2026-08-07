"""Config-backed implementation of the persistent grant repository port."""

from __future__ import annotations

from voidx.tooling.domain.grants import GrantDelta


class PermissionGrantRepository:
    def __init__(self, settings) -> None:
        self._settings = settings

    def __call__(self, delta: GrantDelta) -> object:
        return self._settings.add_persistent_grant_delta(delta)
