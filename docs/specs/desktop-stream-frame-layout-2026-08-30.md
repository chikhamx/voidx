# Desktop Stream Frame/Layout — P0.4 技术规格

> **Status: Revision required** — Independent re-review pending.
> **Date: 2026-08-30**
> **Audience: Human + LLM**

## TL;DR

本规格闭环 `docs/design/cross-ui-performance-addendum.md` 的 Desktop P0-C（本轮编号 P0.4）：保留 P0.2 已实现的 bounded `StreamingMarkdownProjection` 和 P0.3 已实现的异步 canonical Markdown Worker，把 live stream 的 attached-DOM mutation 收敛到“每个 stream 每帧最多一次”，恢复非重置的 100 ms trailing throttle，并把 transcript 自动滚动纳入同一个 read-before-write 帧事务。

用户位于底部附近时，stream preview 和 canonical Worker settle 继续跟随底部；用户向上查看历史后，后续内容不得抢回滚动位置，并显示可点击的“回到底部”按钮。`commitStream()` 仍同步 drain 最新 provisional projection 并立即返回，不等待 timer、rAF 或 Worker。

本规格不实现 keyed reconciliation、transcript DOM window、Gateway 协议、分页算法、TUI、P0.3 Worker 协议或 Markdown grammar 变更。

## 1. 当前行为与已完成基线

### 1.1 P0.2 / P0.3 已完成，不得重复实现

当前工作树已经具备：

- `frontend/src/utils/markdown.ts` 的 bounded live projection：stable DOM 只追加，provisional prefix 使用 escaped text，mutable tail parser 输入不超过 16 KiB；
- append/replace 与 legacy cumulative update 兼容；canonical raw text 独立于 provisional DOM 保存；
- `frontend/src/utils/markdown.worker.ts` 与 `markdown-worker-client.ts` 的异步 canonical commit：Worker lex/parse/highlight，主线程 DOMPurify、8 ms staging budget、原子安装、stale guard 和 escaped fallback；
- `commitStream()`、`item.completed` 和 turn terminal 状态不等待 Worker。

P0.4 只能调度这些现有能力，不能重新实现 Markdown parser、sanitizer、Worker protocol 或 canonical lifecycle。

### 1.2 Live stream 当前调用链

当前 `frontend/src/utils/stream.ts` 的主要路径是：

```text
appendStreamText(delta)
  ├─ 立即更新 StreamState.text / thinking / revision
  ├─ phase change 时可能立即 reset projection DOM
  ├─ text phase 时立即 hideThinking() DOM write
  ├─ queueProjectionUpdate()
  ├─ scheduleRender()
  │    └─ 全局 requestAnimationFrame，逐 stream render
  └─ transcript.scrollTop = transcript.scrollHeight
       ├─ layout read
       └─ scroll write
```

已有全局 `pendingRenders` 能把同一 stream 的 render 合并进一个 rAF，但仍有四个缺口：

1. `DEBOUNCE_MS = 100` 和 `StreamState.debounceTimer` 已存在但没有形成有效 throttle；
2. phase reset、thinking hide 和新 stream attachment 仍可在 delta handler 中同步写 attached DOM；
3. 一个 frame 中积累的 `replace + append` 会按数组逐条调用 projection，形成同一 stream 多次 render transaction；
4. 每个 delta 都同步读取 `scrollHeight` 并写 `scrollTop`，可能强制 layout，而且无条件把查看历史的用户拉回底部。

### 1.3 其他滚动路径

除 `stream.ts` 外，当前还有两类 transcript 滚动路径：

1. `frontend/src/main.ts` 的 `scrollToBottom()` 直接写 `scrollTop = scrollHeight`，用于 full snapshot、用户主动提交、prompt answer 和显式错误等 force 场景；
2. 后台 renderer 在 attached transcript mutation 后无条件跳底：
   - `frontend/src/utils/render.ts` 的 stats message 与普通 `appendMessageItem()`；
   - `frontend/src/utils/render-thought-items.ts` 的 thought merge/new item；
   - `frontend/src/utils/render-tool-items.ts` 的 tool start/delta/completion；
   - `frontend/src/utils/render-file-changes.ts` 的 summary 与 diff card；
   - `frontend/src/utils/render-notice-status.ts` 的 `appendDiffItem()`；
   - `frontend/src/ui/prompt.ts` 的 conversation prompt insertion。

第二类路径同样会把查看历史的用户拉回底部，必须改用统一的 follow-preserving 请求，不能只修 `stream.ts`。`appendNoticeItem()` 写入 `document.body` 的 toast region，`frontend/src/ui/terminal.ts` 写入 terminal 自身 scroll container，二者不属于 transcript viewport。

`loadEarlierTranscriptPage()` 还在 scroll listener 中同步读取 top threshold，并以 `previousTop + (newHeight - previousHeight)` 恢复 prepend anchor。该算法不是 stream 热路径，本批不改分页协议、触发阈值或 offset 公式，但它必须成为第 5.6 节定义的唯一受控同步 transcript geometry 例外，而不能与“scroll event 只标 dirty”的一般规则并列而不说明。

### 1.4 CSS / DOM 当前边界

- `frontend/index.html` 中 `#transcript` 与 composer 直接同级；没有“回到底部”控件；
- `frontend/css/chat.css` 为 `.transcript` 设置 `overflow-y: auto` 和 `scroll-behavior: smooth`；
- smooth scrolling 会使连续自动跟随跨帧动画，干扰 near-bottom 判断与“一次写入即到目标位置”的不变量。

## 2. 目标与非目标

### 2.1 Goals

- 每个 stream 的首个 pending update 启动一个不重置的 100 ms trailing throttle；连续更新不会无限推迟 render；
- timer callback 只把 stream 标记为 frame-ready，不执行 attached-DOM mutation 或 layout read；
- 所有 ready stream 共用一个 transcript frame；同一 stream 在该 frame 中最多执行一个 render transaction；
- 一帧内的 append/replace/phase update 折叠成至多一个 projection operation；
- stream frame 在任何 attached-DOM write 前读取一次 viewport geometry，全部 mutation 后最多写一次 scroll position；
- 所有当前直接跳底的后台 transcript renderer 改为统一 follow-preserving 请求；用户主动提交等明确动作仍由调用方 force；
- near-bottom 阈值为 48 px；用户离底后停止自动跟随并显示“回到底部”按钮；
- 按钮、full snapshot、用户主动提交和显式错误路径可强制恢复跟随；
- canonical Worker 只有在用户仍处于 follow 状态时才请求一次帧尾滚动；stale settle 不滚动；
- `commitStream()` 作为 barrier，同步 drain 本 stream 最新 pending projection，取消其 timer/frame work，立即返回并异步启动 P0.3 Worker；
- discard、clear、thread switch、controller replacement 和 test reset 不得留下迟到 timer/rAF mutation；
- earlier-page 保留现有同步 prepend anchor，但按第 5.6 节隔离为唯一受控 transcript geometry 例外；
- jsdom 测试通过注入式 frame scheduler、geometry adapter 和 fake timers 确定性验证，不等待真实 rAF/layout。

### 2.2 Non-Goals

