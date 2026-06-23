# 配置全局化 — 技术设计文档

> **Status: Done**

## Context

当前所有配置存储在 `<workspace>/.voidx/settings.json`，用户切换工作区后需要重新配置 MCP 服务器、API Key、IDE 偏好等。只有 provider/model/api_key 通过 `~/.voidx/store/voidx.db` 实现了全局化。用户期望"配一次，处处生效"。

## Goals and Non-Goals

### Goals

- 引入 `~/.voidx/settings.json` 全局配置层
- 读取时按"全局 → 工作区 → 硬编码"三层合并
- 全局归属项的 setter 写入全局文件
- 工作区可覆盖全局配置（含 MCP 服务器禁用）
- 现有用户升级时自动迁移，行为无感变化
- 清理 `custom_providers` / `custom_models` 的 legacy JSON 读取路径；custom provider/model 信息以已保存的 DB model profiles 为准
- 降低多工作区写全局文件的冲突面；低频用户配置仍保留在可编辑 JSON 中

### Non-Goals

- 不改变 `~/.voidx/store/voidx.db` 中 model_profiles 的存储方式
- 不改变 `.voidx/lsp.json` 的存储位置（LSP 配置跟项目技术栈绑定）
- 不改变 `.voidx/skills.json` 的存储位置（skill 启用状态跟项目绑定）
- 不做配置 UI / TUI 界面改动（本次只改存储层和读取逻辑）
- 不提供显式的全局配置管理命令（如 `/config --global`）
- 不新增 `global_settings`、`custom_providers`、`custom_models` DB 表

## 配置项全表

### 归属分类

| 配置项 | 归属 | 合并策略 | 理由 |
|---|---|---|---|
| `current_profile` | 全局 | 简单值覆盖 | 用户选的模型跨项目一致 |
| `mcpServers` | 全局为基础，工作区可覆盖 | 字典合并 | tavily 等通用工具全局配一次；项目专属 MCP 工作区覆盖/禁用 |
| `tavily_api_key` | 全局 | 简单值覆盖 | API key 是用户级 |
| `custom_providers` | 废弃 legacy JSON | 不参与合并 | provider 协议/base_url 已由 DB model_profiles 承载 |
| `custom_models` | 废弃 legacy JSON | 不参与合并 | custom model 列表从 DB profiles 推导 |
| `codeIde` | 全局 | 简单值覆盖 | IDE 偏好是用户级 |
| `userProfile` | 全局 | 字典合并 | 语言/语气是用户偏好 |
| `web` (search/fetch) | 全局为基础，工作区可覆盖 | 字典合并 | 默认全局，项目可覆盖 |
| `update_check` | 全局 | 字典合并 | 版本检查是用户级 |
| `parallel_subagents` | 全局为基础，工作区可覆盖 | 字典合并 | 一般全局，特殊项目可调 |
| `permission_mode` | 工作区 | 不合并 | 不同项目安全策略不同 |
| `sandbox_mode` | 工作区 | 不合并 | 同上 |
| `sandbox_workspace_write` | 工作区 | 不合并 | 路径跟项目绑定 |
| `approval_policy` | 工作区 | 不合并 | 安全策略项目级 |
| `approval_reviewer` | 工作区 | 不合并 | 同上 |
| `ask_compact` | 工作区 | 不合并 | 运行时行为 |
| `skills` (enabled/disabled/auto) | 工作区 | 不合并 | 不同项目用不同 skill |

### 不涉及的配置（已有独立机制）

| 配置 | 存储位置 | 说明 |
|---|---|---|
| model_profiles (provider/model/api_key/base_url/protocol) | `~/.voidx/store/voidx.db` | 已全局化 |
| `AGENTS.md` | `~/.voidx/AGENTS.md` + workspace | 已有全局+工作区层级 |
| `lsp.json` | `<workspace>/.voidx/lsp.json` | 跟项目技术栈绑定，不全局化 |
| `skills.json` | `<workspace>/.voidx/skills.json` | skill 启用状态跟项目绑定，不全局化 |

## Architecture

### 文件布局

