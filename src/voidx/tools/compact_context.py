"""Inline context compaction summary tool."""

from __future__ import annotations

from pydantic import BaseModel, Field

from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema


class CompactContextInput(BaseModel):
    summary: str = Field(
        description=(
            "Structured Markdown summary of the older context to preserve. "
            "Keep durable facts, decisions, constraints, progress, blockers, "
            "verification results, and relevant files."
        ),
        min_length=1,
    )
    tail_anchor_id: str = Field(
        default="",
        description=(
            "Optional id of the first live message that should remain after "
            "compaction. Use the tail_anchor_id shown in VOIDX_COMPACTION_GUIDE."
        ),
    )


class CompactContextTool(BaseTool):
    id = "compact_context"
    description = (
        "Submit a structured summary for older conversation context when "
        "VOIDX_COMPACTION_GUIDE is present. This updates runtime memory and "
        "removes older live messages; it does not answer the user."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(CompactContextInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = CompactContextInput.model_validate(args)
        summary = inp.summary.strip()
        tail_anchor_id = inp.tail_anchor_id.strip()
        return ToolResult(
            title="context compacted",
            output="Compacted older context into the runtime summary.",
            metadata={
                "inline_compaction": {
                    "summary": summary,
                    "tail_anchor_id": tail_anchor_id,
                }
            },
        )
