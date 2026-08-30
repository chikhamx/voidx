# Desktop Markdown Worker — P0.3 技术规格

> **Status: Done** — Archived on 2026-08-30.

> **Date: 2026-08-30**  
> **Audience: Human + LLM**

## TL;DR

将 Desktop assistant stream 的 canonical Markdown commit 从浏览器主线程移到专用 module Worker。Worker 对完整 canonical 文本执行一次 lex/parse 和代码高亮，按顶层 Markdown block 返回不可信渲染描述；主线程继续独占 DOMPurify 和 DOM mutation，在隐藏 staging 容器中按每帧最多 8 ms 的预算安装，全部完成后原子替换 provisional preview。Worker、净化或安装失败时必须显示完整 escaped plain-text canonical 内容；过期 revision、已清理 stream 和旧 thread generation 的结果必须丢弃。

本规格只闭环 `docs/design/cross-ui-performance-addendum.md` 的 Desktop P0.3，不包含 P0.4 rAF/滚动布局、keyed reconciliation、DOM window 或 Gateway 协议变更。

## 1. 实现前行为

### 1.1 Live projection

P0.3 开始前，`frontend/src/utils/markdown.ts` 的 `StreamingMarkdownProjectionImpl` 已维护完整 canonical raw text，并把实时预览拆为 stable、provisional 和 bounded mutable 三个区域。append 热路径的 parser 输入受 16 KiB hard limit 约束，provisional 内容使用纯文本节点。

### 1.2 Canonical commit

P0.3 开始前，`frontend/src/utils/stream.ts:commitStream()` 同步调用 `renderStreamText()`，后者最终调用 projection `commit()`；`frontend/src/utils/markdown.ts` 的 commit 再通过 `renderMarkdown()` 在主线程完成：

1. `marked.parse()` 全量解析；
2. DOMPurify 全量净化；
3. highlight.js 遍历并高亮所有 code block；
4. `replaceChildren()` 安装最终 DOM。

因此 50k+ assistant response 在完成时可能产生浏览器主线程 long task；`item.completed`、snapshot replay 和直接调用 `commitStream()` 都经过该同步路径。

### 1.3 必须保留的行为

- canonical 原文只来自 `StreamState.text` / projection raw text，不能从 provisional DOM 反向构造；
- live append/replace、legacy cumulative API、thinking/text phase 语义保持不变；
- `commitStream()` 继续立即返回 `{ text, thinking, el } | null`，调用者不等待 canonical render；
- `item.completed` 和 turn terminal 状态可以先推进；
- DOMPurify、代码高亮和同步 `renderMarkdown()` 仍保留；非 stream 调用者不强制改为 Worker；
- 正常异步结果与当前一次性 `renderMarkdown(canonicalText)` 的 sanitized/highlighted DOM 语义等价。

## 2. 目标与非目标

### 2.1 Goals

- 完整 Markdown lex/parse 和代码高亮在 `frontend/src/utils/markdown.worker.ts` 中执行；
- Worker 返回按顶层 Markdown block 分段的描述，不返回可信 DOM；
- 主线程对所有 HTML 片段执行 DOMPurify；
- staging DOM 的净化与 mutation 每帧最多消耗 8 ms，允许 canonical commit 跨多帧完成；
- canonical install 完成前保留原 provisional preview，完成后一次原子切换；
- Worker result 使用 `itemId + revision + jobId + generation` 做 stale guard；
- Worker、消息协议、净化或安装失败时完整保留正文并使用 escaped plain-text fallback；
- 单个超大 raw HTML block 在进入主线程 DOMPurify 前降级为 escaped plain text，并记录 `html_block_budget`；
- clear、discard、replace、同 ID 新 stream、thread switch 和 test reset 都使旧任务失效；
- 测试覆盖非阻塞、stale result、DOMPurify 边界、代码高亮、失败 fallback、预算化安装和最终等价。

### 2.2 Non-Goals

- 不实现 P0.4 的“同一 frame 最多一次 live mutation”和滚动 read/write 隔离；
- 不改变现有 100 ms live projection debounce；
- 不实现 keyed turn/item reconciliation 或 transcript DOM window；
- 不修改 Gateway capability、stream wire protocol、snapshot 或 persistence；
- 不修改 TUI canonical commit；
- 不修改 `desktop/` Tauri shell；
- 不处理范围外的工具输出阈值改动；
- 不用更长 debounce 掩盖 canonical commit 成本。

