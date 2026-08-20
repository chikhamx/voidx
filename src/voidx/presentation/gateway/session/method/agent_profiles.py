"""Agent profile JSON-RPC method handlers for GatewaySession."""

from __future__ import annotations

from collections.abc import Mapping

from voidx.agent.facade import (
    AgentProfileConflict,
    AgentProfileNotFound,
    AgentProfileReadOnly,
    AgentProfileValidationError,
    delete_agent_profile,
    get_agent_profile,
    list_agent_profiles,
    save_agent_profile,
    validate_agent_profile,
)
from voidx.presentation.protocol.v2.envelope import (
    ERR_AGENT_PROFILE_CONFLICT,
    ERR_AGENT_PROFILE_NOT_FOUND,
    ERR_AGENT_PROFILE_READ_ONLY,
)
from voidx.presentation.protocol.v2.methods import MethodParamsError


class AgentProfileMethods:
    """Profile configuration methods mixed into GatewaySession."""

    def _method_agent_profiles_list(self, params: dict) -> dict:
        profiles = list_agent_profiles(self._workspace or ".")
        return {"profiles": [self._public_profile(profile) for profile in profiles]}

    def _method_agent_profiles_get(self, params: dict) -> dict:
        scope, name = self._scope_and_name(params, allow_bundled=True)
        try:
            detail = get_agent_profile(
                self._workspace or ".", scope=scope, name=name
            )
        except AgentProfileNotFound as exc:
            raise MethodParamsError(
                "agent profile not found", code=ERR_AGENT_PROFILE_NOT_FOUND
            ) from exc
        except AgentProfileConflict as exc:
            raise MethodParamsError(
                "agent profile conflict",
                code=ERR_AGENT_PROFILE_CONFLICT,
                data=self._conflict_data(exc),
            ) from exc
        except ValueError as exc:
            raise MethodParamsError(str(exc)) from exc
        return {
            "profile": self._public_profile(detail.info),
            "yaml": detail.yaml_text,
            "read_only": detail.read_only,
        }

    def _method_agent_profiles_validate(self, params: dict) -> dict:
        scope, name = self._scope_and_name(params)
        yaml_text, payload = self._profile_input(params)
        try:
            result = validate_agent_profile(
                self._workspace or ".",
                scope=scope,
                name=name,
                yaml_text=yaml_text,
                payload=payload,
            )
        except ValueError as exc:
            raise MethodParamsError(str(exc)) from exc
        return {
            "valid": bool(result.valid),
            "diagnostics": [item.model_dump(mode="json") for item in result.diagnostics],
            "snapshot": self._public_snapshot(result.snapshot),
        }

    def _method_agent_profiles_save(self, params: dict) -> dict:
        scope, name = self._scope_and_name(params, allow_bundled=True)
        yaml_text, payload = self._profile_input(params)
        expected_revision, expected_hash = self._optimistic_guard(params)
        try:
            result = save_agent_profile(
                self._workspace or ".",
                scope=scope,
                name=name,
                yaml_text=yaml_text,
                payload=payload,
                expected_revision=expected_revision,
                expected_hash=expected_hash,
            )
        except AgentProfileConflict as exc:
            raise MethodParamsError(
                "agent profile conflict",
                code=ERR_AGENT_PROFILE_CONFLICT,
                data=self._conflict_data(exc),
            ) from exc
        except AgentProfileValidationError as exc:
            raise MethodParamsError(
                "invalid agent profile",
                data={
                    "diagnostics": [
                        item.model_dump(mode="json") for item in exc.diagnostics
                    ]
                },
            ) from exc
        except AgentProfileReadOnly as exc:
            raise MethodParamsError(
                "agent profile is read-only", code=ERR_AGENT_PROFILE_READ_ONLY
            ) from exc
        except ValueError as exc:
            raise MethodParamsError(str(exc)) from exc
        return {
            "snapshot": self._public_snapshot(result.snapshot),
            "diagnostics": [item.model_dump(mode="json") for item in result.diagnostics],
        }

    def _method_agent_profiles_delete(self, params: dict) -> dict:
        scope, name = self._scope_and_name(params, allow_bundled=True)
        expected_revision, expected_hash = self._optimistic_guard(params)
        try:
            delete_agent_profile(
                self._workspace or ".",
                scope=scope,
                name=name,
                expected_revision=expected_revision,
                expected_hash=expected_hash,
            )
        except AgentProfileConflict as exc:
            raise MethodParamsError(
                "agent profile conflict",
                code=ERR_AGENT_PROFILE_CONFLICT,
                data=self._conflict_data(exc),
            ) from exc
        except AgentProfileReadOnly as exc:
            raise MethodParamsError(
                "agent profile is read-only", code=ERR_AGENT_PROFILE_READ_ONLY
            ) from exc
        except AgentProfileNotFound as exc:
            raise MethodParamsError(
                "agent profile not found", code=ERR_AGENT_PROFILE_NOT_FOUND
            ) from exc
        except ValueError as exc:
            raise MethodParamsError(str(exc)) from exc
        return {"ok": True}

    @staticmethod
    def _scope_and_name(params: dict, *, allow_bundled: bool = False) -> tuple[str, str]:
        scope = params.get("scope")
        name = params.get("name")
        scopes = {"global", "project"}
        if allow_bundled:
            scopes.add("bundled")
        if not isinstance(scope, str) or scope not in scopes:
            raise MethodParamsError("invalid scope")
        if not isinstance(name, str) or not name:
            raise MethodParamsError("name is required")
        return scope, name

    @staticmethod
    def _profile_input(params: dict) -> tuple[str | None, Mapping[str, object] | None]:
        yaml_text = params.get("yaml")
        payload = params.get("payload")
        if (yaml_text is None) == (payload is None):
            raise MethodParamsError("exactly one of yaml or payload is required")
        if yaml_text is not None and not isinstance(yaml_text, str):
            raise MethodParamsError("yaml must be a string")
        if payload is not None and not isinstance(payload, dict):
            raise MethodParamsError("payload must be an object")
        return yaml_text, payload

    @staticmethod
    def _optimistic_guard(params: dict) -> tuple[int | None, str | None]:
        revision = params.get("expected_revision")
        content_hash = params.get("expected_hash")
        if (revision is None) == (content_hash is None):
            raise MethodParamsError(
                "exactly one expected_revision or expected_hash is required"
            )
        if revision is not None and (not isinstance(revision, int) or isinstance(revision, bool)):
            raise MethodParamsError("expected_revision must be an integer")
        if content_hash is not None and not isinstance(content_hash, str):
            raise MethodParamsError("expected_hash must be a string")
        return revision, content_hash

    @staticmethod
    def _public_profile(profile: object) -> dict:
        return {
            "name": profile.name,
            "display_name": profile.display_name,
            "revision": profile.revision,
            "content_hash": profile.content_hash,
            "source": profile.source,
            "run_mode": profile.run_mode,
            "hitl_mode": profile.hitl_mode,
            "availability": "available" if profile.available else "unavailable",
            "diagnostics": [item.model_dump(mode="json") for item in profile.diagnostics],
        }

    @staticmethod
    def _public_snapshot(snapshot: object | None) -> dict | None:
        if snapshot is None:
            return None
        return {
            "profile_id": snapshot.profile_id,
            "revision": snapshot.revision,
            "source": snapshot.source,
            "content_hash": snapshot.content_hash,
            "snapshot_hash": snapshot.snapshot_hash,
        }

    @classmethod
    def _conflict_data(cls, exc: AgentProfileConflict) -> dict:
        return {
            "current": cls._public_profile(exc.current) if exc.current is not None else None,
            "diagnostics": [item.model_dump(mode="json") for item in exc.diagnostics],
        }
