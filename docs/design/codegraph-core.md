# CodeGraph Core — 技术设计文档

> 用 Rust 实现代码知识图谱核心引擎，编译为共享库，通过 C FFI 暴露给 voidx Python 侧调用。

## 1. 背景与动机

### 1.1 现状

voidx 当前通过 `repo_map`（正则解析）+ `lsp_*`（LSP 逐符号查询）理解代码结构，存在三个核心缺口：

| 缺口 | 表现 |
|------|------|
| **多语言解析粗糙** | repo_map 仅 Python 有专用正则，其他语言用通用 fallback |
| **无调用关系图谱** | 只能逐个查 LSP references 拼凑调用链，无法一次返回完整影响半径 |
| **无持久化索引** | 每次调用 repo_map 从零扫描，无缓存无增量更新 |

### 1.2 为什么不用 CodeGraph（Node.js MCP）

| 问题 | 影响 |
|------|------|
| 双运行时依赖 | voidx 从纯 Python 变成 Python + Node.js |
| 数据割裂 | CodeGraph 索引在 `.codegraph/`，voidx 状态在 `~/.voidx/` |
| LSP 无法协同 | CodeGraph 不知道 voidx 的 LSP，无法用 LSP 补充动态引用 |
| 进程管理开销 | 需要管理 MCP 子进程生命周期 |
| 用户体验 | `pip install voidx` 不够，还得装 Node.js + npm install |

### 1.3 为什么用 Rust

| 优势 | 说明 |
|------|------|
| **tree-sitter 原生** | tree-sitter 本身是 C 库，Rust 绑定零开销 |
| **tree-sitter-language-pack** | 一个 crate 包含 305 种语言的预编译 grammar + 结构化提取 API |
| **性能** | 解析 + 图遍历在毫秒级，适合大型代码库 |
| **单文件分发** | 编译为 `.so`/`.dylib`/`.dll`，Python 侧 cffi 加载，无需额外运行时 |
| **与 voidx LSP 互补** | Rust 做静态解析，Python 侧调 LSP 补充动态引用 |

## 2. 架构总览

```
┌──────────────────────────────────────────────────────────────┐
│                      voidx Python 层                         │
│                                                              │
│  codegraph_* 工具 ──→ CodeGraphBridge ──→ cffi/ctypes       │
│       │                              │                        │
│       │ (LSP fallback)               │ (C FFI 调用)          │
│       ↓                              ↓                        │
│  LspManager                    libcodegraph_core.so           │
│                                                              │
├──────────────────────────────────────────────────────────────┤
│                      Rust codegraph-core                     │
│                                                              │
│  ┌─────────────┐  ┌─────────────┐  ┌──────────────────────┐ │
│  │   Parser     │  │   Edges     │  │   Graph (SQLite)     │ │
│  │              │  │             │  │                      │ │
│  │ tree-sitter  │→ │ calls       │→ │ nodes + edges + FTS5 │ │
│  │ language-pack│  │ imports     │  │ 增量索引             │ │
│  │              │  │ inherits    │  │ 图遍历查询           │ │
│  │ process()    │  │             │  │                      │ │
│  └─────────────┘  └─────────────┘  └──────────────────────┘ │
│                                                              │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │   C FFI Layer (cdylib)                                  │ │
│  │   cg_index / cg_query / cg_search / cg_free             │ │
│  └─────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
```

## 3. Rust Crate 设计

### 3.1 依赖

```toml
# crates/codegraph-core/Cargo.toml
[package]
name = "codegraph-core"
version = "0.1.0"
edition = "2021"

[lib]
crate-type = ["cdylib", "rlib"]

[dependencies]
tree-sitter-language-pack = "1.9"    # 305 语言 grammar + 结构化提取
rusqlite = { version = "0.32", features = ["bundled"] }  # SQLite（内置编译）
serde = { version = "1", features = ["derive"] }
serde_json = "1"
sha2 = "0.10"                         # 文件 content hash（增量索引）
walkdir = "2"                          # 目录遍历
ignore = "0.4"                         # .gitignore 感知的文件扫描
rayon = "1.10"                         # 并行解析
```

### 3.2 模块结构

```
crates/codegraph-core/src/
├── lib.rs          # 公共 API + FFI 导出
├── parser.rs       # 调用 tree-sitter-language-pack 提取符号/导入
├── edges.rs        # 从 AST 提取调用边、继承边
├── resolver.rs     # 跨文件引用解析（import → 定义匹配）
├── graph.rs        # 图谱构建 + 增量更新
├── db.rs           # SQLite schema + CRUD + 图遍历查询
├── query.rs        # 高级查询：callers/callees/impact/search
├── ffi.rs          # C FFI 接口定义
└── types.rs        # 共享数据类型
```

### 3.3 核心数据类型

