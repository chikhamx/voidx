"""Doc template loader — loads structured document skeletons on demand."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from voidx.tools.base import BaseTool, model_to_json_schema, ToolContext, ToolResult

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "skills" / "bundled" / "superpowers" / "writing-design-docs" / "templates"

_DOC_TYPES = ("prd", "tech-design", "rfc", "api-doc", "readme")


class LoadDocTemplateInput(BaseModel):
    doc_type: str = Field(
        description=(
            f"Document type to load. One of: {', '.join(_DOC_TYPES)}. "
            "Returns the skeleton template for the AI to fill in."
        )
    )


class LoadDocTemplateTool(BaseTool):
    id = "load_doc_template"
    description = (
        "Load a document template skeleton by type. Returns a structured "
        "template with {placeholder} fields and <!-- guidance comments --> "
        "for the AI to fill in. Use before writing design docs, PRDs, RFCs, "
        "API docs, or READMEs."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(LoadDocTemplateInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        inp = LoadDocTemplateInput.model_validate(args)
        doc_type = inp.doc_type.strip().lower()

        if doc_type not in _DOC_TYPES:
            return ToolResult(
                output=(
                    f"Unknown doc_type '{inp.doc_type}'. "
                    f"Available types: {', '.join(_DOC_TYPES)}"
                ),
                metadata={"error": True},
            )

        template_path = _TEMPLATES_DIR / f"{doc_type}.md"

        # Check project-level override first
        project_override = Path(ctx.workspace) / ".voidx" / "templates" / f"{doc_type}.md"
        # Then global override
        global_override = Path.home() / ".voidx" / "templates" / f"{doc_type}.md"

        source_path = None
        source_label = ""
        for path, label in (
            (project_override, "project"),
            (global_override, "global"),
            (template_path, "bundled"),
        ):
            if path.exists() and path.is_file():
                source_path = path
                source_label = label
                break

        if source_path is None:
            return ToolResult(
                output=f"Template not found for doc_type '{doc_type}'.",
                metadata={"error": True},
            )

        content = source_path.read_text(encoding="utf-8")

        # Extract display_name from frontmatter for metadata
        display_name = doc_type
        lines = content.splitlines()
        if lines and lines[0].strip() == "---":
            for line in lines[1:]:
                if line.strip() == "---":
                    break
                if line.startswith("display_name:"):
                    display_name = line.split(":", 1)[1].strip()
                    break

        return ToolResult(
            title=f"Template: {display_name} (from {source_label})",
            output=content,
            metadata={
                "doc_type": doc_type,
                "display_name": display_name,
                "source": source_label,
            },
        )
