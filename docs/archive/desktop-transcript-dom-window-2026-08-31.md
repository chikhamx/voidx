# Desktop Transcript Continuous DOM Window — P1 技术规格

> **Status: Done** — Implemented after automatic verification, real Chrome page-only smoke, and independent focused review.
> **Date: 2026-08-31**
> **Audience: Human + LLM**
> **Review:** Three independent review rounds resolved all substantive findings; final focused TypeScript/signature/path check PASS.
> **Depends on:** `61247342` (`feat(frontend): make transcript reconciliation transactional`)

## TL;DR

本规格闭环 `docs/design/cross-ui-performance-addendum.md` 中尚未完成的 Desktop transcript DOM window/virtualization 子项。在不修改 Gateway、协议或 transcript persistence 的前提下，Frontend 保留已加载 canonical transcript model，只限制 attached DOM：按完整 logical block、viewport 像素预算和小幅 overscan 双向物化/裁剪，并用 spacer 与 anchor journal 保持连续滚动。

用户在已加载历史范围内必须可以连续上下滚动，不出现“返回最新”分段或重新请求已加载页面；尚未加载的更早历史继续使用既有 `transcript.page(before_turn_id)`。active stream、pending canonical Worker、pending-local、prompt/request 和其他无法安全移交的 owner 必须 pin；无法证明安全时允许暂时超出窗口预算，不得误删 owner、清空 transcript、强制回到底部或留下半提交窗口。

本轮不新增向后分页 API，不修改 Markdown grammar/Worker protocol，不实现 P2 输入或 durable live-history eviction，也不交付统一跨端 benchmark。真实浏览器滚动 smoke 属于本轮验收，但项目没有现成 Playwright/Puppeteer 依赖，因此 V1 使用可复现人工 smoke，不新增浏览器自动化依赖。

## 1. 已完成基线与当前缺口

### 1.1 不得重复实现或回退的能力

以下能力已经完成，是本规格的前置不变量：

- `stream_append_v1`、`workspace_patch_v1`、`transcript_window_v1` capability 与 Gateway 编码分支；
- `transcript.page(thread_id, before_turn_id, turn_limit)`、windowed snapshot 和 bounded JSONL range read；
- bounded live Markdown projection、异步 canonical Markdown Worker 和 stale settle guards；
- 每 stream 每帧 mutation transaction、100 ms trailing throttle 和 commit flush barrier；
- transcript viewport controller、48 px near-bottom follow、return-to-bottom 和 interaction generation；
- stable reconciliation key、visible fingerprint、multi-root logical block 和 full/windowed absence 语义；
- committed stream/file-card owner reserve → validate → DOM staging → final validate → no-fail commit；
- ordinary revision-gap recovery、blocked authoritative full replacement 和 pagination rollback；
- pending-local FIFO handoff、structured turn shape replacement 和 preserve-live earlier-page prepend。

权威规格：

- `docs/archive/gateway-transcript-window.md`
- `docs/specs/desktop-keyed-reconciliation-2026-08-31.md`
- `docs/design/cross-ui-performance-addendum.md`

### 1.2 当前缺口

当前 Frontend 只会向上加载并 prepend 更早页面：

- `frontend/src/main.ts` 的 `transcriptWindows` 持续合并已加载 nodes；
- `frontend/src/utils/render.ts` 会为当前需要的 snapshot/page 构造完整 detached logical blocks；
- attached DOM 随已加载历史持续增长；
- 没有 block 高度缓存、attached range、spacer、远端裁剪或内存内重新物化；
- `transcript.page` 只支持 `before_turn_id`，不存在向后分页 API。

因此 V1 必须把“canonical model 是否保留”与“DOM 是否 attached”分离。已加载 nodes 保留在当前 thread 的内存 window state 中；virtualization 只改变 attached roots 和 spacer，不把已加载历史重新交给网络。

## 2. 目标、非目标与成功标准

### 2.1 目标

1. attached transcript DOM 的正常规模受 `viewport + overscan + pinned islands` 约束，不与已加载历史长度线性增长。
2. 已加载范围内连续双向滚动；从顶部/底部 spacer进入被裁剪区时同步重新物化，不显示分段按钮或空白断层。
3. 裁剪、物化、reconciliation 和 pagination 始终以完整 logical block 为单位，不拆 multi-root compound。
4. spacer 高度与 anchor journal 保持用户当前内容的视觉位置；后台更新不得 force scroll。
5. active/pending owner 不被虚拟化误删、重复、抢占或提前 settle。
6. full/windowed snapshot、blocked recovery、thread switch/reset 和 earlier-page pagination 保持现有原子失败语义。
7. 高度测量、owner reservation 或 DOM staging 无法证明安全时，保持现有 DOM并允许暂时超预算。

### 2.2 非目标

本轮明确不实现：

- Gateway、protocol schema、capability、broadcast 或 persistence 改动；
- `transcript.page` 的 `after_turn_id`/向后请求；
- 从内存中驱逐已加载 canonical nodes；
- TUI live-history eviction；
- P2 input/paste/candidate 优化；
- Markdown grammar、sanitization 或 Worker protocol变化；
- 统一跨端绝对 benchmark；
- 新增 Playwright、Puppeteer 或其他浏览器测试依赖。

### 2.3 可验证成功标准