```
~/.voidx/
├── settings.json          ← 新增：全局配置
├── store/voidx.db         ← 不变：model_profiles
├── AGENTS.md              ← 不变：全局指令
├── sessions/              ← 不变
├── logs/                  ← 不变
└── skills/                ← 不变：全局 skill 定义

<workspace>/.voidx/
├── settings.json          ← 不变：工作区配置（全局项迁移后变薄）
├── skills.json            ← 不变
├── lsp.json               ← 不变
└── transcript.log         ← 不变
```

### 三层合并读取

```
┌─────────────────┐
│  硬编码默认值     │  最底层：ModelConfig、Enum 默认值等
├─────────────────┤
│  全局配置         │  ~/.voidx/settings.json
│  (GLOBAL_KEYS)   │  用户级偏好：profile、MCP、API key、IDE 等
├─────────────────┤
│  工作区配置       │  <workspace>/.voidx/settings.json
│  (所有 key)      │  覆盖全局 + 工作区专属项
└─────────────────┘
```

**合并规则：**

1. **简单值**（`codeIde`、`tavily_api_key`、`current_profile`）：工作区有值 → 用工作区；无值 → 用全局；都无 → 硬编码
2. **字典合并**（`mcpServers`、`userProfile`、`web`、`update_check`、`parallel_subagents`）：以全局为基础，工作区同名 key 覆盖全局，工作区独有的 key 追加
3. **Legacy custom provider/model**：`custom_providers` / `custom_models` 不参与新合并；`Settings.create()` 的旧 profiles 迁移可读取一次旧 provider 字段后删除这些 JSON key，运行时 custom model/provider 信息从 DB profiles 推导
4. **MCP 特殊处理**：工作区可设 `disabled: true` 禁用全局服务器；不引入 `_removed` 标记
5. **纯工作区项**（`permission_mode` 等）：只读工作区，不读全局

### 写入策略

```
setter 调用
    │
    ├── key ∈ GLOBAL_KEYS 且工作区无覆盖 → 写入 _global_data + _save_global()
    ├── key ∈ GLOBAL_KEYS 且工作区有覆盖 → 写入 _data + _save()（保留覆盖）
    ├── key ∈ WORKSPACE_ONLY_KEYS       → 写入 _data + _save()
    └── key ∈ GLOBAL_KEYS 且用户显式要求全局 → 写入 _global_data + _save_global()
```

**简化实现**：全局归属项的 setter 默认写全局；如果工作区已有该 key 的值，写工作区（保持覆盖语义）。不提供显式全局管理命令。

## Data Model

### 全局 settings.json 结构

```json
{
  "current_profile": "typex/zai-org/GLM-5-FP8",
  "mcpServers": {
    "tavily": {
      "command": "npx",
      "args": ["-y", "tavily-mcp@latest"],
      "env": { "TAVILY_API_KEY": "..." },
      "disabled": false,
      "tools": ["tavily_search", "tavily_extract", "tavily_crawl", "tavily_map", "tavily_research"],
      "transport": ""
    }
  },
  "tavily_api_key": "...",
  "codeIde": "trae",
  "userProfile": { "language": "zh-CN", "tone": "concise" },
  "web": {
    "search": { "backend": "legacy", "server": "", "tool": "" },
    "fetch": { "backend": "legacy", "server": "", "tool": "" }
  },
  "update_check": { "enabled": true, "last_checked_at": 0, "last_latest_version": "" },
  "parallel_subagents": { "enabled": false, "max_concurrent": 4 }
}
```

### 工作区 settings.json 结构（迁移后）

```json
{
  "permission_mode": "accept-edits",
  "sandbox_mode": "workspace-write",
  "sandbox_workspace_write": [],
  "approval_policy": "untrusted",
  "approval_reviewer": "user",
  "ask_compact": false
}
```

工作区仍可包含全局 key 来覆盖全局配置：

```json
{
  "permission_mode": "accept-edits",
  "mcpServers": {
    "internal-api": { "command": "...", "disabled": false },
    "tavily": { "disabled": true }
  }
}
```

### Key 归属常量

