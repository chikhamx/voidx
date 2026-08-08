import pytest

from voidx.config import PermissionMode
from voidx.tooling.policy.permission.presets import resolve_mode_decision
from voidx.tooling.domain.risk import ApprovalScope, RiskAssessment, RiskTag


def test_ai_approval_mode_sandbox_and_policy():
    assert PermissionMode.AI_APPROVAL.sandbox_mode == "workspace-write"
    assert PermissionMode.AI_APPROVAL.approval_policy == "untrusted"


@pytest.mark.parametrize(
    ("risk", "expected_action"),
    [
        (RiskAssessment.dangerous(tool_name="write", pattern="README.md"), "allow"),
        (RiskAssessment.extreme(tool_name="bash", pattern="python script.py"), "allow"),
        (RiskAssessment.extreme(tool_name="bash", pattern="curl example.com", tags=(RiskTag.NETWORK,)), "ask"),
        (RiskAssessment.dangerous(tool_name="write", pattern="../outside", tags=(RiskTag.EXTERNAL_PATH,)), "ask"),
    ],
)
def test_ai_approval_uses_project_trusted_risk_boundary(risk, expected_action):
    ai_decision = resolve_mode_decision(PermissionMode.AI_APPROVAL, risk)
    trusted_decision = resolve_mode_decision(PermissionMode.PROJECT_TRUSTED, risk)

    assert ai_decision.action == trusted_decision.action == expected_action
    assert ai_decision.allowed_scopes == trusted_decision.allowed_scopes
    assert ai_decision.default_scope == trusted_decision.default_scope


def test_ai_approval_extreme_network_stays_once():
    risk = RiskAssessment.extreme(
        tool_name="bash",
        pattern="curl https://example.com",
        tags=(RiskTag.NETWORK,),
    )
    decision = resolve_mode_decision(PermissionMode.AI_APPROVAL, risk)
    assert decision.action == "ask"
    assert decision.allowed_scopes == (ApprovalScope.ONCE,)


def test_existing_permission_modes_are_unchanged():
    normal = RiskAssessment.normal(tool_name="read")
    assert resolve_mode_decision(PermissionMode.AI_APPROVAL, normal).action == "allow"

    blocked = RiskAssessment.blocked(tool_name="bash")
    assert resolve_mode_decision(PermissionMode.AI_APPROVAL, blocked).action == "blocked_ack"


def test_ai_approval_config_defaults():
    from voidx.config import AiApprovalConfig

    config = AiApprovalConfig()
    assert config.profile_name == ""
    assert config.timeout_seconds == 12.0


def test_ai_approval_config_timeout_bounds():
    import pytest
    from voidx.config import AiApprovalConfig

    with pytest.raises(ValueError):
        AiApprovalConfig(timeout_seconds=0.5)
    with pytest.raises(ValueError):
        AiApprovalConfig(timeout_seconds=60.1)
    with pytest.raises(ValueError):
        AiApprovalConfig(timeout_seconds=float("nan"))


def test_ai_approval_settings_round_trip(tmp_path):
    from voidx.config import AiApprovalConfig, Settings

    settings = Settings(str(tmp_path))
    settings.set_ai_approval_config(AiApprovalConfig(profile_name="openai/reviewer", timeout_seconds=20))
    assert settings.get_ai_approval_config() == AiApprovalConfig(profile_name="openai/reviewer", timeout_seconds=20)


def test_ai_approval_settings_are_workspace_only(tmp_path, monkeypatch):
    from voidx.config import AiApprovalConfig, Settings

    monkeypatch.setattr(Settings, "_settings_home", staticmethod(lambda: tmp_path / "home"), raising=False)
    settings = Settings(str(tmp_path / "workspace"))
    settings.set_ai_approval_config(AiApprovalConfig())
    assert (tmp_path / "workspace" / ".voidx" / "settings.json").exists()
    assert not (tmp_path / "home" / ".voidx" / "settings.json").exists()


def test_ai_approval_corrupt_settings_fall_back_to_defaults(tmp_path):
    from voidx.config import Settings

    settings = Settings(str(tmp_path))
    settings._data["ai_approval"] = {"timeout_seconds": "invalid"}
    assert settings.get_ai_approval_config().timeout_seconds == 12.0