- 合成 10,000 logical blocks 时，attached 非 pinned block 数量由窗口预算决定，不随 10,000 线性增长；
- viewport 高度变化后，窗口重新收敛到新像素预算；
- 在已加载范围内向上、向下连续穿越多个窗口，block 顺序、identity、内容和 anchor正确；
- 单个超高 block、multi-root tool/file compound 和 pinned island 不被拆分；
- near-bottom live append继续 follow；查看历史时 live append不抢滚动位置；
- existing full Frontend suite、production build、批准的 TypeScript基线、changed-path allowlist 与真实浏览器人工 smoke均通过。

## 3. 核心不变量

### 3.1 Canonical model 与 attached DOM 分离

每个active windowed thread必须有一个单一canonical model。新增纯函数：

```ts
type CanonicalMergeMode = "full" | "windowed" | "earlier-page";

type CanonicalMergeResult =
  | { status: "merged"; snapshot: TranscriptSnapshot; descriptors: TranscriptNodeDescriptor[] }
  | { status: "stale"; reason: string; recovery: "ordinary" | "blocked" };

mergeCanonicalTranscript(
  current: TranscriptSnapshot | null,
  incoming: TranscriptSnapshot,
  mode: CanonicalMergeMode,
): CanonicalMergeResult;
```

固定规则：

1. 对`current.nodes`和`incoming.nodes`分别完整调用`buildTranscriptDescriptors()`；duplicate node id、空id、malformed compound或descriptor key重复立即stale，不提交任何model/DOM/window state。
2. merge原子单位是descriptor；descriptor的`memberNodeIds`必须完整保留。不得先逐node过滤再建descriptor。
3. **full**：incoming descriptors完整替换current；revision和全部window metadata取incoming。失败进入blocked recovery，因为authoritative full install已无法安全解释。
4. **初始windowed**（current为null或不同thread）：incoming成为canonical model；metadata全部取incoming。
5. **后续windowed**：以descriptor key匹配。matching key只有在member-id集合完全相等时才原位替换；集合部分相交、同member id属于不同key或incoming key顺序无法作为current key序列的一个连续有序子序列时stale并进入ordinary recovery。incoming-only descriptors只允许插入在两个可锚定matching neighbors之间，或matching range的首/尾边界；没有任何matching key时，只允许incoming的`before_turn_id..after_turn_id`与current window游标证明其位于current之前或之后，否则ordinary recovery。absence保留current descriptors。
6. **earlier-page**：incoming先完整建descriptor。若incoming descriptor任一member id已存在于current，则该descriptor必须与current中单一descriptor的key和完整member-id集合相同，随后整block skip；partial overlap为stale且不提交page。所有incoming-only descriptors必须整体前插在current首descriptor之前，并保持incoming顺序；若page的`after_turn_id`不小于current的`before_turn_id`且又没有完整overlap anchor，视为无法锚定并stale。page失败只丢弃response，不升级snapshot recovery。
7. merged `snapshot.nodes`只能由最终descriptor顺序`flatMap(descriptor.memberNodes)`生成，禁止单独维护node顺序。
8. windowed merge的`revision`取incoming；已加载范围游标取并集：`before_turn_id`取可证明的最早边界，`after_turn_id`取可证明的最晚边界；`has_earlier`来自最早边界所属页面，`has_later`来自最晚边界所属页面。无法证明边界来源则stale。
9. successful merge只产生candidate model；revision、model和window state必须与DOM transaction在同一commit set发布。

`buildTranscriptDescriptors(snapshot.nodes)`始终是logical block顺序和compound membership的唯一来源。DOM裁剪不得从canonical model删除节点；full snapshot删除是唯一可移除旧canonical descriptor的普通路径。

### 3.2 Logical block 原子性

一个 `TranscriptNodeDescriptor` 对应一个 logical block。以下操作都必须以整个 block 为单位：

- 测量；
- attached/omitted 分类；
- detached materialization；
- owner reservation；
- DOM insertion/removal；
- height cache更新；
- anchor选择；
- rollback。

不得逐 node、逐 root或逐 tool member裁剪 compound。

### 3.3 Pixel budget，而非固定 turn 数

窗口主预算按像素定义：

```ts
interface TranscriptDomWindowBudget {
  overscanViewportsBefore: number; // V1 default: 3
  overscanViewportsAfter: number;  // V1 default: 3
  maxAttachedUnpinnedBlocks: number; // V1 default: 240
}
```

`maxAttachedUnpinnedBlocks`只防止高度未知/零高导致无界物化，不得替代像素预算。单个超高block即使超过预算也完整attach。

canonical坐标由ordered descriptor extents的前缀和唯一确定。每个descriptor的estimated extent必须为有限正数；未知值使用同shape中位数，无样本时使用`DEFAULT_BLOCK_ESTIMATE_PX = 96`。planner不读取DOM；调用方先把实测/估值归一化后传入。

### 3.4 安全优先于预算

以下任一条件成立时，不得为满足预算强行裁剪：

- block 有 active stream、pending canonical Worker 或 retained committed owner；
- production file/tool owner无法成功reserve；
- pending-local或request/prompt DOM无法映射到可重建canonical block；
- block extent无法得到有限非负高度且没有安全估值；
- viewport/context generation已变化；
- DOM children与plan source identity不一致；
- transaction内发生reentry或外部mutation。

结果可以暂时超预算，但必须保持正确DOM、owner和scroll状态。

## 4. 数据模型

### 4.1 Thread window state

在新模块`frontend/src/utils/transcript-dom-window.ts`定义状态、canonical merge和纯规划函数；`main.ts`只持有每thread controller/state引用。