- 不修改 `markdown-renderer.ts`、`markdown-worker-protocol.ts` 或 Worker 消息字段；
- 不改变 DOMPurify、highlight.js、16 KiB tail/raw-HTML budget 或 8 ms canonical install budget；
- 不实现 keyed turn/item reconciliation、transcript DOM window、live-history eviction 或 pagination redesign；
- 不修改 Gateway capabilities、stream wire format、snapshot、revision 或 persistence；
- 不修改 TUI、`desktop/` Tauri shell或 Backend；
- 不把 full snapshot、earlier-page prepend 或普通 message renderer 的 DOM mutation 强制迁入 stream keyed frame；本批只统一它们既有的滚动意图；
- 不使用更长 debounce、`IntersectionObserver` 或 smooth-scroll 动画掩盖 layout 成本；
- 不从 DOM `textContent` 反向构造 canonical stream text。

## 3. 必须保持的不变量

### 3.1 Stream / canonical 语义

- `appendStreamText()` 继续立即更新内存中的 `text`、`thinking`、`phase` 和 canonical revision；只有可见 DOM 延后；
- append、replace、legacy cumulative snapshot 和 phase switch 的最终可见内容保持现有语义；
- `commitStream()` 返回值仍为 `{ text, thinking, el } | null`，且不等待 100 ms timer、rAF、Worker 或 canonical staging；
- commit 前尚未显示的最新 delta 必须由 barrier drain，不能丢失；
- `item.completed` 与 turn terminal 状态仍可先推进；`data-render-pending="true"` 的 P0.3 语义不变；
- canonical settle 后正常 DOM 与 `renderMarkdown(canonicalText)` 等价，fallback 的 `textContent` 与 canonical 原文逐字符相等；
- snapshot dedupe 继续读取保存的 canonical text，不读取 provisional DOM。

### 3.2 Frame / layout 语义

- “每个 stream 每帧最多 mutate 一次”指一个 render transaction；transaction 内 projection 更新、thinking visibility、cursor 和 attachment 可以执行多个 DOM API，但不能为同一 stream 重复进入 render；
- 一个 transcript controller frame 对 geometry 只取一个 snapshot：`scrollTop`、`clientHeight`、`scrollHeight` 各读取一次；
- geometry snapshot 必须发生在该 frame 的所有 attached-DOM write 前；
- frame 不得在 mutation 后再次读取 `scrollHeight`；若需到底，写 `scrollTop = Number.MAX_SAFE_INTEGER`，由浏览器 clamp；
- 不同 stream 可在同一 frame 各 mutate 一次；整个 frame 最多写一次 `scrollTop`；
- detached element 的构造不计 attached-DOM mutation；把 stream element 接入 transcript 必须在 render transaction 内完成；
- commit/discard/clear/thread reset 是 barrier，可同步 mutate，但仍必须使用 read-before-write viewport transaction 或显式取消 pending work；
- 第 5.6 节 earlier-page prepend anchor 是唯一允许在 mutation 后同步重读 `scrollHeight` 的 transcript 例外；其读写不得抽象成普通 renderer 可调用的 API；
- 除 `transcript-viewport.ts` 的 controller 写入和 `main.ts` 的受控 prepend anchor 外，生产代码不得直接赋值 transcript 的 `scrollTop`；`ui/terminal.ts` 操作的是独立 terminal container，不受此规则约束。

### 3.3 用户滚动语义

- `bottomGap = max(0, scrollHeight - clientHeight - scrollTop)`；`bottomGap <= 48` 为 near-bottom；
- controller 初始处于 follow 状态；
- controller 自有的 `scroll` listener 只标记 geometry dirty 并请求 frame，不立即交错 layout read/write；`main.ts` 的 pagination listener 只允许额外同步读取一次 `scrollTop <= 24` 作为第 5.6 节分页触发条件；
- geometry frame 观察到用户离底后，将 follow 设为 false 并显示按钮；后续 stream、后台 renderer 和 canonical update 不写 `scrollTop`；
- 用户手动滚回 near-bottom 后，下一 frame 恢复 follow 并隐藏按钮；
- 点击“回到底部”或显式 force 请求，在下一 frame 写一次 scroll position、恢复 follow 并隐藏按钮；
- P0.3 canonical install 和后台 renderer 都是 controller 外部 mutation；它们只能调用 follow-preserving 请求，不能 force。若用户在请求与 frame 之间离底，该请求必须失效；
- 普通 message renderer 不判断业务意图：需要 force 的 full snapshot、用户主动提交、prompt answer 和显式错误必须由 `main.ts` 在 renderer 返回后显式调用 force API。

## 4. 架构

### 4.1 组件关系

```text
appendStreamText
  ├─ update canonical in-memory state immediately
  ├─ fold one PendingStreamProjectionUpdate
  └─ start one non-resetting 100 ms timer
          │
          ▼
     timer expires
  └─ viewport.enqueueMutation(streamKey, renderLatestStreamState)
          │
          ▼
     one transcript frame
  ├─ READ geometry once
  ├─ MUTATE each keyed stream once
  ├─ WRITE scrollTop at most once
  └─ WRITE return-button visibility

commitStream(streamKey)
  ├─ cancel timer
  ├─ viewport.flushMutationNow(streamKey, renderLatestCommittedState)
  ├─ freeze canonical work item
  ├─ start P0.3 Worker
  └─ return immediately

P0.3 Worker settle
  └─ if installed/fallback and still following:
       viewport.requestFollowAfterExternalMutation()
```

### 4.2 `transcript-viewport.ts`

新增 `frontend/src/utils/transcript-viewport.ts`，作为 frame、geometry 和 follow 状态的单一所有者。

必须导出以下类型和工厂；方法名与参数是实施契约，不得由实现阶段自行改名：

```ts
export const TRANSCRIPT_NEAR_BOTTOM_PX = 48;

export type TranscriptFrameHandle =
  | number
  | ReturnType<typeof setTimeout>;

export interface TranscriptViewportGeometry {
  scrollTop: number;
  clientHeight: number;
  scrollHeight: number;
}

export interface TranscriptViewportOptions {
  transcript: HTMLElement;
  returnToBottomButton?: HTMLButtonElement | null;
  scheduleFrame?: (
    callback: FrameRequestCallback,
  ) => TranscriptFrameHandle;
  cancelFrame?: (handle: TranscriptFrameHandle) => void;
  readGeometry?: () => TranscriptViewportGeometry;
  writeScrollTop?: (value: number) => void;
}

export interface TranscriptViewportController {
  readonly transcript: HTMLElement;
  enqueueMutation(key: object, mutate: () => void): void;
  cancelMutation(key: object): void;
  flushMutationNow(key: object, mutate: () => void): void;
  getInteractionGeneration(): number;
  prepareForSynchronousPrepend(expectedInteractionGeneration: number): boolean;
  requestFollowAfterExternalMutation(): void;
  forceScrollToBottom(): void;
  isFollowing(): boolean;
  reset(): void;
  dispose(): void;
}

export function createTranscriptViewportController(
  options: TranscriptViewportOptions,
): TranscriptViewportController;
```

生产默认值只有在`requestAnimationFrame`与`cancelAnimationFrame`都可用时才使用原生pair；任一缺失都改用可取消的16 ms timeout pair。`scheduleFrame`与`cancelFrame`必须同时提供或同时省略；只提供其中一个时factory必须在创建任何listener或frame前抛出`Error`，不得把自定义scheduler与默认canceler混用。测试必须成对注入scheduler/canceler和geometry adapter，不得monkey-patch生产逻辑。两种函数必须接受同一`TranscriptFrameHandle`联合类型，timeout fallback不能遗留不可取消的callback。

### 4.3 Controller 内部状态

