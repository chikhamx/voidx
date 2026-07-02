# @ 路径引用提示机制 — 技术设计文档

> **Status: Approved** — 设计完整，可直接进入实施阶段。实施完成后，按文档生命周期规则将文档移至 `docs/archive/` 并标记 `> **Status: Done**`。

## Context

当前 `@path` 被 `src/voidx/agent/attachments.py` 当作附件入口处理。workspace 内文件会在用户消息构造阶段被读取并嵌入 LLM 上下文；workspace 外路径会在 `_resolve_workspace_path()` 中被 `relative_to(workspace)` 拦截，并追加 `Attachment skipped outside workspace: ...` warning。随后 `src/voidx/agent/graph/turn_runner.py` 遍历 `payload.warnings` 并通过 `host._ui.ui.warn()` 打印，因此用户每次引用 workspace 外文件都会看到异常式警告。

这个行为有两个问题：

- `@` 既承担“路径引用”又承担“自动附件嵌入”，职责过重。
- workspace 外路径属于用户显式给出的引用意图，不应该在消息解析阶段打印成异常式 warning。
- 自动嵌入文件内容会放大上下文占用，尤其是大文件或用户只是想让模型按需查看路径时。
- workspace 外文件是否可读应由文件读取工具和权限系统判断，而不是附件解析层提前决定。

目标是把 `@` 从“附件嵌入机制”调整为“路径引用提示机制”：解析层只识别并规范化用户引用的路径，把路径提示交给 LLM；真正读取文件时，由 LLM 主动调用 `read` 工具，并由工具/权限层处理 workspace 内外路径、审批和拒绝。

## Goals and Non-Goals

### Goals

- 将普通 `@path` 文件引用从自动附件嵌入改为路径提示，不在用户消息构造阶段读取文件内容。
- workspace 内路径保留为可读路径提示，LLM 需要内容时调用 `read(file_path="...")`。
- workspace 外路径也保留为路径提示，不再触发 `Attachment skipped outside workspace` warning。
- workspace 外文件读取由 `read` 工具和权限系统处理：允许、询问或拒绝都应发生在工具调用阶段。
- 保留 `[image-...]` 等剪贴板图片附件的现有多模态嵌入能力。
- 保留 `<pasted>...</pasted>` 内 `@` 不被解析为引用的现有行为。
- 为 workspace 内路径、workspace 外路径、缺失路径、带空格路径、Windows 绝对路径、`..` 路径补测试。

### Non-Goals

- 不让附件解析层绕过权限系统直接读取 workspace 外文件。
- 不把 workspace 外路径自动加入永久白名单或配置。
- 不改变 write/replace/file 等写工具的沙箱边界。
- 不移除图片粘贴或显式图片附件的内容嵌入机制。
- 不要求 LLM 一定读取每一个 `@path`；`@` 表示“用户引用了该路径”，是否读取由任务需要决定。

## Architecture

### 当前状态

```
User text
  └── build_user_message_payload()
        ├── _attachment_tokens() 提取 @path / [image-*]
        ├── _resolve_workspace_path() 强制限制在 workspace 内
        ├── workspace 内文件：读取内容并嵌入 text_sections
        ├── workspace 外路径：payload.warnings += Attachment skipped outside workspace
        └── turn_runner: host._ui.ui.warn(payload.warnings)
```

### 目标状态

```
User text
  └── build_user_message_payload()
        ├── _reference_tokens() 提取普通 @path
        ├── 普通 @path：生成路径引用提示，不读取文件内容
        ├── [image-*]：继续走图片附件嵌入
        ├── 不因 workspace 外路径写入 payload.warnings
        └── LLM 后续按需调用 read(file_path=...)
                  └── permission / sandbox 决定 allow / ask / deny
```

### 模块职责

| 模块 | 新职责 |
|------|--------|
| `src/voidx/agent/attachments.py` | 识别用户显式路径引用，生成可读路径提示；只对真正附件保留内容嵌入。 |
| `src/voidx/agent/graph/turn_runner.py` | 继续展示真正需要用户关注的 warnings；不需要为普通外部路径做特殊处理。 |
| `src/voidx/tools/file_ops/read.py` | 执行实际文件读取，并记录 read coverage。 |
| `src/voidx/permission/*` | 对 workspace 外读取提供明确 allow / ask / deny 行为。 |
| `tests/test_agent/test_attachments.py` | 覆盖 `@` 解析、提示生成、warning 行为和 `<pasted>` 排除规则。 |
| `tests/test_permission/*` 或相关 read 工具测试 | 覆盖 workspace 外 read 的审批/拒绝行为。 |

## Data Model

### PathReference

可作为内部 dataclass 引入，也可以先用 `Attachment` 旁路的轻量结构实现。

```
PathReference
├── raw_path: str          # 用户输入的原始路径，不展开、不读取
├── display_path: str      # 写入 LLM 提示和 UI 展示的路径
├── is_workspace_relative: bool | None
└── token_span: tuple[int, int]
```

### UserMessagePayload