```ts
interface TranscriptDomWindowState {
  threadId: string;
  snapshot: TranscriptSnapshot;
  descriptors: TranscriptNodeDescriptor[];
  heights: Map<string, number>;
  estimates: Map<string, number>;
  attachedKeys: Set<string>;
  pinnedKeys: Set<string>;
  spacerSegments: TranscriptSpacerSegment[];
  generation: number;
  loadingEarlier: boolean;
}

interface TranscriptSpacerSegment {
  startIndex: number;       // inclusive descriptor index
  endIndex: number;         // exclusive descriptor index
  omittedKeys: string[];    // exactly descriptors[startIndex:endIndex]
  canonicalStartPx: number;
  canonicalEndPx: number;
  cssHeightPx: number;
}
```

`omittedKeys`必须是descriptor顺序中的连续区段。pinned island可把省略范围分成多个spacer，不能假设只有top/bottom两个spacer。state发布前必须验证`descriptors.flatMap(memberNodes)`与`snapshot.nodes`逐identity/id顺序一致。

### 4.2 Attached islands与完整layout序列

attached DOM可以包含viewport附近主窗口、分离的pinned owner islands、pending-local/conversation prompt等external retained roots，以及分隔它们的spacers。DOM中的真实滚动坐标不能只由canonical descriptors推导，因为external roots同样占用高度和flex gap。

planner输入使用完整有序layout entries：

```ts
type TranscriptLayoutEntry =
  | { kind: "canonical"; key: string; descriptorIndex: number; extentPx: number }
  | { kind: "external"; id: string; element: HTMLElement; extentPx: number; insertion: TranscriptExternalInsertion };

type TranscriptExternalInsertion =
  | { edge: "before-all" | "after-all" }
  | { beforeKey: string }
  | { afterKey: string };
```

调用方按当前top-level DOM与canonical metadata解析external root的位置：

- 位于两个canonical blocks之间时，记录`afterKey`为左邻block；
- leading/trailing root使用edge；
- 连续多个external roots保持source child顺序；
- 同一external root不能同时拥有两个位置；锚点key消失或位置歧义时defer destructive trim。

完整layout坐标是entries顺序的extent前缀和，**每两个相邻layout entries之间都加一个container row gap**。spacer不是planner输入entry；它只是连续omitted canonical entries的DOM表示。external entries始终attached/pinned，不生成spacer，也不计入`maxAttachedUnpinnedBlocks`。

因此真实`scrollTop`直接映射完整layout坐标，而非纯canonical坐标。canonical spacer高度仍只替代其omitted canonical区段；external root自身extent和它与邻entry的边界gap由真实DOM保留。external extent变化会使layout dirty并触发replan/anchor correction。

数据模型允许多个retained islands，每两个attached islands之间至多一个spacer。spacer不是logical block，不得拥有reconcile key，不得被`collectExistingTranscriptBlocks()`当成retained legacy content；使用专用`data-transcript-spacer` metadata并由collector显式识别。

### 4.3 Height cache与flex gap数学

设container computed `rowGap = G`。block自身测量口径固定为：

```text
blockExtent = lastRoot.bottom - firstRoot.top
```

它包含multi-root block内部root之间浏览器实际产生的gap，不包含block与相邻logical block的外部gap。single-root就是该root的border-box高度。

规划canonical坐标时，每个block贡献`blockExtent`，相邻两个descriptor之间另加一个`G`。因此descriptor区间`[i,j)`在完整DOM中的目标贡献为：

```text
segmentContribution = sum(blockExtent[k], k=i..j-1) + G * max(0, j-i-1)
```

用一个spacer替换该区间时，flex container会自动在spacer与attached邻居之间产生外部gap。该外部gap本来就是省略区间与邻居之间的边界gap，不属于`segmentContribution`，所以固定采用：

```text
cssHeightPx = segmentContribution
```

具体情形：

- leading spacer：DOM自动产生spacer→右邻居的1个边界gap；height仅为省略blocks及其内部gap；
- trailing spacer：DOM自动产生左邻居→spacer的1个边界gap；同上；
- interior spacer：DOM自动产生左右2个边界gap；同上；
- 全部canonical blocks省略：只有spacer，无外部gap；height等于全部blocks及其内部gap；
- pinned island切段：每段独立按上述公式，边界gap由flex自动提供。

禁止把边界gap加入blockExtent或cssHeight，否则会双算。测试必须以固定`G`对leading/trailing/interior/all-omitted/multi-root断言具体数值。

缓存规则：

- 只接受有限正数；真实零高block视为未知并使用估值；
- attached mutation、container宽度/font变化、Worker settle、tool collapse/expand使对应key dirty；
- materialize后用实测替换估值，并通过anchor delta修正scroll；
- estimator只影响窗口与spacer，不影响canonical顺序、merge或owner决策。

## 5. Window planning

### 5.1 唯一纯planner契约与确定性算法

```ts
interface TranscriptDomWindowViewport {
  scrollTop: number;
  clientHeight: number;
  following: boolean;
}

interface TranscriptDomWindowPlannerInput {
  entries: readonly TranscriptLayoutEntry[]; // complete ordered layout, no spacers
  attachedKeys: ReadonlySet<string>;
  pinnedKeys: ReadonlySet<string>;
  rowGapPx: number;
  budget: TranscriptDomWindowBudget;
  viewport: TranscriptDomWindowViewport;
  activation: "initial" | "replan";
  anchorKey: string | null; // preselected by caller using §5.2 DOM geometry
}

interface TranscriptDomWindowPlan {
  materializeKeys: string[];
  trimKeys: string[];
  nextAttachedKeys: Set<string>;
  spacerSegments: TranscriptSpacerSegment[];
  anchorKey: string | null;
  overBudgetReason: string | null;
}

planTranscriptDomWindow(
  input: TranscriptDomWindowPlannerInput,
): TranscriptDomWindowPlan;
```