```python
GLOBAL_KEYS = frozenset({
    "current_profile",
    "mcpServers",
    "tavily_api_key",
    "codeIde",
    "userProfile",
    "web",
    "update_check",
    "parallel_subagents",
})

# custom_providers / custom_models 是 legacy JSON key，不进入 GLOBAL_KEYS。
# Settings.create() 可在旧 profiles 迁移时读取一次并删除，运行时列表从 DB profiles 推导。

WORKSPACE_ONLY_KEYS = frozenset({
    "permission_mode",
    "sandbox_mode",
    "sandbox_workspace_write",
    "approval_policy",
    "approval_reviewer",
    "ask_compact",
    "skills",
})
```

## custom_providers / custom_models 收敛为 legacy 清理

### 背景

`custom_providers` 和 `custom_models` 已不是新的写入来源。当前代码中 `add_custom_provider()` / `add_custom_model()` 是 legacy no-op；provider 的 `protocol` / `base_url` 和 model 名都已经随 `model_profiles` 存入 DB。剩余问题是旧 JSON 中可能还有残留 key：

- `_migrate_legacy_profiles()` 仍需要在迁移旧 `profiles` 时读取 `custom_providers[provider].base_url/protocol`
- 迁移完成后删除 `custom_providers` / `custom_models`
- 运行时 `list_custom_models(provider)` 从 `list_profiles()` 推导
- 运行时 `list_custom_providers()` 不再读取 `_data["custom_providers"]`

### 不新增 DB Schema

不新增 `custom_providers` 或 `custom_models` 表。现有 `model_profiles` 已保存：

- `provider`
- `model`
- `base_url`
- `protocol`
- `api_key`

### SettingsCustomProviderMixin 改造

```python
class SettingsCustomProviderMixin:
    def list_custom_providers(self) -> list[dict[str, str]]:
        # 删除运行时读取 legacy _data["custom_providers"] 的逻辑。
        # build_config 直接使用 resolved Profile 的 protocol/base_url。
        return []

    def add_custom_provider(self, name: str, protocol: str = "openai", base_url: str = "") -> None:
        # Legacy no-op. Provider protocol/base_url live on saved DB profiles.
        ...

    def remove_custom_provider(self, name: str) -> None:
        # Remove legacy JSON residue only.
        ...

    async def list_custom_models(self, provider: str) -> list[str]:
        # Derive from saved DB profiles.
        ...

    def add_custom_model(self, provider: str, model: str) -> None:
        # Legacy no-op.
        ...

    def remove_custom_model(self, provider: str, model: str) -> None:
        # Remove legacy JSON residue only.
        ...
```

### 迁移/清理

`_migrate_legacy_profiles()` 继续在 `Settings.create()` 中执行：

1. 读取旧 `profiles`
2. 若存在 `custom_providers[provider]`，取出 `base_url` / `protocol` 写入对应 `model_profiles`
3. 删除 `profiles` / `default_profile` / `custom_providers` / `custom_models`

`_migrate_to_global()` 不负责 custom provider/model DB 迁移。

## API Contract

### Settings 类变更

```python
class Settings:
    # 新增属性
    _global_path: Path          # ~/.voidx/settings.json
    _global_data: dict          # 全局配置数据

    # 新增方法
    def _load_global(self) -> dict
    def _save_global(self) -> None
    def _effective_data(self) -> dict   # 三层合并结果，带缓存
    async def _migrate_to_global(self) -> None  # 首次迁移，只在 create() 调用

    # 修改方法
    def __init__(self, workspace): ...   # 加载 _global_data
    async def create(cls, workspace): ... # 加载 _global_data + async 迁移
```

### _effective_data 合并逻辑

```python
def _effective_data(self) -> dict:
    if self._effective_cache is not None:
        return _deep_copy(self._effective_cache)
    merged = {}
    # 1. 全局基础
    for k, v in self._global_data.items():
        if k in GLOBAL_KEYS:
            merged[k] = _deep_copy(v)
    # 2. 工作区覆盖全局项
    for k, v in self._data.items():
        if k not in GLOBAL_KEYS:
            continue
        if k in merged and isinstance(v, dict) and isinstance(merged[k], dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = _deep_copy(v)
    # 3. 工作区独有项
    for k, v in self._data.items():
        if k not in GLOBAL_KEYS:
            merged[k] = _deep_copy(v)
    self._effective_cache = _deep_copy(merged)
    return merged
```

