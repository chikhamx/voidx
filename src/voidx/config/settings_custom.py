"""Legacy custom model/provider settings helpers."""

from __future__ import annotations


class SettingsCustomProviderMixin:
    async def list_custom_models(self, provider: str) -> list[str]:
        """Return user-added custom model names for a provider."""
        custom = self._data.get("custom_models", {})
        result: list[str] = []
        if not isinstance(custom, dict):
            models = []
        else:
            models = custom.get(provider, [])
        if isinstance(models, list):
            result.extend(str(model) for model in models)
        for profile in await self.list_profiles():
            if profile.provider == provider and profile.model not in result:
                result.append(profile.model)
        return result

    def add_custom_model(self, provider: str, model: str) -> None:
        """Legacy no-op. Custom models are derived from saved DB profiles."""
        _ = (provider, model)

    def remove_custom_model(self, provider: str, model: str) -> None:
        """Remove a custom model name for a provider. Saves."""
        custom = self._data.get("custom_models", {})
        if not isinstance(custom, dict):
            return
        models = custom.get(provider, [])
        if not isinstance(models, list):
            return
        if model in models:
            models.remove(model)
            if not models:
                del custom[provider]
            self._save()

    # ── custom providers ──────────────────────────────────────────────────

    def list_custom_providers(self) -> list[dict[str, str]]:
        """Return list of {name, protocol, base_url} for custom providers."""
        providers = self._data.get("custom_providers", {})
        if not isinstance(providers, dict):
            return []
        result: list[dict[str, str]] = []
        for name, fields in providers.items():
            if isinstance(fields, dict):
                result.append({
                    "name": name,
                    "protocol": fields.get("protocol", "openai"),
                    "base_url": fields.get("base_url", ""),
                })
        return result

    def add_custom_provider(self, name: str, protocol: str = "openai", base_url: str = "") -> None:
        """Legacy no-op. Provider protocol/base URL live on saved DB profiles."""
        _ = (name, protocol, base_url)

    def remove_custom_provider(self, name: str) -> None:
        """Remove a custom provider and its custom models. Saves."""
        providers = self._data.get("custom_providers", {})
        if isinstance(providers, dict) and name in providers:
            del providers[name]
        # Also remove custom models for this provider
        custom = self._data.get("custom_models", {})
        if isinstance(custom, dict) and name in custom:
            del custom[name]
        self._save()
