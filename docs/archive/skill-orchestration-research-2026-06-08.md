# Coding Agent 技能编排调研：Voidx vs Claude Code vs Codex CLI

> **日期**: 2026-06-08
> **状态**: Done

## 1. 背景

对 Voidx、Claude Code、OpenAI Codex CLI 三款终端 coding agent 的技能编排机制进行横向对比，识别 Voidx 的差异化优势和改进空间。

## 2. 架构总览

| 维度 | **Voidx** | **Claude Code** | **Codex CLI** |
|------|-----------|-----------------|---------------|
| 语言 | Python | TypeScript (Bun) | Rust |
| 代码规模 | ~中 | 510K LoC | 549K LoC |
| Agent Loop | LangGraph 状态图 | 单文件 `while(true)` | Queue-Pair `Submission→Event` |
| 技能格式 | `SKILL.md` + YAML frontmatter | `SKILL.md` + YAML frontmatter | `SKILLS.md` + 结构化 Rust 元数据 |
| 技能本质 | Prompt 注入 + 工作流状态机 | Prompt 注入 | Prompt 注入 + 依赖/策略声明 |

## 3. 技能定义

### 3.1 文件约定

三者均采用 Markdown 文件作为技能载体，将指令注入 LLM 系统提示词。

| | **Voidx** | **Claude Code** | **Codex CLI** |
|---|-----------|-----------------|---------------|
| 文件名 | `<dir>/SKILL.md` | `<dir>/SKILL.md` | `<dir>/SKILLS.md` |
| 支撑文件 | ✅ 同目录引用 | ✅ `/scripts`, `/references`, `/assets` | ✅ 编译时嵌入 |

### 3.2 Frontmatter 元数据

| 字段 | **Voidx** | **Claude Code** | **Codex CLI** |
|------|-----------|-----------------|---------------|
| `name` | ✅ | ✅ | ✅ |
| `description` | ✅ | ✅ | ✅ |
| `enabled` | ✅ | — | — |
| `triggers` | ✅ 关键词列表 | — | — |
| `scope` | ✅ bundled/global/project | — | ✅ System/User/Project |
| `auto_invoke` | — | ✅ | — |
| `allowed-tools` | — | ✅（声明但未强制） | — |
| `model` | — | ✅ 子 agent 模型选择 | — |
| `when_to_use` | — | ✅ | — |
| `short_description` | — | — | ✅ |
| `interface` | — | — | ✅ 显示名/图标/品牌色 |
| `dependencies` | — | — | ✅ MCP 服务器/环境变量/工具 |
| `policy` | — | — | ✅ 产品范围/隐式调用权限 |

**Voidx 差距**：缺少 `allowed-tools`（工具门控声明）、`auto_invoke`（自动激活控制）、`model`（子 agent 模型选择）、动态上下文注入（Claude Code 的 `` !`cmd` `` 语法）、`dependencies`（依赖声明）。

### 3.3 Voidx 当前内置技能（8 个）

| 技能 | 优先级 | 触发场景 |
|------|--------|---------|
| brainstorming | 5 | design/create intent |
| systematic-debugging | 10 | debug intent |
| receiving-code-review | 20 | review feedback |
| writing-design-docs | 25 | design intent |
| writing-plans | 30 | plan mode / planning intent |
| test-driven-development | 40 | implement intent/role |
| verification-before-completion | 50 | implement/debug lifecycle |
| requesting-code-review | 60 | review intent / substantial work completion |

### 3.4 Claude Code 生态技能示例（HyperFrames）

HyperFrames 通过 `npx skills add heygen-com/hyperframes` 安装，包含 15 个子技能：

| 技能 | 功能 |
|------|------|
| hyperframes | 核心视频制作循环 |
| hyperframes-cli | CLI 操作 |
| hyperframes-media | 媒体处理 |
| css-animations | CSS 动画 |
| gsap | GSAP 动画 |
| animejs | Anime.js 动画 |
| waapi | Web Animations API |
| lottie | Lottie 动画 |
| three | Three.js 3D |
| tailwind | Tailwind CSS |
| remotion-to-hyperframes | Remotion 迁移 |
| website-to-hyperframes | 网页转视频 |

## 4. 技能发现与激活