Controller 至少维护：

```text
pendingMutations: Map<object, () => void>
frameHandle
following
scrollGeometryDirty
forceFollowRequested
externalFollowRequested
interactionGeneration
transactionActive
owned/disposed generation
```

规则：

- 相同 key 再次 enqueue 覆盖旧 callback；callback 必须读取最新 stream state；
- 不同 key 在同一 frame 顺序执行，但 layout snapshot 共享；
- factory 创建时设置 `following = true`、`interactionGeneration = 0` 并把按钮设为 `hidden = true`；
- `scroll` listener 同步递增 `interactionGeneration`，再置 `scrollGeometryDirty = true` 并请求 frame；程序化 anchor write 产生的后续 `scroll` event 也可递增，但不会反向影响已经完成的 prepend；
- button listener 只调用 `forceScrollToBottom()`；`forceScrollToBottom()` 在建立 force flag 前同步递增 `interactionGeneration`，因此 pending 与已执行 force 都会使旧 pagination response 失效；
- `getInteractionGeneration()` 只返回当前值，不读取 layout；accepted pagination request 捕获该值作为 response ownership token；
- `prepareForSynchronousPrepend(expected)` 只有在 expected 等于当前 interaction generation 时才能准备事务并返回 true；不匹配时不得清 flag、改 following/按钮或取消 frame，直接返回 false；
- `externalFollowRequested` 仅在请求时 `following === true` 才建立；frame 开始后若 dirty geometry 表明用户已离底，则清除该请求；
- `forceFollowRequested` 不受旧 follow 状态限制；
- `reset()` 取消 frame、清空 mutation/flag、递增 controller 内部 generation 与 `interactionGeneration`、恢复 following 并隐藏按钮；
- `dispose()` 先执行 reset，再移除 scroll/button listeners并标记 disposed；旧 callback 用 generation/ownership guard no-op；
- controller transaction 不递归：异步 frame 或 `flushMutationNow()` 在执行 callback 前先移走本轮 work；callback 内的 enqueue/force/external/scroll-dirty 属于下一 transaction。

### 4.4 Frame transaction 顺序

每次异步 frame 必须严格按以下顺序：

1. 清除当前 `frameHandle`，把当时的 pending mutations 和三个 flags 快照到局部变量，并立即从 controller 全局状态中移除/清零这些 work；
2. 调用一次 `readGeometry()`；
3. 计算`wasNearBottom`；若局部`scrollGeometryDirty`为true，用它更新`following`；若dirty为false、本轮有content mutation、没有force/external请求、当前`following`为true但`wasNearBottom`为false，也把`following`降为false并显示按钮，避免缺失scroll event时状态卡在following；
4. 此后不得再读geometry；
5. 执行局部快照中的每个keyed mutation一次；同key在callback中再次enqueue时进入全局map，不能在本轮递归执行；
6. 仅用局部flags、步骤3后的`following`与`wasNearBottom`计算`shouldScroll`：
   - 有局部force request时无条件为true，并把`following`恢复为true；显式force覆盖同帧的旧离底状态；
   - 否则，dirty geometry已判定用户离底时为false；
   - 否则，局部external-follow request且仍following时为true；external请求代表mutation已经发生，不能因其造成的新bottom gap误降following；
   - 否则，本帧有content mutation且mutation前`following && wasNearBottom`时为true；
7. `shouldScroll`为true时写一次`Number.MAX_SAFE_INTEGER`；整个transaction不得第二次写`scrollTop`；
8. 按最终`following`更新按钮`hidden`状态；
9. 若callback执行期间全局map/flags又出现work，另排下一frame；否则不留空frame。

`flushMutationNow(key, mutate)` 用于 commit barrier，必须执行以下确定算法：

1. 从 `pendingMutations` 删除目标 key；目标 queued callback 不执行，由参数 `mutate` 代表目标最新状态；
2. 与异步 frame 相同，把调用开始时的 `scrollGeometryDirty`、`forceFollowRequested`、`externalFollowRequested` 全部快照并从全局状态清零；这些旧 flags 由本次 barrier 消费，不留给原 frame；
3. 保留其他 key 的 pending callbacks，不把它们放入本次局部 mutation 集；
4. 读取一份 geometry，按第 4.4 节步骤 3、4、6、7、8 的同一顺序更新 `following`、同步执行一次 `mutate`、至多写一次 scroll 并更新按钮；因此 dirty-away 会阻止普通 follow，force 会恢复 following，external-follow 仅在仍 following 时生效；
5. `mutate` 内新产生的 flags 或 enqueue 不属于本次快照，必须保留到下一 frame；
6. 完成后若其他 key、新 flags 或重入 work 仍存在，确保恰有一个 frame；若均为空，取消尚未执行的旧 handle或让带 generation guard 的旧 callback no-op。

因此 flush 不会重复执行目标 key，也不会丢失其他 key；被 barrier 消费的 force/external 意图若成立，会在 barrier mutation 后完成一次写入，而不是迟到影响后续无关 mutation。production stream transaction 不得从 controller mutation callback 内递归调用 `commitStream()`；测试应验证普通 enqueue 重入被延到下一 frame。

### 4.5 Stream pending update 折叠

`frontend/src/utils/types.ts` 将数组式 `pendingProjectionUpdates` 收敛为单值：

```ts
export interface PendingStreamProjectionUpdate {
  text: string;
  operation: "append" | "replace";
}

export interface StreamState {
  text: string;
  thinking: string;
  phase: string;
  el: HTMLElement;
  thinkingEl: HTMLElement;
  thinkingLabel: HTMLElement;
  thinkingBody: HTMLElement;
  textEl: HTMLElement;
  renderTimer: ReturnType<typeof setTimeout> | null;
  renderQueued: boolean;
  attached: boolean;
  markdownProjection?: import("./markdown").StreamingMarkdownProjection;
  pendingProjectionUpdate: PendingStreamProjectionUpdate | null;
  canonicalRevision: number;
  streamGeneration: number;
  committed?: boolean;
}
```

折叠规则：

| 当前 pending | 新 update | 新 pending |
|---|---|---|
| none | append Δ | append Δ |
| append A | append B | append A+B |
| none/append/replace | replace canonical | replace canonical |
| replace A | append B | replace A+B |
| 任意 | explicit phase reset 到 text | replace 新 text phase 的完整 canonical text |
| 任意 | explicit phase reset 到 thinking | replace 空字符串，清除旧 text projection |

同一 frame 最终只允许调用一次 `markdownProjection.update(text, operation)`。folded projection update 不因当前 phase 为 thinking 而跳过；thinking phase 的空 `replace` 是清除旧 text DOM 的必要 barrier。`stream.text` / `thinking` 始终是最新 canonical 内存值，不能从 pending update 或 DOM 推导。

### 4.6 100 ms trailing throttle

`DEBOUNCE_MS` 重命名为语义明确的 `STREAM_RENDER_THROTTLE_MS = 100`。

- 首个 pending visible update 启动一个 timer；
- 后续 update 只折叠 state，不 clear/restart timer；
- timer 到期后只设置 `renderQueued = true` 并调用 controller `enqueueMutation()`；
- timer 已到期、rAF 尚未运行时到达的 update 继续由已 queued callback读取；不得再开 timer；
- render transaction 开始时清除 `renderQueued`，并消费当时最新 pending state；
- render 完成后到达的新 update 才开启下一 100 ms window；
- 因此持续 stream 不会被 resettable debounce 永久饿死，render 频率不高于每 stream 约 10 Hz，延迟上界为 100 ms 加一帧。