```rust
// types.rs

/// 代码符号节点
#[derive(Debug, Clone, Serialize)]
pub struct CodeNode {
    pub id: String,                    // hash(qualified_name + file_path)
    pub kind: NodeKind,                // function / class / method / ...
    pub name: String,                  // 原始名称
    pub qualified_name: String,        // "src/auth.py::AuthService.validate_token"
    pub file_path: String,
    pub start_line: u32,
    pub end_line: u32,
    pub signature: Option<String>,     // 函数签名文本
    pub doc_comment: Option<String>,   // 文档字符串
    pub visibility: Option<String>,    // public / private / ...
}

#[derive(Debug, Clone, Serialize)]
pub enum NodeKind {
    Function,
    Method,
    Class,
    Struct,
    Interface,
    Enum,
    Trait,
    Module,
    Namespace,
    Variable,
    Constant,
    Property,
    Component,    // React/Svelte/Vue 组件
    Route,        // HTTP 路由处理器
    Other(String),
}

/// 代码关系边
#[derive(Debug, Clone, Serialize)]
pub struct CodeEdge {
    pub source_id: String,
    pub target_id: String,
    pub kind: EdgeKind,
    pub file_path: Option<String>,
    pub line_number: Option<u32>,
    pub provenance: Provenance,
}

#[derive(Debug, Clone, Serialize)]
pub enum EdgeKind {
    Calls,        // A 调用 B
    Imports,      // A 导入 B
    Inherits,     // A 继承 B
    DependsOn,    // A 依赖 B（导入但未直接调用）
    Implements,   // A 实现 B（接口）
    Contains,     // A 包含 B（类包含方法）
}

#[derive(Debug, Clone, Serialize)]
pub enum Provenance {
    Static,       // tree-sitter AST 直接提取
    Resolved,     // 跨文件引用解析后生成
    Lsp,          // LSP 补充（Python 侧回填）
}
```

## 4. 解析管线

### 4.1 Phase 1：符号提取（parser.rs）

直接使用 `tree-sitter-language-pack` 的 `process()` API：

```rust
use tree_sitter_language_pack::{process, ProcessConfig};

pub fn extract_symbols(
    source: &str,
    language: &str,
    file_path: &str,
) -> Result<Vec<CodeNode>> {
    let config = ProcessConfig::new(language)
        .structure(true)
        .imports(true)
        .exports(true)
        .docstrings(true)
        .symbols(true);

    let result = process(source, &config)?;

    let nodes = result.structure.iter().map(|item| {
        structure_item_to_node(item, file_path)
    }).collect();

    Ok(nodes)
}
```

**关键**：`tree-sitter-language-pack` 的 `process()` 一次调用返回：
- `structure`：函数/类/方法/接口等，含嵌套 children、签名、文档字符串
- `imports`：导入语句，含 source/items/alias
- `exports`：导出语句
- `symbols`：符号定义
- `diagnostics`：解析错误

**这意味着 305 种语言的符号提取几乎零开发量**，一个 `process()` 调用搞定。

### 4.2 Phase 2：边提取（edges.rs）

从 `process()` 返回的 `structure` 和 `imports` 中提取边：

#### 4.2.1 Contains 边（最简单）

```rust
// 类包含方法 → Contains 边
// structure 的 children 字段直接给出嵌套关系
fn extract_contains_edges(
    items: &[StructureItem],
    parent_id: &str,
    file_path: &str,
) -> Vec<CodeEdge> { ... }
```

**难度**：⭐ 简单。`StructureItem.children` 直接给出。

#### 4.2.2 Imports 边

```rust
// from x import y → Imports 边
// process() 返回的 ImportInfo 直接给出
fn extract_import_edges(
    imports: &[ImportInfo],
    file_path: &str,
) -> Vec<CodeEdge> { ... }
```

**难度**：⭐ 简单。`ImportInfo` 直接给出 source/items。

#### 4.2.3 Calls 边（核心难点）

tree-sitter-language-pack 的 `process()` **不直接返回调用关系**。需要从 AST 中提取。

**策略**：对每种语言编写 tree-sitter query 提取 call 表达式：

```rust
// Python: (call function: (identifier) @callee)
// Python: (call function: (attribute attribute: (identifier) @callee))
// Go:     (call_expression function: (selector_expression) @callee)
// Rust:   (call_expression function: (field_expression) @callee)
// Java:   (method_invocation name: (identifier) @callee)
```

**逐语言难度**：

| 语言 | call 表达式 AST 节点 | 难度 | 说明 |
|------|---------------------|------|------|
| Go | `call_expression` | ⭐ | receiver + method 名直接可读 |
| Rust | `call_expression` | ⭐ | 路径明确 |
| Python | `call` | ⭐⭐ | `obj.method()` 需匹配 attribute |
| Java | `method_invocation` | ⭐⭐ | 需匹配 object 类型 |
| TypeScript | `call_expression` | ⭐⭐⭐ | JSX `<Comp />` 是特殊调用 |
| C/C++ | `call_expression` | ⭐⭐⭐⭐ | 函数指针、宏调用难解析 |
| Ruby | `call` / `method_call` | ⭐⭐⭐⭐ | `method_missing`、`send` 无法静态解析 |