### MCP 合并逻辑

```python
def list_mcp_servers(self) -> list[McpServerConfig]:
    global_servers = dict(self._global_data.get("mcpServers", {}))
    ws_servers = dict(self._data.get("mcpServers", {}))
    merged = {**global_servers, **ws_servers}
    result = []
    for name, cfg in merged.items():
        if not isinstance(cfg, dict):
            continue
        result.append(McpServerConfig(name=name, **cfg))
    return result
```

### 写入归属判断

```python
def _write_target(self, key: str) -> str:
    """返回 'global' 或 'workspace'"""
    if key in WORKSPACE_ONLY_KEYS:
        return "workspace"
    if key in GLOBAL_KEYS:
        # 工作区已有该 key 的值 → 写工作区（保持覆盖）
        if key in self._data:
            return "workspace"
        return "global"
    return "workspace"
```

## 迁移策略

### 首次运行迁移

当 `~/.voidx/settings.json` 不存在，但工作区 `settings.json` 包含全局归属项时：

```python
async def _migrate_to_global(self) -> None:
    if self._global_path.exists():
        return
    global_items = {}
    for k in GLOBAL_KEYS:
        if k in self._data:
            global_items[k] = self._data[k]
    if not global_items:
        return
    self._global_data = global_items
    self._save_global()
    # 从工作区移除已迁移的全局项
    for k in global_items:
        self._data.pop(k, None)
    self._save()
```

### 迁移时机

只在 `Settings.create()` 中执行，顺序为：

1. 同步加载工作区 `_data` 和全局 `_global_data`
2. `await _migrate_legacy_profiles()`：清理旧 `profiles` / `custom_providers` / `custom_models`
3. `await _migrate_to_global()`：把工作区中的全局归属项搬到 `~/.voidx/settings.json`

`Settings.__init__()` 保持同步，只加载文件，不执行需要 DB 或 async API 的迁移。

### 多工作区场景

- 工作区 A 迁移后，`~/.voidx/settings.json` 已存在
- 工作区 B 首次运行时，检测到全局文件已存在，跳过迁移
- 工作区 B 的本地全局归属项会成为工作区覆盖项，而不是被全局覆盖
- 这保留旧工作区的显式本地配置，避免无提示改变行为；同时也意味着"配一次，处处生效"只对没有本地覆盖的工作区成立
- 后续可提供诊断/清理提示，列出工作区 settings 中仍覆盖全局的 key，用户确认后再删除

### 回滚

删除 `~/.voidx/settings.json`，将全局项手动复制回工作区 `settings.json` 即可恢复旧行为。

## 并发写入策略

### 问题

多个 voidx 实例（不同工作区）同时写入 `~/.voidx/settings.json` 可能导致数据丢失。

### 方案：全局 JSON 为主，DB 只存 model profiles

| 配置项 | 存储方式 | 理由 |
|---|---|---|
| `model_profiles` | `~/.voidx/store/voidx.db` | 已有全局 DB 存储 |
| `current_profile` | settings.json | 简单用户偏好；保留同步读取/写入路径 |
| `update_check` | settings.json | 每日级低频写入；保持 `mark_update_check()` 同步 |
| `mcpServers` | settings.json | 低频写入，用户需可手动编辑 |
| `tavily_api_key` | settings.json | 低频写入 |
| `codeIde` | settings.json | 低频写入 |
| `userProfile` | settings.json | 低频写入 |
| `web` | settings.json | 低频写入 |
| `parallel_subagents` | settings.json | 低频写入 |

不新增 `global_settings` 表。`current_profile` 留在全局 settings JSON 中，避免在 `resolve_profile()` / `save_profile()` 中引入额外异步 DB 读写；`update_check` 保持当前同步 JSON API。

