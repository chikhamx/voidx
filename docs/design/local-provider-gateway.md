---
name: local-provider-gateway
display_name: Local Provider Gateway
description: 本地反向代理供应商——用 claude-code-proxy 托管 OAuth，voidx 零改动接入，fork 扩展 antigravity + OpenAI 协议出口
doc_type: tech-design
audience: human+llm
---

# Local Provider Gateway — 技术设计文档

## TL;DR

用 `claude-code-proxy`（Rust 二进制）作为本地反向代理，托管 codex/grok/kimi/cursor 的 OAuth 登录态。voidx **零代码改动**——通过现有自定义 provider profile 指向 `http://127.0.0.1:18765` 即可接入。后续 fork claude-code-proxy 补齐 antigravity（Google OAuth）和 OpenAI 协议出口（`/v1/chat/completions` body 透传）。

## Context

### 需求

1. 本地起一个通用代理，托管上游 coding agent 的 OAuth 登录态（codex、grok、antigravity 等）。
2. 对下游暴露统一协议入口，支持 OpenAI 和 Anthropic 协议透传（body 不改，仅注入鉴权头）。
3. 集成进 voidx 现有 provider 体系。
4. 参考现有开源项目，直接拉代码改造接入，不从零写 OAuth。

### 选型结论

| 项目 | 语言 | OAuth 托管 | 协议出口 | antigravity | 选用 |
|---|---|---|---|---|---|
| `raine/claude-code-proxy` | Rust | codex/grok/kimi/cursor | 仅 Anthropic | ❌ | ✅ |
| `luispater/CLIProxyAPI` | Go | codex/claude/gemini/antigravity | OpenAI + Anthropic | ✅ | 参考 |
| `antigravity-claude-proxy` | Node.js | antigravity（Google OAuth） | Anthropic | ✅ | fork 参考 |

选 `claude-code-proxy` 因为：OAuth 实现成熟（PKCE + device-code + token 自动续期）、独立 HTTP 服务、凭证独立存储、Rust 性能好。gap（antigravity + OpenAI 协议出口）通过 fork 补齐。

### 为什么 voidx 不需要改

voidx 已有完整的自定义 provider 机制：

- `Profile`（`src/voidx/config/models.py`）支持 `base_url`、`protocol`、`api_key` 字段。
- `SettingsApiKeyMixin`（`src/voidx/config/settings_api_keys.py`）从 profile 解析 base_url 和 protocol。
- `create_chat_model`（`src/voidx/llm/provider.py`）的 anthropic protocol 分支直接用 `base_url` 构造 `ChatAnthropic`。

用户只需存一个 profile，voidx 就能接入——零代码改动。

### claude-code-proxy 现状

- **安装**：`brew install raine/claude-code-proxy/claude-code-proxy` 或 install script。
- **服务**：`claude-code-proxy serve`，默认监听 `127.0.0.1:18765`，暴露 `/v1/messages`（Anthropic 协议）。
- **OAuth 登录**：`claude-code-proxy <provider> auth login`（浏览器 PKCE）/ `auth device`（headless device-code）。
- **凭证存储**：macOS Keychain / Linux `${XDG_CONFIG_HOME:-$HOME/.config}/claude-code-proxy/<provider>/auth.json`（mode 0600），独立于原生 CLI。
- **token 续期**：过期前 5 分钟自动刷新，single-flight guard。
- **模型路由**：通过请求的 model 字段选择上游，支持模型 id 列表：
  - codex: `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.5`, `gpt-5.4`, `gpt-5.3-codex`, `gpt-5.4-mini`, `gpt-5.2`
  - kimi: `kimi-for-coding`, `kimi-k2.6`, `k2.6`
  - grok: `grok-composer-2.5-fast`, `grok-4.5`
  - cursor: `cursor`, `cursor-plan`, `cursor-ask`, `composer-2.5`, `composer-2.5-fast`

## Goals / Non-Goals

### Goals

