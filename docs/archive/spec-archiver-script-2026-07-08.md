# Spec 文档归档脚本 — 技术设计文档

Date: 2026-07-08

> **Status: Done**。

## Context

AGENTS.md 定义了文档生命周期：`docs/specs/` 中的设计文档在实现完成后需移动到 `docs/archive/`，并在文件头部添加 `> **Status: Done**` 标记。目前没有自动化脚本，归档全靠手动操作（移动文件 + 编辑头部），容易遗漏头部标记或放错位置。

需要一个通用 Python 脚本，自动完成归档的一系列操作。实现文件是否真正存在、功能是否完整的验证由 LLM 在归档前完成，脚本不负责验证。

## Goals and Non-Goals

### Goals

- 将指定的 spec 文件从 `docs/specs/` 移动到 `docs/archive/`
- 在文件头部添加或替换 `> **Status: Done**` 标记
- 支持批量归档多个文件
- 支持 dry-run 预览
- 与项目现有脚本风格一致（argparse + pathlib）

### Non-Goals

- 不验证实现文件是否存在或功能是否完整（由 LLM 负责）
- 不自动扫描哪些 spec 可以归档（由调用者指定文件）
- 不处理 `docs/design/` 目录的 RFC 文档

## Architecture

单文件 CLI 脚本，无外部依赖。核心流程：

```
输入: 文件路径列表
  ↓
对每个文件:
  1. 解析路径，确认源文件在 docs/specs/ 下
  2. 读取文件内容
  3. 在头部添加或替换 Status: Done 标记
  4. 移动到 docs/archive/（保持文件名不变）
  ↓
输出: 归档结果摘要
```

### 归档目标路径

直接放 `docs/archive/` 根目录，保持文件名不变。这是现有 archive 目录中最常见的模式（63 个文件中大部分是直接放在根目录）。

### Status 标记处理规则

| 文件头部现状 | 处理方式 |
|------------|---------|
| 无任何 Status 标记 | 在文件头部插入 `> **Status: Done**`。插入位置：如果第一行是 `#` 标题，则在标题行之后插入（标题 → 空行 → Status → 空行 → 正文）；否则在文件最前面插入 |
| 有 `> **Status: Spec**`（或 Spec 后带补充说明） | 替换状态词为 `Done`，保留原有补充说明 |
| 已有 `> **Status: Done**` | 保持不变，跳过头部修改 |
| 有其他 `> **Status: ...**` | 替换状态词为 `Done`，保留原有补充说明 |

现有归档文件中两种格式都存在（Status 在标题前或在标题后），脚本统一采用"标题后"格式，与多数归档文件一致。

匹配模式：`^>\s*\*\*Status:\s*\*\*(\w+)(.*)$`（匹配 Status blockquote 行，捕获状态词及后续文本）。替换时将状态词改为 `Done`；如指定了 `--status-note`，则用新说明替换原有补充文本。

## API Contract

### CLI 接口

```
./scripts/archive.py <files...> [--dry-run] [--status-note TEXT]
```

### 参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `files` | 位置参数，可多个 | 是 | 要归档的 spec 文件路径（相对或绝对） |
| `--dry-run` | flag | 否 | 只打印将要执行的操作，不实际移动或修改文件 |
| `--status-note` | string | 否 | Status: Done 后的补充说明，如 `--status-note "实现完成，全部测试通过"` |

### 输出

每处理一个文件输出一行：
- 成功：`✅ archived: specs/foo.md → archive/foo.md`
- dry-run：`🔍 [dry-run] would archive: specs/foo.md → archive/foo.md`
- 跳过（已在 archive）：`⏭️  already archived: archive/foo.md`
- 错误：`❌ not a spec file: foo.md (expected in docs/specs/)` 或 `❌ file not found: foo.md`

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| 文件不存在 | 报错跳过，继续处理其他文件 |
| 文件不在 `docs/specs/` 下 | 报错跳过，继续处理其他文件 |
| 目标文件已存在于 `docs/archive/` | 报错跳过，不覆盖 |
| 文件无法读取/写入 | 报错跳过，继续处理其他文件 |

所有错误都打印到 stderr，最终 exit code：全部成功为 0，有任一失败为 1。

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 归档到 archive/ 根目录 | 按日期子目录归档 | 根目录模式是现有最常见的结构 |
| 不自动扫描可归档文件 | 扫描 specs/ 并检测实现状态 | 验证由 LLM 负责，脚本只执行操作 |
| Status 标记放在标题之后 | 放在标题之前 | 多数归档文件采用标题后格式（标题 → 空行 → Status blockquote） |
| 单文件脚本 | 拆分为模块 | 脚本逻辑简单，单文件足够 |

## Open Questions

- [ ] 是否需要支持 `--by-date` 参数按日期子目录归档？目前设计不支持，可后续按需添加。
