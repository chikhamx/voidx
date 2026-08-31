# Desktop Transcript Keyed Reconciliation — P1 技术规格

> **Status: Approved — independent execution-readiness review PASS; implementation not started.**
> **Date: 2026-08-31**
> **Audience: Human + LLM**
> **Independent review: PASS — `run_6f0bb6d1a46347a49e22b6fd039111f4` (ninth-round scoped final review).**

## TL;DR

本规格闭环 `docs/design/cross-ui-performance-addendum.md` 的 Desktop P1 keyed reconciliation 子项。Gateway capability negotiation、`stream_append_v1`、`workspace_patch_v1`、turn terminal metadata patch、revision-gap snapshot recovery 和 transcript page/window 已经实现；本轮不重复修改协议。

目标是在权威 `workspace.snapshot` 到达时，按稳定 key 对 transcript logical block 执行 keep、replace、insert、remove、move，而不是把“同 id 已存在”一律视为内容正确，也不得清空并重建整个 transcript。可见语义未变化的 block 必须保持 DOM identity；变化的 block 只原位替换自身；窗口外历史、pending-local、active/retained committed stream 和 canonical Worker 生命周期必须保持现有契约。

本轮不实现 transcript DOM window/virtualization、Gateway 协议、分页算法、Markdown grammar/Worker protocol、TUI 或 benchmark。DOM window 在 keyed reconciliation 验收通过后单独设计。

## 1. 当前行为与已完成基线

### 1.1 P1 协议与窗口基线已完成

以下能力已存在，不得重复实现或回退：

- `src/voidx/presentation/protocol/v2/incremental.py` 定义 `stream_append_v1`、`workspace_patch_v1`、`transcript_window_v1`；
- Gateway 按 client capability 独立编码 append delta、metadata-only `workspace.patch` 或兼容 full snapshot；
- 正常 turn terminal 广播对 incremental client 使用 patch，不构造完整 transcript snapshot；
- Frontend 对 patch revision gap 请求 `snapshot.requested`，正常 patch 不触碰 transcript DOM；
- `transcript.page`、windowed snapshot、bounded JSONL range read 和 preserve-live earlier-page prepend 已实现；
- legacy client、首次连接、显式 recovery 和 revision mismatch 继续使用权威 snapshot。

权威说明见 `docs/archive/gateway-transcript-window.md`。本轮不得修改协议 schema、capability 名称、Gateway 广播或 transcript persistence。

### 1.2 P0.2–P0.4 Desktop 基线

当前 Frontend 已具备：

- bounded live Markdown projection 与异步 canonical Markdown Worker；
- 每 stream 每帧最多一个 render transaction、100 ms 非重置 throttle 和 commit flush barrier；
- transcript viewport controller、48 px near-bottom follow、统一 external follow、force follow 和 lifecycle generation guards；
- detached historical page renderer、interaction-generation race guard 和同步 prepend anchor；
- active/committed stream、canonical owner 与 viewport controller replacement barrier。

Keyed reconciliation 只能组合这些能力，不能绕过 controller 直接写 transcript scroll，也不能让 snapshot replacement 接管仍活跃的 stream/canonical owner。

### 1.3 当前 `renderTranscript()` 已是部分 reconciliation

`frontend/src/utils/render.ts` 当前会：

1. 用 `[data-item-id]`、`[data-stream-id]`、`[data-compaction-item-id]` 收集现有元素；
2. 对已存在 id 直接跳过大多数节点；
3. 追加缺失节点；
4. 删除 full/window snapshot 中未出现且不在 retained committed 集合的节点；
5. 用 `reorderTranscriptNodes()` 移动 top-level block；
6. 对 assistant committed stream 做 id/text 去重。

该实现避免了常规全量清空，但仍有六个缺口：

- 同 id 节点的文本、status、elapsed、payload 或展示类型改变时，旧 DOM 通常被直接保留；
- assistant thought+answer、tool group+file card 等一个 snapshot node 可能对应多个 DOM 元素，没有统一 logical block ownership；
- production file-change `cards` 是模块级 cache，DOM 删除或替换不会自动失效 cache；
- `main.ts` 对每次已通过校验的 snapshot 都先调用 `resetTranscriptViewport()`，再移除全部 pending-local DOM、渲染 snapshot并重建 pending-local，因此当前并不保持同线程 pending-local identity；
- 同线程 recovery snapshot 与 thread/workspace replacement 使用同一 reset/render序列，没有为 keyed transaction 单独定义 controller和generation边界；
- identity、更新、删除、重排、windowed 保留和 lifecycle 组合缺少系统性测试。

## 2. 目标、非目标与成功标准

### 2.1 目标

1. 为每个可渲染 snapshot node 定义稳定 reconciliation key、可见语义 fingerprint 和 logical block。
2. unchanged block 零重建并保持 DOM identity；changed block 只替换自身；新增、删除和重排只触碰必要 block。
3. full snapshot 与 windowed snapshot 使用不同删除语义；窗口外历史不会因“本页未出现”被误删。
4. pending-local、active stream、retained committed stream 和 canonical settle 不被 snapshot reconciliation 误删、重复或抢占。
5. 所有 DOM mutation 收敛到一个 viewport keyed transaction，并保持 follow-preserving，不 force scroll。
6. file-change/tool/stream 模块级状态随 logical block 安装、替换和删除一致失效。
7. 失败时不留下半安装 block；无法证明安全时允许对单个 logical block rebuild，不允许清空整个 transcript。

### 2.2 非目标

- transcript DOM window、overscan、eviction、placeholder 或高度估算；
- Gateway、protocol schema、capability、snapshot/page DTO、revision 或 cursor 变更；
- earlier-page detached renderer 与 prepend 算法重写；
- Markdown parser、DOMPurify、Worker protocol、canonical grammar 或 syntax highlighting 变更；
- live stream throttle/frame transaction 变更；
- TUI、desktop Rust shell、P2 input/live-history；
- 统一 benchmark 和真实浏览器性能观测。

### 2.3 成功标准

对一个包含 `N` 个现有 block、`K` 个发生结构或可见语义变化的 snapshot：

- DOM create/replace/remove/move 数量与 `K + 新增 + 删除 + 必要移动` 相关；
- unchanged block 的根元素 identity 保持；
- 普通snapshot/recovery不执行`root.replaceChildren()`、`root.innerHTML = ...`或等价全量transcript清空；仅§6.2具名blocked helper例外；
- 正常 metadata patch 仍为 transcript 零 mutation；
- reconciliation 后 DOM 的 canonical 可见顺序与 snapshot 一致，保留集合除外；
- follow 状态、scroll ownership 和 canonical stale guards 与 P0.4 等价。

本轮不承诺 DOM 常驻数量有界；该指标属于后续 DOM window。

## 3. 数据模型

### 3.1 Descriptor、turn owner 与 canonical key

新增内部类型，放在新模块 `frontend/src/utils/transcript-reconciliation.ts`：

```ts
export type TranscriptReconciliationKey = string;

export interface TranscriptNodeDescriptor {
  key: TranscriptReconciliationKey;
  nodeId: string;
  nodeType: string;
  turnId: string | null;
  toolCallId: string | null;
  rendererShapeVersion: string;
  fingerprint: string;
  node: TranscriptNode;
}
```

`deriveTurnOwners(orderedNodes)` 只使用有序数组中的最近前置 turn：

1. 初始 `currentTurnId = null`；
2. 遇到 `node_type === "turn"` 时，要求非空且未重复的 `node.id`，随后令 `currentTurnId = node.id`，turn节点自身归属该turn；
3. 后续节点归属当前 `currentTurnId`，直到下一个turn；
4. turn前缀中的非turn节点owner为`null`；
5. `parent_id`只参与depth/可见语义，不覆盖线性turn owner；跨turn parent、缺失parent与循环parent属于malformed depth graph，在plan阶段失败；
6. page级`before_turn_id/after_turn_id`是整数边界，node/turn id是字符串；V1不在客户端比较两者，也不以其证明逐节点删除。