## 3. 架构

```text
commitStream(itemId)
  ├─ freeze canonical work item
  ├─ mark stream element data-render-pending=true
  ├─ remove stream from active map / retain element as today
  ├─ post CanonicalRenderRequest to module Worker
  └─ return result immediately

markdown.worker.ts
  ├─ marked.lexer(canonicalText) once
  ├─ preserve TokensList.links for cross-block references
  ├─ parse top-level blocks with shared renderer
  ├─ highlight fenced/indented code in Worker
  └─ post CanonicalRenderSuccess | CanonicalRenderFailure

main thread coordinator
  ├─ reject stale itemId/revision/jobId/generation
  ├─ keep provisional DOM visible
  ├─ sanitize/append blocks into detached staging container
  │    └─ at most 8 ms per animation frame
  ├─ validate completed result
  └─ atomically replace preview, clear render-pending

failure path
  └─ install one text node containing the complete canonical source
```

### 3.1 Ownership boundaries

| Responsibility | Owner |
|---|---|
| canonical stream text | `frontend/src/utils/stream.ts` |
| lex/parse and code highlighting | `frontend/src/utils/markdown.worker.ts` + Worker-safe shared renderer |
| Worker lifecycle and pending-job registry | `frontend/src/utils/markdown-worker-client.ts` |
| DOMPurify and HTML-to-DOM conversion | async path: `frontend/src/utils/markdown-worker-client.ts`; sync `renderMarkdown()`: `frontend/src/utils/markdown.ts` |
| per-frame staging/install scheduling | `frontend/src/utils/markdown-worker-client.ts` |
| stream revision/generation invalidation | `frontend/src/utils/stream.ts` |
| protocol delta revision | existing `frontend/src/main.ts`; unchanged |

Worker 与主线程必须共享纯渲染配置，避免复制 `marked` options、语言注册或 code renderer。共享模块不得引用 `document`、`window`、DOMPurify 或其他仅主线程 API。

## 4. Worker 协议

新增 `frontend/src/utils/markdown-worker-protocol.ts`，协议必须是 structured-clone-safe 的 discriminated union。

```ts
export interface CanonicalRenderRequest {
  type: "render";
  jobId: number;
  itemId: string;
  revision: number;
  generation: number;
  canonicalText: string;
}

export type CanonicalBlockDescriptor =
  | {
      kind: "html";
      html: string;
      sourceLength: number;
    }
  | {
      kind: "text";
      text: string;
      reason: "html_block_budget";
    };

export interface CanonicalRenderSuccess {
  type: "rendered";
  jobId: number;
  itemId: string;
  revision: number;
  generation: number;
  blocks: CanonicalBlockDescriptor[];
}

export interface CanonicalRenderFailure {
  type: "failed";
  jobId: number;
  itemId: string;
  revision: number;
  generation: number;
  reason: "worker_parse" | "worker_protocol";
}
```

约束：

- `canonicalText` 只在 request 中传入一次；response 不回传全文；主线程 pending work item 保留不可变 canonical 文本用于验证和 fallback；
- Worker 不接受 append/replace；每个 request 都是一次完整 canonical commit；
- `jobId` 在页面生命周期内单调递增；`revision` 是 stream 本地 canonical revision，不复用 Gateway revision；
- response 缺字段、字段类型不符或 identity 不匹配时按 `worker_protocol` 失败处理；
- 每个顶层 token 都必须返回一个 descriptor，包括 parser 输出为空的 token；主线程以 HTML block 的 `sourceLength` 和 text block 的 `text.length` 累计覆盖长度，结果必须严格等于 `canonicalText.length`，缺块、重复块或超长覆盖均按 `worker_protocol` 整项 fallback；
- Worker 不能返回 DOM、TrustedHTML 或声称已净化的标记。

## 5. Canonical block render

### 5.1 一次 lex，保留全局 links

Worker 必须对完整文本只调用一次 `marked.lexer()`。Marked 18 的 `TokensList` 附带全局 `links`；reference-style link/definition 解析不能通过逐段重新 lex 实现。

分块步骤：

