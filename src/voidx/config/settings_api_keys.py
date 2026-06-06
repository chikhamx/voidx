"""Settings API-key and provider lookup helpers."""

from __future__ import annotations


class SettingsApiKeyMixin:
    async def resolve_api_key(self, provider: str) -> str | None:
        runtime = self._runtime_keys.get(provider)
        if runtime:
            return runtime
        for p in await self.list_profiles():
            if p.provider == provider:
                return p.api_key
        return None

    def set_runtime_api_key(self, provider: str, key: str) -> None:
        self._runtime_keys[provider] = key

    async def resolve_base_url(self, provider: str) -> str | None:
        for p in await self.list_profiles():
            if p.provider == provider and p.base_url:
                return p.base_url
        for cp in self.list_custom_providers():
            if cp["name"] == provider and cp["base_url"]:
                return cp["base_url"]
        return None

    async def resolve_protocol(self, provider: str) -> str | None:
        for p in await self.list_profiles():
            if p.provider == provider and p.protocol:
                return p.protocol
        for cp in self.list_custom_providers():
            if cp["name"] == provider:
                return cp["protocol"]
        return None

    # ── tavily API key ─────────────────────────────────────────────────────

    def get_tavily_api_key(self) -> str | None:
        """Get Tavily API key. Env var TAVILY_API_KEY takes priority over config file."""
        import os
        env_key = os.environ.get("TAVILY_API_KEY")
        if env_key:
            return env_key
        return self._data.get("tavily_api_key") or None

    def set_tavily_api_key(self, api_key: str) -> None:
        self._data["tavily_api_key"] = api_key
        self._save()

    def delete_tavily_api_key(self) -> None:
        self._data.pop("tavily_api_key", None)
        self._save()
