# Ask-First Permission Model Implementation Plan

> **Status: Done** — Archived on 2026-07-14.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace voidx's current sandbox/approval split with a simpler ask-first permission model that uses risk levels, user-friendly presets, and one consistent approval token from authorization through tool execution.

**Architecture:** Add a risk classification layer that produces `normal`, `dangerous`, `extreme`, or `blocked` assessments with tags and reasons. Add a preset resolver that maps risk to `allow`, `ask`, or `blocked_ack`, then pass approved risk tokens into `ToolContext` so Bash/PowerShell do not re-block an approved dynamic shell command. Keep catastrophic commands non-executable but visible through a notice-only approval prompt.

**Tech Stack:** Python 3, Pydantic/dataclasses, existing voidx permission engine, existing UI event protocol, TypeScript frontend, `./test.py` backend/frontend runners.

---

## Source Design

- Design doc: `docs/design/ask-first-permission-model.md`
- Current permission engine: `src/voidx/permission/engine.py`
- Current shell policy: `src/voidx/permission/shell_policy.py`
- Current Bash execution: `src/voidx/tools/bash/tool.py`
- Current PowerShell execution: `src/voidx/tools/powershell/tool.py`
- Current UI permission prompt: `src/voidx/agent/graph/permissions.py`, `src/voidx/ui/output/events/schema.py`, `frontend/src/main.ts`

## Implementation Principles

- Default to `ask`, not `deny`, for anything risky but not catastrophic.
- Read Only is a stateless approval mode, not a hard-deny mode.
- Blocked commands still surface in the approval UI, but only with `Do not run`.
- Tool execution should trust the approval decision for the exact tool call and risk tags.
- Keep old config fields working during migration; simplify user-facing UI first, remove internals later.
- Do not broaden safety silently: every auto-allow change must be covered by a preset/risk test.

## File Structure

| File | Responsibility |
| --- | --- |
| `src/voidx/permission/risk.py` | New risk types, tags, assessment model, and helpers |
| `src/voidx/permission/presets.py` | New preset resolver for risk-to-decision behavior |
| `src/voidx/permission/shell_policy.py` | Refactor shell static policy into risk classification + hard block detection |
| `src/voidx/permission/rules.py` | Keep tool capability classification; route shell/file/git risks to new assessment layer |
| `src/voidx/permission/engine.py` | Return `allow` / `ask` / `blocked_ack`, attach risk metadata |
| `src/voidx/permission/context.py` | Extend `PermissionDecision` with risk, scopes, and blocked acknowledgement action |
| `src/voidx/permission/service.py` | Store preset, session/project grants, approval scope behavior |
| `src/voidx/tools/base.py` | Add approved risk tokens to `ToolContext` |
| `src/voidx/tools/bash/tool.py` | Honor approved shell risks; keep hard blocks |
| `src/voidx/tools/powershell/tool.py` | Same as Bash for PowerShell |
| `src/voidx/agent/graph/permissions.py` | Build risk-aware prompts, handle once/session/project choices, pass approval tokens |
| `src/voidx/agent/graph/tool_executor/executor.py` | Include approval tokens in `ToolContext` for approved tool calls |
| `src/voidx/ui/output/events/schema.py` | Extend permission prompt details with risk level/tags/scopes |
| `src/voidx/ui/protocol/requests.py` | Carry risk-aware permission details to frontend requests |
| `src/voidx/ui/gateway/adapter.py` | Forward risk metadata in prompt notifications |
| `frontend/src/main.ts` | Render risk-aware prompt details and blocked acknowledgement buttons |
| `frontend/src/settings.ts` | Replace low-level permission controls with presets in normal UI |
| `frontend/src/protocol.schema.json` / `frontend/src/protocol.d.ts` | Regenerated from Python schema |
| `src/tests/test_permission/test_risk.py` | New risk classifier and preset resolver tests |
| `src/tests/test_agent/test_permission.py` | Update core authorization expectations |
| `src/tests/test_agent/test_permission_phase6.py` | Update shell policy authorization tests |
| `src/tests/test_tools/bash/test_tool.py` | Add approved dynamic shell regression tests |
| `src/tests/test_tools/test_powershell_tool_phase6.py` | Add approved PowerShell risk regression tests |
| `src/tests/test_ui/gateway/test_adapter.py` | Verify risk metadata appears in permission prompt protocol |
| `frontend/test/settings.test.ts` / `frontend/test/main.test.ts` | Verify preset UI and permission prompt rendering |