Key/compound规则按turn先分组，再生成block：

- 若一个turn（从该`turn`节点起到下一个`turn`前）包含任意`tool_call`或`tool_result`，该turn内**全部可渲染节点**、全部tool call/result、唯一turn file card统一为一个连续compound：`turn-with-tools:${turnId}`。中间message/thought/assistant/diff等不再拥有独立top-level block key，而作为有序member进入compound fingerprint；整个turn range共同keep/replace/move/remove。这样与production按`turnId`聚合唯一file card的事实一致。
- turn前缀若包含tool，所有前缀可渲染节点与`__unscoped__` file card统一为`prefix-with-tools` compound。
- 不含tool的turn：simple节点使用`node:${node.id}`；assistant的`thinking_text`与answer共属该simple block；连续standalone thought按最大连续run聚合为`thoughts:${turnId ?? "prefix"}:${firstThoughtId}`。
- `tool_result`必须在同一turn内引用已出现或同turn可定位的`tool_call_id`；缺失、跨turn引用或重复tool call id为malformed。
- file-change summary message在无tool turn中归属该message simple key；在tool turn中归属`turn-with-tools` compound。
- 重复node id、重复compound key、空id、一个top-level root被多个block认领，或tool turn roots不能构成从turn首个可见root到末个可见root的连续range，均在DOM mutation前失败。

参数化测试必须覆盖同一turn两个被message/thought分隔的tool run、两段均产生diff：只能生成一个turn compound和一张file card；任一run/member变化替换整个compound；full remove与reorder不留下共享/孤儿card。

### 3.2 Production render/fingerprint权威表

本表只描述`renderTranscript()` production snapshot入口；`renderNodeElement()`是generic node viewer，不是本轮production reconciliation renderer。以后production switch新增可见字段或node type时，必须同时更新本表和参数化测试。

| node type | production行为 / owner | 规范化fingerprint字段（固定顺序） |
|---|---|---|
| `root`, `startup`, `todo`, `permission`, `subagent` | 当前production switch跳过，不生成descriptor；不得因generic renderer可显示而在本批悄悄启用 | 无 |
| `turn` | 有可见`payload.text/raw_text`或header/body时为simple user/guidance block，否则只建立turn owner anchor | `shape`, `node_type`, `id`, `parent_id`, `status`, `collapsed`, `title`, `header`, `body_lines`, `payload.style`, `payload.text`, `payload.raw_text` |
| `message` | 按下方message shape dispatch；shape本身进入fingerprint | 公共字段 + `meta`, `elapsed`, `payload.role`, `payload.style`, `payload.raw_text`, `payload.title` |
| `assistant` | thought+answer compound | 公共字段 + `meta`, `elapsed`, `payload.raw_text`, `payload.thinking_text`, `payload.phase` |
| standalone `thought` | 连续thought compound | 每成员公共字段 + `meta`, `elapsed`, `payload.raw_text` |
| `tool_call`, `tool_result` | 所在turn的`turn-with-tools` compound；整个turn所有可渲染member共同fingerprint | 每成员公共字段 + `tool_call_id`, `elapsed`, `payload.tool_name`, `payload.label`, canonicalized `payload.args/raw_args`, `payload.raw_text`, `payload.summary`, `payload.diff_text`, `payload.detail`, success/error status |
| `diff` | simple diff block | 公共字段 + `payload.diff_text`, `payload.title` |
| `error`, `warn` | `suppressed-snapshot-notice`；snapshot/recovery不生成descriptor/root或toast | 公共字段 + `payload.raw_text`, `payload.style`（仅用于bookkeeping测试，不生成fingerprint block） |
| `status` | 仅`payload.outcome === "compacted"`生成compaction block；其他status跳过 | 公共字段 + `payload.outcome`, `payload.detail`, `payload.label`, `payload.ok` |
| `checkpoint` | simple details block | 公共字段 |

公共字段固定为：`rendererShapeVersion`、`node_type`、`id`、`parent_id`、derive后的`turnId`、`status`、`collapsed`、`title`、`header`、`body_lines`。`parent_id`与完整祖先链派生的depth都进入fingerprint，因此任一祖先关系变化会replace受影响block。

`args/raw_args`递归按object key排序；array顺序保留；`undefined`规范化为字段缺失，`null`保留。未知payload字段忽略。每个表中字段都必须有“一字段变化导致replace”的参数化测试；未知字段变化必须keep。

Fingerprint使用稳定JSON字符串，不要求hash库。DOM primary存储`data-reconcile-key`、`data-reconcile-fingerprint`、`data-reconcile-turn-id`和`data-reconcile-shape`。`rendererShapeVersion`当前固定为`production-v1`；historical为`historical-v1`，shape不同即使key/fingerprint语义相同也必须replace。

以下是**目标 production dispatch**，不是当前`renderTranscript()`前置file/style分流的描述。必须新增唯一纯函数`classifyProductionMessage(node)`；production snapshot、recovery、detached builder及分类测试都只调用该函数。`renderTranscript()`必须删除classifier前的`renderFileChangeSummary()`与thought/diff/error/warning分支，不得在classifier外二次决定message shape。Classifier先取`payload.raw_text`，缺失时使用现有header/body fallback，再按固定优先级分类：

| 优先级 | 条件 | shape / logical root |
|---|---|---|
| 1 | `parseTurnStats(text)` 成功 | `message-stats`，一个稳定 transcript root |
| 2 | `formatSwitchNotification(text)` 成功 | `suppressed-switch`，不生成 descriptor/root |
| 3 | `parseSessionChangeSummary(text)` 非空 | `message-file-summary`；无 tool turn 为 message block，有 tool turn进入整个 `turn-with-tools` compound |
| 4 | trim 后包含 `MCP connecting:`、`LSP setup failed:`、`LSP startup:`、`LSP warmup:`，或同时含 `→` 与 `warming...`/`ready`/`failed` | `suppressed-runtime-noise`，不生成 descriptor/root |
| 5 | style=`thought` | `message-thought`，参与相邻 standalone thought run；不调用 attached merge API |
| 6 | style=`diff` | `message-diff`，稳定 diff root |
| 7 | style=`error`/`warning` | `suppressed-snapshot-notice`，不生成 transcript root，也不创建/刷新 body toast timer；live `item` notification 的 toast 行为不变。本批有意停止 snapshot/recovery 重放旧 toast |
| 8 | style=`markdown`/`guidance`/`ansi`/`text`或其他 | `message-${style}`稳定root；复用当前 markdown/user/pre shape |

`error`/`warn` node type进入同一个classifier adapter并得到`suppressed-snapshot-notice`，不生成descriptor/root；它们只在live notification到达时显示toast。Suppressed node id仍进入成功snapshot的known/rendered bookkeeping，避免后续重复处理，但不进入DOM order。每个优先级必须有测试；组合测试至少覆盖同时命中stats/file-like（stats优先）、file-summary/runtime-noise（file优先），以及snapshot notice既不创建也不刷新`.notice-toast-region`。


### 3.3 Logical block

```ts
export interface TranscriptLogicalBlock {
  key: TranscriptReconciliationKey;
  fingerprint: string;
  rendererShapeVersion: string;
  turnId: string | null;
  roots: HTMLElement[];
  primary: HTMLElement;
  memberNodeIds: Set<string>;
  ownedToolCallIds: Set<string>;
  ownedFileChangeKeys: Set<string>;
}
```

约束：

- `roots`按显示顺序列出连续top-level transcript children；primary持有metadata；任一root最多归属一个block；
- assistant roots为可选thought + answer；
- standalone thought run是一个compound，production与historical builder都按成员列表构造一个root，不再让多个独立key共享merge root；live attached thought merge语义不在本批改变，但snapshot安装后metadata必须表达compound成员；
- `turn-with-tools`以该turn从首个到末个可见root的连续range为roots；其中可含user/message/thought/assistant/tool group/file card等，primary为首个root并持有整个turn compound metadata；
- 无tool turn中的summary message产生的独立file card与该message同block；tool turn中的summary与唯一turn file card归整个`turn-with-tools`；
- details的open/closed等纯UI状态不进fingerprint，replace时仅在相同shape和稳定子控件key可证明时迁移，否则使用默认值并由测试锁定。

