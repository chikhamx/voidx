"""Permission preset defaults for configuration."""

from __future__ import annotations

from voidx.config.enums import ApprovalPolicy, ApprovalReviewer, PermissionMode, SandboxMode

def permission_mode_defaults(mode: PermissionMode) -> tuple[SandboxMode, ApprovalPolicy]:
    if mode == PermissionMode.DEFAULT:
        return SandboxMode.WORKSPACE_WRITE, ApprovalPolicy.UNTRUSTED
    if mode == PermissionMode.READ_ONLY:
        return SandboxMode.READ_ONLY, ApprovalPolicy.UNTRUSTED
    if mode == PermissionMode.ACCEPT_EDITS:
        return SandboxMode.WORKSPACE_WRITE, ApprovalPolicy.UNTRUSTED
    if mode == PermissionMode.AUTO_REVIEW:
        return SandboxMode.WORKSPACE_WRITE, ApprovalPolicy.UNTRUSTED
    if mode == PermissionMode.FULL_ACCESS:
        return SandboxMode.DANGER_FULL_ACCESS, ApprovalPolicy.NEVER
    return SandboxMode.WORKSPACE_WRITE, ApprovalPolicy.UNTRUSTED


def permission_mode_reviewer_default(mode: PermissionMode) -> ApprovalReviewer:
    if mode == PermissionMode.AUTO_REVIEW:
        return ApprovalReviewer.AUTO_REVIEW
    return ApprovalReviewer.USER