### 4.7 Render transaction

一次 `renderLatestStreamState(stream, committed = false)` 负责：

1. 若尚未 attached，把 `stream.el` append 到 controller transcript；
2. 先移除现有 `.stream-cursor`；
3. 若存在 folded projection update，无论当前 phase 是 text 还是 thinking，都调用一次 `markdownProjection.update()` 并清空 pending；
4. 按最新 phase 显示最后五行 thinking，或隐藏 thinking；
5. 非 committed 的 text phase 在 projection 后追加且只追加一个 cursor；thinking phase 或 committed transaction 不创建 cursor；
6. committed empty stream 保持现有隐藏语义。

不得在 `appendStreamText()` 的 production path 中对 attached stream 调用 `hideThinking()`、`projection.reset()`、`replaceChildren()`、`append()` 或直接设置 scroll position。

## 5. 生命周期

### 5.1 Append / replace / legacy cumulative

- API 签名保持 `appendStreamText(streamId, text, phase, operation?)`；
- in-memory text 与 revision 同步更新；
- explicit append 使用 delta；无法证明前缀时折叠为 replace canonical；
- explicit replace 总是覆盖 pending；
- legacy cumulative API 在新文本扩展旧前缀时折叠 append，否则 replace；
- 创建 stream 时可同步构造 detached DOM，但 attachment 延后到 transaction。

### 5.2 Phase switch

- explicit protocol update 的 phase 变化继续重置上一 phase 的 text projection：切到 text 时 folded 为完整 text replace，切到 thinking 时 folded 为空 replace；
- legacy cumulative API 保持现状：切到 thinking 不主动清除已有 text projection，后续切回 text 时用收到的完整 snapshot replace；
- reset 只更新内存与 folded `replace` 标记，不立即写 attached DOM；
- thinking → text 与 text → thinking 都由一个 latest-state transaction 更新 thinking/cursor；
- thinking 与 text 不共享 mutable tail。

### 5.3 Commit barrier

`commitStream()` 必须：

1. 找到 stream；不存在仍返回 `null`；
2. cancel `renderTimer`；
3. cancel/consume该 stream 的 queued frame mutation；
4. 调用 `flushMutationNow(stream, () => renderLatestStreamState(stream, true))`；
5. 标记 committed、更新 revision/owner、保存 canonical text、移出 active map；
6. 按 P0.3 现有方式启动 Worker；
7. 立即返回 result。

旧 timer/rAF callback 之后不得重复 projection、重建 cursor、清除新 stream 状态或改变滚动。

### 5.4 Canonical settle 与 viewport owner

`stream.ts` 维护单调递增的 `transcriptViewportGeneration`。每个 canonical owner 除现有 revision、stream generation 与 target 外，还捕获 commit 当时的 `viewportController` 和 `viewportGeneration`；`CanonicalCommitWork.isCurrent()` 的 P0.3 stream/revision 规则保持不变。

`CanonicalCommitWork.onSettled(outcome)` 必须按以下顺序处理：

1. 记录 `canonicalOwners.get(streamId) === owner`；
2. 只有 owner 仍 current、`outcome.status` 为 `installed` 或 `fallback`、捕获 controller 与当前 controller 是同一对象、捕获 generation 等于当前 `transcriptViewportGeneration`、`owner.target.isConnected` 且 `capturedController.transcript.contains(owner.target)` 时，才对**捕获的 controller**调用 `requestFollowAfterExternalMutation()`；
3. `stale` 永不请求滚动；任一 identity/generation/containment guard 失败也不请求，不能回退为调用当前全局 controller；
4. owner 仍 current 时按现有语义从 map 删除。

因此旧 target 即使发生迟到 settle，也不能让新 transcript 滚动。canonical owner 只在成功 replacement 时跨 `transcriptViewportGeneration` 失效；同一 controller 上的普通 frame reset 不改变该 generation，避免 full snapshot 保留的有效 canonical work失去后续 follow。thread/workspace 切换则先 invalid owner再 reset。上述规则不改变 P0.3 DOM install/stale语义，也不修改 Worker response、staging scheduler、DOMPurify或fallback reason。

### 5.5 Clear、reset 与 controller replacement barrier

清理规则：

- `discardStream()`、`clearActiveStreams()`：cancel timer、cancel keyed mutation，再移除 DOM 和 invalidation owner；
- `clearCommittedStreams()` 保持 P0.3 owner invalidation并清空 retained committed stream elements；
- thread activation、workspace switch 和 full snapshot 都必须在 transcript 清空/替换前 cancel pending viewport work；thread activation 与 workspace switch 在清空旧 DOM 前依次调用 `clearCommittedStreams()`、`clearActiveStreams()`、`resetTranscriptViewport()`；
- 已通过 stale/revision 校验的 full snapshot 在 `renderTranscript()` 前调用 `resetTranscriptViewport()`，由 `renderTranscript()` 既有 `clearActiveStreams({ preserveCanonicalCommits: true })` 清理 active stream，render 完成后显式 force bottom；被拒绝的 snapshot 不 reset；
- `resetTranscriptViewport()`只调用当前controller的`reset()`：退休其内部frame generation、清空queue/flags、恢复follow并隐藏按钮；它不改变`transcriptViewportGeneration`，不替换controller，也不自行invalid canonical DOM work；所有production调用点必须在同一同步调用栈中先清理active stream或紧接着由`renderTranscript()`清理，二者之间不得`await`/排microtask，避免被reset清掉的queued mutation留下`renderQueued = true`；thread/workspace切换必须先通过clear API invalid owner，full snapshot则允许仍连接且仍current的canonical target继续settle；
- `stream.ts::_resetForTest()` 必须 cancel所有 stream timer/mutation、清空 active/committed/canonical owner、dispose当前 controller、递增 `transcriptViewportGeneration`、清空 controller/transcript引用并使 retired callback no-op；
- `main.ts::_resetWorkbenchForTest()` 只调用 `resetTranscriptViewport()`，因为 production模块仍持有同一个 DOM/controller；它不得 dispose controller。

`setTranscriptElement()`和`_setTranscriptViewportControllerForTest()`都是controller attach/replacement barrier，不迁移任何work。首次attach与后续replacement一律只允许同时满足：

```text
streams.size === 0
committedEls.length === 0
canonicalOwners.size === 0
```

若任一条件不满足，函数必须同步抛出`Error("cannot replace transcript viewport while stream or canonical work is active")`，且不得创建production candidate、dispose当前controller、接管test candidate、修改transcript引用或递增generation。调用方必须先走上述clear/reset生命周期，不能靠attach/replacement偷渡取消或迁移work。

成功attach/replacement的固定顺序是：验证quiescent前置条件；为`setTranscriptElement()`创建candidate controller；dispose旧controller（若有）；递增`transcriptViewportGeneration`；原子更新controller与transcript引用。test setter对传入candidate执行相同的前置检查、dispose、generation与接管顺序；失败时candidate仍归调用方所有且函数不得修改或dispose它。test setter若传入的candidate就是当前controller，则在quiescent前置条件通过后作为显式no-op返回：不dispose、不递增generation、不改变引用；非quiescent时仍按固定Error拒绝。旧controller已捕获frame由dispose generation guard no-op；active `renderQueued`因前置条件不可能存在；旧canonical settle由owner invalidation以及第5.4节identity/generation guard拒绝。