### 3.4 Retained DOM 与 handoff token

以下元素不由snapshot absence直接删除：

- pending-local message：plan前由`pendingLocalMessages`提供按queue顺序排列的`{threadId,itemId,text,style,element}`只读descriptor；
- active stream；
- 通过新增只读`peekCommittedStreamsForSnapshot()`捕获的committed stream；
- windowed snapshot中的所有既有canonical block；
- conversation prompt等temporary UI。

`takeCommittedStreams()`是destructive API，plan阶段禁止调用。Owner变更使用reservation；不可逆canonical cancellation只允许出现在最终no-fail batch：

```ts
export interface CommittedStreamClaim {
  element: HTMLElement;
  streamId: string;
  streamGeneration: number;
  canonicalRevision: number;
}
export interface CommittedStreamReservation {
  token: CommittedStreamClaim;
  originalIndex: number;
}
export function peekCommittedStreamsForSnapshot(): readonly CommittedStreamClaim[];
export function reserveCommittedStream(expected: CommittedStreamClaim): CommittedStreamReservation | null;
export function validateCommittedStreamReservations(
  reservations: readonly CommittedStreamReservation[],
): boolean;
export function commitCommittedStreamReservationsNoFail(
  reservations: readonly CommittedStreamReservation[],
): void;
export function releaseCommittedStreamReservations(
  reservations: readonly CommittedStreamReservation[],
): void;
```

Peek只读；reserve只验证并锁定identity/generation/revision，不消费collection、不取消canonical Worker、不删除canonical text。Release只解除尚未提交的锁。`validate...`在最终batch前只读确认全部reservation仍有效。`commit...NoFail`的前置条件是同一同步call stack内validation刚成功；它只能调用新增的generation-only/no-callback primitive，更新预先存在的owner generation/token、committed collection与canonical-text map。它**禁止**调用`canonicalMarkdownCoordinator.invalidate()`/`invalidateAll()`、`invalidateCommittedStreamElement()`或任何会触发`settle/onSettled`、viewport follow、DOM removal、timer cancellation、分配、parse、render、用户callback或失败返回的API。旧Worker自然settle时由generation/owner guard丢弃。进入no-fail函数后不得再执行任何可抛步骤，也不承诺恢复已失效的Worker operation。Failed/stale plan只release reservation。测试断言final validation前冲突可rollback；batch同步执行期间`onSettled`与follow spy均为0；之后模拟旧settle仍不能安装或follow。

Pending handoff固定算法：只考虑同线程、规范化style相同（`text`对应canonical `user`，`guidance`对应`guidance`）、严格原始文本相同、canonical id此前未出现在`knownSnapshotUserNodeIds`的candidate；pending按queue顺序、candidate按snapshot顺序做FIFO一对一匹配。一个entry/node最多匹配一次。优先保留pending element identity并写入canonical metadata；shape不兼容时才replace。queue与known-id bookkeeping仅在apply成功后提交；old revision、known id、windowed absence、failed/stale plan均不消费。

## 4. Reconciliation plan

### 4.1 两阶段执行

必须先完整 **plan**，再单次 **apply**：

```ts
interface TranscriptReconciliationPlan {
  keep: TranscriptLogicalBlock[];
  replace: Array<{ current: TranscriptLogicalBlock; next: DetachedBlock }>;
  insert: DetachedBlock[];
  remove: TranscriptLogicalBlock[];
  order: Array<TranscriptReconciliationKey | RetainedDomKey>;
}
```

Plan阶段完全只读production state：

1. 校验node id、turn owner、depth graph、canonical/compound key与root唯一归属；
2. 通过peek API捕获committed/file reservation候选token，通过pending queue构造FIFO handoff；
3. 扫描现有top-level DOM并分类snapshot-owned、window-retained、pending、stream-retained、temporary UI；
4. 比较key、shape、fingerprint，决定keep/replace/insert/remove/order；
5. 在detached context构造全部next block和待安装cache state；
6. 预计算每一步apply操作及其逆操作所需数据；
7. 任一校验或detached render失败则丢弃plan，DOM、queue、revision和production cache零变化。

Apply在一个同步viewport wrapper中执行：

1. 重新校验thread/controller/viewport/reconciliation generation、snapshot revision及所有reservation候选token；
2. **在任何mutation前**构造完整rollback journal：每个旧root的`parent/nextSibling`、被替换/删除roots、待插入roots、旧metadata、owner token/state、pending与§6.4 bookkeeping snapshot；
3. reserve全部committed/file owner；任一冲突时release已取得reservation并以stale退出，DOM/bookkeeping零变化；
4. 执行DOM replace/insert/remove/move并安装detached metadata；
5. staging提交pending FIFO handoff与§6.4 SnapshotCommitSet；此时仍可按journal rollback；
6. 对全部committed/file reservation执行一次只读`validateOwnerReservationsBatch()`；失败则release并完整rollback；
7. 调用`commitOwnerReservationsBatchNoFail()`，内部按预验证结果一次性提交file state/generation、移除committed collection项、删除canonical text并推进canonical generation使旧Worker settle失效；该函数不得失败；
8. no-fail batch返回后transaction已提交，只释放journal并返回；此后不得执行render、parse、callback、断言或任何可抛步骤。

故障注入覆盖每个reserve、DOM operation、bookkeeping staging与final validation之前/之后，均须完整恢复DOM identity/order、production maps、committed collection顺序、pending queue、incremental stream state和accepted revisions。no-fail batch内部不设“失败后恢复Worker”的伪测试；改为断言batch无失败分支、旧Worker settle无法安装、file generation与committed collection一次性达到目标状态。

Apply不得调用attached-DOM append API逐节点构造；这些API会触发follow和production map副作用。必须使用detached builder/显式context。

### 4.2 Keep

当 key 与 fingerprint 都相同：

- 保留所有 logical roots identity；
- 不重新运行 Markdown、highlight、tool/file renderer；
- 不改变 `<details open>`、用户选择文本、内部滚动或其他纯 UI 状态；
- 允许 order 阶段移动整个 block；移动不算 replace。

### 4.3 Replace

当 key 相同但 fingerprint 不同：

- detached 构造完整 next logical block；
- apply 时以 next roots 原位替换 current roots；
- 旧block的stream/canonical/tool/file ownership必须先取得reservation；DOM/bookkeeping staging后统一final validation，再由no-fail batch提交，不得提前destructive invalidate；
- `<details open>` 等纯 UI 状态：相同控件语义和稳定子 key 时应迁移；无法安全迁移可使用 renderer 默认值，但必须有测试明确该选择；
- 不允许复用 stale child DOM 后只改 `textContent` 来绕过 Markdown/tool 状态更新。

### 4.4 Insert

新 key 使用 detached block。Apply 时按 canonical order 插入，不依赖“当前 root 最后一个 child”推断 turn。新 block 安装前不得注册到 production maps。

### 4.5 Remove

#### Full snapshot

`windowed === false` 时，snapshot-owned block 不在 descriptor 集合且不属于 retained DOM 分类，必须删除并失效 cache。

#### Windowed snapshot

`windowed === true` 时，V1 **不因absence删除任何既有canonical block**。当前DTO没有逐节点turn id，page boundary是整数而node/turn id是字符串，客户端不得推断二者可比较。Windowed plan只对本页出现的key执行keep/replace/insert；obsolete删除延后到后续authoritative full snapshot或DOM window规格。

`before_turn_id` / `after_turn_id` 只用于分页状态，不参与node key、turn owner、删除或DOM排序比较。

### 4.6 Reorder

