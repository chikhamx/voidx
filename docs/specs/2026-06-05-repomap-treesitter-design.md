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

# TypeScript example
TYPESCRIPT_QUERIES = {
    "overview": """
        (class_declaration name: (type_identifier) @class.name)
        (function_declaration name: (identifier) @function.name)
        (interface_declaration name: (type_identifier) @interface.name)
        (type_alias_declaration name: (type_identifier) @type.name)
        (export_statement) @export
    """,
    "signatures": """
        (class_declaration
            name: (type_identifier) @class.name
            body: (class_body
                (method_definition name: (property_identifier) @method.name
                    parameters: (formal_parameters) @method.params)))
        (function_declaration
            name: (identifier) @function.name
            parameters: (formal_parameters) @function.params
            return_type: (type_annotation) @function.return_type)
    """,
}
```

### Symbol Extraction Architecture

```python
@dataclass
class SymbolInfo:
    name: str
    kind: str           # "class", "function", "method", "interface", "struct", etc.
    signature: str      # full signature text (only in "signatures" mode)
    line_start: int
    line_end: int
    parent: str | None  # parent class/interface name
    decorators: list[str] = field(default_factory=list)
    is_exported: bool = False

class LanguageParser(ABC):
    @abstractmethod
    def extract_symbols(self, source: str, detail: str) -> list[SymbolInfo]: ...

class TreeSitterParser(LanguageParser):
    def __init__(self, language_name: str, grammar, queries: dict[str, str]):
        self._language = Language(grammar)
        self._parser = TSParser(self._language)
        self._queries = queries

    def extract_symbols(self, source: str, detail: str) -> list[SymbolInfo]:
        tree = self._parser.parse(source.encode())
        query = self._language.query(self._queries.get(detail, self._queries["overview"]))
        captures = query.captures(tree.root_node)
        return self._build_symbols(captures, source)

class RegexFallbackParser(LanguageParser):
    """Fallback for languages without tree-sitter grammars."""
    ...
```

### Parser Registry

```python
class ParserRegistry:
    _parsers: dict[str, LanguageParser] = {}

    @classmethod
    def get(cls, language: str) -> LanguageParser | None:
        if language in cls._parsers:
            return cls._parsers[language]
        parser = cls._try_tree_sitter(language)
        if parser:
            cls._parsers[language] = parser
            return parser
        return cls._regex_fallback(language)

    @classmethod
    def _try_tree_sitter(cls, language: str) -> TreeSitterParser | None:
        try:
            grammar = _import_grammar(language)
            queries = _QUERIES.get(language, {})
            return TreeSitterParser(language, grammar, queries)
        except ImportError:
            return None
```

### Language Detection

Detect language from file extension (already in `src/voidx/lsp/config.py`):

```python
EXTENSION_TO_LANGUAGE = {
    ".py": "python",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".rb": "ruby",
    ".lua": "lua",
}
```

### Tiered Detail Levels

Replace the current two-level (`overview` / `signatures`) with three levels:

| Level | What's included | Token cost | Use case |
|-------|----------------|------------|----------|
| `tree` | File tree only, no symbols | ~500 tokens | Quick orientation |
| `overview` | Top-level classes, functions, interfaces | ~2000 tokens | Understanding structure |
| `signatures` | All symbols with full signatures | ~6000 tokens | Detailed implementation work |

Token budget increases from 4000 to 8000 for `signatures` mode.

### Relevance Ranking

When the token budget is exceeded, prioritize files by relevance:

```python
def rank_files(files: list[Path], query: str | None, recent_edits: set[str]) -> list[Path]:
    """Rank files by relevance to the current task."""
    scores: dict[Path, float] = {}
    for f in files:
        score = 0.0
        if str(f) in recent_edits:
            score += 10.0  # recently edited files are most relevant
        if query and _matches_query(f, query):
            score += 5.0   # files matching the query pattern
        score += _import_centrality(f)  # files imported by many others
        scores[f] = score
    return sorted(files, key=lambda f: scores.get(f, 0), reverse=True)