### 4.1 发现路径

| | **Voidx** | **Claude Code** | **Codex CLI** |
|---|-----------|-----------------|---------------|
| 搜索顺序 | bundled → global → project | 用户/项目/插件/内置 | System（编译嵌入）→ User → Project |
| 全局目录 | `~/.voidx/skills/` | `~/.config/claude/skills/` | `CODEX_HOME/skills/` |
| 项目目录 | `.voidx/skills/` | `.claude/skills/` | 项目级 |
| 缓存 | mtime+size 缓存 | — | 指纹缓存失效 |
| 分发机制 | ❌ 无 | ✅ `npx skills add` + skills.sh | ❌ 无 |

### 4.2 激活方式

| | **Voidx** | **Claude Code** | **Codex CLI** |
|---|-----------|-----------------|---------------|
| 显式引用 | ✅ `$skill-name` | ✅ `/skill-name` | ✅ 手动 |
| 关键词匹配 | ✅ `triggers` 列表 | ✅ `auto_invoke` + 描述匹配 | ✅ `policy.implicit_invocation` |
| **工作流策略** | ✅ **`workflow_skill_activations()`** | ❌ | ❌ |
| LLM 建议 | ✅ `on_intent.suggested_skills` | — | — |

**Voidx 核心差异化**：`workflow_skill_activations()` 是三方中唯一提供 bundled workflow 激活策略的机制。当前 transition 是 LLM-driven soft constraint，不是 runtime-enforced 状态机链——

- debug intent → `systematic-debugging` + `verification-before-completion`
- implement intent → `test-driven-development` + `verification-before-completion`
- design intent → `brainstorming` → `writing-plans`
- plan mode → `brainstorming` + `writing-plans`

## 5. 意图分类与工具门控

### 5.1 意图分类

| | **Voidx** | **Claude Code** | **Codex CLI** |
|---|-----------|-----------------|---------------|
| 意图层级 | 6 级：chat/inspect/design/review/implement/debug | 无显式分类 | 无显式分类 |
| 分类方式 | 两层：① 关键词 `infer_task_intent` ② LLM `on_intent` 工具 | 无 | 无 |
| 置信度 | ✅ LLM 输出 0-1 置信度 | 无 | Guardian 风险评分 0-100 |

### 5.2 工具门控

Voidx 按意图分层控制工具可用性：

| 意图 | 可用工具 |
|------|---------|
| chat / ambiguous | ∅ |
| inspect | read, glob, grep, webfetch, websearch, repo_map, lsp_* |
| design | inspect + agent, todo, bash |
| review / debug | inspect + agent, todo, bash |
| implement | 全部 agent 工具 |

| | **Voidx** | **Claude Code** | **Codex CLI** |
|---|-----------|-----------------|---------------|
| 门控方式 | ✅ 按意图分层，运行时强制 | `allowed-tools` 声明但**未强制执行** | Trait 类型系统 + Guardian AI 审批 |
| 低置信度降级 | ✅ implement <0.65 → design + PendingApproval | 无 | Guardian <80 自动放行 |
| Plan 模式 | ✅ 强制 implement→design，移除写工具 | 无等价 | 无等价 |

### 5.3 运行时上下文自感知

Voidx 通过 `VOIDX_RUNTIME_CONTEXT` 块让 LLM 实时感知：

- 当前 intent 和 phase
- 活跃技能及状态
- 可用工具列表
- Pending approval
- 建议技能

这是三方中唯一让模型**感知自身编排状态**的设计。Claude Code 和 Codex CLI 均无等价机制。

## 6. 安全与审批

| | **Voidx** | **Claude Code** | **Codex CLI** |
|---|-----------|-----------------|---------------|
| 审批机制 | `PendingApproval`：design→implement 需用户确认 | 权限模式隐式 yes/no | Guardian AI 风险评分 + 用户审批 |
| Guardian AI | ❌ | ❌ | ✅ 独立 LLM（gpt-5.4）审批工具调用 |
| 沙箱 | workspace-write | 仅 macOS `sandbox-exec` | 3 平台原生沙箱（17K LoC） |
| 网络控制 | ❌ | ❌ | ✅ MITM 代理 |