**实现方式**：每种语言一个 query 文件（`.scm`），运行时加载：

```
crates/codegraph-core/src/queries/
├── python.scm
├── go.scm
├── rust.scm
├── java.scm
├── typescript.scm
└── ...
```

#### 4.2.4 Inherits 边

```rust
// Python: class Foo(Bar) → Inherits 边
// Java: class Foo extends Bar → Inherits 边
// Go: 无继承，但有 interface embedding
// Rust: struct Foo; impl Trait for Foo → Implements 边
```

**难度**：⭐⭐ 中等。从 `StructureItem` 的父类信息提取，大部分语言语法显式。

### 4.3 Phase 3：跨文件引用解析（resolver.rs）

将 import 语句匹配到 `cg_nodes` 中的定义节点：

```rust
pub fn resolve_references(
    nodes: &[CodeNode],
    edges: &[CodeEdge],
) -> Vec<CodeEdge> {
    // 1. 收集所有 import 边
    // 2. 对每个 import，在 nodes 中查找匹配的 qualified_name
    //    from auth import AuthService → 匹配 src/auth.py::AuthService
    // 3. 生成 Resolved 边：调用者 → 被调用定义
    // 4. 对同文件内的 calls 边，尝试匹配到同文件定义
}
```

**匹配策略**：
1. **精确匹配**：`from auth import AuthService` → 查找 `*::AuthService`
2. **路径匹配**：`import src.auth` → 查找 `src/auth.py::*`
3. **模糊匹配**：同名符号优先匹配同目录/同包的

**难度**：⭐⭐⭐ 难。这是整个系统最复杂的部分，但不需要完美——70-80% 的匹配率已经很有价值，剩余的用 LSP fallback。

## 5. SQLite 图谱存储

### 5.1 Schema

```sql
-- 节点表
CREATE TABLE cg_nodes (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    signature TEXT,
    doc_comment TEXT,
    visibility TEXT,
    workspace TEXT NOT NULL,
    indexed_at TEXT NOT NULL
);

CREATE INDEX idx_nodes_file ON cg_nodes(file_path);
CREATE INDEX idx_nodes_name ON cg_nodes(name);
CREATE INDEX idx_nodes_kind ON cg_nodes(kind);
CREATE INDEX idx_nodes_workspace ON cg_nodes(workspace);

-- 边表
CREATE TABLE cg_edges (
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    file_path TEXT,
    line_number INTEGER,
    provenance TEXT NOT NULL DEFAULT 'static',
    PRIMARY KEY (source_id, target_id, kind)
);

CREATE INDEX idx_edges_source ON cg_edges(source_id);
CREATE INDEX idx_edges_target ON cg_edges(target_id, kind);
CREATE INDEX idx_edges_kind ON cg_edges(kind);

-- FTS5 全文搜索
CREATE VIRTUAL TABLE cg_nodes_fts USING fts5(
    name,
    qualified_name,
    signature,
    doc_comment,
    content=cg_nodes,
    content_rowid=rowid
);

-- 文件索引状态（增量更新）
CREATE TABLE cg_file_index (
    file_path TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    language TEXT NOT NULL,
    indexed_at TEXT NOT NULL
);

-- 工作区元数据
CREATE TABLE cg_workspaces (
    root_path TEXT PRIMARY KEY,
    last_full_index TEXT NOT NULL,
    node_count INTEGER NOT NULL DEFAULT 0,
    edge_count INTEGER NOT NULL DEFAULT 0
);
```

### 5.2 存储位置

```
~/.voidx/codegraph.db    # 全局图谱数据库（多 workspace 共享）
```

与 voidx 现有的 `~/.voidx/voidx.db` 分开，避免 schema 耦合。未来可考虑合并。

### 5.3 增量索引

```rust
pub fn sync_workspace(db: &Connection, workspace: &str) -> Result<SyncResult> {
    // 1. 扫描工作区文件（ignore crate 感知 .gitignore）
    // 2. 对每个文件计算 SHA256 hash
    // 3. 与 cg_file_index 中的 hash 比对
    // 4. 只重新解析变更文件
    // 5. 删除旧节点/边，插入新节点/边
    // 6. 重新运行跨文件引用解析
}
```

### 5.4 图遍历查询