```

Import centrality is a simple heuristic: count how many other files import this file. Files imported by many others are likely core modules.

### Incremental Updates

Cache parsed symbols per file, invalidate on mtime change:

```python
class RepoMapCache:
    """Cache parsed symbols keyed by (file_path, mtime, detail_level)."""
    _cache: dict[tuple[str, float, str], list[SymbolInfo]] = {}

    def get(self, path: str, mtime: float, detail: str) -> list[SymbolInfo] | None:
        return self._cache.get((path, mtime, detail))

    def set(self, path: str, mtime: float, detail: str, symbols: list[SymbolInfo]) -> None:
        self._cache[(path, mtime, detail)] = symbols
```

On subsequent `repo_map` calls, only re-parse files whose mtime changed.

### Import/Export Information

In `signatures` mode, also extract import statements:

```python
@dataclass
class ImportInfo:
    module: str
    names: list[str]       # imported names
    is_from: bool          # from X import Y vs import X
    line: int
```

This lets the agent understand module dependencies without reading every file.

## Scope

In scope:

- Tree-sitter integration with 10 language grammars.
- Language-specific query definitions for overview and signatures.
- `ParserRegistry` with tree-sitter + regex fallback.
- Three-tier detail levels with increased token budgets.
- Relevance ranking for large codebases.
- Incremental cache based on file mtime.
- Import/export extraction in signatures mode.
- Optional grammar dependencies (graceful fallback).

Out of scope:

- Call graph generation (future — needs cross-file analysis).
- Type inference / type checking (LSP already provides this).
- Semantic search / embedding-based search (separate feature).
- WASM-based grammar loading (native bindings are sufficient).
- Custom grammar loading from user directories.

## File Changes

| File | Change |
|------|--------|
| `src/voidx/tools/repomap.py` | Major rewrite — use `ParserRegistry`, tiered detail, ranking, caching |
| `src/voidx/tools/ts_parser.py` | New — `TreeSitterParser`, `ParserRegistry`, `SymbolInfo`, `ImportInfo` |
| `src/voidx/tools/ts_queries.py` | New — language-specific tree-sitter queries for 10 languages |
| `src/voidx/tools/ts_detect.py` | New — language detection from file extensions |
| `pyproject.toml` | Add optional `tree-sitter` dependency group |
| `tests/test_tools/test_repomap.py` | Update — test tree-sitter parsing, fallback, caching |
| `tests/test_tools/test_ts_parser.py` | New — parser tests for each language |

## Dependency Strategy

Tree-sitter grammars are optional. In `pyproject.toml`:

```toml
[project.optional-dependencies]
tree-sitter = [
    "tree-sitter>=0.22",
    "tree-sitter-python>=0.21",
    "tree-sitter-typescript>=0.21",
    "tree-sitter-go>=0.21",
    "tree-sitter-rust>=0.21",
    "tree-sitter-java>=0.21",
    "tree-sitter-c>=0.21",
    "tree-sitter-cpp>=0.22",
    "tree-sitter-javascript>=0.21",
    "tree-sitter-ruby>=0.21",
    "tree-sitter-lua>=0.21",
]
```

If no grammar is installed, `ParserRegistry._try_tree_sitter()` returns `None` and the regex fallback is used. This keeps the base install lightweight.

## Risks

| Risk | Mitigation |
|------|-----------|
| Tree-sitter grammars add install size (~20 MB total) | Optional dependency group; regex fallback always available |
| Grammar API changes between tree-sitter versions | Pin minimum versions; test against specific versions |
| Query syntax varies per grammar | Per-language query definitions tested independently |
| Large repos still exceed token budget | Relevance ranking + tiered detail; `tree` mode for orientation |
| Incremental cache grows unbounded | LRU eviction with max 1000 entries |
| Import centrality is expensive to compute | Only compute on first call; cache results |
| Regex fallback produces worse results than before | Keep existing regex patterns as-is; tree-sitter is additive |