def test_ai_approval_response_requires_complete_unique_batch():
    from voidx.tooling.application.ai_approval import AiApprovalItemResult, validate_ai_approval_response

    expected = {"call-1", "call-2"}
    result = validate_ai_approval_response(
        {"decisions": [
            {"id": "call-2", "decision": "deny", "reason": "wide effect"},
            {"id": "call-1", "decision": "allow", "reason": "local"},
        ]},
        expected,
    )
    assert result.allowed_ids == frozenset({"call-1"})
    assert result.reviewed_ids == frozenset(expected)
    assert result.denied_reasons == {"call-2": "wide effect"}
    assert result.reason == "reviewed"


def test_ai_approval_response_rejects_missing_unknown_duplicate_and_invalid():
    from voidx.tooling.application.ai_approval import validate_ai_approval_response

    expected = {"call-1", "call-2"}
    for decisions in (
        [{"id": "call-1", "decision": "allow"}],
        [{"id": "call-1", "decision": "allow"}, {"id": "other", "decision": "deny"}],
        [{"id": "call-1", "decision": "allow"}, {"id": "call-1", "decision": "deny"}],
        [{"id": "call-1", "decision": "maybe"}, {"id": "call-2", "decision": "deny"}],
    ):
        result = validate_ai_approval_response({"decisions": decisions}, expected)
        assert result.allowed_ids == frozenset()
        assert result.reason == "invalid_response"


def test_ai_approval_response_accepts_bare_array_from_json_mode():
    from voidx.tooling.application.ai_approval import validate_ai_approval_response

    expected = {"call-1", "call-2"}
    result = validate_ai_approval_response(
        [{"id": "call-2", "decision": "deny", "reason": "wide effect"},
         {"id": "call-1", "decision": "allow", "reason": "local"}],
        expected,
    )
    assert result.allowed_ids == frozenset({"call-1"})
    assert result.reviewed_ids == frozenset(expected)
    assert result.denied_reasons == {"call-2": "wide effect"}
    assert result.reason == "reviewed"


def test_ai_approval_response_accepts_json_string():
    from voidx.tooling.application.ai_approval import validate_ai_approval_response

    expected = {"call-1"}
    result = validate_ai_approval_response(
        '{"decisions": [{"id": "call-1", "decision": "allow"}]}',
        expected,
    )
    assert result.allowed_ids == frozenset({"call-1"})
    assert result.reason == "reviewed"


def test_ai_approval_response_accepts_include_raw_wrapper():
    from voidx.tooling.application.ai_approval import validate_ai_approval_response

    expected = {"call-1"}
    result = validate_ai_approval_response(
        {"raw": None, "parsed": {"decisions": [{"id": "call-1", "decision": "allow"}]}},
        expected,
    )
    assert result.allowed_ids == frozenset({"call-1"})
    assert result.reason == "reviewed"


def test_ai_approval_response_extracts_decision_from_raw_text():
    from voidx.tooling.application.ai_approval import validate_ai_approval_response

    expected = {"call-1"}
    raw = {
        "raw": type("FakeMsg", (), {"content": "Analysis: bounded command.\nDecision: ALLOW\nDone."})(),
        "parsed": None,
        "parsing_error": "Invalid json",
    }
    result = validate_ai_approval_response(raw, expected)
    assert result.allowed_ids == frozenset({"call-1"})
    assert result.reason == "reviewed"


def test_ai_approval_response_normalizes_approved_field():
    from voidx.tooling.application.ai_approval import validate_ai_approval_response

    expected = {"call-1", "call-2"}
    result = validate_ai_approval_response(
        {"decisions": [{"id": "call-1", "approved": True}, {"id": "call-2", "approved": False}]},
        expected,
    )
    assert result.allowed_ids == frozenset({"call-1"})
    assert result.reviewed_ids == frozenset(expected)
    assert result.reason == "reviewed"


def test_ai_approval_response_extracts_decision_from_list_content():
    from voidx.tooling.application.ai_approval import validate_ai_approval_response

    expected = {"call-1"}
    raw = {
        "raw": type("FakeMsg", (), {"content": [{"type": "text", "text": "Decision: ALLOW"}]})(),
        "parsed": None,
    }
    result = validate_ai_approval_response(raw, expected)
    assert result.allowed_ids == frozenset({"call-1"})
    assert result.reason == "reviewed"