```rust
/// 查找调用者：谁调用了 target？
pub fn query_callers(db: &Connection, target_id: &str, depth: u32) -> Result<Vec<CodeNode>> {
    // 递归 CTE：
    // WITH RECURSIVE callers AS (
    //   SELECT source_id FROM cg_edges WHERE target_id = ? AND kind = 'calls'
    //   UNION ALL
    //   SELECT e.source_id FROM cg_edges e JOIN callers c ON e.target_id = c.source_id
    //   WHERE e.kind = 'calls'
    // )
    // SELECT n.* FROM cg_nodes n JOIN callers c ON n.id = c.source_id
    // LIMIT ?
}

/// 查找被调用者：target 调用了谁？
pub fn query_callees(db: &Connection, source_id: &str, depth: u32) -> Result<Vec<CodeNode>> { ... }

/// 影响半径：变更 target 会影响什么？（callers + 依赖者，多跳）
pub fn query_impact(db: &Connection, target_id: &str, max_depth: u32) -> Result<ImpactResult> {
    // 同时向上遍历 callers 和 depends_on
}

/// FTS5 搜索
pub fn search_symbols(db: &Connection, query: &str, limit: u32) -> Result<Vec<CodeNode>> {
    // SELECT * FROM cg_nodes WHERE id IN (
    //   SELECT rowid FROM cg_nodes_fts WHERE cg_nodes_fts MATCH ?
    // )
}
```

## 6. C FFI 接口

### 6.1 设计原则

- 最小接口：只暴露 voidx Python 侧需要的操作
- 所有权清晰：Rust 分配的内存由 Rust 释放（`cg_free_*`）
- JSON 传输：复杂返回值序列化为 JSON 字符串，Python 侧反序列化
- 线程安全：所有 FFI 函数接受 `*mut CodeGraphCtx` 上下文指针

### 6.2 接口定义

```rust
// ffi.rs

/// 上下文句柄
pub struct CodeGraphCtx {
    db: Connection,
    workspace: String,
}

/// 创建上下文（打开/创建数据库）
#[no_mangle]
pub extern "C" fn cg_create_ctx(
    db_path: *const c_char,     // ~/.voidx/codegraph.db
    workspace: *const c_char,   // 工作区根路径
) -> *mut CodeGraphCtx;

/// 销毁上下文
#[no_mangle]
pub extern "C" fn cg_destroy_ctx(ctx: *mut CodeGraphCtx);

/// 索引工作区（全量或增量）
/// 返回 JSON: {"files_indexed": 42, "nodes_added": 350, "edges_added": 1200, "duration_ms": 850}
#[no_mangle]
pub extern "C" fn cg_index(
    ctx: *mut CodeGraphCtx,
    force: bool,                // true = 全量重建
) -> *mut c_char;

/// 查询调用者
/// 返回 JSON: [{"id": "...", "kind": "function", "name": "...", ...}]
#[no_mangle]
pub extern "C" fn cg_query_callers(
    ctx: *mut CodeGraphCtx,
    qualified_name: *const c_char,
    depth: u32,
) -> *mut c_char;

/// 查询被调用者
#[no_mangle]
pub extern "C" fn cg_query_callees(
    ctx: *mut CodeGraphCtx,
    qualified_name: *const c_char,
    depth: u32,
) -> *mut c_char;

/// 影响半径
/// 返回 JSON: {"direct": [...], "transitive": [...], "files_affected": [...]}
#[no_mangle]
pub extern "C" fn cg_query_impact(
    ctx: *mut CodeGraphCtx,
    qualified_name: *const c_char,
    max_depth: u32,
) -> *mut c_char;

/// 搜索符号
#[no_mangle]
pub extern "C" fn cg_search(
    ctx: *mut CodeGraphCtx,
    query: *const c_char,
    limit: u32,
) -> *mut c_char;

/// 探索（综合查询：符号 + 调用流 + 影响半径）
#[no_mangle]
pub extern "C" fn cg_explore(
    ctx: *mut CodeGraphCtx,
    query: *const c_char,
) -> *mut c_char;

/// 释放 Rust 分配的字符串
#[no_mangle]
pub extern "C" fn cg_free_string(s: *mut c_char);
```

### 6.3 线程安全

- `CodeGraphCtx` 内部使用 `Mutex<Connection>` 保护 SQLite 写入
- 读操作（query/search）可并发，SQLite WAL 模式支持并发读
- FFI 函数本身是线程安全的，Python 侧无需额外加锁

## 7. Python 侧集成

### 7.1 桥接层