建议扩展字段，而不是把路径引用塞进 `attachments`，避免 UI 误展示为已附加文件。

```
UserMessagePayload
├── raw_text: str
├── clean_text: str
├── display_text: str
├── title_text: str
├── content: str | list[dict[str, Any]]
├── content_format: str
├── attachments: list[Attachment]
├── path_references: list[PathReference]
└── warnings: list[str]
```

如果希望最小改动，可以暂不公开 `path_references` 字段，只在 `_build_text_content()` 中追加路径提示；但长期看显式字段更利于 UI 展示和测试。

## API Contract

### build_user_message_payload

- **Signature**: `build_user_message_payload(user_text: str, workspace: str, *, text_prefix: str = "", extra_removed_spans: list[tuple[int, int]] | None = None) -> UserMessagePayload`
- **Input**: 用户原始文本和当前 workspace。
- **Output**:
  - `clean_text`: 移除已识别的普通 `@path` token，保留用户自然语言。
  - `content`: 包含用户文本和 `Referenced paths:` 区块，不自动嵌入普通文件内容。
  - `attachments`: 仅包含仍需要自动嵌入的真实附件，例如图片。
  - `warnings`: 不包含 workspace 外普通路径引用 warning。
- **Errors**: 普通路径引用解析失败不抛异常；保留原文或作为普通文本处理。

示例输出：

```text
请看一下这个文件

Referenced paths:
- src/voidx/agent/attachments.py
- C:\Users\me\Desktop\notes.txt
```

### read 工具权限行为

- **Workspace 内路径**: 默认按现有读权限策略允许，除非命中敏感规则（例如 `.env`）。
- **Workspace 外路径**: 应进入权限判断；在需要用户确认的策略下触发 ask，而不是由附件解析层提前 warn。
- **审批通过**: 工具读取文件并返回带行号内容。
- **审批拒绝或沙箱拒绝**: 工具返回明确拒绝原因，LLM 根据拒绝结果继续回答或询问用户。

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| `@path` 指向 workspace 外 | 作为 `Referenced paths` 原样传给 LLM；不加入 `payload.warnings`。 |
| `@path` 指向不存在文件 | 不在解析阶段报 warning；LLM 调用 `read` 时由工具返回 not found。 |
| `@path` 是目录 | 仍只传路径提示；目录列表由后续 `read` / `glob` / `grep` 等工具按需处理。 |
| `@path` 带空格 | 支持 `@"path with spaces.txt"`；未加引号的空格路径按现有 token 规则截断。 |
| `@path` 在 `<pasted>` 块内 | 继续跳过解析，保持原文。 |
| workspace 外 read 被拒绝 | 权限层或沙箱层返回拒绝原因，不回退为附件 warning。 |
| LLM 没有主动读取路径 | 允许；`@` 只表示引用提示，不保证读取。必要时可在系统提示或用户消息中增强“需要内容时调用 read”。 |

## Implementation Plan

1. 在 `attachments.py` 中拆分普通路径引用和真实附件处理：
   - 保留 `[image-*]` 图片附件路径。
   - 普通 `@path` 不再调用 `_text_file_section()` 或 `_directory_section()`。
   - 普通 `@path` 生成 `PathReference` 或等价结构。
2. 调整文本构造：
   - `_build_text_content()` 追加 `Referenced paths:` 区块。
   - `_display_text()` 可显示 `[references: ...]`，避免标成 `[attachments: ...]`。
   - `title_text` 继续基于 clean text，避免只有路径时标题为空。
3. 删除或改造普通 `@path` 的 workspace 外 warning：
   - 不再使用 `Attachment skipped outside workspace` 表示普通路径引用。
   - 只对图片附件、过大图片、真实读取失败等保留 warning。
4. 补齐 read 工具权限行为：
   - 确认 `read` 是否会对 workspace 外路径触发 permission ask。
   - 如果当前只在工具内部 `resolve_safe()` 直接拒绝，则把 workspace 外读取改为先进入权限判断，再由策略决定 ask/deny。
   - 保持写工具 sandbox 不变。
5. 更新测试：
   - `@src/main.py` 不再嵌入文件内容，只输出 `Referenced paths`。
   - workspace 外绝对路径不产生 `payload.warnings`。
   - `@../outside.py` 不产生附件 warning，并作为路径提示保留。
   - `@"path with spaces.py"` 正确保留完整路径。
   - `<pasted>` 内 `@pytest.mark.asyncio` 仍不解析。
   - 图片 `[image-*]` 行为不回归。
6. 更新提示或文案：
   - 如有必要，在运行时上下文中说明：用户提供的 `Referenced paths` 是可按需读取的本地路径。

## Testing

目标测试命令：

```bash
./python.sh -m pytest tests/test_agent/test_attachments.py -v
./python.sh -m pytest tests/test_permission/ -v
./python.sh -m pytest tests/test_tools/ -v
```

重点断言：

- 普通 `@path` 不再导致文件内容提前进入 `payload.content`。
- workspace 外普通路径不写入 `payload.warnings`。
- 图片附件仍生成 structured multimodal content。
- `<pasted>` 块内 token 仍被忽略。
- workspace 外 `read` 的审批/拒绝路径有可重复测试覆盖。

