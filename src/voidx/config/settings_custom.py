"""Legacy custom model/provider settings helpers."""

from __future__ import annotations


class SettingsCustomProviderMixin:
    async def list_custom_models(self, provider: str) -> list[str]:
        """Return custom model names derived from saved DB profiles."""
        result: list[str] = []
        for profile in await self.list_profiles():
            if profile.provider == provider and profile.model not in result:
                result.append(profile.model)
        return result

    # ── custom providers ──────────────────────────────────────────────────

    def list_custom_providers(self) -> list[dict[str, str]]:
        """Legacy provider definitions are no longer read at runtime."""
        return []