def test_ai_approval_response_extracts_deny_from_raw_text():
    from voidx.tooling.application.ai_approval import validate_ai_approval_response

    expected = {"call-1"}
    raw = {
        "raw": type("FakeMsg", (), {"content": "The command writes outside workspace.\nDecision: DENY"})(),
        "parsed": None,
    }
    result = validate_ai_approval_response(raw, expected)
    assert result.allowed_ids == frozenset()
    assert result.reviewed_ids == frozenset({"call-1"})
    assert result.reason == "reviewed"


def test_ai_approval_projection_redacts_and_hashes_args():
    from voidx.tooling.application.ai_approval import project_tool_args

    projected, digest = project_tool_args({"command": "echo hi", "api_key": "secret"}, tool_name="bash")
    assert projected == {
        "command": "echo hi",
        "api_key": "<redacted>",
        "shell_context": {
            "shell": "bash",
            "working_directory": "workspace_root",
            "contains_sensitive_data": True,
        },
    }
    assert len(digest) == 64


def test_ai_approval_projection_is_tool_specific_and_bounded():
    from voidx.tooling.application.ai_approval import project_tool_args

    bash, _ = project_tool_args({"command": "echo hi"}, tool_name="bash")
    assert bash == {
        "command": "echo hi",
        "shell_context": {
            "shell": "bash",
            "working_directory": "workspace_root",
            "contains_sensitive_data": False,
        },
    }
    file_args, _ = project_tool_args({"operation": "write", "file_path": "a.txt", "content": "secret"}, tool_name="write")
    assert file_args["operation"] == "write"
    assert file_args["file_path"] == "a.txt"
    assert file_args["content"]["length"] == 6
    assert len(file_args["content"]["sha256"]) == 64
    unknown, _ = project_tool_args({"value": "x"}, tool_name="unknown")
    assert unknown is None
    with pytest.raises(ValueError):
        project_tool_args({"command": "x" * (16 * 1024 + 1)}, tool_name="bash")


def test_ai_approval_projection_adds_shell_context_for_common_tools():
    from voidx.tooling.application.ai_approval import project_tool_args

    projected, _ = project_tool_args({"command": "python -m pytest -q"}, tool_name="bash")

    assert projected["command"] == "python -m pytest -q"
    assert projected["shell_context"] == {
        "shell": "bash",
        "working_directory": "workspace_root",
        "contains_sensitive_data": False,
    }


@pytest.mark.parametrize(
    ("tool_name", "command", "secrets"),
    [
        (
            "bash",
            "curl -H 'Authorization: Bearer sk-live-secret' https://example.com",
            ("sk-live-secret",),
        ),
        ("bash", "API_TOKEN=env-secret python script.py", ("env-secret",)),
        ("bash", "curl https://alice:url-secret@example.com", ("alice", "url-secret")),
        ("bash", "tool --password flag-secret", ("flag-secret",)),
        ("powershell", "$env:API_TOKEN = 'ps-env-secret'; python script.py", ("ps-env-secret",)),
        (
            "powershell",
            "Invoke-WebRequest -Headers @{ Authorization = 'Bearer ps-header-secret' } https://example.com",
            ("ps-header-secret",),
        ),
        (
            "powershell",
            "Invoke-WebRequest -Headers @{ 'Authorization' = 'Bearer ps-quoted-secret' } https://example.com",
            ("ps-quoted-secret",),
        ),
        ("powershell", "tool -Password ps-flag-secret", ("ps-flag-secret",)),
        ("bash", "sshpass -p sshpass-secret ssh example.com", ("sshpass-secret",)),
        (
            "bash",
            "npm config set //registry.npmjs.org/:_authToken npm-token-secret",
            ("npm-token-secret",),
        ),
    ],
)
def test_ai_approval_projection_redacts_embedded_shell_credentials(tool_name, command, secrets):
    import json

    from voidx.tooling.application.ai_approval import project_tool_args

    projected, _ = project_tool_args({"command": command}, tool_name=tool_name)
    encoded = json.dumps(projected, sort_keys=True)

    assert projected["shell_context"]["contains_sensitive_data"] is True
    assert "<redacted>" in projected["command"]
    for secret in secrets:
        assert secret not in encoded