### 5.6 Earlier-page preserve-live 同步例外

分页协议、`scrollTop <= 24` 触发阈值、page merge/dedupe 与 anchor 公式不变，但 earlier-page DOM 安装必须使用独立的 preserve-live prepend 模式，不能调用 full-snapshot `renderTranscript()` 语义：

1. pagination `scroll` listener 可以同步读取一次 `transcriptEl.scrollTop`，仅用于 `<= 24` 判断；它不得写 scroll 或替代 controller listener；
2. accepted request 只捕获既有 thread/context/stale token 与 `paginationInteractionGeneration = controller.getInteractionGeneration()`；请求前不得读取或保存 `scrollHeight`/anchor `scrollTop`；
3. response 先通过既有 thread/context/stale guards，再调用 `prepareTranscriptForSynchronousPrepend(paginationInteractionGeneration)`；若 interaction generation 不匹配则 response 视为 viewport-stale：不 render、不 prepare、不写 anchor，保留用户的新 force、manual scroll、follow状态和已排frame；page cursor/state也不得推进，使后续 near-top事件可重新请求该页；
4. prepare返回true时，不读写`scrollTop`、不执行或丢弃keyed mutation，也不递增owner或interaction generation；它同步清除请求接受前遗留且尚未消费的`scrollGeometryDirty`、force与external-follow三类flags，将`following = false`并显示按钮；若清除flags后没有pending keyed mutation，立即cancel仅为旧flags排定的空frame；若仍有keyed mutation，保留原handle并在下一transaction以`following = false`执行；
5. prepare成功后立即在同一同步调用栈读取`previousHeight = scrollHeight`与`previousTop = scrollTop`；从该baseline read到步骤7 anchor write之间不得`await`、排microtask或执行无关业务回调；因此RPC等待期间已经完成的stream/tool/message mutation不进入prepend高度差；
6. 同步调用`renderTranscript(pageItems, { mode: "prepend-preserve-live" })`并完成pending-local restore和empty-state mutation。该模式只可在现有第一个transcript子节点前插入本页历史节点：不得调用`clearActiveStreams()`/`clearCommittedStreams()`，不得删除、重排或替换现有DOM，不得修改active stream的state、pending projection、timer、`renderQueued`、incremental revision/cursor或canonical owner；thought/tool/file renderer不得把历史项合并、复用或挂接到现有live节点/card。历史页内部仍按既有dedupe顺序构造，已存在item ID保持现有节点且不重复插入；
7. mutation后只允许此路径重读一次`scrollHeight`，并直接写`previousTop + (scrollHeight - previousHeight)`；不得写`Number.MAX_SAFE_INTEGER`；
8. anchor write产生的后续scroll event重新进入controller正常geometry frame；用户主动点击按钮或滚回near-bottom后才恢复follow。

这组 response-time baseline/post-read 是第 3.2 节 frame read-before-write规则的唯一例外。分页失败、thread/context stale或viewport-stale response不调用render、不修改controller或page state。本批不改变RPC时序、page merge/dedupe算法或anchor数学，只为`renderTranscript`增加严格隔离的prepend render mode；full snapshot继续使用原有replace模式。

### 5.7 后台 transcript mutation 的统一 follow API

`stream.ts` 导出 `requestTranscriptFollowAfterMutation()`，实现仅对当前 controller调用 `requestFollowAfterExternalMutation()`；无 controller时 no-op。它表达“若用户仍在跟随，则在下一 controller frame 到底”，不得同步读取 geometry、直接写 scroll或升级为 force。

下列现有直接 `scrollTop = scrollHeight` 必须删除，并在各自 attached transcript mutation完成后调用该统一 API：

- `render.ts`：stats message与普通 `appendMessageItem()`；
- `render-thought-items.ts`：thought merge与新 item；
- `render-tool-items.ts`：tool start以及已有 tool的 delta/completion；
- `render-file-changes.ts`：summary card与 diff card；
- `render-notice-status.ts`：`appendDiffItem()`；
- `ui/prompt.ts`：conversation prompt insertion。

这些模块已依赖 `stream.ts` 的 `getTranscriptElement()`，继续从同一模块直接导入 follow helper，不新建反向依赖。helper只替换当前强制跳底，不要求把 renderer DOM mutation迁入 keyed stream frame，也不为原本不滚动的 toast、status或 compaction路径新增滚动。

普通 renderer不推断 force语义。`main.ts` 的 full snapshot、非 guidance 用户主动提交、prompt answer和显式错误在 mutation后显式调用 `forceTranscriptScrollToBottom()`；其余后台 notification只 follow。`ui/terminal.ts` 保留 terminal output自身的直接滚动。

## 6. 主动滚动与 UI

### 6.1 DOM

`frontend/index.html` 在 transcript 周围增加相对定位 wrapper，并保留 `#transcript` 的 `aria-live`：

```html
<div class="vx-transcript-viewport">
  <div class="transcript" id="transcript" aria-live="polite"></div>
  <button
    type="button"
    class="vx-return-bottom"
    id="transcript-return-bottom"
    aria-label="回到底部"
    hidden
  >回到底部</button>
</div>
```

实现必须采用上面的文本按钮结构；本批不加入 SVG、图标依赖或仅图标可访问分支。

### 6.2 CSS

- `.vx-transcript-viewport` 固定使用 `position: relative; display: flex; flex: 1; flex-direction: column; min-height: 0; min-width: 0`；
- transcript 继续是唯一 scroll container，并保持 `flex: 1; min-height: 0; min-width: 0`；
- empty state 使用 `.vx-main-canvas.empty .vx-transcript-viewport { display: none; }`，替换现有只隐藏 transcript 的 selector；
- `.vx-return-bottom` 固定使用 `position: absolute; left: 50%; bottom: var(--vx-space-3); transform: translateX(-50%); z-index: 2; background: var(--vx-bg-elevated); border: 1px solid var(--vx-border-strong); border-radius: var(--vx-radius-full); box-shadow: var(--vx-shadow-sm); color: var(--vx-text-primary); cursor: pointer; font: inherit; padding: var(--vx-space-2) var(--vx-space-4); transition: background var(--vx-transition-fast)`；
- hover 仅设置 `background: var(--vx-bg-hover)`；focus ring 复用 `base.css` 的全局 `:focus-visible`；`.vx-return-bottom[hidden] { display: none; }`；
- 删除 `.transcript { scroll-behavior: smooth; }`，自动跟随与按钮跳底都使用一次即时 write，避免动画期间 near-bottom 抖动；
- 不新增 raw color、像素形式的重复 spacing/radius/shadow 常量或新 design token。

### 6.3 `main.ts` 与固定接线 API

- `frontend/src/services/state.ts` 新增并由 `initStateDom()` 填充 `transcriptReturnBottomEl: HTMLButtonElement`，`main.ts` 从现有 `./services` barrel 导入；
- `stream.ts` 是当前 controller 的唯一所有者；`main.ts` 在 `initStateDom()` 后调用 `setTranscriptElement(transcriptEl, transcriptReturnBottomEl)` 完成首次初始化；
- `stream.ts` 必须导出以下固定接线 API：

  ```ts
  export function setTranscriptElement(
    el: HTMLElement,
    returnToBottomButton?: HTMLButtonElement | null,
  ): void;
  export function requestTranscriptFollowAfterMutation(): void;
  export function forceTranscriptScrollToBottom(): void;
  export function getTranscriptInteractionGeneration(): number;
  export function prepareTranscriptForSynchronousPrepend(
    expectedInteractionGeneration: number,
  ): boolean;
  export function resetTranscriptViewport(): void;
  export function _setTranscriptViewportControllerForTest(
    controller: TranscriptViewportController,
  ): void;
  ```

