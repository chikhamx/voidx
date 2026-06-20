# Remove AgentDef.tools Whitelist

> **Status: In Progress**

## Problem

`AgentDef.tools` and `CHILD_RUN_TOOLS` duplicate the tool registry as a hardcoded whitelist. Every new tool must be added to both `ToolRegistry._register_builtins()` and the whitelist in `agents.py` — the `delete` tool was missed, proving this pattern is error-prone.

## Current Architecture

Three consumers of the whitelist:

1. **`_agent_static_tool_defs()`** (helpers.py) — filters `tools_for_llm()` by `agent.tools` before sending to LLM
2. **`run_subagent()`** (subagent.py) — uses `agent_def.tools` for `filtered_copy()` to build child tool registry
3. **`child_agent_descriptions_for_llm()`** (agents.py) — renders tool list into agent tool description text

## New Architecture

Remove the whitelist entirely. All agents inherit the full tool registry. Access control is handled by:

- **Workflow `denied_tools`** — brainstorm/plan/debug nodes block write tools
- **Permission layer** — `capability_for_tool()` classifies tools; plan mode blocks `FILE_WRITE`
- **`can_delegate`** — still controls whether `agent` tool is available to child runs

### Changes

| File | Change |
|------|--------|
| `agents.py` | Remove `tools` field from `AgentDef`, remove `CHILD_RUN_TOOLS`, simplify `child_run_agent_def` and `child_agent_descriptions_for_llm` |
| `helpers.py` | Remove `_agent_static_tool_defs()` |
| `llm.py` | Replace `_agent_static_tool_defs(agent, ...)` with direct `self.tools.tools_for_llm()` |
| `subagent.py` | Remove `filtered_copy` logic; child inherits `parent_tools` directly. Keep `can_delegate` → discard `agent` |
| `wiring.py` | Update `register_agent_tool` — remove `child_agent_descriptions` param or simplify it |
| `tools/service.py` | Remove `AgentTool.child_agent_descriptions` field if it exists |
| Tests | Update assertions that check `agent.tools` |

### AgentDef After

```python
class AgentDef(BaseModel):
    name: str
    description: str
    when_to_use: str
    can_write: bool
    can_delegate: bool
    hidden: bool = False
    model: str | None = None
    mcp_tools: bool = False
```

### child_agent_descriptions_for_llm After

No longer lists individual tools. Instead describes capabilities:

```
Available child agents:
- voidx: Isolated child run of voidx that follows the supplied workflow route.
  Write access: True
```

### Subagent Tool Inheritance

```python
# Before
allowed_ids = set(agent_def.tools)
if not agent_def.can_delegate:
    allowed_ids.discard("agent")
agent_tools = base_tools.filtered_copy(allowed_ids)

# After
agent_tools = parent_tools or ToolRegistry()
if not agent_def.can_delegate:
    agent_tools = agent_tools.filtered_copy(
        set(agent_tools.ids()) - {"agent"}
    )
```

## Verification

- Full test suite passes
- `delete` tool visible to LLM without explicit whitelist entry
- Workflow denied_tools still blocks write tools in brainstorm/plan/debug
- Plan mode still blocks FILE_WRITE capability tools