def test_ai_approval_projection_digest_is_based_on_redacted_payload():
    import hashlib
    import json

    from voidx.tooling.application.ai_approval import project_tool_args

    raw = {"command": "tool --password low-entropy-secret"}
    projected, digest = project_tool_args(raw, tool_name="bash")
    projected_json = json.dumps(projected, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    raw_json = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    assert digest == hashlib.sha256(projected_json.encode()).hexdigest()
    assert digest != hashlib.sha256(raw_json.encode()).hexdigest()


def test_ai_approval_header_redaction_preserves_following_shell_command():
    from voidx.tooling.application.ai_approval import project_tool_args

    projected, _ = project_tool_args(
        {"command": "curl -H X-API-Key:sk-secret https://example.com && rm -rf build"},
        tool_name="bash",
    )

    assert projected["command"] == (
        "curl -H X-API-Key:<redacted> https://example.com && rm -rf build"
    )


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            "curl -H 'Authorization: Bearer sk-live-secret' https://example.com && echo done",
            "curl -H 'Authorization: <redacted>' https://example.com && echo done",
        ),
        (
            'curl -H "Cookie: sid=secret-cookie" https://example.com | tee out',
            'curl -H "Cookie: <redacted>" https://example.com | tee out',
        ),
    ],
)
def test_ai_approval_quoted_header_redaction_preserves_quote_boundary(command, expected):
    from voidx.tooling.application.ai_approval import project_tool_args

    projected, _ = project_tool_args({"command": command}, tool_name="bash")

    assert projected["command"] == expected


def test_ai_approval_url_credential_redaction_preserves_host_path_and_suffix():
    from voidx.tooling.application.ai_approval import project_tool_args

    projected, _ = project_tool_args(
        {"command": "curl https://token:url-secret@example.com/path && echo done"},
        tool_name="bash",
    )

    assert projected["command"] == (
        "curl https://<redacted>@example.com/path && echo done"
    )


@pytest.mark.parametrize(
    ("action", "level", "expected"),
    [
        ("ask", "dangerous", True),
        ("ask", "extreme", True),
        ("ask", "normal", False),
        ("ask", "blocked", False),
        ("blocked_ack", "blocked", False),
        ("allow", "extreme", False),
    ],
)
def test_ai_approval_candidate_is_every_approvable_ask(action, level, expected):
    from types import SimpleNamespace

    from voidx.tooling.application.ai_approval import is_ai_approval_candidate
    from voidx.tooling.domain.risk import RiskAssessment

    risk_factory = getattr(RiskAssessment, level)
    risk = risk_factory(tool_name="bash", pattern="opaque command")
    decision = SimpleNamespace(
        action=action,
        name="bash",
        risk=risk,
        tool_call={"name": "bash", "args": {"command": "opaque command"}, "id": "call_1"},
    )

    assert is_ai_approval_candidate(decision) is expected


def test_ai_approval_candidate_rejects_missing_risk():
    from types import SimpleNamespace

    from voidx.tooling.application.ai_approval import is_ai_approval_candidate

    assert is_ai_approval_candidate(SimpleNamespace(action="ask", risk=None)) is False


def test_ai_approval_system_prompt_guides_common_shell_review():
    from voidx.tooling.application.ai_approval import ai_approval_system_prompt

    prompt = ai_approval_system_prompt()

    assert "python" in prompt
    assert "curl" in prompt
    assert "ssh" in prompt
    assert "bounded" in prompt
    assert "human review" in prompt


def test_ai_approval_classifies_provider_transport_errors():
    import httpx

    from voidx.tooling.application.ai_approval import _classify_ai_approval_failure

    class APITimeoutError(Exception):
        pass

    assert _classify_ai_approval_failure(APITimeoutError()) == "timeout"
    assert _classify_ai_approval_failure(httpx.ConnectError("offline")) == "connection_error"
    assert _classify_ai_approval_failure(RuntimeError("bad response")) == "error"


@pytest.mark.asyncio
async def test_ai_approval_service_without_settings_is_unavailable():
    from voidx.tooling.application.ai_approval import AiApprovalService

    result = await AiApprovalService().review([], None)
    assert result.allowed_ids == frozenset()
    assert result.reason == "unavailable"