Full snapshot以descriptor顺序为canonical backbone；compound roots整体连续移动，pending/temporary/active retained roots按classification的既有业务锚点保留，不clone unchanged block。

Windowed snapshot按不可跨越root分成contiguous segments：任何未出现在本页的canonical/retained/pending/active/temporary root都是segment boundary。

1. 每个existing page key固定归属其apply前segment；只允许segment内按descriptor投影排序；
2. 若descriptor顺序要求existing key跨segment（如`[page:A,outside:X,page:B]`而目标`[B,A]`），plan判stale并请求full recovery，不做mutation；
3. 新key只有在其前后最近existing descriptor anchor都位于同一segment，或仅一侧anchor唯一确定segment时才能插入；两侧跨segment或无anchor且DOM已有canonical roots时请求full recovery；
4. 页面全为新key且DOM没有canonical roots时插入canonical区域末尾、pending/active/temporary之前；
5. 不删除或跨越boundary。测试覆盖A/X/B、多个segment、跨segment冲突、单/双anchor和only-new页面。

## 5. Renderer 与 cache 边界

### 5.1 新模块职责

新增 `frontend/src/utils/transcript-reconciliation.ts`，负责：

- descriptor/key/fingerprint；
- existing DOM index 与 retained classification；
- plan/validate/apply；
- reconciliation test hooks。

`frontend/src/utils/render.ts` 继续负责节点/compound block 的 detached render adapter，并将 `renderTranscript()` 收敛为 orchestration，不在两个模块重复 node-type switch。

### 5.2 Detached production block builder

新增以下固定接口；实施不得另建第二套detached builder：

```ts
export interface TranscriptRenderContext {
  root: DocumentFragment;
  toolGroups: Map<string, HTMLElement>;
  tools: Map<string, HTMLElement>;
  fileChanges: HistoricalFileChangeContext;
  follow: "none";
}

export function renderTranscriptBlocksDetached(
  descriptors: TranscriptNodeDescriptor[],
): DetachedTranscriptBlocks;
```

可以复用 P0.4 historical renderer 的 context-local helper，但不能把 snapshot reconciliation 直接委托给 `renderHistoricalTranscriptPage()`：historical snapshot-only UI 与 production canonical block 语义必须继续明确，尤其是 assistant canonical Worker metadata、tool completion、file cards 和 future fingerprint metadata。

### 5.3 File-change cache

`frontend/src/utils/render-file-changes.ts`使用同一batch reservation协议：

```ts
export interface FileChangeCardToken {
  key: string;
  card: HTMLElement;
  generation: number;
}
export interface FileChangeCardReservation {
  key: string;
  previous: { token: FileChangeCardToken; state: FileChangeCardState } | null;
  next: FileChangeCardState | null;
  moduleEpoch: number;
}
export function peekFileChangeCard(key: string): FileChangeCardToken | null;
export function reserveFileChangeCard(
  key: string,
  expected: FileChangeCardToken | null,
  next: FileChangeCardState | null,
): FileChangeCardReservation | null;
export function validateFileChangeCardReservations(
  reservations: readonly FileChangeCardReservation[],
): boolean;
export function commitFileChangeCardReservationsNoFail(
  reservations: readonly FileChangeCardReservation[],
): void;
export function releaseFileChangeCardReservations(
  reservations: readonly FileChangeCardReservation[],
): void;
```

Reserve验证expected owner/module epoch并锁定key，但不改变card/state/generation；expected null可安全reserve不存在的key，next已在detached plan阶段构造完成。Validation只读且不分配。`commit...NoFail`只执行预验证后的map写入/删除与generation赋值，不调用renderer/callback且无失败返回；release只解除未提交锁。Detached builder只用context-local map；reset递增module epoch使旧reservation validation失败。Stream与file reservation由§4.1同一个`validateOwnerReservationsBatch()`和`commitOwnerReservationsBatchNoFail()`协调，禁止逐项可失败finalize。

### 5.4 Stream 与 canonical owner

- active stream、retained committed stream或canonical owner活跃时，普通apply不得绕过reservation直接替换其DOM；
- snapshot descriptor覆盖committed stream且fingerprint与canonical raw text等价时keep；
- fingerprint不同且canonical Worker仍pending时，reserve阶段保持旧operation有效；最终no-fail batch推进owner generation并移除旧canonical ownership，旧settle因guard失效；
- stale Worker settle不得写入detached/removed target，也不得请求follow；
- 不得削弱`stream.ts`的controller identity、viewport generation、stream generation、connected/containment guards。

只能新增§3.4/§5.3定义的窄peek/reserve/validate/no-fail-commit/release接口，以及内部`invalidateCanonicalOwnerGenerationOnlyNoCallback()` primitive。该primitive只比较并推进既有owner generation、删除owner/text引用，不调用coordinator invalidate、settle callback、DOM或viewport API。禁止在batch内复用当前`invalidateCommittedStreamElement()`，也不得从reconciler直接destructive handoff或导出整个内部map。

## 6. Viewport transaction 与失败语义

### 6.1 Controller 原子接线

现有`flushMutationNow()`在mutation前调用`takeFlags()`，因此不能在callback内再调用`requestFollowAfterExternalMutation()`来宣称同一transaction。V1允许对`transcript-viewport.ts`做一个窄、向后兼容的API扩展：

```ts
export interface TranscriptFlushOptions {
  followAfterMutation?: boolean;
}

flushMutationNow(
  key: object,
  mutate: () => void,
  options?: TranscriptFlushOptions,
): void;
```

固定时序：

1. 若`followAfterMutation === true && following === true`，在`takeFlags()`前把external intent并入本次flags；
2. 运行单次`runTransaction([mutate], flags)`；geometry仍只读一次且read-before-write；
3. mutate抛错时`runTransaction`不得执行follow scroll write，external intent视为已消费且不调度第二帧，异常向上传播给reconciliation rollback/recovery；
4. 离底用户的option是no-op；既有force flag优先级不变；
5. transaction active重入时，pending map必须同时保存mutate和options，下一帧执行，不能丢失intent。

`stream.ts`只暴露窄wrapper：

```ts
export function flushTranscriptReconciliationNow(mutate: () => void): void {
  viewportController?.flushMutationNow(
    transcriptReconciliationMutationKey,
    mutate,
    { followAfterMutation: true },
  );
}
```

无controller时同步调用mutate。普通同线程snapshot使用该wrapper；handler返回前DOM、pending handoff与revision commit已一致。`main.ts`之后仍执行既有显式full-snapshot force；这不属于reconciliation transaction，但属于已经存在且本批不改变的snapshot UX。Earlier-page与metadata patch不得使用该option。

### 6.2 Rollback 与失败语义

§4.1的rollback journal覆盖no-fail batch之前的全部普通apply步骤：