```python
# src/voidx/codegraph/bridge.py

import ctypes
import json
import platform
from pathlib import Path
from typing import Any

_LIB_NAME = {
    "Darwin": "libcodegraph_core.dylib",
    "Linux": "libcodegraph_core.so",
    "Windows": "codegraph_core.dll",
}

class CodeGraphBridge:
    """Python wrapper around the Rust codegraph-core shared library."""

    def __init__(self, workspace: str) -> None:
        self._workspace = workspace
        self._lib = self._load_lib()
        db_path = str(Path.home() / ".voidx" / "codegraph.db")
        self._ctx = self._lib.cg_create_ctx(
            db_path.encode(), workspace.encode()
        )

    def _load_lib(self) -> ctypes.CDLL:
        lib_file = Path(__file__).parent / "bin" / _LIB_NAME[platform.system()]
        lib = ctypes.CDLL(str(lib_file))
        # 声明函数签名
        lib.cg_create_ctx.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        lib.cg_create_ctx.restype = ctypes.c_void_p
        lib.cg_index.argtypes = [ctypes.c_void_p, ctypes.c_bool]
        lib.cg_index.restype = ctypes.c_char_p
        lib.cg_query_callers.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        lib.cg_query_callers.restype = ctypes.c_char_p
        # ... 其他函数签名
        lib.cg_free_string.argtypes = [ctypes.c_char_p]
        lib.cg_free_string.restype = None
        return lib

    def index(self, force: bool = False) -> dict:
        result = self._lib.cg_index(self._ctx, force)
        data = json.loads(result)
        self._lib.cg_free_string(result)
        return data

    def callers(self, qualified_name: str, depth: int = 1) -> list[dict]:
        result = self._lib.cg_query_callers(
            self._ctx, qualified_name.encode(), depth
        )
        data = json.loads(result)
        self._lib.cg_free_string(result)
        return data

    def callees(self, qualified_name: str, depth: int = 1) -> list[dict]:
        result = self._lib.cg_query_callees(
            self._ctx, qualified_name.encode(), depth
        )
        data = json.loads(result)
        self._lib.cg_free_string(result)
        return data

    def impact(self, qualified_name: str, max_depth: int = 3) -> dict:
        result = self._lib.cg_query_impact(
            self._ctx, qualified_name.encode(), max_depth
        )
        data = json.loads(result)
        self._lib.cg_free_string(result)
        return data

    def search(self, query: str, limit: int = 20) -> list[dict]:
        result = self._lib.cg_search(
            self._ctx, query.encode(), limit
        )
        data = json.loads(result)
        self._lib.cg_free_string(result)
        return data

    def explore(self, query: str) -> dict:
        result = self._lib.cg_explore(self._ctx, query.encode())
        data = json.loads(result)
        self._lib.cg_free_string(result)
        return data

    def __del__(self) -> None:
        if hasattr(self, "_ctx") and self._ctx:
            self._lib.cg_destroy_ctx(self._ctx)
```

### 7.2 voidx 工具注册

```python
# src/voidx/tooling/builtin/codegraph.py

class CodegraphIndexTool(BaseTool):
    id = "codegraph_index"
    description = "Index the current workspace into a code knowledge graph. Run once before other codegraph tools."

class CodegraphExploreTool(BaseTool):
    id = "codegraph_explore"
    description = "Explore code structure: returns symbols, call flows, and impact radius for a query."

class CodegraphSearchTool(BaseTool):
    id = "codegraph_search"
    description = "Search symbols by name or keyword using full-text search."

class CodegraphCallersTool(BaseTool):
    id = "codegraph_callers"
    description = "Find all functions/methods that call a specific symbol."

class CodegraphCalleesTool(BaseTool):
    id = "codegraph_callees"
    description = "Find all functions/methods called by a specific symbol."

class CodegraphImpactTool(BaseTool):
    id = "codegraph_impact"
    description = "Trace the full impact radius of changing a symbol (callers + dependents, multi-hop)."
```

### 7.3 与 LSP 的协作

```
用户: "改了 validateToken 会影响什么？"

1. codegraph_impact("validateToken")
   → Rust 图遍历，返回直接 + 传递影响者（静态解析，~80% 覆盖）

2. 如果需要更精确的类型信息:
   lsp_definition("validateToken")
   → LSP 返回精确类型定义

3. 如果需要运行时验证:
   lsp_references("validateToken")
   → LSP 补充 tree-sitter 无法解析的动态调用
```

**协作策略**：
- `codegraph_*` 工具优先使用（快速、全局视图）
- LSP 作为 fallback 补充（精确、但逐符号）
- Rust 图谱中 `provenance='lsp'` 的边由 Python 侧回填

### 7.4 repo_map 升级

`repo_map` 工具可以升级为调用 `codegraph_search` + `codegraph_explore`，替代正则解析：

```python
# 改造前：正则扫描文件
symbols = _extract_python_symbols(f)

# 改造后：查询预索引图谱
symbols = bridge.search(file_path=f, detail="signatures")
```

好处：
- 多语言统一支持（不再只有 Python 专用解析）
- 4000 token 预算限制可优化（图谱查询精准返回，不需要截断）
- 增量更新（文件变更只重新索引该文件）

## 8. 构建与分发

### 8.1 构建流程

```bash
# 开发时
cd crates/codegraph-core
cargo build --release

# 复制到 voidx 包目录
cp target/release/libcodegraph_core.so src/voidx/codegraph/bin/
cp target/release/libcodegraph_core.dylib src/voidx/codegraph/bin/
```

### 8.2 分发策略