`entries`中的extent必须已经归一化为有限正数；canonical未知值使用同shape中位数或96px fallback，external root必须来自本次DOM实测，无法测量时defer destructive trim。`rowGapPx`必须有限非负。

确定性算法：

1. 按完整entries顺序建立layout前缀坐标，相邻entries加`rowGapPx`；canonical和external都影响坐标。
2. initial且following=true时目标viewport固定到layout尾部：`bottom=totalLayoutExtent`、`top=max(0,bottom-clientHeight)`。replan使用输入`scrollTop`；非following恢复位置由调用方已发布anchor/scroll state体现，不由planner猜测。
3. `clientHeight <= 0`时不trim现有attached；若尚无attached，只选择最后1个canonical block加全部pinned canonical keys，external entries始终保留。
4. 正常目标区间为`[max(0, top-beforeOverscan*V), min(total, bottom+afterOverscan*V)]`。选择layout extent与该区间相交的完整canonical blocks，再与pinned keys取并集。
5. safety cap只作用于非pinned canonical blocks。先保留与真实viewport相交者；其余候选按排序元组`[distancePx, descriptorIndex]`选择，其中区间`[a,b]`到viewport`[top,bottom]`的`distancePx = max(0, top-b, a-bottom)`。viewport相交blocks即使超过cap也全部保留。
6. 单个超高block只选一次且完整保留。external entries不计cap但可把canonical omitted range切成多个segments。
7. `materializeKeys = target-attached`、`trimKeys = attached-target`，均按descriptor index排序；输入anchor key永不进入trim。
8. 仅对layout中连续omitted canonical entries生成spacer。被external entry隔开的omitted ranges必须生成不同segments，即使descriptor index连续。
9. anchor为空且存在destructive trim时，返回`overBudgetReason="anchor unavailable"`并令`trimKeys=[]`；可以materialize。
10. planner不得读DOM、选择anchor、推断external位置或依赖当前spacer elements。

示例（`G=12`,`V=100`,overscan=1，仅为表格简化）：

| entries | viewport | pinned | target |
|---|---|---|---|
| canonical A..D各50 | top=100,height=100 | none | 与layout 0..300相交的A..D |
| canonical A=500,B=50,C=50 | top=0,height=100 | none | A |
| A50, external P80, B50, C50 | top=130,height=100 | none | 按包含P和全部gaps的layout坐标选择B/C，P始终attached |
| A..F各50 | top=200,height=100 | B | B及目标区间内canonical blocks |
| A..10000各96 | initial following,height=600 | none | layout尾部viewport及前3V overscan，受240 cap |

scroll进入spacer时，spacer维持omitted canonical entries在完整layout中的贡献；planner以真实`scrollTop/clientHeight`映射完整layout前缀坐标，无需从spacer内部反推DOM子节点。

### 5.2 Anchor与source identity

transaction source snapshot固定为开始时`Array.from(transcript.children)`，包含canonical roots、spacers、pending-local和所有retained roots；initial/final source validation逐位置使用`===`。

anchor优先级：

1. viewport内第一个完整可见、非spacer、非pending-local root所属block；
2. viewport内第一个部分可见logical block；
3. following=true时最后attached canonical block的bottom anchor；
4. 无canonical block时不写scroll。

```ts
interface TranscriptWindowAnchorJournal {
  key: string;
  oldRoot: HTMLElement;
  offsetFromViewportTop: number;
  scrollTop: number;
  scrollHeight: number;
  interactionGeneration: number;
}
```

anchor key默认禁止进入`trimKeys`。只有同key descriptor replacement可以改变root identity：journal保留oldRoot/key，成功后按key解析new primary并以新root坐标恢复offset；rollback恢复old source children与原scrollTop。普通trim不得用“等价replacement”绕过anchor保护。anchor key/new root无法唯一解析时，取消destructive trim并defer。

### 5.3 触发条件

窗口replan由以下事件触发，并收敛到viewport keyed transaction：

- transcript scroll接近任一spacer边界；
- viewport resize；
- earlier page成功合并；
- successful full/windowed snapshot commit；
- live block append/commit或canonical Worker settle使高度dirty；
- tool/file/thought collapse或展开；
- pinned owner集合或external retained root高度/位置变化；
- thread activation/reset。

scroll事件只请求replan，不在原生scroll handler中执行大规模render。同步pagination prepend仍可使用现有barrier，但必须与window transaction共享generation和anchor规则。

## 6. Materialize、trim 与 spacer transaction

### 6.1 Phase 1：只读计划与detached staging

1. 捕获thread/context/interaction/window generation；
2. 从完整canonical nodes构建descriptors；
3. 计算pinned keys；
4. 读取attached block extents并更新临时height plan；
5. 生成window plan；
6. 只为`materializeKeys`构造detached production logical blocks；
7. 获取stream live-owner tokens、main external pins和file cache tokens/staged states，不改变owner或DOM；
8. 构造next spacer elements与完整next DOM order；
9. 捕获anchor journal。

禁止在Phase 1调用`clearActiveStreams()`、`clearCommittedStreams()`、`resetFileChangeCards()`、`resetTranscriptViewport()`、`replaceChildren()`或scroll write。

### 6.2 Phase 2：reserve 与初始validation