- detached/validation失败发生在wrapper前，production state零变化；
- no-fail batch前任一步失败时，callback内部同步rollback并release reservations，成功后再抛错；controller不写follow；
- rollback本身若失败，设置`reconciliationBlocked = true`、推进reconciliation generation、取消pending plan，拒绝patch/windowed/incremental/普通snapshot apply，并调用`requestSnapshotRecovery(threadId, { forceRetry: true })`；
- `snapshotRecoveryStates: Map<string, SnapshotRecoveryState>`替代当前`pendingSnapshotRequests: Set<string>`，value固定为`{ mode: "ordinary-gap" | "blocked"; requestedRevision: number | null; inFlight: boolean; retryAttempt: number; timerGeneration: number }`；`requestedRevision`记录发送请求时该thread最后成功接受的snapshot revision（无已接受revision为`null`），匹配response必须是当前thread authoritative full snapshot且`incomingRevision >= requestedRevision`（null仅要求合法非负revision）；同thread升级到`blocked`后不得降回ordinary；
- ordinary revision-gap仅在`inFlight === false`时发送并置true；收到当前thread authoritative full snapshot后，纯validation先确认thread、`windowed === false`、workspace/snapshot revision不旧，再将该次`inFlight=false`并取消timer；stale、duplicate或windowed response不结束in-flight；成功install删除state；
- blocked install失败时保持blocked，`retryAttempt += 1`，按`min(250 * 2 ** (attempt - 1), 4000)`毫秒调度重试；timer仅在thread仍active、socket/controller generation未变、mode仍blocked且自身`timerGeneration`匹配时发送并置`inFlight=true`，因此保留state不会阻止重发，也不会同步自旋；
- `main.ts`使用`onSocketChange(ws)`只处理transport对象换代：推进`socketGeneration`、卸载/失效旧transport listener并取消旧generation timer；它**不**把新对象视为已连接。对每个非null新transport注册generation-guarded `open`与`close` listener；若注册时`readyState === WebSocket.OPEN`，以同一guard异步执行一次open迁移；CONNECTING/CLOSING/CLOSED均不发送；
- 当前generation transport的`close`事件执行disconnect迁移：对**所有**recovery state置`inFlight=false`、取消timer并递增`timerGeneration`，保留`mode/requestedRevision/retryAttempt`；旧transport或重复close因socket generation/state guard为no-op。当前generation transport的`open`事件执行active-thread reconnect迁移：blocked按原attempt安排retry；ordinary-gap若`inFlight=false`则立即发送一次并置true。重复open、注册时OPEN检查与listener回调由`socketGeneration + inFlight`双重guard保证exactly-once；
- 当前`onSocketChange()`不得再无条件clear recovery state。测试分别模拟`_setSocket(new CONNECTING socket)`（open前零发送）、该socket `open`（exactly one resend）、重复`open`（仍一次）、同一socket `close`（复位in-flight）、旧socket迟到事件（no-op）与后续新socket `open`；
- thread/workspace replacement删除旧thread state并取消timer；blocked同时由replacement reset解除。成功blocked install删除state、取消timer并解除blocked。同revision full snapshot允许重试，因为accepted snapshot revision只在成功install提交；
- 测试覆盖ordinary gap request → disconnect → reconnect → exactly one resend → full snapshot success；以及第一次blocked install失败、退避后第二次同revision成功、stale/duplicate、disconnect/reconnect和thread switch取消旧timer。

Blocked full recovery只通过具名`installBlockedFullSnapshot()`执行，并固定为以下三阶段；它不使用已损坏的旧journal，也不承诺保留旧canonical或pending DOM identity。

#### Phase 1 — Prebuild（只读production）

入口纯validation再次确认当前thread、`reconciliationBlocked === true`、`windowed === false`、workspace/snapshot revision与socket/controller generation。随后在detached context一次性预构造：canonical + 未handoff pending roots组成的完整fragment、file/tool next cache、known/rendered sets、incremental next map、window/revision commit set、pending next queue、viewport/reconciliation next generation与recovery success state。任何renderer、parse、DOM create、array/map allocation均只能发生在本阶段；失败丢弃next state并按§6.2重试，production零变化。

#### Phase 2 — Blocked-only quiesce（replacement前，可失败但幂等）

本阶段只允许新增的具名blocked primitives；每个primitive必须可重复调用，并在中途异常后允许下一次full snapshot从Phase 1重新开始：

- `quiesceStreamsForBlockedInstallNoCallback()`：取消active stream timer/viewport mutation，推进stream/canonical generation，清active/committed/owner/text引用；**不得**调用`canonicalMarkdownCoordinator.invalidate/invalidateAll`、`settle/onSettled`、follow或移除transcript DOM；旧Worker自然settle时由guard丢弃；
- `quiesceFileToolCachesForBlockedInstallNoDom()`：推进cache epoch并脱离旧map引用；不得逐card `.remove()`；
- controller闭包新增单调递增、永不回退的`activityGeneration`。`enqueueMutation()`、有效`flushMutationNow()`（含transaction-active reentry）、`requestFollowAfterExternalMutation()`、`forceScrollToBottom()`以及任何接受新mutation、设置pending flag或安排callback的入口，都必须在接受work时先推进它；scroll/reset/dispose及其他会使旧blocked plan失效的入口也必须推进该值或已有且被token校验的generation。work执行、取消或消费后不得回退`activityGeneration`；
- `TranscriptViewportController.quiesceForBlockedInstallNoDom(): BlockedViewportQuiesceToken | null`：若`transactionActive === true`，不得改变该标志、pending flags、generation或当前调用栈状态，立即返回`null`；`validateBlockedInstallReady()`把null视为永久无效，本次blocked install停在Phase 2，并在外层transaction完整返回后重新调度Phase 1/2。仅当`transactionActive === false`时，quiesce才幂等取消私有frame handle、清空全部未知key的`pendingMutations`与全部transaction flags，推进callback/interaction/quiesce generation，并返回`{ controller, callbackGeneration, interactionGeneration, quiesceEpoch, activityGeneration }`token；不得读取geometry、写scroll、更新follow button DOM、运行pending mutation或其他callback；
- `TranscriptViewportController.validateBlockedQuiesceToken`是导出的窄只读validator；唯一入参类型为`BlockedViewportQuiesceToken`，返回`boolean`。它直接在controller闭包内验证controller identity及token中的四个generation/epoch仍与当前值相等，并确认`frameHandle === null`、`pendingMutations.size === 0`、全部transaction flags为false、`transactionActive === false`；不得变更generation、读取geometry、操作DOM或调用callback。任何quiesce后新work都会永久推进`activityGeneration`，因此即使frame/flush/follow已消费且controller重新空闲，旧token仍保持false。`validateBlockedInstallReady()`必须调用该validator，false则不进入Phase 3；
- `quiesceConversationPromptForBlockedInstallNoDom(): BlockedPromptQuiesceToken`定义在`frontend/src/ui/prompt.ts`：只推进prompt generation、令模块私有`activePrompt=null`并返回`{ generation, quiesceEpoch, previousRequestId }`token；不得调用`removeReplyControls()`、移除DOM、触发reply/follow/callback。Reply controls实际位于`prompt.element`内且随其附着于transcript，由Phase 3 replacement统一删除；旧button listener已有`activePrompt !== prompt`guard，quiesce后必须no-op；
- `validateBlockedPromptQuiesceToken`是`prompt.ts`导出的窄只读validator；唯一入参类型为`BlockedPromptQuiesceToken`，返回`boolean`。它校验当前prompt generation/quiesce epoch等于token且`activePrompt === null`；不得操作DOM、改变generation或调用callback。Quiesce后安装任何新prompt必须推进generation，使validator返回false；`validateBlockedInstallReady()`必须调用该validator，false则不进入Phase 3；
- 本阶段**禁止直接调用**现有`clearActiveStreams()`、`clearCommittedStreams()`、`invalidateCommittedStreamElement()`、`invalidateAllCanonicalOwners()`、`resetFileChangeCards()`、`resetTranscriptViewport()`与`resetConversationPrompts()`，因为它们包含DOM操作、coordinator settle callback或未证明无失败的组合副作用。

Phase 2完成后执行一次只读`validateBlockedInstallReady()`，确认thread/socket/controller generation、quiesce epochs和所有预构造引用仍匹配；失败不进入Phase 3，只保留blocked并重试。

#### Phase 3 — Single replacement + no-fail publish

`publishBlockedFullSnapshotNoFail(prebuilt)`是唯一允许调用`transcriptEl.replaceChildren(prebuilt.fragment)`的位置。`replaceChildren()`是本阶段唯一可能抛出的操作：若它抛出，不执行publish并保持blocked；若它返回，函数只把Phase 1已分配完成的array/map/set/cache/controller引用及number/boolean/string标量赋给production slots，不迭代、不分配、不删除DOM、不取消timer、不调用renderer/parse/Worker/coordinator/callback/assert/follow，也不含可注入失败点。提交内容按固定顺序为file/tool cache引用、stream/viewport generations、pending queue、known/rendered sets、incremental map、window/revisions、recovery state与最后的`reconciliationBlocked=false`；这些都是预构造引用或标量赋值。

