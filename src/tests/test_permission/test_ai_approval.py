import pytest

from voidx.config import PermissionMode
from voidx.permission.presets import resolve_mode_decision
from voidx.permission.risk import ApprovalScope, RiskAssessment


def test_ai_approval_mode_sandbox_and_policy():
    assert PermissionMode.AI_APPROVAL.sandbox_mode == "workspace-write"
    assert PermissionMode.AI_APPROVAL.approval_policy == "untrusted"


def test_ai_approval_dangerous_uses_safe_scopes():
    risk = RiskAssessment.dangerous(tool_name="write", pattern="README.md")
    decision = resolve_mode_decision(PermissionMode.AI_APPROVAL, risk)
    assert decision.action == "ask"
    assert decision.allowed_scopes == (ApprovalScope.ONCE, ApprovalScope.SESSION)
    assert decision.default_scope == ApprovalScope.ONCE


def test_ai_approval_extreme_stays_once():
    risk = RiskAssessment.extreme(tool_name="bash", pattern="python script.py")
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
    from voidx.permission.ai_approval import AiApprovalItemResult, validate_ai_approval_response

    expected = {"call-1", "call-2"}
    result = validate_ai_approval_response(
        {"decisions": [
            {"id": "call-2", "decision": "deny", "reason": "wide effect"},
            {"id": "call-1", "decision": "allow", "reason": "local"},
        ]},
        expected,
    )
    assert result.allowed_ids == frozenset({"call-1"})
    assert result.reason == "reviewed"


def test_ai_approval_response_rejects_missing_unknown_duplicate_and_invalid():
    from voidx.permission.ai_approval import validate_ai_approval_response

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


def test_ai_approval_projection_redacts_and_hashes_args():
    from voidx.permission.ai_approval import project_tool_args

    projected, digest = project_tool_args({"command": "echo hi", "api_key": "secret"}, tool_name="bash")
    assert projected == {"command": "echo hi", "api_key": "<redacted>"}
    assert len(digest) == 64


def test_ai_approval_projection_is_tool_specific_and_bounded():
    from voidx.permission.ai_approval import project_tool_args

    bash, _ = project_tool_args({"command": "echo hi"}, tool_name="bash")
    assert bash == {"command": "echo hi"}
    file_args, _ = project_tool_args({"operation": "write", "file_path": "a.txt", "content": "secret"}, tool_name="write")
    assert file_args["operation"] == "write"
    assert file_args["file_path"] == "a.txt"
    assert file_args["content"]["length"] == 6
    assert len(file_args["content"]["sha256"]) == 64
    unknown, _ = project_tool_args({"value": "x"}, tool_name="unknown")
    assert unknown is None
    with pytest.raises(ValueError):
        project_tool_args({"command": "x" * (16 * 1024 + 1)}, tool_name="bash")


@pytest.mark.asyncio
async def test_ai_approval_service_without_settings_is_unavailable():
    from voidx.permission.ai_approval import AiApprovalService

    result = await AiApprovalService().review([], None)
    assert result.allowed_ids == frozenset()
    assert result.reason == "unavailable"


def test_approved_tool_risk_accepts_ai_source_and_legacy_metadata():
    from voidx.tools.base import ApprovedToolRisk

    assert ApprovedToolRisk(tool_name="write", approved_by="ai").approved_by == "ai"
    assert ApprovedToolRisk(tool_name="write").approved_by == "user"


def test_permission_service_ai_approval_counter():
    from voidx.permission.service import PermissionService

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