1. `const tokens = marked.lexer(canonicalText, options)`；
2. 保留 `tokens.links`；
3. 对每个顶层 token 构造仍携带同一 `links` 的单块 token list；
4. 使用同一个 renderer/options 调用 Marked parser；
5. `space` / `def` 等空输出 token 可与相邻 block 合并，但不能丢失对后续 reference link 的影响；
6. 输出 block 顺序必须与一次性 parser 完全一致。

不得按空行切字符串、逐块重新 lex，或用正则模拟 Markdown grammar。

### 5.2 代码高亮

将语言注册和 code renderer 抽到 Worker-safe 共享模块：

- 支持语言集合保持 python/py、javascript/js、typescript/ts、bash/sh/shell、json、rust/rs、diff；
- 指定且已注册的语言使用 `hljs.highlight()`；
- 未指定或未知语言使用 `hljs.highlightAuto()`；
- highlight 失败时输出 escaped code；
- 输出 class 和 HTML 结构必须与当前 `renderMarkdown()` 的最终 code block 语义一致。

同步 `renderMarkdown()` 与 Worker 路径必须复用同一 renderer，不能保留“先 parse、再遍历 DOM 高亮”的第二份独立规则。

### 5.3 Raw HTML budget

主线程不得接收一个需要无界同步 DOMPurify 的超大 raw HTML block。首版常量：

```ts
export const CANONICAL_RAW_HTML_BLOCK_MAX_CHARS = 16 * 1024;
```

Worker 识别顶层 `html` token；其 `raw`/`text` 超过该阈值时返回：

```ts
{ kind: "text", text: token.raw, reason: "html_block_budget" }
```

该 block 在主线程用 `textContent` 安装。阈值内 HTML 仍返回 `kind: "html"`，并必须经过 DOMPurify。此 fallback 只改变超大 raw HTML block 的样式，不得丢字符或执行 HTML。

## 6. 主线程安全与预算化安装

### 6.1 Worker client

`frontend/src/utils/markdown-worker-client.ts` 提供可注入 Worker factory 的 coordinator，生产默认值：

```ts
() => new Worker(new URL("./markdown.worker.ts", import.meta.url), {
  type: "module",
})
```

公开 coordinator 接口保持最小：

```ts
interface CanonicalMarkdownCoordinator {
  start(work: CanonicalCommitWork): number;
  invalidate(itemId: string): void;
  invalidateAll(): void;
}
```

测试可通过 factory 注入 fake Worker，不依赖 jsdom 原生 Worker。

### 6.2 Pending work item

主线程 pending registry 以 `jobId` 为键，并至少保存：

```ts
interface CanonicalCommitWork {
  itemId: string;
  revision: number;
  generation: number;
  canonicalText: string;
  target: HTMLElement;
  isCurrent(): boolean;
  onSettled?(outcome: CanonicalCommitOutcome): void;
}
```

安装前和每个续帧都必须重新执行 `isCurrent()`。仅 DOM `isConnected` 不足以证明结果有效；同 ID 新 stream 或同一 element 被重新使用也必须由 revision/generation 拒绝旧结果。

### 6.3 DOMPurify boundary

- `kind: "html"` 的每个 block 在主线程独立调用 DOMPurify；
- sanitized string 只写入 detached template/container，不直接写入当前可见 target；
- `kind: "text"` 只能通过 `textContent` / `Text` node 安装；
- script、event handler、危险 URL 和其他 DOMPurify 默认拒绝内容不得进入 staging DOM；
- DOMPurify 抛错时整项进入 escaped canonical fallback，不能安装部分 canonical DOM。

### 6.4 8 ms budget

`CANONICAL_INSTALL_BUDGET_MS = 8`。每个 animation frame：

1. 读取 frame 起始 `performance.now()`；
2. 至少处理一个尚未处理的 block，避免单个小 block 因计时边界饿死；
3. 每处理完一个 block检查 elapsed；达到预算即 yield 到下一帧；
4. 所有 mutation 只发生在 detached staging container；
5. 所有 blocks 完成后才对 visible target 执行一次 `replaceChildren(...)`。

如果 `requestAnimationFrame` 不可用，使用 `setTimeout(..., 16)`；测试必须注入 scheduler/clock，不能依赖真实墙钟。