## Task 1: Introduce Risk Types and Classification Skeleton

**Files:**
- Create: `src/voidx/permission/risk.py`
- Modify: `src/voidx/permission/__init__.py`
- Test: `src/tests/test_permission/test_risk.py`

- [x] **Step 1: Write failing tests for risk models**

Add tests covering construction and serialization:

```python
from voidx.permission.risk import RiskAssessment, RiskLevel, RiskTag, ApprovalScope


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
```

- [x] **Step 2: Run the focused test and confirm it fails**

Run:

```bash
./test.py --backend -- src/tests/test_permission/test_risk.py -v
```

Expected: fail because `voidx.permission.risk` does not exist.

- [x] **Step 3: Implement the risk models**

Create enums and model:

```python
class RiskLevel(str, Enum):
    NORMAL = "normal"
    DANGEROUS = "dangerous"
    EXTREME = "extreme"
    BLOCKED = "blocked"


class RiskTag(str, Enum):
    SAFE_READ = "safe_read"
    WORKSPACE_EDIT = "workspace_edit"
    DYNAMIC_SHELL = "dynamic_shell"
    NESTED_INTERPRETER = "nested_interpreter"
    EXTERNAL_PATH = "external_path"
    NETWORK = "network"
    DEPENDENCY_INSTALL = "dependency_install"
    GIT_WRITE = "git_write"
    GIT_PUSH = "git_push"
    MASS_DELETE = "mass_delete"
    SYSTEM_DESTRUCTIVE = "system_destructive"
    PRIVILEGE_ESCALATION = "privilege_escalation"


class ApprovalScope(str, Enum):
    ONCE = "once"
    SESSION = "session"
    PROJECT = "project"
    GLOBAL = "global"


class RiskAssessment(BaseModel):
    level: RiskLevel
    tags: tuple[RiskTag, ...] = ()
    reason: str = ""
    tool_name: str
    pattern: str = ""

    @property
    def approvable(self) -> bool:
        return self.level != RiskLevel.BLOCKED
```

- [x] **Step 4: Export models**

Export the new names from `src/voidx/permission/__init__.py` so tests and future modules use one import surface.

- [x] **Step 5: Verify task tests pass**

Run:

```bash
./test.py --backend -- src/tests/test_permission/test_risk.py -v
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add src/voidx/permission/risk.py src/voidx/permission/__init__.py src/tests/test_permission/test_risk.py
git commit -m "feat(permission): add risk assessment model"
```

## Task 2: Add Preset Resolver

**Files:**
- Create: `src/voidx/permission/presets.py`
- Modify: `src/voidx/config/enums.py`
- Modify: `src/voidx/config/permissions.py`
- Modify: `src/voidx/permission/service.py`
- Test: `src/tests/test_permission/test_risk.py`
- Test: `src/tests/test_agent/test_permission.py`

- [x] **Step 1: Write preset resolver tests**

Add tests:

```python
from voidx.permission.presets import PermissionPreset, resolve_preset_decision
from voidx.permission.risk import ApprovalScope, RiskAssessment, RiskLevel, RiskTag


def _risk(level, *tags):
    return RiskAssessment(level=level, tags=tags, tool_name="bash", pattern="cmd")


def test_read_only_asks_once_for_dangerous_risk():
    decision = resolve_preset_decision(PermissionPreset.READ_ONLY, _risk(RiskLevel.DANGEROUS, RiskTag.WORKSPACE_EDIT))

    assert decision.action == "ask"
    assert decision.allowed_scopes == (ApprovalScope.ONCE,)


def test_safe_allows_normal_and_asks_dangerous():
    assert resolve_preset_decision(PermissionPreset.SAFE, _risk(RiskLevel.NORMAL)).action == "allow"
    decision = resolve_preset_decision(PermissionPreset.SAFE, _risk(RiskLevel.DANGEROUS, RiskTag.WORKSPACE_EDIT))
    assert decision.action == "ask"
    assert ApprovalScope.SESSION in decision.allowed_scopes


def test_project_trusted_allows_workspace_edit_but_asks_dynamic_shell():
    edit = resolve_preset_decision(PermissionPreset.PROJECT_TRUSTED, _risk(RiskLevel.DANGEROUS, RiskTag.WORKSPACE_EDIT))
    shell = resolve_preset_decision(PermissionPreset.PROJECT_TRUSTED, _risk(RiskLevel.DANGEROUS, RiskTag.DYNAMIC_SHELL))

    assert edit.action == "allow"
    assert shell.action == "ask"


def test_blocked_is_notice_only_for_every_preset():
    blocked = _risk(RiskLevel.BLOCKED, RiskTag.SYSTEM_DESTRUCTIVE)

    for preset in PermissionPreset:
        decision = resolve_preset_decision(preset, blocked)
        assert decision.action == "blocked_ack"
        assert decision.allowed_scopes == ()
```