@pytest.mark.asyncio
async def test_ai_approval_service_reports_timeout(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from voidx.config import Profile
    from voidx.tooling.application.ai_approval import AiApprovalService
    from voidx.tooling.domain.risk import RiskAssessment

    class FakeRunnable:
        async def ainvoke(self, _messages):
            raise asyncio.TimeoutError

    class FakeResolver:
        def with_structured_output(self, _schema):
            return FakeRunnable()

    profile = Profile(name="openai/reviewer", api_key="reviewer-key")

    class FakeSettings:
        def get_ai_approval_config(self):
            return SimpleNamespace(profile_name=profile.name, timeout_seconds=1.0)

        async def list_profiles(self):
            return [profile]

    model_factory = lambda *_args, **_kwargs: object()
    resolver_model_factory = lambda *_args, **_kwargs: FakeResolver()
    decision = SimpleNamespace(
        action="ask",
        risk=RiskAssessment.extreme(tool_name="bash", pattern="python build.py"),
        tool_call={"name": "bash", "args": {"command": "python build.py"}, "id": "call_1"},
    )

    result = await AiApprovalService(model_factory, resolver_model_factory).review([decision], FakeSettings())

    assert result.reason == "timeout"
    assert result.allowed_ids == frozenset()


@pytest.mark.asyncio
async def test_ai_approval_service_tracks_candidates_skipped_before_review(monkeypatch):
    from types import SimpleNamespace

    from voidx.config import Profile
    from voidx.tooling.application.ai_approval import AiApprovalService
    from voidx.tooling.domain.risk import RiskAssessment

    class FakeRunnable:
        async def ainvoke(self, _messages):
            return {"decisions": [{"id": "call_bash", "decision": "deny", "reason": "unclear"}]}

    class FakeResolver:
        def with_structured_output(self, _schema):
            return FakeRunnable()

    profile = Profile(name="openai/reviewer", api_key="reviewer-key")

    class FakeSettings:
        def get_ai_approval_config(self):
            return SimpleNamespace(profile_name=profile.name, timeout_seconds=1.0)

        async def list_profiles(self):
            return [profile]

    model_factory = lambda *_args, **_kwargs: object()
    resolver_model_factory = lambda *_args, **_kwargs: FakeResolver()
    risk = RiskAssessment.dangerous(tool_name="tool", pattern="operation")
    decisions = [
        SimpleNamespace(
            action="ask",
            risk=risk,
            tool_call={"name": "bash", "args": {"command": "./build.sh"}, "id": "call_bash"},
        ),
        SimpleNamespace(
            action="ask",
            risk=risk,
            tool_call={"name": "mcp__remote", "args": {"operation": "read"}, "id": "call_mcp"},
        ),
    ]

    result = await AiApprovalService(model_factory, resolver_model_factory).review(decisions, FakeSettings())

    assert result.reason == "reviewed"
    assert result.reviewed_ids == frozenset({"call_bash"})
    assert result.skipped_reasons == {"call_mcp": "tool is not supported by AI approval"}


@pytest.mark.asyncio
async def test_ai_approval_service_marks_oversized_batch_as_skipped(monkeypatch):
    from types import SimpleNamespace

    from voidx.config import Profile
    from voidx.tooling.application.ai_approval import AiApprovalService
    from voidx.tooling.domain.risk import RiskAssessment

    class FakeRunnable:
        async def ainvoke(self, _messages):
            pytest.fail("oversized approval batch must not reach the model")

    class FakeResolver:
        def with_structured_output(self, _schema):
            return FakeRunnable()

    profile = Profile(name="openai/reviewer", api_key="reviewer-key")

    class FakeSettings:
        def get_ai_approval_config(self):
            return SimpleNamespace(profile_name=profile.name, timeout_seconds=1.0)

        async def list_profiles(self):
            return [profile]

    model_factory = lambda *_args, **_kwargs: object()
    resolver_model_factory = lambda *_args, **_kwargs: FakeResolver()
    risk = RiskAssessment.dangerous(tool_name="bash", pattern="large command")
    decisions = [
        SimpleNamespace(
            action="ask",
            risk=risk,
            tool_call={
                "name": "bash",
                "args": {"command": f"echo {index} {'x' * 13_000}"},
                "id": f"call_{index}",
            },
        )
        for index in range(4)
    ]

    result = await AiApprovalService(model_factory, resolver_model_factory).review(decisions, FakeSettings())

    assert result.reason == "skipped"
    assert result.allowed_ids == frozenset()
    assert result.skipped_reasons == {
        f"call_{index}": "approval batch exceeds the 48 KiB limit"
        for index in range(4)
    }


@pytest.mark.asyncio
async def test_ai_approval_service_never_sends_shell_secret_in_pattern_or_args(monkeypatch):
    import json
    from types import SimpleNamespace

    from voidx.config import Profile
    from voidx.tooling.application.ai_approval import AiApprovalService
    from voidx.tooling.domain.risk import RiskAssessment, RiskTag

    captured = {}

    class FakeRunnable:
        async def ainvoke(self, messages):
            captured.update(json.loads(messages[1].content)[0])
            return {"decisions": [{"id": "call_1", "decision": "deny"}]}

    class FakeResolver:
        def with_structured_output(self, _schema):
            return FakeRunnable()

    profile = Profile(name="openai/reviewer", api_key="reviewer-key")

    class FakeSettings:
        def get_ai_approval_config(self):
            return SimpleNamespace(profile_name=profile.name, timeout_seconds=1.0)

        async def list_profiles(self):
            return [profile]

        async def resolve_profile(self):
            return profile

    model_factory = lambda *_args, **_kwargs: object()
    resolver_model_factory = lambda *_args, **_kwargs: FakeResolver()
    command = "curl -H 'Authorization: Bearer shell-secret' https://example.com"
    decision = SimpleNamespace(
        action="ask",
        name="bash",
        risk=RiskAssessment.extreme(
            tool_name="bash",
            pattern=command,
            tags=(RiskTag.NETWORK,),
        ),
        tool_call={"name": "bash", "args": {"command": command}, "id": "call_1"},
    )

    result = await AiApprovalService(model_factory, resolver_model_factory).review([decision], FakeSettings())
    encoded = json.dumps(captured, sort_keys=True)

    assert result.reason == "reviewed"
    assert result.allowed_ids == frozenset()
    assert "shell-secret" not in encoded
    assert "<redacted>" in captured["pattern"]
    assert "<redacted>" in captured["args"]["command"]


def test_approved_tool_risk_accepts_ai_source_and_legacy_metadata():
    from voidx.tooling.domain.risk import ApprovedToolRisk

    assert ApprovedToolRisk(tool_name="write", approved_by="ai").approved_by == "ai"
    assert ApprovedToolRisk(tool_name="write").approved_by == "user"


def test_permission_service_ai_approval_counter():
    from voidx.tooling.adapters.permission.in_memory_state import create_permission_service as PermissionService

    service = PermissionService(permission_mode="ai_approval")
    assert service.ai_approval_count == 0
    assert service.permission_mode_label() == "AI approval"

    old_revision = service.state_revision
    service.inc_ai_approval_count()
    assert service.ai_approval_count == 1
    assert service.state_revision > old_revision
    assert service.permission_mode_label() == "AI approval (1)"

    service.clear_session_permissions()
    assert service.ai_approval_count == 0
    assert service.permission_mode_label() == "AI approval"


@pytest.mark.asyncio
async def test_ai_approval_service_falls_back_to_json_mode_on_unparseable_response(monkeypatch):
    from types import SimpleNamespace

    from voidx.config import Profile
    from voidx.tooling.application.ai_approval import AiApprovalService
    from voidx.tooling.domain.risk import RiskAssessment

    call_log: list[dict] = []

    class FakeRunnable:
        def __init__(self, response):
            self._response = response

        async def ainvoke(self, messages):
            return self._response

    class FakeResolver:
        def __init__(self):
            self._call_count = 0

        def with_structured_output(self, _schema, **kwargs):
            call_log.append(kwargs)
            self._call_count += 1
            if self._call_count == 1:
                return FakeRunnable({"raw": None, "parsed": None})
            return FakeRunnable({"decisions": [{"id": "call_1", "decision": "allow"}]})

    profile = Profile(name="openai/reviewer", api_key="sk-test")
    resolver = FakeResolver()

    class FakeSettings:
        def get_ai_approval_config(self):
            return SimpleNamespace(profile_name=profile.name, timeout_seconds=1.0)

        async def list_profiles(self):
            return [profile]

    model_factory = lambda *_args, **_kwargs: object()
    resolver_model_factory = lambda *_args, **_kwargs: resolver

    decision = SimpleNamespace(
        action="ask",
        risk=RiskAssessment.dangerous(tool_name="bash", pattern="rm -rf /tmp/test"),
        tool_call={"name": "bash", "args": {"command": "rm -rf /tmp/test"}, "id": "call_1"},
    )

    result = await AiApprovalService(model_factory, resolver_model_factory).review([decision], FakeSettings())

    assert result.reason == "reviewed"
    assert result.allowed_ids == frozenset({"call_1"})
    assert len(call_log) >= 2
    assert call_log[1].get("method") == "json_mode"