单次 DOMPurify 调用本身无法被抢占，因此 raw HTML block 先受 16 KiB budget 限制。普通 parser HTML block 仍按 Marked 顶层 token 分块，以限制单次工作量。

### 6.5 Atomic visibility

- pending 期间保持现有 preview 完整可见；
- staging container 不挂到可见 transcript；
- 不允许逐 block 同时显示 preview 和 canonical DOM；
- 成功完成后一次 `replaceChildren()`，再清除 `data-render-pending`；
- fallback 同样一次替换为完整 canonical text node；
- 空 canonical 文本沿用当前隐藏空 stream 行为。

## 7. Stream 生命周期与 stale guard

### 7.1 Local revision

在 `StreamState` 增加单调 `canonicalRevision`。以下操作必须递增或失效旧 revision：

- 每次 append/replace canonical text 更新；
- explicit phase switch/reset；
- commit；
- discard/clear；
- 同一 `streamId` 创建新 state。

`commitStream()` 捕获最终 `{itemId, canonicalRevision, generation, canonicalText, textEl}` 后立即启动 Worker，不等待结果。

### 7.2 Generation

维护 stream module generation：

- `clearActiveStreams()`、`clearCommittedStreams()`、thread activation 清理和 `_resetForTest()` 增加 generation；
- 所有 pending work item 捕获创建时 generation；
- generation 不匹配时结果和续帧安装均静默丢弃并清理 staging。

### 7.3 Committed element retention

现有 `committedEls` 行为保持：

- commit 后 element 可立即参与 snapshot 去重和 DOM 排序；
- pending canonical render 不阻塞 `item.completed`；
- `takeCommittedStreams()` 只转移 element 所有权，不应自动取消仍有效的 canonical job；
- element 被 `clearCommittedStreams()` 移除、thread switch 清空或 snapshot 明确淘汰后，对应 job 必须失效；
- P0.3 不新增 live-history eviction，但使用 `data-render-pending="true"` 暴露后续 P1/P2 所需状态。

### 7.4 Failure outcomes

允许记录的稳定 reason：

```ts
type CanonicalFallbackReason =
  | "worker_unavailable"
  | "worker_error"
  | "worker_parse"
  | "worker_protocol"
  | "sanitize_error"
  | "install_error"
  | "html_block_budget";
```

首版观测可通过 element dataset 和测试 hook 暴露：

```ts
target.dataset.canonicalFallback = reason;
```

成功时删除旧 fallback dataset。除 `html_block_budget` 可只降级单个 block 外，其余错误一律用完整 `canonicalText` 做整项 escaped plain fallback。

## 8. 等价定义

设计文档中“canonical raw text 逐字符等价”指数据源不能被截断或由 preview 反推；对于 Markdown DOM，`textContent` 不可能与含 `**`、链接目标等语法字符的源码逐字符相同。因此本规格采用以下可执行判据：

1. pending work item 的 `canonicalText` 必须逐字符等于 commit 时 `StreamState.text`；
2. 正常 Worker 路径最终 DOM 经标准化后与 `renderMarkdown(canonicalText)` 等价，包括 reference links、列表、代码结构、高亮 class 和 sanitized HTML；
3. 最终用户可见语义内容不得缺失或重排；
4. fallback 路径的 `target.textContent` 必须逐字符等于完整 `canonicalText`；
5. provisional DOM 永远不能成为 canonical 协议、snapshot 或 persistence 数据源。

DOM 等价测试应比较结构化 `innerHTML`/DOM tree，并单独断言危险内容已净化；不得只比较最终字符串长度。

## 9. 文件变更