- [x] **Step 2: Run tests and confirm failure**

Run:

```bash
./test.py --backend -- src/tests/test_permission/test_risk.py -v
```

Expected: fail because `presets.py` does not exist.

- [x] **Step 3: Implement `PermissionPreset` and decision model**

Add:

```python
class PermissionPreset(str, Enum):
    READ_ONLY = "read-only"
    SAFE = "safe"
    PROJECT_TRUSTED = "project-trusted"
    FULL_ACCESS = "full-access"


class PresetDecision(BaseModel):
    action: Literal["allow", "ask", "blocked_ack"]
    risk: RiskAssessment
    allowed_scopes: tuple[ApprovalScope, ...] = ()
    default_scope: ApprovalScope | None = None
```

Implement `resolve_preset_decision(preset, risk)`.

- [x] **Step 4: Make `permission_preset` the high-level runtime entrypoint**

Final implementation decision:

- `permission_preset` is the only high-level permission runtime input.
- Missing legacy config defaults to `safe`.
- `permission_mode` and `approval_policy` may remain in state/UI compatibility shapes, but they must not drive new permission decisions.
- `sandbox_mode` remains an execution boundary only; it does not decide whether to ask the user.

- [x] **Step 5: Update settings defaults without reviving old runtime semantics**

- Add `permission_preset` to settings and config models.
- Read `permission_preset` directly in `build_config()`.
- When old settings lack `permission_preset`, default to `safe` instead of mapping old `permission_mode` / `approval_policy` combinations.
- Remove old setter paths that actively write `permission_mode` or `approval_policy`.

- [x] **Step 6: Verify focused permission tests**

Run:

```bash
./test.py --backend -- src/tests/test_permission/test_risk.py src/tests/test_agent/test_permission.py -v
```

Expected: new risk tests pass; existing permission tests may still pass or show expected failures to address in later tasks.

- [ ] **Step 7: Commit**

```bash
git add src/voidx/permission/presets.py src/voidx/config/enums.py src/voidx/config/permissions.py src/voidx/permission/service.py src/tests/test_permission/test_risk.py src/tests/test_agent/test_permission.py
git commit -m "feat(permission): add ask-first preset resolver"
```

## Task 3: Convert Shell Policy to Risk Classification

**Files:**
- Modify: `src/voidx/permission/shell_policy.py`
- Modify: `src/voidx/tools/bash/safety.py`
- Modify: `src/voidx/tools/powershell/safety.py`
- Test: `src/tests/test_agent/test_permission_phase6.py`
- Test: `src/tests/test_tools/bash/test_router_safety.py`

- [x] **Step 1: Write shell risk tests**

Add tests that current `shell_policy_for_command()` or new `assess_shell_command()` returns risk instead of direct denial:

```python
from voidx.permission.risk import RiskLevel, RiskTag
from voidx.permission.shell_policy import assess_shell_command


def test_pipe_is_dynamic_shell_risk_not_blocked():
    risk = assess_shell_command("cat file.txt | head -5")

    assert risk.level == RiskLevel.DANGEROUS
    assert RiskTag.DYNAMIC_SHELL in risk.tags


def test_python_script_is_extreme_nested_interpreter_risk():
    risk = assess_shell_command("python3 /tmp/parse_msgs.py")

    assert risk.level == RiskLevel.EXTREME
    assert RiskTag.NESTED_INTERPRETER in risk.tags


def test_recursive_home_delete_is_blocked():
    risk = assess_shell_command("rm -rf /home")

    assert risk.level == RiskLevel.BLOCKED
    assert RiskTag.SYSTEM_DESTRUCTIVE in risk.tags
```

- [x] **Step 2: Run focused shell tests and confirm failure**

Run:

```bash
./test.py --backend -- src/tests/test_agent/test_permission_phase6.py src/tests/test_tools/bash/test_router_safety.py -v
```

Expected: fail for new `assess_shell_command` expectations.

- [x] **Step 3: Implement `assess_shell_command()`**

Keep current parser helpers where possible. Map:

