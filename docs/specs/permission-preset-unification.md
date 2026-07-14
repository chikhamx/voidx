# Specification: Permission Preset Unification and Sandbox Mode Cleanup

## Goal
Simplify the permission system by removing the legacy `PermissionMode`, `ApprovalReviewer`, and `sandbox_mode` configuration options. The entire permission and sandbox behavior will be fully driven by the four `PermissionPreset` values: `read_only`, `safe`, `project_trusted`, and `full_access`.

## Proposed Changes

### 1. Remove Legacy Enums and Configuration Fields
* **Enums (`src/voidx/config/enums.py`)**:
  - Delete `PermissionMode` enum.
  - Delete `ApprovalReviewer` enum.
  - Delete `SandboxMode` enum.
* **Config Models (`src/voidx/config/models.py`)**:
  - Remove `approval_reviewer` from `Config`.
  - Remove `sandbox_mode` and `permission_mode` from settings keys.
* **Settings (`src/voidx/config/settings.py` & `settings_permissions.py`)**:
  - Remove `get_approval_reviewer`, `set_approval_reviewer`, `set_sandbox_mode` from `SettingsPermissionMixin`.
  - Remove `"permission_mode"`, `"sandbox_mode"`, `"approval_reviewer"` from `WORKSPACE_ONLY_KEYS`.

### 2. Simplify Permission Service and Context
* **Permission Context (`src/voidx/permission/context.py`)**:
  - Remove `permission_mode`, `sandbox_mode`, `approval_policy`, and `approval_reviewer` fields from `PermissionContext`.
  - Update `PermissionContext.from_service` to only pass `permission_preset` and access grants.
* **Permission Service (`src/voidx/permission/service.py`)**:
  - Remove `self._custom_permission_mode`, `self._custom_sandbox_mode`, `self._custom_approval_policy`, and `self.approval_reviewer`.
  - Remove properties/setters for `sandbox_mode`, `approval_policy`, `permission_mode`.
  - Remove `set_sandbox_mode` method.
  - Update `_context()` to construct `PermissionContext` without the removed fields.
  - Update `status_details()` to only return `(preset_label, session_label)`.
  - Update `show_rules()` to display preset and grants only.
* **Grants (`src/voidx/permission/grants.py`)**:
  - Change `ApprovalPrecondition.permission_mode` to `permission_preset`.
  - Change `AccessGrants.permission_mode` to `permission_preset`.

### 3. Update Sandbox Checks to Use Preset Directly
* **Sandbox Checks (`src/voidx/permission/sandbox.py` & `shell_policy.py`)**:
  - Update sandbox checks to determine the sandbox boundary directly from the `PermissionPreset` value of the context.
  - `PermissionPreset.READ_ONLY` -> read-only sandbox.
  - `PermissionPreset.FULL_ACCESS` -> danger-full-access sandbox.
  - `PermissionPreset.SAFE` and `PermissionPreset.PROJECT_TRUSTED` -> workspace-write sandbox.
* **Bash/PowerShell Tools (`src/voidx/tools/bash/safety.py` & `src/voidx/tools/powershell/sandbox.py`)**:
  - Update `_sandbox_denial` to check the preset from `ctx` (or map preset to sandbox behavior).

### 4. Remove `/sandbox` Slash Command
* **Slash Catalog (`frontend/src/slash.ts` & `src/voidx/ui/command_catalog.py` & `commands.py`)**:
  - Remove `/sandbox` command from catalog and UI targets.
  - Remove `/sandbox` handler from `SlashHandler` in `src/voidx/agent/slash/handler.py`.
* **Frontend UI (`frontend/src/settings.ts`)**:
  - Remove sandbox mode display and dropdowns. Only display and allow setting `PermissionPreset`.

## Verification Plan
* Run `./test.py --backend` to ensure all permission and tool tests pass.
* Run `./test.py --frontend` to ensure settings and slash command tests pass.