- **阶段一**：voidx 通过现有 profile 机制接入 claude-code-proxy，使用 codex/grok/kimi/cursor 的 OAuth 登录态。
- **阶段二**：fork claude-code-proxy，新增 antigravity provider（Google OAuth）。
- **阶段二**：fork 版本新增 `/v1/chat/completions` OpenAI 协议入口，body 透传不翻译。

### Non-Goals

- 不改 voidx 代码——现有 provider/profile 机制足够。
- 不在 voidx 内用 Python 重写 OAuth 逻辑。
- 不做 voidx 原生 HTTP 代理服务。
- 不做多账号负载均衡（claude-code-proxy 自身已支持）。

## Architecture

### 阶段一：voidx 零改动接入

```text
voidx (Python, 零改动)
  │  anthropic protocol
  │  base_url = http://127.0.0.1:18765  (来自 profile)
  ▼
claude-code-proxy (Rust, 独立进程)
  │  OAuth token 注入 + Anthropic→上游协议翻译
  ▼
codex / grok / kimi / cursor 上游 API
```

voidx 侧配置（通过现有 profile 机制，无代码改动）：

```
profile name: claude-code-proxy/gpt-5.6-sol
base_url: http://127.0.0.1:18765
protocol: anthropic
api_key: unused
```

使用方式：

```bash
# 1. 安装 claude-code-proxy
brew install raine/claude-code-proxy/claude-code-proxy

# 2. OAuth 登录（一次性）
claude-code-proxy codex auth login

# 3. 启动代理
claude-code-proxy serve &

# 4. voidx 里存 profile 指向 localhost，然后正常使用
./python.py -p claude-code-proxy -m gpt-5.6-sol
```

### 阶段二：fork 扩展

```text
voidx (Python, 零改动)
  │  anthropic protocol  OR  openai protocol（透传）
  ▼
claude-code-proxy (fork, Rust)
  │  + antigravity provider (Google OAuth)
  │  + /v1/chat/completions 入口（OpenAI 协议，body 透传）
  ▼
codex / grok / kimi / cursor / antigravity 上游 API
```

## Fork 扩展计划（阶段二）

### 改动文件（Rust，fork 仓库内）

| 文件 | 职责 |
|---|---|
| `src/providers/antigravity.rs` | antigravity provider——Google OAuth PKCE flow |
| `src/server.rs` | 加 `/v1/chat/completions` OpenAI 协议入口路由 |
| `src/registry.rs` | 加 antigravity 模型 id 注册 |
| `src/auth.rs` | 复用 token 存储机制，扩展 Google OAuth token 类型 |

### 任务清单

- [ ] fork `raine/claude-code-proxy`
- [ ] 新增 `src/providers/antigravity.rs`——Google OAuth PKCE flow，参考 `antigravity-claude-proxy`（Node.js）的实现逻辑
- [ ] `src/server.rs` 加 `/v1/chat/completions` 路由，OpenAI 协议 body 透传：
  - 上游是 OpenAI 兼容（codex/grok）→ body 完全不改，仅注入 OAuth `Authorization` 头
  - 上游是 anthropic → 走现有翻译逻辑
- [ ] `src/registry.rs` 加 antigravity 模型 id
- [ ] CI 构建多平台二进制并发布

### 验证命令

```bash
# antigravity OAuth
<fork-binary> antigravity auth login
<fork-binary> serve &

# voidx 接入（profile 指向 localhost）
./python.py -p claude-code-proxy -m <antigravity-model-id>

# OpenAI 协议透传
curl http://127.0.0.1:18765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.6-sol","messages":[{"role":"user","content":"hi"}],"stream":true}'
```

## Constraints

- **voidx 零代码改动**——阶段一完全通过现有 profile 机制接入。
- **不引入 Rust 工具链依赖到 voidx**——claude-code-proxy 是预编译二进制。
- **OAuth 凭证由 claude-code-proxy 独立管理**，voidx 不读写其 token 文件。
- **fork 的改动在 claude-code-proxy 仓库内**，不污染 voidx 仓库。

## Forbidden Changes

- 不改 voidx 的 `src/` 任何文件。
- 不在 voidx 内实现 OAuth 流程。
- 不把 claude-code-proxy 二进制打包进 voidx wheel。