| Current condition | New risk |
| --- | --- |
| empty/comment/static read command | `normal` |
| shell operator `|`, `>`, `&&`, newline | `dangerous` + `dynamic_shell` |
| `$`, backticks, process substitution | `extreme` + `dynamic_shell` |
| nested interpreter (`python3`, `node`, `bash`, etc.) | `extreme` + `nested_interpreter` |
| unknown shell command | `dangerous` |
| catastrophic pattern | `blocked` + hard block tag |

- [x] **Step 4: Keep hard block helper separate**

Add a single helper such as:

```python
def hard_block_reason_for_shell(command: str) -> str | None:
    risk = assess_shell_command(command)
    return risk.reason if risk.level == RiskLevel.BLOCKED else None
```

Use this for non-executable catastrophic cases only.

- [x] **Step 5: Preserve static path planning**

Keep static path extraction for simple read commands. If process sandbox is available and an external path lacks sufficient approval, classify it as `dangerous`/`external_path`, not direct denial.

- [x] **Step 6: Verify shell tests**

Run:

```bash
./test.py --backend -- src/tests/test_agent/test_permission_phase6.py src/tests/test_tools/bash/test_router_safety.py -v
```

Expected: pass after updating old assertions from `deny/defer` to risk-based expectations.

- [ ] **Step 7: Commit**

```bash
git add src/voidx/permission/shell_policy.py src/voidx/tools/bash/safety.py src/voidx/tools/powershell/safety.py src/tests/test_agent/test_permission_phase6.py src/tests/test_tools/bash/test_router_safety.py
git commit -m "refactor(permission): classify shell risks"
```

## Task 4: Update Permission Engine to `allow` / `ask` / `blocked_ack`

**Files:**
- Modify: `src/voidx/permission/schema.py`
- Modify: `src/voidx/permission/context.py`
- Modify: `src/voidx/permission/engine.py`
- Modify: `src/voidx/permission/rules.py`
- Test: `src/tests/test_agent/test_permission.py`
- Test: `src/tests/test_agent/test_permission_phase6.py`

- [x] **Step 1: Write authorization tests for ask-first behavior**

Add tests:

```python
from voidx.permission.engine import authorize_tool_call
from voidx.permission.context import PermissionContext


def test_complex_bash_is_ask_not_deny_in_safe_mode(tmp_path):
    decision = authorize_tool_call(
        {"name": "bash", "args": {"command": "cat file.txt | head -5"}},
        PermissionContext(workspace=str(tmp_path), permission_preset="safe", sandbox_mode="workspace-write"),
    )

    assert decision.action == "ask"
    assert decision.risk.level == "dangerous"


def test_read_only_write_is_ask_once_not_deny(tmp_path):
    decision = authorize_tool_call(
        {"name": "bash", "args": {"command": "echo hi > out.txt"}},
        PermissionContext(workspace=str(tmp_path), permission_preset="read_only", sandbox_mode="read-only"),
    )

    assert decision.action == "ask"
    assert decision.allowed_scopes == ("once",)


def test_blocked_command_returns_blocked_ack(tmp_path):
    decision = authorize_tool_call(
        {"name": "bash", "args": {"command": "rm -rf /home"}},
        PermissionContext(workspace=str(tmp_path), permission_preset="full_access", sandbox_mode="danger-full-access"),
    )

    assert decision.action == "blocked_ack"
```

- [x] **Step 2: Run tests and confirm failure**

Run:

```bash
./test.py --backend -- src/tests/test_agent/test_permission.py src/tests/test_agent/test_permission_phase6.py -v
```

Expected: fail because `blocked_ack`, risk metadata, and Read Only ask behavior do not exist yet.

- [x] **Step 3: Extend permission action type**

Update `src/voidx/permission/schema.py`:

```python
Action = Literal["allow", "ask", "deny", "blocked_ack"]
```

Keep `deny` internally for user denial/session denial if still useful, but avoid using it for risky-but-approvable operations.

- [x] **Step 4: Extend `PermissionDecision`**

Add:

```python
risk: RiskAssessment | None = None
allowed_scopes: tuple[str, ...] = ()
default_scope: str | None = None
```

Use JSON-friendly strings if dataclass serialization gets awkward.

- [x] **Step 5: Route risky operations through preset resolver**

In `authorize_tool_call()`:

1. classify tool call
2. assess risk
3. apply session deny/allow if compatible
4. if risk is blocked, return `blocked_ack`
5. apply preset resolver
6. return `allow` or `ask`