- stream live-owner tokens只用于pin/validation，不对active或pending owner做destructive reservation；
- file cache按§7.4对trim/materialize/replace分别reserve；retained committed stream只有现有keyed snapshot接管路径可reserve；
- active/pending owner从`trimKeys`移除并标记pinned；
- 一个batch内完成live-owner token、external pin、file/committed stream reservation、viewport和window generation只读validation；
- 任一reservation冲突：release全部reservation，返回`deferred/over-budget`，DOM与state不变。

### 6.3 Phase 3：单一DOM transaction

所有mutation必须进入一个anchor-preserving viewport transaction：

1. 验证source children identity；
2. 插入materialized roots；
3. 插入/更新spacers；
4. 移除trim roots；
5. 按descriptor顺序排列attached islands与spacers；
6. final只读validation；
7. 测量新attached block高度；
8. 恢复anchor；
9. no-fail commit owner reservations；
10. 发布next window state。

owner commit、height/state发布不得发生在final validation与anchor恢复之前。

### 6.4 Rollback

journal必须能恢复：

- 原始top-level child identity与顺序；
- 原始spacer metadata/height；
- 原始attached/pinned/height/window generation state；
- 原始`scrollTop`；
- 未提交owner reservations。

DOM insert/remove/order、测量、final validation或anchor write任一步抛错，必须同步rollback并release reservations。rollback不得调renderer、callback、follow或另起frame。

## 7. Owner 与 pinning 规则

### 7.1 窄live-owner token契约

`stream.ts`不得导出私有`streams`或`canonicalOwners` map。允许新增：

```ts
type TranscriptLiveOwnerKind = "active-stream" | "pending-canonical" | "retained-committed";

interface TranscriptLiveOwnerToken {
  kind: TranscriptLiveOwnerKind;
  streamId: string;
  element: HTMLElement;      // top-level .stream-buffer
  streamGeneration: number;
  canonicalRevision: number;
  viewportGeneration: number;
}

peekTranscriptLiveOwners(): readonly TranscriptLiveOwnerToken[];
validateTranscriptLiveOwnerTokens(tokens: readonly TranscriptLiveOwnerToken[]): boolean;
```

固定来源与验证：

- active stream来自私有`streams`，element为`StreamState.el`；
- pending canonical来自私有`canonicalOwners`，element为owner target最近的`.stream-buffer`；
- retained committed来自现有`committedClaims`；
- 同一stream同时满足多类时按`pending-canonical > active-stream > retained-committed`去重；
- validator比较kind、streamId、element identity、stream generation、canonical revision、viewport generation与当前私有状态，只读且不分配；
- token映射到logical block时，先使用element的reconcile key；没有metadata时只允许通过descriptor中的assistant id/stream id唯一匹配。0或多于1个匹配都视为retained pinned content，不猜测key。

### 7.2 main显式pin inputs与prompt token

window模块不得读取`main.ts`私有pending队列或`ui/prompt.ts`内部状态。`main.ts`在每次plan前传入external pins：

```ts
interface TranscriptExternalPinInput {
  kind: "pending-local" | "conversation-prompt" | "retained";
  threadId: string;
  itemId: string;
  element: HTMLElement;
  generation: number;
}

interface ConversationPromptWindowToken {
  requestId: string;
  itemId: string;
  threadId: string;
  element: HTMLElement;
  submitting: boolean;
  generation: number;
}

peekConversationPromptWindowToken(threadId: string): ConversationPromptWindowToken | null;
validateConversationPromptWindowToken(
  proof: ConversationPromptWindowToken,
): boolean;
```

- pending-local输入来自`pendingLocalMessages`与`pendingLocalMessageElement()`；generation使用随queue任何mutation推进的新`pendingLocalGeneration`；
- prompt token由`ui/prompt.ts`窄peek返回，不导出`activePrompt`；validator比较activePrompt identity对应的requestId/itemId/threadId/element/submitting/generation与containment；
- `promptGeneration`在任何影响pin eligibility、内容或生命周期的变化上单调推进：show、begin response、fail response、resolve、complete、blocked quiesce和reset；即使top-level element identity不变也必须推进；
- `pendingConversationPrompt()`可保留现有业务语义，但window transaction只使用上述token/validator；
- transcript外permission/tool approval dialog不属于pin集合，禁止修改`frontend/src/ui/dialog.ts`；
- external element若能唯一落入canonical block则pin该key；否则按§4.2记录为external layout entry并保持attached；
- final validation重新比较pending-local generation、prompt token validator、element identity/containment和thread id。plan后发生begin/fail response必须使validation失败。

### 7.3 必须pin与可裁剪内容

必须pin：

- token标识的active stream、pending canonical target、尚未接管的retained committed stream；
- pending-local optimistic message；
- active conversation prompt；
- owner reservation冲突的tool/file compound；
- 无法唯一映射canonical descriptor的attached retained content；
- 当前anchor block。

canonical block只有同时满足以下条件才可trim：descriptor/metadata完整、位于viewport/overscan外、不在pinned set、所有production cache owner可reserve、canonical nodes足以由共享detached renderer重建。

### 7.4 File cache与静态历史owner语义

`renderTranscriptBlocksDetached()`继续使用context-local file cache。其返回值必须允许transaction只读取得每个materialized file key的staged `FileChangeCardState`，但不得在detached阶段写production `cards`。

reservation固定分三类：

1. **trim**：production token `expected → null`；
2. **materialize**：detached context的staged state执行`reserveFileChangeCard(key, null, nextState)`；
3. **replace**：production token `expected → staged nextState`，不再保留指向旧DOM的state。

所有reservation在DOM staging前完成、final validation和anchor恢复后统一no-fail commit。冲突时释放全部reservation且DOM/cache/state不变。

