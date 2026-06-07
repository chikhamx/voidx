# Repo Map Upgrade — Tree-sitter Design

Date: 2026-06-05

## Goal

Upgrade the `repo_map` tool from regex-based Python-only signature extraction to tree-sitter-based multi-language AST parsing. This gives the agent accurate, language-aware code structure across all major programming languages, not just Python.

## Current State

Key files:

- `src/voidx/tools/repomap.py` — `RepoMapTool` with regex-based parsing.
  - `_extract_top_level()` and `_extract_all_symbols()` use regex patterns.
  - Only Python has real signature extraction (`def`, `class`, `async def`).
  - Other languages get file tree only — no symbol information.
  - Token budget: 4000 tokens (often too small for medium projects).
  - No incremental updates — re-scans the entire directory every call.

Observed gaps:

- **Python-only signatures** — TypeScript, Go, Rust, Java, C/C++ get no symbol info.
- **Regex is fragile** — breaks on decorators, multi-line signatures, type annotations, nested classes.
- **No import/dependency info** — agent can't see what a module imports or exports.
- **No call graph** — agent can't trace function call relationships.
- **Token budget too small** — 4000 tokens covers ~20 files, insufficient for most real projects.
- **No incremental updates** — re-parses everything even if only one file changed.
- **No language-specific detail levels** — Python classes get method lists, but Go structs don't get field lists.

## External References

- **Aider** repo map: uses tree-sitter for multi-language symbol extraction with ranked relevance.
- **Sourcegraph** SCIP: tree-sitter-based code intelligence with precise navigation.
- **tree-sitter** grammars: 60+ language grammars available as WASM or native bindings.
- **py-tree-sitter** (`tree-sitter` Python package): Python bindings for tree-sitter.

References:

- https://aider.chat/docs/repomap.html
- https://tree-sitter.github.io/tree-sitter/
- https://github.com/grantjenks/py-tree-sitter

## Design

### Approach: Tree-sitter with Language Grammars and Tiered Detail

Replace regex parsing with tree-sitter AST queries. Ship pre-built language grammars for the top 10 languages. Use tiered detail levels to balance information density vs. token budget.

### Tree-sitter Integration

Use the `tree_sitter` Python package (v0.22+) with pre-compiled language grammars:

```python
import tree_sitter_python as tspython
import tree_sitter_typescript as tstypescript
import tree_sitter_go as tsgo
import tree_sitter_rust as tsrust
import tree_sitter_java as tsjava
import tree_sitter_c as tsc
import tree_sitter_cpp as tscpp
import tree_sitter_javascript as tsjavascript
import tree_sitter_ruby as tsruby
import tree_sitter_lua as tslua
```

Each grammar is a separate pip package (~1-2 MB). They're optional dependencies — if a grammar isn't installed, fall back to regex parsing for that language.

### Language Query Definitions

Define tree-sitter queries per language to extract symbols:

```python
# Python example
PYTHON_QUERIES = {
    "overview": """
        (class_definition name: (identifier) @class.name)
        (function_definition name: (identifier) @function.name)
        (decorator) @decorator
    """,
    "signatures": """
        (class_definition
            name: (identifier) @class.name
            body: (block
                (function_definition name: (identifier) @method.name
                    parameters: (parameters) @method.params)))
        (function_definition
            name: (identifier) @function.name
            parameters: (parameters) @function.params
            return_type: (type) @function.return_type)
        (decorator) @decorator
    """,
}
```

### Tiered Detail Levels

| Level | What's included | Token cost |
|-------|----------------|------------|
| `overview` | Top-level class/function names only | Low (~50 tokens/file) |
| `signatures` | Full signatures with params and return types | Medium (~200 tokens/file) |
| `full` | Signatures + docstrings + imports | High (~500 tokens/file) |

The `detail` parameter on `repo_map` selects the tier. Default is `overview`.

### Incremental Updates

Cache parsed ASTs and only re-parse files whose mtime changed:

```python
class RepoMapCache:
    _cache: dict[str, tuple[float, Tree]]  # path → (mtime, tree)

    def get_or_parse(self, path: str, language: Language) -> Tree:
        mtime = os.path.getmtime(path)
        if path in self._cache and self._cache[path][0] == mtime:
            return self._cache[path][1]
        tree = parse_file(path, language)
        self._cache[path] = (mtime, tree)
        return tree
```

### Testing

| Test | Description |
|------|-------------|
| `test_repomap_python_signatures` | Python class/method signatures extracted correctly |
| `test_repomap_typescript_signatures` | TypeScript interface/function signatures |
| `test_repomap_fallback_to_regex` | Falls back to regex when grammar not installed |
| `test_repomap_tiered_detail` | overview < signatures < full in token cost |
| `test_repomap_incremental_cache` | Only re-parses changed files |