Do not allow Read Only to convert write risks into hard deny.

- [x] **Step 6: Update existing tests**

Replace expectations that currently assert direct `deny` for Read Only risky operations with `ask` + once-only scope. Keep catastrophic command tests as `blocked_ack`.

- [x] **Step 7: Verify permission tests**

Run:

```bash
./test.py --backend -- src/tests/test_agent/test_permission.py src/tests/test_agent/test_permission_phase2.py src/tests/test_agent/test_permission_phase6.py -v
```

Expected: pass.

- [ ] **Step 8: Commit**

```bash
git add src/voidx/permission/schema.py src/voidx/permission/context.py src/voidx/permission/engine.py src/voidx/permission/rules.py src/tests/test_agent/test_permission.py src/tests/test_agent/test_permission_phase2.py src/tests/test_agent/test_permission_phase6.py
git commit -m "refactor(permission): make authorization ask-first"
```

## Task 5: Add Approval Tokens to Tool Execution Context

**Files:**
- Modify: `src/voidx/tools/base.py`
- Modify: `src/voidx/agent/graph/permissions.py`
- Modify: `src/voidx/agent/graph/tool_executor/executor.py`
- Test: `src/tests/test_agent/graph/test_execute_tools_guard.py`
- Test: `src/tests/test_agent/test_permission.py`

- [x] **Step 1: Write tests for approved risk propagation**

Add or update graph-level test with a fake tool that records `ctx.approved_risks`:

```python
class CapturingTool:
    id = "bash"
    async def execute(self, args, ctx):
        seen.append(ctx.approved_risks)
        return ToolResult(output="ok", metadata={"exit_code": 0})
```

Simulate approval choice `"y"` for a dynamic shell command and assert the executed context includes one approved risk token with matching tool call id and risk tags.

- [x] **Step 2: Run focused tests and confirm failure**

Run:

```bash
./test.py --backend -- src/tests/test_agent/graph/test_execute_tools_guard.py -v
```

Expected: fail because `ToolContext.approved_risks` does not exist.

- [x] **Step 3: Add `ApprovedRisk` model and context field**

In `src/voidx/tools/base.py`:

```python
class ApprovedRisk(BaseModel):
    tool_call_id: str
    level: str
    tags: tuple[str, ...] = ()
    scope: str = "once"
    pattern: str = ""


class ToolContext(BaseModel):
    approved_risks: list[ApprovedRisk] = Field(default_factory=list)
```

- [x] **Step 4: Track approvals by tool call id**

In `GraphPermissionMixin._ask_and_apply_permission`, when user approves, return or store an approval token per approved call. Avoid global mutable state leakage between turns.

Recommended shape:

```python
approved: list[dict]
approval_tokens: dict[str, ApprovedRisk]
```

If current executor signatures make this awkward, add a `_approved_risks_by_call_id` dict on the graph host and clear it after each execute cycle.

- [x] **Step 5: Include tokens in `make_context()`**

In `GraphToolExecutor.execute_tools`, build `ToolContext(approved_risks=[token_for_current_call])` for each executed tool. The context should include only risks approved for that exact tool call unless a session/project grant matches.

- [x] **Step 6: Verify propagation**

Run:

```bash
./test.py --backend -- src/tests/test_agent/graph/test_execute_tools_guard.py src/tests/test_agent/test_permission.py -v
```

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add src/voidx/tools/base.py src/voidx/agent/graph/permissions.py src/voidx/agent/graph/tool_executor/executor.py src/tests/test_agent/graph/test_execute_tools_guard.py src/tests/test_agent/test_permission.py
git commit -m "feat(permission): pass approved risks to tools"
```

## Task 6: Make Bash and PowerShell Honor Approved Risks

**Files:**
- Modify: `src/voidx/tools/bash/tool.py`
- Modify: `src/voidx/tools/powershell/tool.py`
- Modify: `src/voidx/tools/shell/common.py`
- Test: `src/tests/test_tools/bash/test_tool.py`
- Test: `src/tests/test_tools/test_shell_tool_phase6.py`
- Test: `src/tests/test_tools/test_powershell_tool_phase6.py`

- [x] **Step 1: Write Bash regression tests**

Add tests:

```python
from voidx.tools.base import ApprovedRisk, ToolContext