### 全局配置最终存储分布

```
~/.voidx/store/voidx.db
└── model_profiles       ← 已有：provider/model/api_key/base_url/protocol

~/.voidx/settings.json
├── current_profile
├── mcpServers
├── tavily_api_key
├── codeIde
├── userProfile
├── web
├── update_check
└── parallel_subagents
```

### 并发安全

- DB 写入：仅 model_profiles 走 SQLite，使用现有事务机制
- settings.json 写入：低频配置，并发冲突概率较低；若发生，后写覆盖，可接受
- `_effective_data` 使用缓存；任何 `_save()` / `_save_global()` / 迁移写入后都必须将 `_effective_cache` 置空

## Error Handling

| 失败场景 | 处理策略 |
|---------|---------|
| 全局文件 JSON 解析失败 | 忽略全局层，仅用工作区+硬编码，log warning |
| 全局文件写入失败（权限等） | 降级写入工作区，log warning |
| 迁移过程中写入全局失败 | 不移除工作区中的全局项，保持旧行为 |
| 全局和工作区同一 MCP 服务器定义冲突 | 工作区定义覆盖全局（含 disabled 状态） |

## Decisions Log

| 决策 | 备选方案 | 选择理由 |
|------|---------|---------|
| 全局配置用 JSON + 既有 model_profiles DB | 纯 JSON / 新增 global_settings DB | model profiles 已全局 DB 化；其它配置低频且用户需可编辑，保留 JSON 更简单 |
| 三层合并而非两层 | 仅全局+硬编码 | 工作区覆盖是刚需（如项目级 MCP 禁用） |
| `custom_providers` / `custom_models` 作为 legacy JSON 清理 | 新增 DB 表 | 现有 add API 已是 no-op；provider/model 信息已在 model_profiles 中，无需新表 |
| `current_profile` 留在全局 JSON | 存入 DB global_settings | 简单偏好值，保留同步读写路径，避免新增表和调用链复杂度 |
| `update_check` 留在全局 JSON | 存入 DB global_settings | 每日级低频写入，当前 API 同步，放 DB 收益不足 |
| 不提供显式全局配置管理命令 | 提供 `/config --global` | 减少用户认知负担，setter 自动判断归属即可 |
| MCP 工作区禁用使用 `disabled: true` | `_removed` 内部标记 | 避免把内部 tombstone 暴露给用户 JSON；禁用语义已存在 |
| 迁移时从工作区删除全局项 | 保留在工作区 | 避免重复存储，减少混淆 |
| setter 默认写全局（工作区无覆盖时） | 所有 setter 写工作区 | 全局项配一次生效，符合用户预期 |

## Review Issues Resolved

- custom provider/model：已改为 legacy JSON 清理，不新增 DB 表，不再描述完整迁移。
- `_effective_data`：已改为全局基础、全局项覆盖、workspace-only 三段逻辑，并要求缓存/失效。
- MCP 删除语义：不引入 `_removed`，工作区禁用统一使用 `disabled: true`。
- `current_profile`：已确定留在全局 JSON，不进入 DB `global_settings`。
- 多工作区残留：已明确会成为工作区覆盖项，不会被全局覆盖；后续可做诊断/清理提示。
- `update_check`：已确定留在全局 JSON，保持同步 API。
- `_migrate_to_global`：已标注为 async，并限定只在 `Settings.create()` 中调用。

## Resolved Questions

- [x] 是否需要 `/config --global` 命令让用户显式管理全局配置？ → **暂不提供**，setter 自动判断归属
- [x] `custom_providers` 和 `custom_models` 是否应该新增 DB 表？ → **否**，清理 legacy JSON 读取，运行时从 model_profiles 推导
- [x] `current_profile` 是否进 DB？ → **否**，留在全局 settings JSON，避免 `global_settings` 表
- [x] `update_check` 是否进 DB？ → **否**，低频同步 JSON 写入即可
- [x] 多工作区并发写入全局文件时是否需要文件锁？ → **暂不需要**，低频配置写 JSON 可接受后写覆盖；model_profiles 继续走 DB
