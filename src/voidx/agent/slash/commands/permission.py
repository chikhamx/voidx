"""Slash /permission commands."""
from __future__ import annotations



class PermissionCommandsMixin:
    async def _permission_mode(self, arg: str) -> None:
        from voidx.config import AiApprovalConfig, PermissionMode

        parts = arg.strip().split(None, 1)
        raw = parts[0].lower().replace("-", "_") if parts else ""
        requested_profile = parts[1].strip() if len(parts) > 1 else None
        labels = {
            PermissionMode.READ_ONLY.value: "Read only",
            PermissionMode.SAFE.value: "Safe",
            PermissionMode.AI_APPROVAL.value: "AI approval",
            PermissionMode.PROJECT_TRUSTED.value: "Project trusted",
            PermissionMode.FULL_ACCESS.value: "Full access",
        }
        choices = [
            (labels[PermissionMode.READ_ONLY.value], PermissionMode.READ_ONLY.value, "Ask for writes and block/acknowledge unsafe operations."),
            (labels[PermissionMode.SAFE.value], PermissionMode.SAFE.value, "Ask before writes or risky commands."),
            (labels[PermissionMode.AI_APPROVAL.value], PermissionMode.AI_APPROVAL.value, "AI pre-screens dangerous tools; uncertain calls still ask you."),
            (labels[PermissionMode.PROJECT_TRUSTED.value], PermissionMode.PROJECT_TRUSTED.value, "Allow workspace edits; ask for broader risk."),
            (labels[PermissionMode.FULL_ACCESS.value], PermissionMode.FULL_ACCESS.value, "Allow most operations; still ask for extreme risk."),
        ]
        valid = set(labels)

        app = self.host.app
        if not raw and app is not None:
            raw = await app.ask_choice("Permission mode", choices) or ""

        if not raw:
            current = getattr(self.host.permission, "permission_mode", PermissionMode.SAFE.value)
            self.host.ui.print(f"Permission mode: [cyan]{labels.get(current, labels[PermissionMode.SAFE.value])}[/cyan]")
            self.host.ui.print("Usage: /permission [read_only|safe|ai_approval [profile]|project_trusted|full_access]")
            return
        if raw not in valid:
            self.host.ui.error(f"Invalid permission mode: {raw}. Use: {', '.join(sorted(valid))}")
            return

        settings = self.host.settings
        selected_profile: str | None = None
        if raw == PermissionMode.AI_APPROVAL.value and settings is not None:
            profiles = [profile for profile in await settings.list_profiles() if profile.api_key]
            if requested_profile is not None:
                match = next((profile for profile in profiles if profile.name == requested_profile), None)
                if match is None:
                    self.host.ui.error(f"Unknown or unconfigured AI approval profile: {requested_profile}")
                    return
                selected_profile = match.name
            elif app is not None:
                profile_choices = [
                    ("Current main profile (default)", "", "Follow the active model profile."),
                    *((profile.name, profile.name, "") for profile in profiles),
                ]
                selected_profile = await app.ask_choice("AI approval profile", profile_choices)

        preset = PermissionMode(raw)
        try:
            self.host.permission.set_permission_mode(preset.value)
        except PermissionError as exc:
            self.host.ui.error(str(exc))
            return
        self.host.clear_successful_dangerous_calls()
        if settings is not None:
            settings.set_permission_mode(preset)
            if selected_profile is not None:
                current = settings.get_ai_approval_config()
                settings.set_ai_approval_config(AiApprovalConfig(
                    profile_name=selected_profile,
                    timeout_seconds=current.timeout_seconds,
                ))
        suffix = f" using {selected_profile or 'current main profile'}" if selected_profile is not None else ""
        self.host.ui.print(f"[dim]Permission mode set to [cyan]{labels[preset.value]}[/cyan]{suffix}[/dim]")

