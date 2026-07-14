# Rename: permission_preset → permission_mode

## Goal

Pure mechanical rename across the full codebase. No logic changes, no backward compatibility.

## Rename Map

| Old | New |
|-----|-----|
| `/permission-preset` (slash command) | `/permission` |
| `PermissionPreset` (enum class) | `PermissionMode` |
| `permission_preset` (field/param/key) | `permission_mode` |
| `set_permission_preset` (method) | `set_permission_mode` |
| `get_permission_preset` (method) | `get_permission_mode` |
| `permission_preset_label` (method) | `permission_mode_label` |
| `resolve_preset_decision` (function) | `resolve_mode_decision` |
| `PresetDecision` (dataclass) | `ModeDecision` |
| `PresetAction` (type alias) | `ModeAction` |
| `PermissionPresetConfig` (TS interface) | `PermissionModeConfig` |
| `PERMISSION_PRESETS` (TS const) | `PERMISSION_MODES` |
| `inferPermissionPreset` (TS function) | `inferPermissionMode` |

## Files to Modify

### Backend source (src/voidx/)

1. **src/voidx/config/enums.py** — `class PermissionPreset` → `PermissionMode`; all self-references
2. **src/voidx/config/__init__.py** — import + `__all__` export
3. **src/voidx/config/models.py** — field `permission_preset: PermissionPreset` → `permission_mode: PermissionMode`
4. **src/voidx/config/settings_permissions.py** — `get_permission_preset`/`set_permission_preset` → `get_permission_mode`/`set_permission_mode`; data key `"permission_preset"` → `"permission_mode"`
5. **src/voidx/config/settings.py** — `WORKSPACE_ONLY_KEYS` entry; `build_config()` call
6. **src/voidx/permission/presets.py** — `PresetAction` → `ModeAction`; `PresetDecision` → `ModeDecision`; `resolve_preset_decision` → `resolve_mode_decision`; import
7. **src/voidx/permission/__init__.py** — imports + `__all__`
8. **src/voidx/permission/context.py** — field `permission_preset` → `permission_mode`; all references
9. **src/voidx/permission/service.py** — attribute, param, methods, labels, status output, `/permission-preset` in show_rules string
10. **src/voidx/permission/grants.py** — `ApprovalPrecondition.permission_preset` → `permission_mode`; `AccessGrants` fields; `from_parts` param
11. **src/voidx/permission/engine.py** — `_preset_decision_for` references
12. **src/voidx/agent/runtime_context.py** — field, imports, properties
13. **src/voidx/agent/graph/subagent.py** — `permission_preset=config.permission_preset.value` → `permission_mode=config.permission_mode.value`
14. **src/voidx/agent/graph/tool_executor/executor.py** — `permission_preset=host._permission.permission_preset` → `permission_mode=host._permission.permission_mode`
15. **src/voidx/agent/graph/wiring.py** — same pattern as subagent.py
16. **src/voidx/agent/slash/handler.py** — dispatch key `/permission-preset` → `/permission`; method `_permission_preset` → `_permission_mode`; Usage string; all `PermissionPreset` refs
17. **src/voidx/agent/loop/slash.py** — `getattr(permission, "permission_preset", ...)` → `"permission_mode"`
18. **src/voidx/ui/command_catalog.py** — command string, category tuple, UI target map, autocomplete entry
19. **src/voidx/ui/commands.py** — help text (5 entries)
20. **src/voidx/ui/gateway/session/method/settings.py** — import, data key, method calls, error message

### Backend tests (src/tests/)

21. **src/tests/test_permission/test_risk.py** — import + all call sites
22. **src/tests/test_agent/test_permission_phase2.py** — method calls, field access, test name
23. **src/tests/test_agent/test_permission_phase4.py** — method calls, field access, constructor param, test name
24. **src/tests/test_agent/test_permission_phase6.py** — constructor param
25. **src/tests/test_agent/graph/test_graph_authorization.py** — field access, method calls
26. **src/tests/test_agent/graph/test_tool_execution_auth.py** — field access
27. **src/tests/test_agent/slash/test_slash_model.py** — import, dispatch string, field access, method calls
28. **src/tests/test_agent/slash/test_slash_model_advanced.py** — import, dispatch string, field access, method calls, test name
29. **src/tests/test_tools/bash/test_tool.py** — constructor param (6 sites)
30. **src/tests/test_tools/test_shell_tool_phase6.py** — constructor param (3 sites)
31. **src/tests/test_tools/test_powershell_tool.py** — constructor param (3 sites)
32. **src/tests/test_tools/test_powershell_tool_phase6.py** — constructor param (3 sites)

### Frontend (frontend/)

33. **frontend/src/settings.ts** — type `PermissionPreset` → `PermissionMode`; interface `PermissionPresetConfig` → `PermissionModeConfig`; const `PERMISSION_PRESETS` → `PERMISSION_MODES`; function `inferPermissionPreset` → `inferPermissionMode`; data key `"permission_preset"` → `"permission_mode"`; HTML `name="permission_preset"` → `name="permission_mode"`
34. **frontend/src/slash.ts** — command `/permission-preset` → `/permission`
35. **frontend/test/settings.test.ts** — data key, selector name
36. **frontend/test/slash.test.ts** — command string

### Docs (all, including docs/archive/)

37. **src/voidx/data/documents/voidx-guide/reference.md** — command table
38. **src/voidx/data/documents/voidx-guide/permission.md** — command table (5 entries)
39. **docs/archive/ask-first-permission-model.md** — all references
40. **docs/archive/ask-first-permission-model-spec.md** — all references

## Execution Order

1. Backend source: enums → config → permission → agent → ui (top-down dependency order)
2. Backend tests
3. Frontend source + tests
4. Docs

## Verification

```bash
# Backend targeted tests
./test.py --backend -- src/tests/test_permission/ src/tests/test_agent/test_permission_phase2.py src/tests/test_agent/test_permission_phase4.py src/tests/test_agent/test_permission_phase6.py src/tests/test_agent/slash/ src/tests/test_agent/graph/test_graph_authorization.py src/tests/test_agent/graph/test_tool_execution_auth.py -v

# Frontend tests
./test.py --frontend -- --reporter=verbose

# Grep to confirm zero remaining old names
# (run via grep tool, not bash)
```

## Risks

- No backward compatibility: existing user settings files with `permission_preset` key will be ignored; permission falls back to `safe` default.
- Pure mechanical rename; no logic changes.
- ~40 files touched but pattern is uniform.
