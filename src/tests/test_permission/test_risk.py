from voidx.permission.presets import PermissionMode, resolve_mode_decision
from voidx.permission.risk import ApprovalScope, RiskAssessment, RiskLevel, RiskTag
from voidx.permission.shell_policy import classify_shell_risk


def test_risk_assessment_serializes_core_fields():
    risk = RiskAssessment(
        level=RiskLevel.EXTREME,
        tags=(RiskTag.DYNAMIC_SHELL, RiskTag.NESTED_INTERPRETER),
        reason="executes dynamic shell code",
        tool_name="bash",
        pattern="python3 /tmp/script.py",
    )

    assert risk.level == RiskLevel.EXTREME
    assert RiskTag.DYNAMIC_SHELL in risk.tags
    assert risk.model_dump(mode="json")["level"] == "extreme"


def test_blocked_risk_is_not_approvable():
    risk = RiskAssessment.blocked(
        tool_name="bash",
        pattern="rm -rf /home",
        tags=(RiskTag.SYSTEM_DESTRUCTIVE,),
        reason="recursive delete of home directory",
    )

    assert risk.level == RiskLevel.BLOCKED
    assert risk.approvable is False


def test_safe_preset_asks_for_dangerous_and_once_only_for_extreme():
    dangerous = RiskAssessment.dangerous(
        tool_name="write",
        pattern="README.md",
        tags=(RiskTag.WORKSPACE_EDIT,),
        reason="writes workspace file",
    )
    extreme = RiskAssessment.extreme(
        tool_name="bash",
        pattern="python3 script.py",
        tags=(RiskTag.NESTED_INTERPRETER,),
        reason="runs nested interpreter",
    )

    dangerous_decision = resolve_mode_decision(PermissionMode.SAFE, dangerous)
    extreme_decision = resolve_mode_decision(PermissionMode.SAFE, extreme)

    assert dangerous_decision.action == "ask"
    assert dangerous_decision.allowed_scopes == (ApprovalScope.ONCE, ApprovalScope.SESSION)
    assert dangerous_decision.default_scope == ApprovalScope.ONCE
    assert extreme_decision.action == "ask"
    assert extreme_decision.allowed_scopes == (ApprovalScope.ONCE,)


def test_read_only_preset_uses_stateless_approval_for_dangerous_risk():
    risk = RiskAssessment.dangerous(
        tool_name="bash",
        pattern="echo hi > file.txt",
        tags=(RiskTag.WORKSPACE_EDIT,),
        reason="writes through shell",
    )

    decision = resolve_mode_decision(PermissionMode.READ_ONLY, risk)

    assert decision.action == "ask"
    assert decision.allowed_scopes == (ApprovalScope.ONCE,)
    assert decision.default_scope == ApprovalScope.ONCE


def test_project_trusted_allows_only_workspace_edit_risk():
    workspace = RiskAssessment.dangerous(
        tool_name="write",
        pattern="src/app.py",
        tags=(RiskTag.WORKSPACE_EDIT,),
        reason="writes workspace file",
    )
    network = RiskAssessment.dangerous(
        tool_name="bash",
        pattern="curl https://example.com",
        tags=(RiskTag.NETWORK,),
        reason="uses network",
    )

    assert resolve_mode_decision(PermissionMode.PROJECT_TRUSTED, workspace).action == "allow"
    assert resolve_mode_decision(PermissionMode.PROJECT_TRUSTED, network).action == "ask"


def test_project_trusted_allows_nested_interpreter_in_workspace():
    risk = classify_shell_risk("python -m pytest", shell="bash")
    assert risk.level == RiskLevel.EXTREME
    assert RiskTag.NESTED_INTERPRETER in risk.tags
    assert resolve_mode_decision(PermissionMode.PROJECT_TRUSTED, risk).action == "allow"


def test_project_trusted_allows_dynamic_shell_in_workspace():
    risk = classify_shell_risk("echo $HOME", shell="bash")
    assert risk.level == RiskLevel.EXTREME
    assert RiskTag.DYNAMIC_SHELL in risk.tags
    assert resolve_mode_decision(PermissionMode.PROJECT_TRUSTED, risk).action == "allow"


def test_project_trusted_allows_dependency_install():
    risk = classify_shell_risk("pip install requests", shell="bash")
    assert risk.level == RiskLevel.EXTREME
    assert RiskTag.DEPENDENCY_INSTALL in risk.tags
    assert resolve_mode_decision(PermissionMode.PROJECT_TRUSTED, risk).action == "allow"


def test_project_trusted_still_asks_for_network():
    risk = classify_shell_risk("curl https://example.com", shell="bash")
    assert risk.level == RiskLevel.EXTREME
    assert RiskTag.NETWORK in risk.tags
    assert resolve_mode_decision(PermissionMode.PROJECT_TRUSTED, risk).action == "ask"


def test_project_trusted_still_asks_for_system_destructive():
    risk = RiskAssessment.extreme(
        tool_name="bash",
        pattern="rm -rf /",
        tags=(RiskTag.SYSTEM_DESTRUCTIVE,),
        reason="system destructive",
    )
    assert resolve_mode_decision(PermissionMode.PROJECT_TRUSTED, risk).action == "ask"


def test_full_access_allows_network_after_extreme_change():
    risk = classify_shell_risk("curl https://example.com", shell="bash")
    assert resolve_mode_decision(PermissionMode.FULL_ACCESS, risk).action == "allow"


def test_full_access_allows_nested_interpreter_after_extreme_change():
    risk = classify_shell_risk("python -m pytest", shell="bash")
    assert resolve_mode_decision(PermissionMode.FULL_ACCESS, risk).action == "allow"


def test_shell_risk_classifies_nested_interpreter_as_extreme_not_blocked():
    risk = classify_shell_risk("python3 /tmp/script.py", shell="bash")

    assert risk.level == RiskLevel.EXTREME
    assert RiskTag.NESTED_INTERPRETER in risk.tags
    assert risk.approvable is True


def test_shell_risk_classifies_catastrophic_delete_as_blocked():
    risk = classify_shell_risk("rm -rf /home", shell="bash")

    assert risk.level == RiskLevel.BLOCKED
    assert RiskTag.SYSTEM_DESTRUCTIVE in risk.tags
    assert risk.approvable is False
