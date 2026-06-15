"""Load a document template by type — used by the design-doc workflow node."""

from __future__ import annotations

import importlib.resources

from pydantic import BaseModel, Field

from voidx.tools.base import BaseTool, ToolContext, ToolResult, model_to_json_schema

_VALID_DOC_TYPES = ("prd", "tech-design", "rfc", "api-doc", "readme")
_PACKAGE = "voidx.data"
_SUBDIR = "templates"


class LoadDocTemplateInput(BaseModel):
    doc_type: str = Field(
        description=(
            "Document type to load a template for. "
            f"One of: {', '.join(_VALID_DOC_TYPES)}."
        ),
    )


class LoadDocTemplateTool(BaseTool):
    id = "load_doc_template"
    description = (
        "Load a document template by type. Use when the design-doc "
        "workflow node is active and you need a template for the document "
        "you are writing. Returns the template content with placeholders."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(LoadDocTemplateInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = LoadDocTemplateInput.model_validate(args)
        doc_type = inp.doc_type.strip().lower()
        if doc_type not in _VALID_DOC_TYPES:
            return ToolResult(
                title="Invalid doc_type",
                output=(
                    f"Unknown doc_type '{inp.doc_type}'. "
                    f"Valid types: {', '.join(_VALID_DOC_TYPES)}"
                ),
            )
        filename = f"{doc_type}.md"
        try:
            ref = importlib.resources.files(_PACKAGE).joinpath(
                f"{_SUBDIR}/{filename}"
            )
            content = ref.read_text(encoding="utf-8")
        except (FileNotFoundError, TypeError) as exc:
            return ToolResult(
                title="Template not found",
                output=f"Template '{doc_type}' is not available: {exc}",
            )
        return ToolResult(
            title=f"Template: {doc_type}",
            output=content,
            summary=f"template: {doc_type}",
            metadata={"doc_type": doc_type},
        )