普通静态assistant rematerialization只创建ownerless canonical DOM，不创建active stream、committed stream或canonical Worker owner。只有file/tool存在production cache安装契约；不得把“rematerialize创建新production owner”泛化到静态历史assistant。

### 7.5 Re-materialization metadata

重新物化复用`renderTranscriptBlocksDetached()`的production shape，禁止第二套renderer。安装后写入与普通reconciliation相同的key、fingerprint、shape、root count、member ids、tool ids与file keys metadata。stale Worker只能通过既有generation/target/containment guards失败，不能绑定到ownerless静态replacement。

## 8. Snapshot、pagination 与生命周期组合

### 8.1 Ordinary initial 与后续windowed snapshot

普通windowed入口不得先调用会构造/attach全部descriptors的现有`renderTranscript()`。

**Initial windowed snapshot固定流程：**

1. `mergeCanonicalTranscript(null, incoming, "windowed")`生成candidate model；
2. 收集live-owner tokens与main external pins；
3. planner以following=true从canonical尾部选bounded target keys；
4. 只为target keys detached render；
5. 在一个window transaction中安装materialized roots、spacers、pending-local handoff metadata和file cache reservations；
6. final validation、anchor/follow处理成功后，同一commit set发布workspace/snapshot revision、canonical model、known/pending bookkeeping与window state。

不得先attach 10,000 blocks再异步trim。初始DOM可为空或只含validated retained/pending roots；source identity仍包含这些roots。

**后续windowed snapshot固定流程：**

- 先按§3.1 merge为candidate model，absence保留已加载历史；
- attached matching block使用现有keyed replacement语义，omitted matching block只更新candidate descriptor/fingerprint；
- planner基于candidate model生成同一transaction中的materialize/trim/spacer plan；
- pending-local FIFO只在最终success commit后消费；structured shape仍不得复用不兼容optimistic DOM；
- merge/reconciliation/window任一步stale或throw，revision、canonical model、pending bookkeeping和window state均不提交，并按§3.1选择ordinary recovery。

### 8.2 Ordinary full snapshot

- `mergeCanonicalTranscript(current, incoming, "full")`以incoming替换canonical model；
- 普通full snapshot同样先plan bounded target，只detached render需要insert/replace/materialize的blocks，不允许全量attach后trim；
- attached matching blocks与owner使用现有keyed transaction；omitted且被full删除的blocks从candidate model、height/estimate/pin metadata删除；
- revision、canonical model、owner commits和window state共享最终commit point；
- full merge malformed、stale或transaction throw时不提交，并升级blocked recovery。

blocked authoritative replacement是唯一允许“先完整attach、后trim”的例外。

### 8.3 Blocked authoritative replacement

既有三阶段 `installBlockedFullSnapshot()` 优先于virtualization：

1. blocked replacement仍以预构造完整authoritative fragment完成唯一`replaceChildren()`；
2. replacement成功、owner/prompt/viewport state发布后，初始化DOM window state；
3. 首次trim安排在后续独立window transaction；
4. 不得把spacer、测量或window callback塞入blocked Phase 2；
5. blocked replacement失败时不创建半初始化window state。

### 8.4 Earlier-page pagination

- 请求协议保持`before_turn_id + turn_limit=20`；
- response先在完整page上建立compound membership，再整block去重；
- canonical nodes合并成功后，只物化window plan需要的earlier blocks；
- 若用户仍在顶部边界，anchor-preserving transaction可以同步扩展；
- 已加载但被trim的较晚blocks通过内存materialization恢复，不发向后RPC；
- stale thread/context/interaction generation不得安装response。

### 8.5 Thread switch/reset

thread switch、workspace replacement和test reset必须：

- 取消pending window frame/resize work；
- 推进window generation；
- release未提交reservations；
- 清理thread state和spacer引用；
- 不允许旧thread callback写新transcript。

是否缓存非active thread的canonical nodes不是V1目标；当前切换语义可继续清理inactive window state。

## 9. CSS 与可访问性

在 `frontend/css/chat.css` 增加专用spacer：

```css
.transcript-window-spacer {
  flex: 0 0 auto;
  min-height: 0;
  pointer-events: none;
}
```

要求：

- `aria-hidden="true"`；
- 不可focus、不可选择；
- 不设置reconcile key或item id；
- 不改变现有message/tool alignment；
- `height`严格使用§4.3的`cssHeightPx = sum(blockExtent) + internal gaps`；leading/trailing/interior边界gap由flex自动产生，不得写入spacer height。

## 10. 文件边界

### 10.1 允许创建/修改

实现阶段changed-path allowlist：

- `docs/specs/desktop-transcript-dom-window-2026-08-31.md`
- `docs/design/cross-ui-performance-addendum.md`
- `frontend/src/main.ts`
- `frontend/src/ui/prompt.ts`
- `frontend/src/utils/transcript-dom-window.ts`（新增）
- `frontend/src/utils/transcript-reconciliation.ts`
- `frontend/src/utils/transcript-viewport.ts`
- `frontend/src/utils/render.ts`
- `frontend/src/utils/render-file-changes.ts`
- `frontend/src/utils/stream.ts`
- `frontend/src/utils/index.ts`
- `frontend/css/chat.css`
- `frontend/test/utils/transcript-dom-window.test.ts`（新增）
- `frontend/test/utils/transcript-reconciliation.test.ts`
- `frontend/test/utils/transcript-viewport.test.ts`
- `frontend/test/utils/render.test.ts`
- `frontend/test/utils/render-file-changes.test.ts`
- `frontend/test/utils/stream.test.ts`
- `frontend/test/main/main.test.ts`
- `frontend/test/main/runtime-profile.test.ts`
- `frontend/test/main/incremental-protocol.test.ts`
- `frontend/test/ui/design-system.test.ts`
- `frontend/test/ui/prompt.test.ts`

