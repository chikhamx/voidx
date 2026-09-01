"""Tool-specific output persistence policy."""

DEFAULT_TOOL_OUTPUT_MAX_CHARS = 8_192

TOOL_RESULT_PERSISTABLE_TOOLS = frozenset({
    "bash",
    "document",
    "find",
    "git",
    "lsp",
    "mcp",
    "search",
    "webfetch",
    "websearch",
})