@pytest.mark.asyncio
async def test_approved_dynamic_shell_executes(tmp_path):
    data = tmp_path / "data.txt"
    data.write_text("a\nb\n", encoding="utf-8")

    result = await BashTool().execute(
        {"command": "cat data.txt | head -1"},
        ToolContext(
            workspace=str(tmp_path),
            approved_risks=[
                ApprovedRisk(
                    tool_call_id="call1",
                    level="dangerous",
                    tags=("dynamic_shell",),
                    scope="once",
                    pattern="cat data.txt | head -1",
                )
            ],
        ),
    )

    assert result.metadata["exit_code"] == 0
    assert "a" in result.display


@pytest.mark.asyncio
async def test_blocked_shell_never_executes_even_with_approval(tmp_path):
    # This token represents a bug or stale caller state. Blocked risks are never approvable.
    result = await BashTool().execute(
        {"command": "rm -rf /home"},
        ToolContext(
            workspace=str(tmp_path),
            approved_risks=[
                ApprovedRisk(tool_call_id="call1", level="blocked", tags=("system_destructive",), scope="once")
            ],
        ),
    )

    assert result.metadata["blocked"] is True
```

- [x] **Step 2: Run tool tests and confirm failure**

Run:

```bash
./test.py --backend -- src/tests/test_tools/bash/test_tool.py src/tests/test_tools/test_shell_tool_phase6.py -v
```

Expected: approved dynamic shell still blocked before implementation.

- [x] **Step 3: Add approval helper**

Add helper in `src/voidx/tools/shell/common.py` or local tool modules:

```python
def has_approved_risk(ctx: ToolContext, *, tag: str, pattern: str | None = None) -> bool:
    for risk in ctx.approved_risks:
        if risk.level == "blocked":
            continue
        if tag in risk.tags and (pattern is None or risk.pattern == pattern):
            return True
    return False
```

The executor should already filter tokens to the current tool call or a matching saved grant. This helper is still deliberately defensive: blocked risk tokens must never authorize execution, even if a stale or malformed caller passes one in.

- [x] **Step 4: Update BashTool**

Change BashTool flow:

1. hard block check first
2. route hint if command should use another tool
3. shell risk precheck
4. if risk is dynamic/external and matching approval exists, execute
5. otherwise return blocked/needs-approval result only for missing approval in direct tool tests

Do not return `shell policy deferred` for an approved exact command.

- [x] **Step 5: Update PowerShellTool**

Mirror BashTool behavior for approved PowerShell risks.

- [x] **Step 6: Verify shell tools**

Run:

```bash
./test.py --backend -- src/tests/test_tools/bash/test_tool.py src/tests/test_tools/test_shell_tool_phase6.py src/tests/test_tools/test_powershell_tool_phase6.py -v
```

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add src/voidx/tools/bash/tool.py src/voidx/tools/powershell/tool.py src/voidx/tools/shell/common.py src/tests/test_tools/bash/test_tool.py src/tests/test_tools/test_shell_tool_phase6.py src/tests/test_tools/test_powershell_tool_phase6.py
git commit -m "fix(shell): execute approved dynamic shell risks"
```

## Task 7: Add Risk-Aware Permission Prompt UI Protocol

**Files:**
- Modify: `src/voidx/ui/output/events/schema.py`
- Modify: `src/voidx/ui/protocol/requests.py`
- Modify: `src/voidx/ui/gateway/adapter.py`
- Modify: `scripts/export_ui_protocol_schema.py` output files
- Modify: `frontend/src/main.ts`
- Test: `src/tests/test_ui/gateway/test_adapter.py`
- Test: `frontend/test/main.test.ts`

- [x] **Step 1: Write adapter tests**

Extend permission prompt tests to assert each tool detail includes:

```json
{
  "risk_level": "extreme",
  "risk_tags": ["dynamic_shell"],
  "risk_reason": "executes dynamic shell code",
  "allowed_scopes": ["once"]
}
```

- [x] **Step 2: Run adapter tests and confirm failure**

Run:

```bash
./test.py --backend -- src/tests/test_ui/gateway/test_adapter.py -v
```

Expected: fail because schema lacks fields.

- [x] **Step 3: Extend `PermissionToolDetail`**

Add optional fields:

```python
risk_level: str = ""
risk_tags: list[str] = Field(default_factory=list)
risk_reason: str = ""
allowed_scopes: list[str] = Field(default_factory=list)
blocked: bool = False
```

- [x] **Step 4: Forward fields through UI request schema and adapter**

Ensure `UiPermissionRequest` and adapter forward `PermissionToolDetail.model_dump()` unchanged.

- [x] **Step 5: Update frontend rendering**