Force follow不是Phase 3原子提交的一部分；helper成功返回后由caller以既有controller API best-effort请求。follow异常不得重新标记blocked或回滚已一致的transcript state。

| 状态 | Phase 1 prebuild | Phase 2 quiesce | Phase 3 publish / retry |
|---|---|---|---|
| canonical + pending DOM | 构造完整单fragment；未handoff pending按原queue顺序和正常anchor预置其中 | 不操作transcript DOM | 唯一`replaceChildren(fragment)`；抛出则blocked重试，返回后无其他DOM操作 |
| active/committed/canonical Worker | 捕获generation并准备next值 | no-callback primitive清引用并使旧settle失效 | 仅发布预构造generation；不恢复旧operation |
| pending queue | FIFO计算handoff与next queue，不改原queue | 不改queue | 发布next queue；pending旧element identity不保证 |
| known/rendered ids | 构造clear-and-seed sets，含suppressed及handoff ids | 不改旧sets | swap预构造sets |
| incremental state | 构造仅移除当前thread state的next map | 不改旧map | swap预构造map |
| file/tool cache | 构造context-local next cache | epoch推进并脱离旧引用，不remove DOM | swap预构造cache |
| viewport/reconciliation | 构造next generations/controller state | 取消pending work并推进quiesce epoch，不读写scroll | 发布next引用；follow留给成功返回后的best-effort调用 |
| window/revisions | 构造full-window与SnapshotCommitSet | 不提交 | 发布预构造state；replace抛出时同revision可重试 |
| conversation prompt | 构造不含transcript内prompt roots的fragment | 仅generation/`activePrompt` no-DOM quiesce并校验token；不清理任何DOM | transcript内prompt/reply controls随replacement删除；server live event可重建 |
| transcript外 dialog/request UI | 不读取、不纳入fragment或commit set | 不处理；`frontend/src/ui/dialog.ts`明确范围外 | 保持原有queue/dialog DOM与行为，不受blocked transcript install影响 |
| `threadTurnContexts` | 保留业务context值并捕获generation | 只推进使旧send/render plan失效的generation | 发布generation；不从DOM反推context |
| recovery/blocked | 匹配response后`inFlight=false`，预构造success state | blocked保持true | 最后删除recovery state并置blocked=false；replace抛出则递增attempt重试 |

测试逐个在Phase 1和每个Phase 2 primitive前/后、ready validation前注入异常，断言Phase 3未调用且下一次retry可幂等进入；在`replaceChildren()`入口注入异常，断言publish slots未变化。另以静态/spy测试证明Phase 3返回路径不存在replace后的故障注入边界，且上述禁用API、`settle/onSettled`、follow在Phase 2/3同步期间调用次数为0。普通snapshot/recovery运行时不得调用该helper。

### 6.3 Lifecycle generation

每个plan捕获：

- active thread id / thread context generation；
- transcript element/controller identity；
- transcript viewport generation；
- snapshot revision；
- reconciliation generation。

Apply前全部匹配才安装。thread/workspace switch、full reset、controller replacement或更新snapshot plan会失效旧plan。即使V1 plan同步构造，也必须保留generation guard，防止未来cooperative detached build或renderer callback引入stale安装。

### 6.4 Snapshot commit set

Snapshot validation、plan和apply之间不得提前提交以下状态：

| 状态 | validation/plan阶段 | apply成功commit | 失败/rollback后 |
|---|---|---|---|
| `lastWorkspaceRevision` | 只读旧值并校验incoming不旧 | 写incoming workspace revision | 保持旧值 |
| `lastSnapshotRevisionByThread` / `rememberSnapshotRevision()` | 不调用写入helper | 为active thread写incoming snapshot revision | 保持旧值 |
| `snapshotRecoveryStates`（替代`pendingSnapshotRequests: Set`） | 保留`requestedRevision/inFlight/retryAttempt/timerGeneration`；收到匹配full snapshot先令`inFlight=false` | install成功删除state并取消timer | install失败保持state、递增attempt并调度retry；不受已有token去重阻挡 |
| `transcriptWindows` | plan使用临时next window state | 写入`typedSnapshot`对应window state/page fields | 保持旧state |
| `renderedItemIdsByThread`与known snapshot user ids | 只构造临时next set；recovery不得提前delete | 普通snapshot按语义合并；recovery按snapshot执行replace/clear-and-seed | 保持旧set |
| `incrementalStreamStates` | 只计算当前thread中被snapshot覆盖的identity；stale workspace revision检查必须先于任何clear | recovery apply成功后清除被full snapshot覆盖的当前thread states | 保持原map/revision/text |
| `pendingLocalMessages` | 只读FIFO descriptors | 删除成功handoff的entries | 保持原queue与DOM |
| committed/file owner reservations | plan只peek；apply可reserve但不改变owner | final validation后由no-fail batch一次性提交 | final validation前失败只release；no-fail batch后无失败/rollback分支 |
| reconciliation generation/blocked flag | capture/validate | generation推进；blocked保持false | rollback成功仅generation失效；rollback失败设置blocked |

`main.ts`必须先完成纯validation并生成`SnapshotCommitSet`，将其交给plan；所有表中write只能在viewport apply callback的journal覆盖范围内执行。`snapshotForRendering()`若会写`transcriptWindows`，必须拆成纯compute与commit两个阶段，或新增纯helper；不得在plan前调用当前有副作用版本。Apply成功前也不得清除recovery去重token，保证同revision失败后可重试。

## 7. Snapshot 与分页集成

### 7.1 `workspace.snapshot`

`frontend/src/main.ts` 负责区分两条路径：

**同线程普通/recovery snapshot：**

1. 完成现有revision/thread/recovery guard；
2. 不调用`resetTranscriptViewport()`，不调用`removePendingLocalMessageElements()`；
3. 将active thread id、thread context generation、snapshot revision与pending-local descriptors传入`renderTranscript()`；
4. 在同步reconciliation apply成功后消费已handoff的pending queue entry，再更新empty state并维持现有显式full-snapshot follow策略；
5. apply失败或stale时pending DOM/queue与last accepted snapshot revision不得提前提交，并请求去重的snapshot recovery。

**thread/workspace replacement：**

1. 保持现有`clearCommittedStreams()`→`clearActiveStreams()`→`resetTranscriptViewport()`及file/prompt/thread state reset顺序；
2. 新transcript为空，仍通过同一descriptor/detached builder安装snapshot，但不要求旧DOM identity；
3. replacement完成后由`main.ts`执行现有force follow。

Revision gap recovery仍发送`snapshot.requested`，但去重与blocked retry统一由§6.2的`snapshotRecoveryStates`状态机管理。旧的`removePendingLocalMessageElements()`/`restorePendingLocalMessages()`可以保留为thread replacement或兼容helper，但同线程snapshot不得再以remove/recreate作为正常路径。

### 7.2 Earlier page

`renderHistoricalTranscriptPage()` 与 `loadEarlierTranscriptPage()`保持P0.4隔离，但metadata与page state必须形成失败安全事务：

1. 在完整、未过滤的page node顺序上先生成descriptor、turn/tool/thought compound membership与`historical-v1` metadata；existing id过滤只能在membership确定后标记整block keep/skip，不能拆散compound；
2. 在detached fragment上完成全部render、key/fingerprint/turn/member metadata安装与唯一性校验；任一步失败则DOM、anchor和`transcriptWindows`零变化；
3. 捕获response-time interaction token与prepend baseline后，执行唯一一次`insertBefore(fragment, firstChild)`并记录所有inserted roots；
4. 恢复anchor；成功后才提交page snapshot、boundary、loading和existing-id state；
5. insert或anchor restore失败时同步移除全部inserted roots、恢复旧scrollTop/page state并保持可重试；若DOM/anchor rollback失败，请求full recovery，不提交page state；
6. pending-local全程no-op，historical build不安装production stream/tool/file cache。