## Risks

- **模型可能不主动读取路径**：从自动嵌入改为按需读取后，LLM 可能只基于路径名回答。缓解方式是在 `Referenced paths` 前后加入明确提示：“Read these files if their contents are needed.”
- **审批行为不一致**：如果 read 工具和权限系统边界不一致，workspace 外路径可能直接被工具拒绝而不是 ask。需要在权限测试中固定期望。
- **UI 语义变化**：之前 `display_text` 显示 `[attachments: ...]`，改为 `[references: ...]` 后可能影响历史展示或快照测试。
- **大文件上下文体验变化**：不会自动嵌入大文件是优点，但依赖模型选择合适 read 范围；需要工具提示保持清晰。
- **安全边界误放宽**：不能为了外部路径读取体验而绕过沙箱/审批；所有实际读取必须经过工具权限层。

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 普通 `@path` 改为路径提示 | 继续自动嵌入 workspace 内文件 | 降低上下文占用，统一 workspace 内外语义，让 LLM 按需读取。 |
| workspace 外路径不在解析阶段 warning | 保持 `Attachment skipped outside workspace` | 用户显式引用路径不是异常；实际读权限应由工具层决定。 |
| 图片附件继续嵌入 | 所有 `@`/附件都只传路径 | 图片需要多模态 payload，不能只靠文本路径表达。 |
| 外部读取交给 read 权限 | 附件层直接读取外部文件 | 保持安全边界和审批可审计。 |

## Open Questions (Resolved)

- [x] **workspace 外 `read` 在当前权限策略下应该默认 ask，还是仅在 `approval_policy=untrusted` 时 ask？**
  - **决策**：仅在 `approval_policy=untrusted` 时触发 ask；`trusted` 下直接允许（配合沙箱检查）。参考 `bash` 工具策略，在权限引擎层面统一：外部路径 `read` 走 `ask`，内部路径走 `allow`。实施时在 `permission/rules.py` 或 `engine.py` 明确外部路径策略，并加测试固化。

- [x] **是否需要在 UI 上专门展示 `[references: ...]`，还是只在 LLM content 中出现 `Referenced paths`？**
  - **决策**：**是**。在 `_display_text()` 中输出 `[references: ...]`，区别于 `[attachments: ...]`。同步更新快照测试。

- [x] **`@path` 是否应支持目录引用后的自动 `glob`/目录列表提示，还是完全交给 LLM 后续工具调用？**
  - **决策**：**不自动展开**。完全交给 LLM 后续 `glob`/`read` 处理，保持 `@path` 语义纯粹。

- [x] **是否需要配置项兼容旧行为，例如 `auto_embed_at_references = true/false`？**
  - **决策**：**不需要**。行为变更是核心目标，旧行为有明确缺陷。如需过渡期可考虑 feature flag，但增加复杂度，不建议引入。

---

## Implementation Notes (from Review)

### Referenced paths 区块提示语
在 `Referenced paths:` 前添加引导语，缓解模型不主动读取的风险：
```text
Referenced paths (read with `read` tool if needed):
- src/voidx/agent/attachments.py
- C:\Users\me\Desktop\notes.txt
```

### PathReference.is_workspace_relative 语义明确
- `True`：workspace 内相对路径（`Path.is_relative_to(workspace)` 为真）
- `False`：workspace 外绝对路径（Windows 绝对路径、显式绝对路径）
- `None`：无法判断（如相对路径 `../outside.py` 穿出 workspace）

### 实施顺序建议
1. 先改 `attachments.py` 核心解析逻辑（步骤 1-3）——最小改动验证核心流程
2. 跑 `test_attachments.py` 确认行为变更（步骤 5 部分测试）
3. 补齐 `read` 工具权限逻辑（步骤 4）——跑 `test_permission/` 和 `test_tools/`
4. 更新 UI 显示与提示语（步骤 2、6）——同步快照测试
5. 全量测试通过后提交

### 测试覆盖补充
| 测试场景 | 建议断言 |
|----------|----------|
| `@src/main.py` workspace 内文件 | `payload.content` **不**包含文件内容；`payload.path_references` 包含 1 项；`payload.warnings` 为空 |
| `@C:\Users\me\notes.txt` Windows 绝对路径 | 同上，无 warning |
| `@../outside.py` 相对路径穿出 workspace | 同上，`is_workspace_relative=None` |
| `@"path with spaces.py"` 带引号空格路径 | `raw_path` 保留完整路径（含空格） |
| `<pasted>\n@decorator\n</pasted>` | `payload.path_references` 为空，`clean_text` 保留原文 |
| `[image-screenshot.png]` 图片附件 | `payload.attachments` 包含 1 项 image，`content_format="structured"` |
| workspace 外 `read` 被用户拒绝 | `ToolResult.metadata.error=True`，`output` 包含 "Read denied by user" |
| workspace 外 `read` 被用户允许 | 返回文件内容，`metadata.already_read=False` |