- `setTranscriptElement()` 与 test setter 都遵守第 5.5 节 quiescent replacement barrier；现有测试的单参数调用只在 `_resetForTest()` 后合法，并创建无按钮 controller；
- 现有 `scrollToBottom()` 保留为 `main.ts` 内部语义入口，但只调用 `forceTranscriptScrollToBottom()`；
- full snapshot、非 guidance 用户主动提交、prompt answer 和显式错误消息继续由 `main.ts` force；stream delta、普通后台 renderer与 canonical settle只 follow；
- accepted earlier-page request读取`getTranscriptInteractionGeneration()`；response按第5.6节把token传给`prepareTranscriptForSynchronousPrepend(token)`，仅在返回true时执行response-time baseline、preserve-live render与同步anchoring；
- thread activation 在 `transcriptEl.replaceChildren()` 前依次 clear active/committed work并调用 `resetTranscriptViewport()`；
- `renderWorkspaceSnapshot()` 仅在 snapshot 通过 thread/stale/revision校验并即将调用 `renderTranscript()` 时 reset viewport；拒绝的 snapshot不得影响当前 follow状态；snapshot render结束后现有 force path恢复底部；
- `_resetWorkbenchForTest()` 调用 `resetTranscriptViewport()`，恢复 follow、隐藏按钮并取消 pending frame，但不 dispose当前 production controller。

## 7. 测试策略

### 7.1 不依赖 jsdom layout

`frontend/test/utils/transcript-viewport.test.ts` 必须注入：

- 可手动 flush/cancel、能记录 handle 的 fake frame scheduler；
- 可变 geometry snapshot；
- 记录调用次数、顺序和值的 `readGeometry` / `writeScrollTop`；
- 独立 button element。

`frontend/test/setup.ts` 的共享 DOM 必须与 production 结构一致：用 `.vx-transcript-viewport` 包裹 `#transcript`，并加入 `#transcript-return-bottom` 文本按钮、`aria-label="回到底部"` 和初始 `hidden`。现有测试仍通过 `#transcript` 选择器工作；需要 controller 的测试在 reset后显式重新 set/inject，不能依赖 jsdom 自动 layout。

测试不能用真实时间等待、`IntersectionObserver`、原生 smooth scrolling 或 jsdom 自动计算 `scrollHeight`。

### 7.2 必测 controller 行为

- 同 key 多次 enqueue只执行最后一个 callback一次；多 key同 frame各执行一次，但 geometry只读一份、scroll只写一次；
- near-bottom mutation后写`Number.MAX_SAFE_INTEGER`，且mutation后不再read；离底mutation不写scroll并显示按钮；即使缺失scroll event，content geometry已away时也demote following；
- 手动回near-bottom恢复follow；按钮点击/force下一frame只写一次并隐藏按钮；
- external-follow在仍following时写；请求后用户离底则不写；external mutation造成的post-mutation gap本身不demote following；
- factory的`scheduleFrame`/`cancelFrame`成对注入可用，只提供任意一个都抛`Error`且不注册listener；任一原生rAF/cancel API缺失时使用可取消timeout pair；
- frame callback内对同key或新key enqueue、request force/external时，本轮不递归执行，恰好延到下一frame；
- `flushMutationNow()`删除且只执行目标key的最新`mutate`，保留其他key，并使旧目标callback不重复；
- flush对dirty-away、force、external三类调用前flags逐一验证：旧flags在barrier中被确定消费，following/按钮/单次scroll结果符合第4.4节，且不泄漏到保留的其他key；flush callback内新flags留到下一frame；
- `getInteractionGeneration()`不读layout；scroll与force分别递增generation；`prepareForSynchronousPrepend(expected)`仅在token匹配时清空旧flags、置away并保留keyed mutation；没有keyed mutation时取消空frame，有keyed mutation时保留handle且后续mutation不自动滚动；token不匹配时完全no-op并返回false；
- cancel/reset/dispose与已退休 callback no-op，dispose移除 scroll/button listeners。

### 7.3 必测 stream / ownership 行为

使用 Vitest fake timers和 controlled viewport：

- 99 ms不 render，100 ms enqueue一次；第二个 delta不重置 deadline；timer到期至 frame执行之间的 delta由同一 render消费；
- append+append、replace+append、append+replace各折叠为一次正确 projection operation；多 stream同 frame各 render一次；
- phase switch在 frame前不写 attached DOM，frame后显示最新 phase；stream attachment、thinking、projection和cursor在同一 transaction；
- commit在timer/frame前同步 drain最新文本，消费目标 queued work与旧 viewport flags，保留其他 stream work，并立即返回；
- discard、clear、同 ID新 stream和reset拒绝旧 timer/frame；
- replacement在 active stream、retained committed stream、canonical owner三种非静默状态下分别同步抛固定 Error，旧 controller/ref/generation不变；完成 clear后 replacement成功，旧 listener/frame不再回写；
- test setter遵守同一 replacement barrier，失败时不接管 candidate，成功时只接管一次；
- canonical installed/fallback只在 owner、controller identity、viewport generation和DOM containment全匹配时 follow；stale不滚动；旧 controller/旧 target的迟到 installed或fallback settle不能请求新 transcript滚动；
- 普通 reset不误杀同 controller且仍连接的 current canonical settle；thread/workspace owner invalidation后settle为stale且不滚动；
- P0.2 bounded parser inputs、P0.3 async commit/stale/fallback测试继续通过。

### 7.4 Renderer / Main / UI 回归

- `render.ts`、thought、tool、file-change、diff和conversation prompt的现有直接跳底路径都改为调用统一 follow helper；controlled controller在 near-bottom时收到合并请求，在 away时不写 scroll；
- 离底后，带 thinking内容的 assistant `item.completed` 同步推进业务完成、保留thinking UI并启动canonical工作，但 thought insertion与canonical settle都不改变 scroll position；
- incremental notification业务状态不等待 frame/Worker；用户离底时普通 `item.delta`、tool delta、file diff、prompt和message不改变 scroll position；
- full snapshot、非 guidance用户主动提交、prompt answer和显式错误使用force path；普通后台notification不force；
- earlier-page测试断言 request只捕获interaction token，response guards/prepare通过后才读取baseline，preserve-live prepend后写精确anchor；RPC等待期间先完成stream frame/tool/message mutation时高度不计入prepend差值；
- page pending期间text与thinking stream继续delta并completion，分页安装前后active state、revision、timer、projection与canonical raw text完整，历史thought/tool/file不与live DOM合并或复用；
- pending force、已执行force、请求期间manual scroll分别递增interaction generation，使旧page response不render、不推进page state、不清flags且不覆盖用户位置；未交互response仍隔离旧follow flags/frame，stale/failed page不改变follow；
- thread/workspace/full snapshot reset顺序与旧frame隔离；按钮结构、可访问名称和hidden切换正确；
- 静态守卫枚举 `stream.ts`、`render.ts`、`render-thought-items.ts`、`render-tool-items.ts`、`render-file-changes.ts`、`render-notice-status.ts`、`ui/prompt.ts`，断言不存在直接 `scrollTop =`；`main.ts`只允许earlier-page精确anchor赋值，`transcript-viewport.ts`只允许adapter默认writer；terminal独立滚动保持允许；
- CSS design-system测试验证wrapper/button selector、现有token、smooth-scroll移除且不引入raw color/token违规。