| 文件 | 操作 | 责任 |
|---|---|---|
| `frontend/src/utils/markdown-renderer.ts` | 新建 | Worker-safe Marked options、语言注册、code renderer 和分块 canonical render |
| `frontend/src/utils/markdown-worker-protocol.ts` | 新建 | structured-clone-safe request/response 和 fallback 类型 |
| `frontend/src/utils/markdown.worker.ts` | 新建 | Worker message handler、canonical lex/parse/highlight |
| `frontend/src/utils/markdown-worker-client.ts` | 新建 | Worker lifecycle、pending registry、stale guard、DOMPurify staging 和预算调度 |
| `frontend/src/utils/markdown.ts` | 修改 | 复用 shared renderer，保留同步 `renderMarkdown()` 的 DOMPurify 与 live bounded projection |
| `frontend/src/utils/stream.ts` | 修改 | local revision/generation、异步 commit 启动、canonical 原文保存和 clear/discard invalidation |
| `frontend/src/utils/render.ts` | 修改 | snapshot 去重读取保存的 canonical 原文，并在 DOM 淘汰时使 pending commit 失效 |
| `frontend/src/utils/types.ts` | 修改 | `StreamState` 的 canonical revision/render-pending 状态 |
| `frontend/test/utils/markdown-worker.test.ts` | 新建 | Worker protocol、render、sanitize、budget、stale/fallback 测试 |
| `frontend/test/utils/stream.test.ts` | 修改 | commit 非阻塞、preview 保留、生命周期和最终等价回归 |
| `frontend/test/main/incremental-protocol.test.ts` | 仅必要时修改 | `item.completed` 先推进且异步 canonical 最终安装 |
| `docs/design/cross-ui-performance-addendum.md` | 实现完成后修改 | 标记 Desktop P0.3 完成并记录 fresh verification |

未修改 `frontend/src/main.ts` 的 Gateway revision 语义；测试通过 `_setCanonicalMarkdownCoordinatorForTest()` 注入可控 fake coordinator，业务通知 handler 不等待 Worker。

## 10. TDD 实施任务

### Task 1 — 共享 renderer 与 Worker block output

- RED：新建 `frontend/test/utils/markdown-worker.test.ts`，覆盖普通 Markdown、跨 block reference link、代码语言/auto highlight、raw HTML block descriptor 和 Worker parse failure。
- GREEN：新增 `markdown-renderer.ts`、`markdown-worker-protocol.ts`、`markdown.worker.ts`；让同步 `renderMarkdown()` 复用共享 renderer。
- 命令：
  - `./test.py --frontend -- test/utils/markdown-worker.test.ts test/utils/markdown.test.ts`
- 期望：新测试通过；现有 Markdown projection/renderer 测试不回退。

### Task 2 — 主线程净化与 8 ms staging install

- RED：使用 fake scheduler/clock 断言多 blocks 跨帧安装、visible preview 在最后一帧前 identity 不变、危险 HTML 被净化、超大 raw HTML 使用 `textContent` 和 `html_block_budget`。
- GREEN：实现 `markdown-worker-client.ts` 的 coordinator、DOMPurify boundary、detached staging 和 atomic install。
- 命令：
  - `./test.py --frontend -- test/utils/markdown-worker.test.ts`
- 期望：每帧处理在注入 clock 下不超过预算规则；可见 target 只在完成时替换一次。

### Task 3 — Stream async commit 与 stale lifecycle

- RED：扩展 `frontend/test/utils/stream.test.ts`，覆盖：
  - `commitStream()` 在 Worker 回复前立即返回；
  - provisional preview 保留且 `render_pending` 为真；
  - 成功结果最终原子切换；
  - append/replace revision、discard、clear、同 ID 新 stream 和 generation 使旧结果失效；
  - Worker error/sanitize error 安装完整 escaped canonical 文本；
  - thinking result 和 committed element retention 保持原行为。
- GREEN：修改 `stream.ts` / `types.ts`，接入 coordinator；提供 `_setCanonicalMarkdownCoordinatorForTest()` 和 reset hook。
- 命令：
  - `./test.py --frontend -- test/utils/markdown-worker.test.ts test/utils/stream.test.ts`
- 期望：异步 lifecycle 全绿，旧 stream API 行为不回退。

### Task 4 — Notification 与完整 Frontend 回归

- RED：若现有 main test 无法表达异步完成，再扩展 `frontend/test/main/incremental-protocol.test.ts`；不得把 main handler 改为等待 Worker。
- GREEN：只做使 `item.completed` 与 pending canonical 状态兼容的最小接线。
- 命令：
  - `./test.py --frontend -- test/utils/markdown-worker.test.ts test/utils/markdown.test.ts test/utils/stream.test.ts test/main/incremental-protocol.test.ts`
  - `./test.py --frontend`
- 期望：聚焦集合与完整 Frontend 均通过；只在最终验证阶段各运行一次完整 Frontend。