In `frontend/src/main.ts`, render permission details with a risk heading before JSON args:

```text
Risk: Extremely Dangerous
Why: ...
Tool: bash
Command: ...
```

For `blocked_ack`, render one button only: `Do not run`.

- [x] **Step 6: Regenerate protocol schema**

Run:

```bash
cd frontend && npm run schema
```

Expected: updates `frontend/src/protocol.schema.json` and `frontend/src/protocol.d.ts`.

- [x] **Step 7: Verify backend and frontend prompt tests**

Run:

```bash
./test.py --backend -- src/tests/test_ui/gateway/test_adapter.py -v
./test.py --frontend -- test/main.test.ts -v
```

Expected: pass.

- [ ] **Step 8: Commit**

```bash
git add src/voidx/ui/output/events/schema.py src/voidx/ui/protocol/requests.py src/voidx/ui/gateway/adapter.py frontend/src/main.ts frontend/src/protocol.schema.json frontend/src/protocol.d.ts src/tests/test_ui/gateway/test_adapter.py frontend/test/main.test.ts
git commit -m "feat(ui): show risk-aware permission prompts"
```

## Task 8: Implement Blocked Acknowledgement Flow

**Files:**
- Modify: `src/voidx/agent/graph/permissions.py`
- Modify: `src/voidx/agent/graph/tool_executor/executor.py`
- Modify: `src/voidx/tools/shell/common.py`
- Test: `src/tests/test_agent/test_permission.py`
- Test: `src/tests/test_agent/graph/test_execute_tools_guard.py`
- Test: `src/tests/test_ui/gateway/test_adapter.py`

- [x] **Step 1: Write blocked acknowledgement tests**

Test that a blocked command:

1. emits a permission prompt with only `Do not run`
2. does not execute the tool
3. returns a blocked ToolMessage / ToolResult

Expected result metadata:

```python
{"blocked": True, "error": True, "not_executed": True, "risk_level": "blocked"}
```

- [x] **Step 2: Run tests and confirm failure**

Run:

```bash
./test.py --backend -- src/tests/test_agent/test_permission.py src/tests/test_agent/graph/test_execute_tools_guard.py src/tests/test_ui/gateway/test_adapter.py -v
```

Expected: fail because `blocked_ack` is not handled.

- [x] **Step 3: Add blocked prompt choices**

In `GraphPermissionMixin`, when `decision.action == "blocked_ack"`:

- add it to a separate notice-only prompt batch
- use choices `[("Do not run", "n", "Do not run")]`
- never append to approved calls
- after user dismisses or if UI unavailable, append denied/blocked result

- [x] **Step 4: Return explicit blocked result without executing tool**

Use existing `build_blocked_result` style or add a generic helper for non-executed blocked tool calls. The command must not reach BashTool/PowerShellTool.

- [x] **Step 5: Verify blocked flow**

Run:

```bash
./test.py --backend -- src/tests/test_agent/test_permission.py src/tests/test_agent/graph/test_execute_tools_guard.py src/tests/test_ui/gateway/test_adapter.py -v
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add src/voidx/agent/graph/permissions.py src/voidx/agent/graph/tool_executor/executor.py src/voidx/tools/shell/common.py src/tests/test_agent/test_permission.py src/tests/test_agent/graph/test_execute_tools_guard.py src/tests/test_ui/gateway/test_adapter.py
git commit -m "feat(permission): add blocked acknowledgement flow"
```

## Task 9: Simplify Settings UI to Permission Presets

**Files:**
- Modify: `frontend/src/settings.ts`
- Modify: `src/voidx/ui/gateway/session/method/settings.py`
- Modify: `src/voidx/config/settings_permissions.py`
- Test: `frontend/test/settings.test.ts`
- Test: `src/tests/test_ui/gateway/test_gateway_v2_session.py`

- [x] **Step 1: Write settings UI tests**

Assert the normal permissions tab shows preset choices:

```text
Read Only
Safe
Project Trusted
Full Access
Full Access
```

Assert it no longer shows separate `Sandbox` and `Approval` selects in the default view.

- [x] **Step 2: Run frontend settings tests and confirm failure**

Run:

```bash
./test.py --frontend -- test/settings.test.ts -v
```

Expected: fail because current settings UI still shows low-level controls.

- [x] **Step 3: Update frontend settings**

Replace the `Permission mode`, `Sandbox`, and `Approval` controls with one preset select. Keep low-level values read-only under an "Advanced" section or hidden debug-only section.