Historical root安装`data-reconcile-shape="historical-v1"`、key、fingerprint、turn owner与compound成员metadata。Production plan只有shape相同才keep；`historical-v1`与`production-v1`固定不兼容，因此authoritative production snapshot对同keyreplace。Historical thought/tool/file采用§3相同membership与整个turn-with-tools compound。测试覆盖metadata build failure、single insert failure、anchor failure rollback、overlap filter不拆compound和page state仅成功后commit。

## 8. 精确文件范围

### 8.1 新增

- `frontend/src/utils/transcript-reconciliation.ts`：descriptor、fingerprint、logical block index、plan与apply；
- `frontend/test/utils/transcript-reconciliation.test.ts`：descriptor/plan/apply/rollback测试；
- `frontend/test/utils/render-thought-items.test.ts`：explicit-root builder与thought compound测试（当前不存在）；
- `frontend/test/utils/render-notice-status.test.ts`：explicit-root diff/compaction builder测试（当前不存在）。

### 8.2 允许修改

- `frontend/src/utils/render.ts`：detached production block builder与`renderTranscript()` orchestration；
- `frontend/src/utils/render-file-changes.ts`：context-local build与identity-safe cache install/invalidate；
- `frontend/src/utils/render-tool-items.ts`：提取explicit-root/context compound builder与失效接口；
- `frontend/src/utils/render-thought-items.ts`：提取explicit-root thought builder与compound membership；
- `frontend/src/utils/render-notice-status.ts`：提取explicit-root diff/compaction builder；
- `frontend/src/utils/stream.ts`：窄reconciliation transaction、non-destructive committed peek/reserve/validate/release与no-fail batch commit封装；
- `frontend/src/utils/transcript-viewport.ts`：仅增加§6.1的`TranscriptFlushOptions`，以及§6.2的`BlockedViewportQuiesceToken`、单调`activityGeneration`最小接线、blocked-only quiesce与只读validator；
- `frontend/src/utils/types.ts`：内部descriptor/block类型若不放新模块；
- `frontend/src/utils/index.ts`：barrel export；
- `frontend/src/main.ts`：snapshot transaction、commit set、pending FIFO、transport open/close recovery与historical metadata接线；
- `frontend/src/ui/prompt.ts`：仅增加§6.2的`BlockedPromptQuiesceToken`、blocked-only no-DOM quiesce与只读validator；
- `frontend/test/utils/render.test.ts`、`render-file-changes.test.ts`、`stream.test.ts`、`transcript-viewport.test.ts`；
- `frontend/test/ui/prompt.test.ts`：新增blocked no-DOM quiesce、stale reply-control guard测试（当前不存在）；
- `frontend/test/main/main.test.ts`、`runtime-profile.test.ts`、`incremental-protocol.test.ts`；
- `frontend/test/ui/design-system.test.ts`：静态全量清空/scroll ownership guard；
- `docs/design/cross-ui-performance-addendum.md`：仅在实现fresh验证通过后写实施记录。

### 8.3 禁止修改

- `src/voidx/presentation/gateway/`、protocol models/schema和persistence；
- `frontend/src/utils/markdown.ts`、`markdown-renderer.ts`、`markdown-worker-protocol.ts`、`markdown.worker.ts`、`markdown-worker-client.ts`；
- `frontend/src/utils/transcript-viewport.ts`除§6.1 options扩展及§6.2 token类型、单调activity generation、blocked-only no-DOM quiesce/validator外不得改变controller语义；`frontend/src/ui/prompt.ts`除§6.2 token类型、no-DOM prompt quiesce/validator外不得改变prompt/live reply语义；
- `desktop/`、`tui/`；
- transcript page RPC、cursor/boundary算法和Gateway capability；
- 范围外用户改动与既有TypeScript基线错误。

## 9. TDD 实施任务

每个任务严格 RED→GREEN；先运行聚焦测试并读取 `./test.py` JSON `results[].status`。

### Task 1 — Descriptor 与 fingerprint

- 新增 `transcript-reconciliation.ts` 与测试；
- 覆盖所有当前可渲染 node type、稳定字段顺序、未知payload忽略、可见字段变化、重复key拒绝；
- 命令：

```bash
./test.py --frontend -- test/utils/transcript-reconciliation.test.ts -v
```

### Task 2 — Existing block index 与 plan

- 建立simple/assistant/tool compound/file card ownership；
- 覆盖keep/replace/insert/remove/order、root唯一归属和malformed DOM保守失败；
- full/windowed删除语义分别测试；
- 同一命令运行目标文件。

### Task 3 — Detached builder 与 cache handoff

- 提取detached production render context；
- 新增file/tool identity-safe install/invalidate；
- 覆盖构造期不触碰attached transcript、follow、production maps、stream/canonical owner；覆盖production/historical shape mismatch、thought/tool compound与file token conflict；
- 命令：

```bash
./test.py --frontend -- \
  test/utils/transcript-reconciliation.test.ts \
  test/utils/render.test.ts \
  test/utils/render-file-changes.test.ts \
  test/utils/render-thought-items.test.ts \
  test/utils/render-notice-status.test.ts -v
```

### Task 4 — Apply、stream owner 与viewport transaction

- 实现generation guards、同步flush、cache invalidation和follow-preserving apply；
- 覆盖unchanged identity、changed replace、stale plan、每个第N步apply failure rollback、rollback blocked state、canonical stale settle、committed/file owner reservation、atomic external-follow option与reentry；
- viewport测试覆盖quiesce取消未知key frame/pending/flags且不读写geometry/DOM；token初始validation为true；quiesce后新mutation frame已执行并重新空闲、同步flush已返回、follow flag已消费、force/reentry work结束后，旧token均因单调`activityGeneration`保持false；mutation callback内触发quiesce必须返回null、不改变`transactionActive`、validator为false且Phase 3不可达，外层transaction完成后重新quiesce才可通过；旧frame callback保持no-op；
- prompt测试覆盖quiesce只清`activePrompt`/推进generation且DOM identity不变，token初始validation为true；quiesce后安装new prompt使validator为false，旧reply listener保持no-op；transcript外dialog/request UI零变化；
- 命令：

```bash
./test.py --frontend -- \
  test/utils/transcript-reconciliation.test.ts \
  test/utils/stream.test.ts \
  test/utils/transcript-viewport.test.ts \
  test/ui/prompt.test.ts \
  test/utils/render.test.ts -v
```

### Task 5 — Snapshot、windowed 与pagination integration

- 接线`main.ts`；
- 覆盖full snapshot、windowed snapshot、revision recovery、pending-local identity、earlier-page metadata与anchor不回退；
- 命令：

```bash
./test.py --frontend -- \
  test/main/main.test.ts \
  test/main/runtime-profile.test.ts \
  test/main/incremental-protocol.test.ts \
  test/utils/transcript-reconciliation.test.ts -v
```

### Task 6 — 静态守卫与文档

- 禁止普通production snapshot/recovery全量清空和新增直接scroll writer；静态守卫仅允许§6.2具名blocked helper及既有thread/workspace replacement调用`replaceChildren()`；
- 禁止Gateway/Markdown/Worker范围漂移；
- fresh验证通过后更新performance addendum，整体仍保持`in-progress/partial`，因为DOM window、P2与统一benchmark/观测未完成。

## 10. 验证命令

按顺序执行：