**Codex CLI 的 4 层安全防线**：
1. 用户审批（approve/deny/modify）
2. Guardian AI 风险评分
3. OS 沙箱（macOS sandbox-exec / Linux Landlock+seccomp / Windows Job Objects）
4. 网络代理

## 7. 上下文管理

| | **Voidx** | **Claude Code** | **Codex CLI** |
|---|-----------|-----------------|---------------|
| 策略 | LangGraph compaction | 4 层级联 (SNIP→Micro→COLLAPSE→Auto) | 2 阶段 (per-rollout + cross-rollout) |
| 复杂度 | 中 | 高（4 层） | 低（2 阶段） |

## 8. 总结

### 8.1 Voidx 核心优势

1. **意图驱动的工作流编排**：7 级意图 × 3 种交互模式 = 21 种工具集组合，三方最精细
2. **Workflow policy 自动激活**：提供 bundled workflow soft transition；硬状态推进留给后续 Phase C
3. **运行时上下文自感知**：LLM 可感知自身编排状态，做出更合理的决策
4. **低置信度安全降级**：implement <0.65 自动降为 design + PendingApproval

### 8.2 Voidx 改进空间

| 优先级 | 改进项 | 参考 |
|--------|--------|------|
| P0 | 技能 `allowed-tools` 声明与强制执行 | Claude Code 的 frontmatter 字段（但需真正执行） |
| P0 | 技能 `auto_invoke` 控制 | Claude Code 的自动激活机制 |
| P1 | 动态上下文注入（`` !`cmd` `` 语法） | Claude Code 的运行时命令替换 |
| P1 | 技能 `dependencies` 声明（MCP/环境变量/工具） | Codex CLI 的结构化依赖 |
| P1 | 技能分发机制（`npx skills add` 等价物） | Claude Code 的 skills.sh |
| P2 | 子 agent `model` 选择 | Claude Code 的 per-skill 模型 |
| P2 | Guardian AI 审批层 | Codex CLI 的独立 LLM 审批 |
| P2 | 多平台沙箱 | Codex CLI 的 3 平台原生沙箱 |
| P3 | 技能 `interface` 元数据（图标/品牌色） | Codex CLI 的展示层声明 |

### 8.3 设计哲学对比

| | 哲学 | 一句话 |
|---|------|--------|
| **Voidx** | 意图驱动编排 | 让 agent 知道自己在做什么，然后按规则做 |
| **Claude Code** | 开发者速度优先 | 信任模型，宽松沙箱，快速迭代 |
| **Codex CLI** | 安全验证优先 | 验证一切，严格沙箱，AI 审 AI |

## 9. 参考来源

- [Claude Code Skills 官方文档](https://code.claude.com/docs/en/skills)
- [Claude Agent Skills Deep Dive](https://leehanchung.github.io/blogs/2025/10/26/claude-skills-deep-dive)
- [Codex CLI 源码](https://github.com/openai/codex)
- [Codex CLI 架构拆解](https://github.com/NeuZhou/awesome-ai-anatomy/tree/main/codex-cli)
- [Codex CLI Agent Loop 设计](https://www.zenml.io/llmops-database/building-production-ready-ai-agents-openai-codex-cli-architecture-and-agent-loop-design)
- [HyperFrames Skills](https://github.com/heygen-com/hyperframes/tree/main/skills)
- [skills.sh 注册表](https://skills.sh)

## 10. 落地决策

调研结论收敛到 `skill-state-machine-2026-06-08.md` 的阶段化实现：

1. **Phase A 先做 context 分层和去重**：bundled workflow skill 由 voidx 自动编排；完整 body 只进入 turn 生命周期的 Skill Context Message 或当前 turn 的 `on_intent` ToolMessage。
2. **用户/project/global skill 不自动注入 full body**：system prompt 只列 `Available Skills` 的 name + description。
3. **Phase B 再做 `load_skills`**：该工具是 read-only，所有 intent/mode 都可见，只按 enabled skill name 加载 body，不允许路径读取。
4. **Phase C 再做完整状态推进**：SATISFIED/BLOCKED 需要先定义 evidence 来源，不在 Phase A 硬编码。

本调研文档只保留横向对比和优先级背景；具体实现边界以状态机设计文档为准。