| 方式 | 说明 | 适用场景 |
|------|------|---------|
| **预编译 wheel** | CI 中为 macOS/Linux/Windows 编译 .so/.dylib/.dll，打包进 pip wheel | 大多数用户 |
| **源码编译** | `pip install voidx[codegraph]` 触发 cargo build | 有 Rust 工具链的用户 |
| **可选依赖** | `codegraph_*` 工具在 .so 不存在时优雅降级，回退到 repo_map + LSP | 不想装 Rust 的用户 |

### 8.3 降级策略

```python
# bridge.py
def _load_lib(self) -> ctypes.CDLL | None:
    try:
        return ctypes.CDLL(str(lib_file))
    except OSError:
        return None

# 工具层
class CodegraphCallersTool(BaseTool):
    async def execute(self, args, ctx):
        bridge = _get_bridge(ctx)
        if bridge is None:
            return ToolResult(
                output="CodeGraph not available. Falling back to LSP references.",
                metadata={"fallback": True}
            )
        # ... 正常路径
```

## 9. 逐语言实现优先级

### 第一档：开箱即用（tree-sitter-language-pack process() 直接覆盖）

| 语言 | 符号 | 导入 | 调用边 | 继承 | 预计覆盖 |
|------|------|------|--------|------|---------|
| **Go** | ✅ | ✅ | ⭐ 简单 | N/A | 90%+ |
| **Rust** | ✅ | ✅ | ⭐ 简单 | ⭐ 简单 | 85%+ |
| **Python** | ✅ | ✅ | ⭐⭐ 中等 | ✅ | 75%+ |
| **Java** | ✅ | ✅ | ⭐⭐ 中等 | ✅ | 80%+ |

### 第二档：需额外 query 处理

| 语言 | 符号 | 导入 | 调用边 | 特殊处理 | 预计覆盖 |
|------|------|------|--------|---------|---------|
| **TypeScript** | ✅ | ✅ | ⭐⭐⭐ | 路径别名、JSX | 70%+ |
| **JavaScript** | ✅ | ✅ | ⭐⭐⭐ | 动态导入 | 65%+ |
| **Kotlin** | ✅ | ✅ | ⭐⭐ | 扩展函数 | 75%+ |
| **C#** | ✅ | ✅ | ⭐⭐ | LINQ、事件 | 70%+ |
| **PHP** | ✅ | ✅ | ⭐⭐ | 动态调用 | 70%+ |

### 第三档：重度依赖 LSP fallback

| 语言 | 符号 | 导入 | 调用边 | 原因 | 预计覆盖 |
|------|------|------|--------|------|---------|
| **C/C++** | ✅ | ⭐⭐⭐ | ⭐⭐⭐⭐ | 宏、头文件、编译配置 | 50%+ |
| **Ruby** | ✅ | ✅ | ⭐⭐⭐⭐ | 元编程、method_missing | 45%+ |
| **Swift** | ✅ | ✅ | ⭐⭐⭐ | 协议扩展 | 65%+ |

## 10. 性能预估

基于 CodeGraph 的公开基准数据和 Rust 的性能特征：

| 操作 | 预估耗时 | 说明 |
|------|---------|------|
| 首次索引（1000 文件） | 2-5 秒 | tree-sitter 解析 + SQLite 批量写入 |
| 增量索引（10 文件变更） | < 200ms | hash 比对 + 只解析变更文件 |
| callers/callees 查询 | < 5ms | SQLite 索引查询 + 递归 CTE |
| impact 查询（3 跳） | < 20ms | 递归 CTE + 结果集较小 |
| FTS5 搜索 | < 10ms | SQLite FTS5 索引 |
| explore 综合查询 | < 50ms | 多个子查询聚合 |

对比现有方案：
- repo_map 扫描 1000 文件：1-3 秒（每次重扫，无缓存）
- LSP references 单次查询：100-500ms（网络 + 进程间通信）
- codegraph 一次 explore：< 50ms（预索引 + 本地 SQLite）

## 11. 风险与缓解

| 风险 | 缓解 |
|------|------|
| Rust 编译环境要求 | 预编译 wheel 分发；可选依赖，降级到 repo_map + LSP |
| tree-sitter-language-pack intel 模块对部分语言提取不完整 | 先覆盖第一档语言；不完整的用自定义 query 补充 |
| 跨文件引用解析不完美 | 70-80% 匹配率已很有价值；LSP fallback 补充 |
| C FFI 内存管理 | JSON 传输 + `cg_free_string` 统一释放；Python 侧 `__del__` 兜底 |
| 跨平台二进制兼容性 | CI 中为 macOS (arm64/x86_64)、Linux (x86_64/aarch64)、Windows 编译 |
| tree-sitter-language-pack 版本更新导致 API 变动 | 锁定版本；FFI 层隔离内部变化 |

## 12. 开发计划