- [x] **Step 4: Update gateway settings method**

Accept and persist `permission_preset` directly. Legacy low-level permission fields may remain in snapshots, but they do not map back into runtime preset decisions.

- [x] **Step 5: Verify settings tests**

Run:

```bash
./test.py --frontend -- test/settings.test.ts -v
./test.py --backend -- src/tests/test_ui/gateway/test_gateway_v2_session.py -v
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/settings.ts src/voidx/ui/gateway/session/method/settings.py src/voidx/config/settings_permissions.py frontend/test/settings.test.ts src/tests/test_ui/gateway/test_gateway_v2_session.py
git commit -m "feat(settings): simplify permission preset UI"
```

## Task 10: End-to-End Regression and Cleanup

**Files:**
- Modify tests only unless verification exposes gaps.
- Update docs if implementation differs from this spec.

- [x] **Step 1: Add end-to-end regression tests for the original failure**

Cover the exact historical pattern:

```text
cat /Users/.../.voidx/tool-results/...txt | head -5
python3 /tmp/parse_msgs2.py 2>&1
python3 /tmp/parse_msgs2.py 2>&1 | head -120
```

Use temp files instead of real user paths. Assert approved commands execute and return real exit codes.

- [x] **Step 2: Run focused backend suite**

Run:

```bash
./test.py --backend -- src/tests/test_permission src/tests/test_agent/test_permission.py src/tests/test_agent/test_permission_phase6.py src/tests/test_tools/bash/test_tool.py src/tests/test_tools/test_powershell_tool_phase6.py src/tests/test_ui/gateway/test_adapter.py -v
```

Expected: pass.

- [x] **Step 3: Run focused frontend suite**

Run:

```bash
./test.py --frontend -- test/main.test.ts test/settings.test.ts -v
```

Expected: pass.

- [x] **Step 4: Run broader backend permission/tool suite**

Run:

```bash
./test.py --backend -- src/tests/test_agent src/tests/test_tools -v --keep-going
```

Expected: pass or only unrelated pre-existing failures. Investigate any permission/tool regressions.

- [x] **Step 5: Regenerate protocol schema if needed**

Run:

```bash
./python.py scripts/export_ui_protocol_schema.py
```

Expected: no diff beyond intended protocol files.

- [x] **Step 6: Update docs status**

If implementation matches this spec, update:

- `docs/specs/ask-first-permission-model.md` status notes if needed
- `docs/design/ask-first-permission-model.md` open questions if resolved

- [ ] **Step 7: Commit final cleanup**

```bash
git add docs/design/ask-first-permission-model.md docs/specs/ask-first-permission-model.md src frontend
git commit -m "test(permission): cover ask-first permission model"
```

## Acceptance Criteria

1. Users see four simple permission presets in normal UI: Read Only, Safe, Project Trusted, Full Access.
2. Read Only no longer hard-denies risky operations; it asks once with no persistent scope options.
3. Dangerous operations default to approval instead of denial in Safe mode.
4. Extremely Dangerous operations can be approved only for the current command by default.
5. Blocked operations show a permission/acknowledgement prompt with only `Do not run` and never execute.
6. Approved complex Bash commands execute and return their real exit code.
7. `cat file | head -5` no longer fails with `shell policy deferred` after approval.
8. `python3 /tmp/script.py 2>&1` no longer fails with `shell policy deferred` after approval.
9. `rm -rf /home` and equivalent catastrophic commands remain non-executable in every preset.
10. Session/project approvals apply only to matching tool, risk tags, and pattern.
11. Existing config files that lack `permission_preset` load with the default `safe` preset.
12. Frontend permission prompts show risk level, reason, command/pattern, and allowed choices.
13. All targeted backend/frontend tests listed in this plan pass.

## Rollout Notes

- Ship this behind existing permission settings first; avoid deleting old fields in the same PR.
- Keep legacy permission fields as state/UI compatibility shape only; do not reintroduce old runtime mappings.
- If user confusion persists around `Full Access`, rename the UI label to `Low Friction` while keeping config value `full-access`.
- If `curl | bash` is judged too permissive as Extreme ask-once, promote it to Blocked in a follow-up patch.

## Implementation Order Summary

1. Risk model
2. Preset resolver
3. Shell risk classification
4. Permission engine action simplification
5. Approval token propagation
6. Bash/PowerShell execution fix
7. Risk-aware UI prompt protocol
8. Blocked acknowledgement flow
9. Settings preset UI
10. End-to-end regression and cleanup