只在实际需求被RED测试证明时使用允许列表中的辅助路径；不得为了方便触碰全部路径。

### 10.2 禁止修改

- `src/voidx/presentation/gateway/**`
- `src/voidx/presentation/protocol/**`
- `src/voidx/presentation/adapters/persistence/**`
- `src/voidx/persistence/**`
- `frontend/src/rpc/protocol.d.ts`
- `frontend/src/rpc/protocol.schema.json`
- Markdown Worker与grammar/sanitizer文件：
  - `frontend/src/utils/markdown.ts`
  - `frontend/src/utils/markdown-worker.ts`
  - `frontend/src/utils/markdown-worker-client.ts`
  - `frontend/src/utils/markdown-worker-protocol.ts`
  - `frontend/src/utils/markdown-worker-runtime.ts`
- `frontend/src/ui/dialog.ts`
- `desktop/**`
- `tui/**`

如实现证明必须修改禁止路径，停止编码并返回设计评审，不得扩大范围。

## 11. TDD实施顺序

每项行为必须先写测试并读取`./test.py` JSON中的`results[].status === "FAIL"`，确认因目标行为缺失而RED，再写最小实现并确认GREEN。

### Task 1：canonical merge与纯window planner

目标文件：

- `frontend/src/utils/transcript-dom-window.ts`
- `frontend/test/utils/transcript-dom-window.test.ts`

RED场景：

- full merge完整替换；windowed merge原位覆盖且absence保留；earlier-page完整compound前插；
- partial compound overlap、duplicate key、无法锚定顺序分别返回固定stale/recovery结果且不修改输入；
- merged nodes严格由descriptor顺序flatMap生成，window metadata按§3.1边界来源合并；
- 10,000 blocks按像素预算选择viewport附近范围；
- 高度未知时使用shape estimate且受block safety cap限制；
- pinned island保留并生成多个连续spacer segments；
- planner唯一签名包含完整layout entries/attached/pinned/gap/budget/viewport/activation/anchor，cap按`[distancePx,index]`确定；
- leading/interior/trailing多个external roots的数值layout坐标正确，external高度变化会改变目标选择；
- compound key只出现一次，不拆member/root；
- viewport resize向前/后扩展；
- 无安全trim候选时返回over-budget而非删除pinned block。

命令：

```bash
./test.py --frontend -- test/utils/transcript-dom-window.test.ts -v
```

### Task 2：DOM metadata、spacer与height measurement

目标文件：

- `frontend/src/utils/transcript-dom-window.ts`
- `frontend/src/utils/transcript-reconciliation.ts`
- `frontend/src/utils/render.ts`
- `frontend/css/chat.css`
- `frontend/test/utils/transcript-dom-window.test.ts`
- `frontend/test/utils/transcript-reconciliation.test.ts`
- `frontend/test/utils/render.test.ts`

RED场景：

- collector显式识别spacer但不把它当logical/retained block；
- 以固定rowGap对leading/trailing/interior/all-omitted/multi-root断言§4.3具体height数值；
- detached rematerialization安装完整reconciliation metadata；
- malformed spacer/metadata触发stale或deferred，不清空DOM。

命令：

```bash
./test.py --frontend -- test/utils/transcript-dom-window.test.ts test/utils/transcript-reconciliation.test.ts test/utils/render.test.ts -v
```

### Task 3：anchor-preserving window transaction

目标文件：

- `frontend/src/utils/transcript-viewport.ts`
- `frontend/src/utils/transcript-dom-window.ts`
- `frontend/test/utils/transcript-viewport.test.ts`
- `frontend/test/utils/transcript-dom-window.test.ts`

RED场景：

- upward materialize后anchor viewport offset不变；
- downward materialize/remote trim不跳动；
- following=true时尾部append继续到底部；
- interaction generation变化拒绝transaction；
- mutation、measurement、final validation和anchor write分别故障注入并完整rollback；
- transaction-active reentry不产生半提交state或无效frame。

命令：

```bash
./test.py --frontend -- test/utils/transcript-viewport.test.ts test/utils/transcript-dom-window.test.ts -v
```

### Task 4：owner reservation与pinning

目标文件：

- `frontend/src/utils/transcript-dom-window.ts`
- `frontend/src/utils/render-file-changes.ts`
- `frontend/src/utils/stream.ts`
- `frontend/src/utils/render.ts`
- `frontend/test/utils/transcript-dom-window.test.ts`
- `frontend/test/utils/render-file-changes.test.ts`
- `frontend/test/utils/stream.test.ts`
- `frontend/test/utils/render.test.ts`

RED场景：

- live-owner token覆盖active stream、pending Worker与retained committed，并由generation/element/viewport只读validator拒绝stale token；
- token→descriptor映射唯一；无法映射时作为retained pinned root，不扫描或导出内部map；
- settled file/tool trim使用expected→null；materialize使用null→staged state；replace使用expected→staged state；
- reservation conflict保持DOM/cache/state不变并允许超预算；
- success后旧file cache generation失效，rematerialized card安装新cache identity；
- ownerless静态assistant rematerialize不创建active/committed/canonical owner；
- stale Worker settle不能写trimmed或错误replacement target。

命令：

