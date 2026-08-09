"""Child-agent policy for shared budget convergence signals."""

from __future__ import annotations

from typing import Literal

from voidx.agent.adapters.langgraph.runtime.budget_convergence import ConvergenceDecision


SubagentConvergenceAction = Literal["continue", "guide", "finalize"]

SOFT_CONVERGENCE_GUIDANCE = (
    "Stop expanding scope. Complete the current objective and provide a concise, complete summary soon."
)
FINAL_CONVERGENCE_GUIDANCE = (
    "Do not call more tools. Complete the current objective from the information already gathered and "
    "respond immediately. If work remains, state the findings, verification, and remaining items."
)
SOFT_CONVERGENCE_GUIDANCE_ZH = "停止扩展范围，完成当前目标，并尽快给出简洁、完整的结果总结。"
FINAL_CONVERGENCE_GUIDANCE_ZH = (
    "不要再调用工具。基于已有信息完成当前任务，并立即输出最终结果；"
    "如尚未全部完成，请明确已有结论、验证情况和剩余项。"
)


def subagent_convergence_action(decision: ConvergenceDecision) -> SubagentConvergenceAction:
    if decision.level == "hard":
        return "finalize"
    if decision.level == "soft":
        return "guide"
    return "continue"


def convergence_guidance(*, final: bool, language: str = "") -> str:
    chinese = language.strip().lower().startswith("zh")
    if final:
        return FINAL_CONVERGENCE_GUIDANCE_ZH if chinese else FINAL_CONVERGENCE_GUIDANCE
    return SOFT_CONVERGENCE_GUIDANCE_ZH if chinese else SOFT_CONVERGENCE_GUIDANCE