```bash
# 1. Task聚焦集合
./test.py --frontend -- \
  test/utils/transcript-reconciliation.test.ts \
  test/utils/render.test.ts \
  test/utils/render-file-changes.test.ts \
  test/utils/render-thought-items.test.ts \
  test/utils/render-notice-status.test.ts \
  test/utils/stream.test.ts \
  test/utils/transcript-viewport.test.ts \
  test/ui/prompt.test.ts \
  test/main/main.test.ts \
  test/main/runtime-profile.test.ts \
  test/main/incremental-protocol.test.ts \
  test/ui/design-system.test.ts -v

# 2. 完整Frontend，只在最终验证阶段fresh运行
./test.py --frontend -v

# 3. Production build与类型检查
(cd frontend && npm run build)
(cd frontend && npx tsc --noEmit)

# 4. 静态检查
git diff --check
git status --short
python3 - <<'PY'
from pathlib import Path
import subprocess
allowed = {
  'docs/specs/desktop-keyed-reconciliation-2026-08-31.md',
  'docs/design/cross-ui-performance-addendum.md',
  'frontend/src/utils/transcript-reconciliation.ts',
  'frontend/src/utils/render.ts',
  'frontend/src/utils/render-file-changes.ts',
  'frontend/src/utils/render-tool-items.ts',
  'frontend/src/utils/render-thought-items.ts',
  'frontend/src/utils/render-notice-status.ts',
  'frontend/src/utils/stream.ts',
  'frontend/src/utils/transcript-viewport.ts',
  'frontend/src/utils/types.ts',
  'frontend/src/utils/index.ts',
  'frontend/src/main.ts',
  'frontend/src/ui/prompt.ts',
  'frontend/test/utils/transcript-reconciliation.test.ts',
  'frontend/test/utils/render.test.ts',
  'frontend/test/utils/render-file-changes.test.ts',
  'frontend/test/utils/render-thought-items.test.ts',
  'frontend/test/utils/render-notice-status.test.ts',
  'frontend/test/utils/stream.test.ts',
  'frontend/test/utils/transcript-viewport.test.ts',
  'frontend/test/main/main.test.ts',
  'frontend/test/main/runtime-profile.test.ts',
  'frontend/test/main/incremental-protocol.test.ts',
  'frontend/test/ui/design-system.test.ts',
  'frontend/test/ui/prompt.test.ts',
}
tracked = set(subprocess.check_output(
  ['git', 'diff', '--name-only', 'HEAD', '--'], text=True
).splitlines())
untracked = set(subprocess.check_output(
  ['git', 'ls-files', '--others', '--exclude-standard'], text=True
).splitlines())
changed = tracked | untracked
outside = sorted(changed - allowed)
assert not outside, f'changed paths outside allowlist: {outside}'
print('changed-path allowlist: PASS')
PY
```

验证规则：

- 必须读取`./test.py` JSON内部`results[].status`，不能只看shell exit code；
- build必须继续产出独立`markdown.worker-*.js` chunk；
- fresh tsc允许的唯一既有基线是：`src/services/connection.ts(121,3) TS2349`；`test/ui/design-system.test.ts(1,43) TS2591`、`(2,25) TS2591`、`(5,24) TS2591`、`(52,44) TS2591`、`(52,75) TS7006`；`test/ui/theme.test.ts(38,25) TS2352`。数量、path、line或code变化均需调查，本批文件不得新增诊断；
- 先聚焦、后完整；完整Frontend只在最终verify阶段fresh运行一次；
- `git status --short`、tracked diff与untracked path的并集必须全部位于§8 allowlist；allowlist脚本输出`PASS`，任何范围外path均失败；
- 禁止目录/协议/Markdown Worker路径未列入allowlist，因此其输出必须为空；
- 不因本批修改或删除范围外测试/实现使结果变绿。

## 11. 验收标准

实施完成前保持未勾选：

- [ ] unchanged simple、assistant、standalone thought run和`turn-with-tools` compound保持DOM identity，且不重新运行Markdown/tool/file renderer；
- [ ] 同key可见语义变化只原位替换该logical block，不清空transcript；
- [ ] insert、remove、reorder只操作必要compound roots，canonical顺序正确；
- [ ] duplicate/empty key、malformed depth/turn/tool ownership、root多重归属与detached render failure在production mutation前失败；
- [ ] full snapshot删除obsolete canonical block；windowed snapshot不因absence删除任何既有block、不比较整数page boundary与字符串turn id，且不跨越窗口外block排序；
- [ ] assistant thought+answer、连续standalone thought run与整个`turn-with-tools` compound各自满足root唯一ownership和共同keep/replace/move/remove；
- [ ] production/historical shape version不兼容时同key replace，historical build仍不污染production maps/follow；
- [ ] file cache与committed stream先reserve并全量只读validation，再由generation-only/no-callback batch一次性提交；validation前失败只release，batch同步期间`onSettled`/follow为0，之后旧Worker settle失效且无rollback分支；
- [ ] pending-local按同线程/style/严格原文与fresh canonical id做双FIFO一对一handoff，identity/顺序/queue提交时点正确；
- [ ] no-fail batch前第N步异常完整rollback DOM/cache/reservation/pending/revision/incremental state；rollback失败后当前线程full snapshot走具名blocked install，第一次失败后按退避重发并由第二次同revision解除；
- [ ] SnapshotCommitSet中的workspace/snapshot revision、recovery install state、window state、rendered/known ids、pending queue与`incrementalStreamStates`只在apply成功后提交；transport层`inFlight/timerGeneration`仅按§6.2接收/重试状态机变化；
- [ ] stale plan、thread/workspace/reset/controller replacement和stale Worker settle不安装、不follow；viewport/prompt quiesce token由各模块窄只读validator验真，quiesce后新work/new prompt使ready validation失败；
- [ ] reconciliation使用`followAfterMutation`原子flush：flags在transaction前捕获、read-before-write、离底no-op、force优先、异常不滚动且不产生第二帧；
- [ ] normal `workspace.patch` transcript零mutation；ordinary revision-gap保持去重；transport replacement为CONNECTING时不发送，当前transport close复位in-flight，open对active thread exactly-once重发，重复/旧transport事件no-op，full snapshot成功清除state；
- [ ] earlier-page detached isolation、interaction token、response-time baseline、单次prepend和anchor保持不回退；
- [ ] 普通production snapshot/recovery无`replaceChildren`/`innerHTML`式全量清空；仅`reconciliationBlocked === true`且validated authoritative full snapshot可调用具名`installBlockedFullSnapshot()`一次性replacement；无新增直接transcript `scrollTop`写入；
- [ ] Gateway、protocol、Markdown Worker/grammar、TUI、DOM window和范围外用户改动未混入或回退；
- [ ] 聚焦与完整Frontend fresh通过，build保留Worker chunk，`git diff --check`通过，本批无新增TypeScript诊断；
- [ ] performance addendum记录fresh计数并继续保持`in-progress/partial`。

## 12. 风险与回滚

### 12.1 主要风险

- tool group与file card是多节点聚合，ownership错误会导致重复DOM或误删相邻turn；
- snapshot同id内容变化可能与pending canonical Worker settle竞争；
- windowed absence不是删除证据，错误删除会造成不可恢复的客户端历史缺口；
- detached builder若复用attached renderer副作用，会提前污染production maps或follow；
- fingerprint遗漏可见字段会保留stale DOM，加入非可见字段会造成无意义replace；
- apply中异常可能产生DOM/cache不一致，因此prebuild和identity-safe handoff是硬约束。

### 12.2 回滚边界

实现必须保持`renderTranscript(root, snapshot)`公开入口。若新reconciler未通过验收，可回滚新模块与接线，恢复当前部分reconciliation；不得回滚P0.2–P0.5、Gateway incremental/window能力、historical prepend隔离或范围外用户改动。

普通snapshot/recovery不得以`replaceChildren()`作为fallback，仍须逐block reconciliation。唯一例外是`reconciliationBlocked === true`且当前thread authoritative full snapshot已完成纯validation与detached build时，具名`installBlockedFullSnapshot()`可执行一次destructive replacement；静态守卫只允许该helper内这一处调用，运行时守卫证明普通路径不可达。Thread/workspace replacement沿用既有clear语义。

## 13. 后续工作

本规格通过实现与fresh验证后，下一项才是 transcript DOM window：定义可见turn窗口、overscan、高度/anchor、eviction与rehydration。DOM window必须复用本规格的stable key、logical block和cache ownership，不得另建第二套节点身份系统。