```bash
./test.py --frontend -- test/utils/transcript-dom-window.test.ts test/utils/render-file-changes.test.ts test/utils/stream.test.ts test/utils/render.test.ts -v
```

### Task 5：snapshot、pagination与lifecycle集成

目标文件：

- `frontend/src/main.ts`
- `frontend/src/ui/prompt.ts`
- `frontend/src/utils/transcript-dom-window.ts`
- `frontend/test/main/main.test.ts`
- `frontend/test/main/runtime-profile.test.ts`
- `frontend/test/main/incremental-protocol.test.ts`

RED场景：

- initial 10,000-block windowed snapshot不调用全量`renderTranscript()`，只attach有界窗口；
- main私有pending-local generation/element与真实conversation prompt token被转换为external pin inputs，final validation拒绝变化；
- plan后`beginConversationPromptResponse()`或`failConversationPromptResponse()`即使top-level element不变也推进generation并使token validation失败；
- pending-local和prompt位于窗口外时保持attached island，成功settle/resolve后可在后续plan裁剪；
- 向上网络page合并后连续滚动，向下恢复不发RPC；
- page partial overlap整compound skip；
- windowed snapshot更新omitted block后，rematerialize显示新fingerprint/content；
- full snapshot删除omitted block并清理height/pin state；
- stale ordinary snapshot不提交window state；
- blocked replacement成功后再初始化/trim，replacement throw不发布window state；
- thread switch/reset使旧scroll/resize/page callback失效。

命令：

```bash
./test.py --frontend -- test/main/main.test.ts test/main/runtime-profile.test.ts test/main/incremental-protocol.test.ts -v
```

### Task 6：静态复杂度与文档记录

目标文件：

- `frontend/test/ui/design-system.test.ts`
- `docs/design/cross-ui-performance-addendum.md`

守卫：

- scroll handler不得直接render全部canonical nodes；
- virtualization不得调用Gateway向后分页或新增协议字段；
- `replaceChildren()`仍只允许blocked authoritative replacement路径；
- 直接`transcriptEl.scrollTop =`写入保持受控边界；
- 10,000-block planner测试不创建10,000 DOM roots；
- addendum只在完整verify和人工browser smoke通过后勾选DOM window，仍保持`in-progress/partial`，因为P2、统一benchmark和完整慢路径观测未完成。

## 12. 验证要求

### 12.1 自动验证

按顺序执行并读取机器可读结果：

```bash
./test.py --frontend -- -v
(cd frontend && npm run build)
(cd frontend && npx tsc --noEmit)
git diff --check
git status --short
```

要求：

- `./test.py` JSON中每个`results[].status === "PASS"`；
- build产出独立`dist/assets/markdown.worker-*.js`；
- TypeScript不得新增诊断。实现开始前先fresh记录当前基线，最终做精确集合比较；
- `git diff --check`无输出且成功；
- changed paths全部位于§10.1 allowlist；
- 禁止路径无diff。

### 12.2 真实浏览器人工smoke

项目当前没有浏览器自动化依赖。使用实际Vite页面连接本地headless web backend：

```bash
./python.py -m voidx.main --web --web-headless
(cd frontend && npm run dev)
```

在Chrome/WebKit系实际桌面WebView中使用一个足够长、可加载多页的合成/测试workspace，记录浏览器版本、viewport尺寸和会话turn数，并执行：

1. 打开最新窗口，确认near-bottom live stream持续follow；
2. 向上加载至少5页，持续滚动穿越spacer边界，确认无空白断层、重复compound或明显anchor跳动；
3. 在历史中停留时产生live append，确认视口不跳到底部；
4. 向下连续滚回最新内容，确认不产生新的`transcript.page` RPC；
5. 展开/折叠tool/file/thought，继续跨窗口滚动，确认高度修正不跳动；
6. 切换thread后立即滚动/返回，确认旧callback不污染新thread；
7. DevTools确认attached canonical roots维持在窗口预算数量级，而已加载canonical model继续增长；
8. 记录任何layout shift、scrollTop突变、长任务或owner重复。

通过标准：无功能错误；肉眼不可见持续空白或明显跳跃；无重复/缺失logical block；attached DOM不随已加载页数线性增长。人工结果必须写入addendum实施记录，不能只在会话中口头声明。

## 13. 实施后文档状态

完成本规格并通过自动验证、人工browser smoke和独立终审后：

- 将本文状态更新为`Implemented`并归档到`docs/archive/`；
- 在`docs/design/cross-ui-performance-addendum.md`新增实施记录并勾选DOM window/virtualization；
- addendum继续保持`status: in-progress`、`implementation_status: partial`；
- 明确保留P2输入、durable live-history、统一绝对benchmark与完整慢路径观测为后续事项。

如人工browser smoke未完成，本文不得标记Implemented或归档，addendum不得宣称DOM window完整闭环。

## 14. 执行禁止事项摘要

实现代理不得：

- 先写实现再补测试；
- 修改Gateway/protocol/persistence来增加向后分页；
- 从canonical model删除仅因DOM窗口外而omitted的nodes；
- 逐node或逐root拆compound；
- 裁剪active/pending owner来强行满足预算，或把transcript外permission dialog误纳入window owner；
- 在owner final validation前发布window state或scroll correction；
- 在普通snapshot/window transaction中清空transcript或reset viewport；
- 新建第二套tool/file/Markdown renderer；
- 用固定turn数冒充像素预算；
- 把jsdom几何测试当作真实浏览器滚动smoke；
- 在P2、benchmark仍未完成时归档整个addendum。
