from __future__ import annotations

from voidx.agent.adapters.langgraph.runtime.budget_convergence import ConvergenceDecision
from voidx.agent.adapters.langgraph.runtime.subagent_convergence import (
    FINAL_CONVERGENCE_GUIDANCE,
    SOFT_CONVERGENCE_GUIDANCE,
    convergence_guidance,
    subagent_convergence_action,
)


def test_subagent_policy_maps_shared_signals_to_runtime_actions() -> None:
    assert subagent_convergence_action(ConvergenceDecision()) == "continue"
    assert subagent_convergence_action(ConvergenceDecision(level="soft")) == "guide"
    assert subagent_convergence_action(ConvergenceDecision(level="hard")) == "finalize"


def test_subagent_guidance_does_not_expose_trigger_reasons() -> None:
    forbidden = {
        "runtime",
        "budget",
        "time",
        "step",
        "context",
        "token",
        "threshold",
        "guard",
        "wall clock",
    }

    for guidance in (SOFT_CONVERGENCE_GUIDANCE, FINAL_CONVERGENCE_GUIDANCE):
        lowered = guidance.lower()
        assert guidance.strip()
        assert not any(word in lowered for word in forbidden)

    assert convergence_guidance(final=False, language="zh").startswith("停止扩展范围")
    assert convergence_guidance(final=True, language="zh").startswith("不要再调用工具")