| 阶段 | 内容 | 预计时间 |
|------|------|---------|
| **P0** | Rust crate 骨架 + parser.rs（process() 调用）+ db.rs（schema）+ FFI 桩 | 2-3 天 |
| **P1** | edges.rs（calls/imports/inherits 提取）+ resolver.rs（跨文件解析） | 3-4 天 |
| **P2** | query.rs（callers/callees/impact/search）+ 增量索引 | 2-3 天 |
| **P3** | Python bridge + voidx 工具注册 + repo_map 升级 | 2-3 天 |
| **P4** | 构建脚本 + CI 多平台编译 + 预编译 wheel | 2-3 天 |
| **P5** | 第二档语言 query + LSP fallback 集成 | 3-5 天 |

**总计**：14-21 天（P0-P4 为核心，约 11-16 天）

## 13. 动态符号更新机制

用户在编辑器中修改代码后，图谱中的符号必须及时反映变化，否则 AI 代理基于过时图谱做出错误决策。

### 13.1 问题分解

"动态修改"涉及三类场景：

| 场景 | 例子 | 时效要求 |
|------|------|----------|
| **voidx 自身修改** | AI 代理通过 write/edit 工具改了文件 | 立即（同一 turn 内） |
| **用户外部修改** | 用户在编辑器中改了代码，再问 voidx | 下次查询前 |
| **批量变更** | git checkout / merge / rebase | 切换后首次查询前 |

### 13.2 现有基础设施

voidx 已有两个关键机制：

1. **`ToolContext.file_mtimes`**：每次 read/write/edit 后记录文件 mtime，编辑前检查 staleness
2. **`SessionChangeTracker`**：跟踪每个 turn 中 voidx 自身修改了哪些文件（`capture_tool_call`），支持 diff 和 rollback

这两个机制可以直接复用来驱动图谱更新。

### 13.3 三层更新策略

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 1: 即时失效（voidx 自身修改）                         │
│  write/edit/lsp_format 后 → 标记文件为 stale → 下次查询前重索引 │
│  延迟：< 1ms（仅标记，不立即解析）                             │
├─────────────────────────────────────────────────────────────┤
│  Layer 2: 查询时校验（所有场景）                              │
│  每次查询前 → 检查涉及文件的 content_hash → 变更则重索引该文件  │
│  延迟：< 200ms（单文件增量索引）                               │
├─────────────────────────────────────────────────────────────┤
│  Layer 3: 后台同步（可选）                                   │
│  file watcher 线程 → 监听文件变更 → 后台增量索引              │
│  延迟：1-3 秒（异步，不阻塞查询）                             │
└─────────────────────────────────────────────────────────────┘
```

### 13.4 Layer 1：即时失效

**原理**：voidx 的 write/edit/lsp_format 工具修改文件后，在 `SessionChangeTracker` 中已有记录。只需在图谱层增加一个 stale 文件集合。

**Rust 侧新增 FFI**：

```rust
/// 标记文件为 stale（不立即重索引，等下次查询时处理）
#[no_mangle]
pub extern "C" fn cg_mark_stale(
    ctx: *mut CodeGraphCtx,
    file_path: *const c_char,
);

/// 批量标记 stale
#[no_mangle]
pub extern "C" fn cg_mark_stale_batch(
    ctx: *mut CodeGraphCtx,
    file_paths_json: *const c_char,  // JSON array of strings
);
```

**Python 侧集成**：

```python
# 在 tool_execution.py 中，工具执行后检查是否修改了文件
# 现有逻辑已追踪 file changes（SessionChangeTracker.capture_tool_call）
# 只需额外调用 cg_mark_stale

class GraphToolExecutionMixin:
    async def _execute_tools(self, state: AgentState):
        # ... 现有工具执行逻辑 ...
        result = await tool.execute(args, ctx)

        # 如果工具修改了文件，标记图谱中该文件为 stale
        if tool_name in {"write", "edit", "lsp_format"}:
            bridge = _get_bridge(ctx)
            if bridge and bridge.is_available():
                file_path = args.get("file_path", "")
                if file_path:
                    bridge.mark_stale(file_path)

        return result
```

**关键**：`cg_mark_stale` 只是把文件路径加入一个 `HashSet<String>`，不触发解析，开销几乎为零。

### 13.5 Layer 2：查询时校验

**原理**：每次 `cg_query_*` / `cg_search` / `cg_explore` 执行前，先检查 stale 集合和 content_hash。

**Rust 侧实现**：

```rust
impl CodeGraphCtx {
    /// 查询前确保图谱是最新的
    fn ensure_fresh(&self) -> Result<()> {
        let stale = self.stale_files.lock().unwrap();
        if stale.is_empty() {
            return Ok(());
        }

        // 收集所有 stale 文件
        let files: Vec<String> = stale.drain().collect();
        drop(stale);

        // 增量索引这些文件
        for file_path in &files {
            // 1. 读取文件当前内容
            // 2. 计算 SHA256 hash
            // 3. 与 cg_file_index 中的 hash 比对
            // 4. 如果不同，重新解析并更新图谱
            self.reindex_file(file_path)?;
        }

        // 重新解析跨文件引用（stale 文件可能影响其他文件的边）
        self.resolve_references()?;

        Ok(())
    }