### Task 5 — 文档与静态验证

- 更新 `docs/design/cross-ui-performance-addendum.md`：
  - Desktop P0.3 子项标记完成；
  - P0.3 跨端整体在 TUI 和 Desktop 均完成后标记完成；
  - 记录本轮实际测试计数，不复制旧计数；
  - 整体仍保持 `status: in-progress` / `implementation_status: partial`，因为 P0.4/P0.5/P1/P2 等仍未完成。
- 命令：
  - `git diff --check`
  - `cd frontend && npx tsc --noEmit`，仅按本批文件诊断与已知全项目基线分类；项目测试仍优先使用根目录 `./test.py`。

## 11. 验收标准

以下验收项已完成：

- [x] production bundle 使用 module Worker，而非测试专用同步替身；
- [x] 50k+ canonical commit 的 lex/parse/highlight 不在主线程执行；
- [x] 主线程只分块 DOMPurify，并按 8 ms scheduler budget 构建 detached staging DOM；
- [x] visible preview 在 canonical 全部就绪前保持不变，最终只原子替换一次；
- [x] Worker HTML 全部经 DOMPurify，raw HTML 超预算安全降级；
- [x] stale revision/job/generation、clear/discard/thread switch 不安装旧结果；
- [x] Worker/净化/安装失败显示完整 escaped canonical 文本；
- [x] 正常路径与一次性 `renderMarkdown()` 对 reference links、列表、代码高亮和 sanitized HTML 等价；
- [x] `commitStream()`、thinking output、snapshot replay 和 committed element retention 兼容；
- [x] 聚焦 Frontend 测试通过；
- [x] 完整 Frontend 在最终验证阶段 fresh 运行并通过；
- [x] `git diff --check` 通过；本批 TypeScript 文件无新增诊断；
- [x] P0.4、P1、TUI 和范围外用户改动未被混入或回退。

Fresh verification（2026-08-30）：

- `./test.py --frontend -- test/utils/markdown-worker.test.ts test/utils/markdown.test.ts test/utils/stream.test.ts test/utils/render.test.ts test/main/incremental-protocol.test.ts test/main/runtime-profile.test.ts`：201 passed；
- `./test.py --frontend`：44 files、719 passed；
- `cd frontend && npm run build`：通过，产出独立 `dist/assets/markdown.worker-*.js`；
- `git diff --check`：通过；
- `cd frontend && npx tsc --noEmit`：仅报告既有 `src/services/connection.ts`、`test/ui/design-system.test.ts`、`test/ui/theme.test.ts` 基线错误，本批文件无新增诊断。

## 12. 风险与回滚

### 风险

- Marked 顶层 token 分块若丢失 `TokensList.links`，reference link 会与一次性 render 不等价；
- 把 highlight 留在主线程会使 P0.3 名义异步但仍产生 long task；
- DOMPurify 单次调用不可抢占，因此必须限制 raw HTML block 大小；
- 仅按 item ID 判断 stale 会误装同 ID 新 stream 的旧结果；
- snapshot 去重可能在 Worker 完成前读取 provisional `textContent`，需继续以 canonical stream state/节点 payload 为事实来源，不能新增 DOM 数据依赖；
- jsdom 没有可靠原生 Worker/rAF/performance 调度，测试必须使用注入式 fake。

### 回滚

Worker 创建失败或运行时异常不回退到主线程同步 full parse；应立即使用完整 escaped canonical fallback，保证正文和安全。若需整体关闭 Worker 路径，应通过内部 factory/feature switch 选择 escaped fallback，而不是恢复主线程 `O(A)` canonical render。

## 13. Fresh-reader / execution-readiness check

- 目标与非目标明确，P0.4/P1 不在本批；
- 实现前同步调用链、实现后异步架构和所有关键生命周期均已列出；
- 新增/修改文件路径、接口、常量和类型明确；
- Worker/主线程安全边界与 forbidden behavior 明确；
- stale、failure、raw HTML 和无 rAF 环境均有处理规则；
- 每个实施任务都有 RED/GREEN 范围、文件和可执行测试命令；
- 验收区分正常 DOM 等价与 plain fallback 的源码逐字符保留；
- 规格以当前 Marked 18、现有 `commitStream()` 和项目测试包装器为依据。