## 8. 文件边界

### 新增

- `frontend/src/utils/transcript-viewport.ts`：frame queue、geometry snapshot、follow状态、按钮、prepend准备和barrier API；
- `frontend/test/utils/transcript-viewport.test.ts`：controller确定性单元测试。

### 修改

生产代码与UI：

- `frontend/src/utils/stream.ts`：100 ms throttle、folded pending update、keyed frame mutation、flush barrier、统一follow/force/prepend接线、replacement barrier与canonical viewport owner；
- `frontend/src/utils/types.ts`：单pending update、timer/queued/attached字段；
- `frontend/src/utils/index.ts`：按现有公共出口导出需要的viewport类型/工厂；
- `frontend/src/utils/render.ts`：stats/普通message删除直接跳底，改为统一follow helper；
- `frontend/src/utils/render-thought-items.ts`：thought merge/new item改为follow helper；
- `frontend/src/utils/render-tool-items.ts`：tool start/delta/completion改为follow helper；
- `frontend/src/utils/render-file-changes.ts`：summary/diff card改为follow helper；
- `frontend/src/utils/render-notice-status.ts`：`appendDiffItem()`改为follow helper；
- `frontend/src/ui/prompt.ts`：conversation prompt insertion改为follow helper；
- `frontend/src/main.ts`：controller初始化、force调用点、earlier-page受控同步例外、thread/snapshot/test reset接线；
- `frontend/src/services/state.ts`：缓存`transcriptReturnBottomEl`；
- `frontend/src/services/connection.ts`：从`../utils/stream`直接导入`clearCommittedStreams()`、`clearActiveStreams()`和`resetTranscriptViewport()`；workspace switch在清空transcript前依次调用三者；
- `frontend/index.html`：transcript wrapper和返回底部按钮；
- `frontend/css/chat.css`：wrapper/button样式并移除smooth scroll；
- `frontend/css/layout.css`：main canvas/empty state wrapper布局。

测试与文档：

- `frontend/test/setup.ts`：共享DOM补齐transcript wrapper和返回底部按钮；
- `frontend/test/utils/stream.test.ts`：throttle、fold、flush flags、replacement和canonical owner回归；
- `frontend/test/utils/render.test.ts`：message/thought/tool/diff统一follow回归；
- `frontend/test/utils/render-file-changes.test.ts`：file card统一follow回归；
- `frontend/test/main/main.test.ts`：thought/tool renderer行为回归；
- `frontend/test/main/incremental-protocol.test.ts`：notification非阻塞、away状态、thinking completion、prompt和canonical集成；
- `frontend/test/main/runtime-profile.test.ts`：snapshot/page anchor、thread reset和旧frame隔离；
- `frontend/test/ui/design-system.test.ts`：按钮DOM/CSS与直接transcript scroll写入静态守卫；
- `frontend/test/ui/workbench.test.ts`：workspace switch reset viewport且旧work不回写；
- `docs/design/cross-ui-performance-addendum.md`：实现和最终验证通过后追加P0.4实施记录，整体保持`in-progress/partial`。

### 禁止修改

以下路径不属于本规格，实施阶段不得修改；若现有接口被证明确实阻塞实现，应停止任务并先修订本规格，而不是自行扩大范围：

- `frontend/src/utils/markdown-renderer.ts`；
- `frontend/src/utils/markdown-worker-protocol.ts`；
- `frontend/src/utils/markdown.worker.ts`；
- Gateway/Python/TUI/desktop shell；
- protocol schema和生成的`protocol.d.ts`；
- P0.3 fallback reason、source coverage或stale identity；
- 范围外用户改动与工具输出阈值。

## 9. TDD 实施任务

### Task 1 — Viewport controller RED → GREEN

- RED：新建`frontend/test/utils/transcript-viewport.test.ts`，覆盖第7.2节，包括scheduler/canceler配对抛错、帧内重入、三类flush flags、prepend准备和retired callback；先确认缺模块/行为失败；
- GREEN：新增`frontend/src/utils/transcript-viewport.ts`最小实现；
- 命令：`./test.py --frontend -- test/utils/transcript-viewport.test.ts`；
- 期望：controller矩阵通过，没有业务模块接线。

### Task 2 — UI shell / shared test DOM RED → GREEN

- RED：扩展`frontend/test/ui/design-system.test.ts`，并先修改测试断言要求production/shared DOM按钮、CSS token、selector和smooth-scroll移除；
- GREEN：修改`frontend/index.html`、`frontend/test/setup.ts`、`frontend/css/chat.css`、`frontend/css/layout.css`和`frontend/src/services/state.ts`；
- 命令：`./test.py --frontend -- test/utils/transcript-viewport.test.ts test/ui/design-system.test.ts`；
- 期望：production与共享测试DOM结构一致，按钮可访问且布局/style规则通过。

### Task 3 — Stream throttle / barrier / ownership RED → GREEN

- RED：在`frontend/test/utils/stream.test.ts`用fake timers/controller覆盖第7.3节：fold、commit消费全局flags、其他key保留、三类replacement拒绝、test setter、旧canonical settle与普通reset；
- GREEN：修改`frontend/src/utils/stream.ts`、`frontend/src/utils/types.ts`和`frontend/src/utils/index.ts`；
- 命令：`./test.py --frontend -- test/utils/transcript-viewport.test.ts test/utils/stream.test.ts test/utils/markdown-worker.test.ts test/utils/markdown.test.ts`；
- 期望：P0.4 stream/owner契约转绿，P0.2 projection与P0.3 coordinator不回退。

### Task 4 — Background renderer follow RED → GREEN

- RED：扩展`frontend/test/utils/render.test.ts`、`frontend/test/utils/render-file-changes.test.ts`、`frontend/test/main/main.test.ts`、`frontend/test/main/incremental-protocol.test.ts`和`frontend/test/ui/design-system.test.ts`，覆盖所有第5.7节路径、away不抢滚动、prompt以及“带thinking的assistant completion”；
- GREEN：修改`frontend/src/utils/render.ts`、`render-thought-items.ts`、`render-tool-items.ts`、`render-file-changes.ts`、`render-notice-status.ts`和`frontend/src/ui/prompt.ts`，只替换直接跳底为统一follow helper；
- 命令：`./test.py --frontend -- test/utils/transcript-viewport.test.ts test/utils/stream.test.ts test/utils/render.test.ts test/utils/render-file-changes.test.ts test/main/main.test.ts test/main/incremental-protocol.test.ts test/ui/design-system.test.ts`；
- 期望：所有后台transcript mutation只follow，明确force路径尚由`main.ts`负责，静态守卫无漏点。

### Task 5 — Main pagination / lifecycle RED → GREEN