    /// 所有查询函数入口都调用 ensure_fresh
    pub fn query_callers(&self, name: &str, depth: u32) -> Result<Vec<CodeNode>> {
        self.ensure_fresh()?;
        // ... 实际查询逻辑
    }
}
```

**性能**：
- 无 stale 文件时：`ensure_fresh()` 只检查 `HashSet::is_empty()`，< 1μs
- 1 个 stale 文件：增量索引 < 200ms（单文件解析 + SQLite 写入）
- 10 个 stale 文件：并行解析 < 500ms

### 13.6 Layer 3：后台同步（可选）

**原理**：用 `notify` crate（Rust 跨平台文件监听库）在后台线程监听文件变更，自动增量索引。

```rust
use notify::{Watcher, RecursiveMode, Event};

impl CodeGraphCtx {
    pub fn start_watcher(&self) -> Result<()> {
        let stale = self.stale_files.clone();
        let workspace = self.workspace.clone();

        let mut watcher = notify::recommended_watcher(move |res: Result<Event, _>| {
            if let Ok(event) = res {
                if matches!(event.kind, EventKind::Modify(_)) {
                    for path in event.paths {
                        let path_str = path.to_string_lossy().to_string();
                        // 只标记为 stale，不立即索引
                        // 查询时由 Layer 2 处理
                        stale.lock().unwrap().insert(path_str);
                    }
                }
            }
        })?;

        watcher.watch(Path::new(&workspace), RecursiveMode::Recursive)?;
        self.watcher = Some(watcher);
        Ok(())
    }
}
```

**注意**：Layer 3 是可选的，默认不启用。原因：
1. voidx 主要在终端中运行，用户外部修改频率不高
2. Layer 1 + Layer 2 已经覆盖了最关键的场景
3. file watcher 在某些环境（WSL、远程 SSH）中可能不可靠

### 13.7 符号重命名传播

当用户重命名一个符号时，需要更新所有引用该符号的边：

```rust
/// 重命名符号：更新节点 + 所有相关边
pub fn rename_symbol(
    db: &Connection,
    old_qualified_name: &str,
    new_qualified_name: &str,
) -> Result<RenameResult> {
    // 1. 更新 cg_nodes 中的 qualified_name
    //    UPDATE cg_nodes SET qualified_name = ? WHERE qualified_name = ?
    // 2. 更新 cg_nodes 中的 id（id = hash(qualified_name + file_path)）
    // 3. 更新 cg_edges 中所有 source_id / target_id
    // 4. 更新 cg_nodes_fts
    // 5. 返回受影响的节点和边数量
}
```

**FFI 接口**：

```rust
#[no_mangle]
pub extern "C" fn cg_rename_symbol(
    ctx: *mut CodeGraphCtx,
    old_name: *const c_char,
    new_name: *const c_char,
) -> *mut c_char;
```

### 13.8 与 voidx 现有 staleness 机制的协作

voidx 已有 `file_mtimes` staleness 检查（`_check_staleness` / `_record_mtime`），用于防止 AI 代理编辑过时文件。CodeGraph 的动态更新机制与之互补：

```
voidx 现有机制（file_mtimes）：
  防止 AI 代理基于过时文件内容做编辑
  → 保护写入安全

CodeGraph 动态更新（stale_files + ensure_fresh）：
  确保图谱查询反映最新代码状态
  → 保护读取准确
```

两者共享同一个信息源——文件是否被修改过。可以统一：

```python
# 统一的文件变更感知层
class FileChangeAwareness:
    """桥接 voidx file_mtimes 和 codegraph stale_files."""

    def on_file_modified(self, file_path: str, ctx: ToolContext):
        # 1. 更新 file_mtimes（现有逻辑）
        _record_mtime(ctx, Path(file_path))
        # 2. 标记图谱 stale
        bridge = _get_bridge(ctx)
        if bridge and bridge.is_available():
            bridge.mark_stale(file_path)
```

### 13.9 更新策略总结

| 场景 | 触发机制 | 更新时机 | 延迟 |
|------|---------|---------|------|
| voidx write/edit | Layer 1 标记 stale | 下次查询前 | < 200ms |
| 用户外部编辑 | Layer 2 查询时校验 | 查询执行前 | < 200ms |
| git checkout/merge | Layer 2 全量 hash 比对 | 首次查询前 | 1-3s |
| 后台监听（可选） | Layer 3 file watcher | 异步 | 1-3s |
| 符号重命名 | `cg_rename_symbol` | 立即 | < 50ms |

**核心原则**：
1. **写时标记，读时更新**——修改文件只做标记（O(1)），查询时才触发增量索引
2. **惰性求值**——如果用户改了文件但没查图谱，就不浪费解析资源
3. **与现有机制统一**——复用 `file_mtimes` 和 `SessionChangeTracker`，不引入新的变更感知通道