- RED：扩展`frontend/test/main/runtime-profile.test.ts`，覆盖request只捕获interaction token、response-time baseline、等待期后台mutation不计入anchor、pending/已执行force与manual scroll使response viewport-stale且page state不推进；
- RED：扩展`frontend/test/main/incremental-protocol.test.ts`，覆盖page pending时text/thinking delta继续到completion，prepend前后active/canonical内容完整且历史thought/tool/file不与live DOM合并；扩展`frontend/test/ui/workbench.test.ts`覆盖thread/workspace/snapshot和旧canonical owner；
- GREEN：修改`frontend/src/main.ts`、`frontend/src/utils/render.ts`和`frontend/src/services/connection.ts`；为`renderTranscript`增加严格`prepend-preserve-live` mode，workspace switch在`transcriptEl.replaceChildren()`前依次调用`clearCommittedStreams()`、`clearActiveStreams()`和`resetTranscriptViewport()`；
- 命令：`./test.py --frontend -- test/utils/transcript-viewport.test.ts test/utils/markdown-worker.test.ts test/utils/markdown.test.ts test/utils/stream.test.ts test/utils/render.test.ts test/utils/render-file-changes.test.ts test/main/main.test.ts test/main/incremental-protocol.test.ts test/main/runtime-profile.test.ts test/ui/design-system.test.ts test/ui/workbench.test.ts`；
- 期望：P0.2/P0.3/P0.4聚焦回归全绿，分页anchor稳定、live stream/canonical完整、用户新交互优先，业务状态不等待frame/Worker。

### Task 6 — 最终验证与文档

代码稳定后才运行以下命令；完整Frontend、build和tsc本批各运行一次，不因工作流状态重复执行：

- `./test.py --frontend`
- `cd frontend && npm run build`
- `cd frontend && npx tsc --noEmit`
- `git diff --check`

分类规则：

- 必须读取`./test.py` JSON内部`results[].status`，不能只看shell exit code；
- build必须保留独立`markdown.worker-*.js` chunk；
- tsc若仍只报告既有`src/services/connection.ts`、`test/ui/design-system.test.ts`、`test/ui/theme.test.ts` 7项基线错误，记录为范围外基线；P0.4文件不得新增诊断；
- fresh结果写入`docs/design/cross-ui-performance-addendum.md`，不得复制P0.3旧计数；
- 整份增补仍保持`status: in-progress` / `implementation_status: partial`，因为P0.5/P1/P2及统一benchmark/观测尚未闭环。

## 10. 验收标准

实施完成前保持未勾选：

- [ ] 连续delta使用非重置100 ms trailing throttle，持续更新不会饿死；
- [ ] 同一stream每个transcript frame最多一个render transaction，一帧内append/replace/phase变更折叠为至多一个projection operation；
- [ ] delta handler不再同步读取`scrollHeight`或写attached stream DOM/scroll position；
- [ ] 每个controller content transaction只读取一份geometry，所有read在write前，scroll最多写一次；
- [ ] `flushMutationNow()`同步drain目标最新projection，确定消费调用前dirty/force/external flags，保留其他key和重入work，并立即返回而不等待Worker；
- [ ] near-bottom用户继续跟随；离底用户不被stream、message、thought、tool、file diff、prompt或canonical settle抢回且看到“回到底部”按钮；
- [ ] 所有后台transcript renderer通过统一follow helper表达滚动；除controller和earlier-page anchor外无直接transcript `scrollTop`赋值；
- [ ] 点击按钮、full snapshot、非guidance用户提交、prompt answer和显式错误可force恢复跟随；
- [ ] canonical installed/fallback仅在owner/controller/generation/containment仍匹配且following时滚动，stale或旧target settle不滚动；
- [ ] active stream、retained committed stream或canonical owner存在时replacement固定抛错且不改变旧owner；quiescent replacement后旧listener/frame/settle不回写；
- [ ] discard、clear、thread/workspace switch、同ID新stream和reset拒绝旧timer/frame，普通reset不误杀同controller的有效canonical settle；
- [ ] earlier-page仅按第5.6节执行受控post-mutation geometry例外，精确保持prepend anchor且旧flags/frame不覆盖anchor；
- [ ] production与`frontend/test/setup.ts`都包含可访问的wrapper/按钮结构；scheduler/canceler配对、帧内重入、thinking completion和replacement矩阵有确定性测试；
- [ ] P0.2 bounded projection与P0.3 Worker安全/等价/stale/fallback契约不回退；snapshot replay和notification业务时序不回退；
- [ ] 聚焦Frontend测试通过；完整Frontend在最终阶段fresh运行一次并通过；
- [ ] production build通过并保留独立Worker chunk；
- [ ] `git diff --check`通过，P0.4文件无新增TypeScript诊断；
- [ ] P0.5、P1/P2、Gateway、TUI和范围外用户改动未混入或回退。

## 11. 风险与回滚

### 11.1 风险

- resettable debounce会在持续token流下永久推迟render；必须使用首个update启动、不中途重置的timer；
- timer已到期但frame未执行时若再次开timer，会产生空render或超过频率；必须用`renderQueued`覆盖该窗口；reset清空controller queue时必须在同一同步栈清理对应active stream；
- post-mutation读取新`scrollHeight`会恢复强制layout；只有earlier-page受控anchor可例外，到底写必须使用clamped最大值；
- CSS smooth scroll会让following状态在动画中抖动；本批必须移除transcript的全局smooth行为；
- 后台renderer或canonical settle若使用force会把离底用户拉回；只能发follow-preserving请求，明确用户动作才force；
- commit若只cancel timer而不drain folded update，或flush把调用前flags泄漏给其他key，会造成visible preview/滚动时序错误；barrier必须按第4.4节同步消费；
- controller reset/replacement若不能退休旧frame、listener和canonical滚动owner，旧work可能修改新thread；replacement必须先验证quiescent且旧settle不能回退到当前全局controller；
- prepend前若不清除旧force/external/dirty flags，或清除flags后留下空frame，anchor写入会被迟到到底写覆盖；必须使用第5.6节prepare顺序；
- 为追求“一个DOM API”而重写P0.2 projection会扩大风险；验收单位是一个render transaction，不是一个MutationRecord。

### 11.2 回滚

若 viewport controller 出现生产兼容问题，允许回滚按钮和follow状态到“仅 near-bottom时即时跟随”的保守实现，但不得恢复每delta `scrollHeight` read/write，也不得恢复主线程同步canonical parse。若100 ms throttle影响交互，可在保持非重置和单frame transaction不变量下调整内部常量；不得通过resettable debounce或同步DOM热路径回滚。

## 12. Fresh-reader / execution-readiness check

- P0.2/P0.3已完成能力与P0.4剩余问题已区分，禁止重写parser、sanitizer、Worker协议或canonical lifecycle；
- 当前与目标调用链、全量直接transcript滚动路径和layout风险有具体文件；terminal/toast排除边界明确；
- controller接口、scheduler pair、geometry算法、frame重入、flush flags和pending fold规则明确；
- append、phase、commit、canonical owner、clear/reset、attach/replacement、thread/workspace/snapshot生命周期明确；
- earlier-page同步baseline/prepare/post-read/anchor是唯一受控frame例外，普通renderer只能表达follow或由`main.ts`表达force；
- UI结构、按钮语义、共享测试DOM、CSS token与smooth-scroll处理明确；
- 新增/修改/禁止文件列明，所有任务有RED/GREEN文件、精确命令和预期结果；
- scheduler/canceler配对、帧内重入、flush三类flags、replacement三种拒绝、旧canonical settle、thinking completion与分页竞态均有必测落点；
- final命令、`./test.py`内部状态读取、独立Worker chunk、既有tsc基线和禁止重复完整验证规则明确；
- 验收标准可由注入式fake scheduler/geometry、fake timers、静态scroll写入守卫和现有项目测试包装器执行。
