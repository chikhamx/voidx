# User Profile 配置设计文档

> **Status: Done**

## 1. 背景

当前 `BASE_SYSTEM_PROMPT` 里有一条硬编码语言规则：

> **Match the user's language.** If the user writes in Chinese, respond in Chinese. If they write in English, respond in English. Mirror their tone.

这完全依赖 LLM 从当前用户输入推断沟通偏好。以下场景会不稳定：

1. 首轮消息很短，例如 `fix this`，无法稳定判断用户偏好的回复语言。
2. compaction 或 resume 后，历史语言线索可能被弱化。
3. 子 agent 重新组装 prompt 后，可能偏回英文。
4. 用户代码、错误日志、提交信息可能是英文，但沟通偏好可能是中文。

语言不是唯一画像信息。语气偏好同样属于用户画像：有些用户希望直接、简洁，有些希望更正式或更友好。因此本阶段引入最小用户画像结构，只包含 `language` 和 `tone`。

## 2. 目标

- 支持 workspace 级用户画像配置。
- 首期只支持 `language` 和 `tone`。
- 主 agent 和子 agent 使用同一套用户画像。
- 不改变 session runtime state，不把用户画像写入每个 session。
- 保留自动语言检测：`language=""` 表示 auto。

## 3. 数据模型

### 3.1 Config 新增 UserProfile

```python
# src/voidx/config/models.py
class UserProfile(BaseModel):
    language: str = ""  # BCP 47 tag, e.g. "zh-CN", "en"; empty = auto
    tone: str = ""      # e.g. "direct", "friendly", "formal"; empty = default


class Config(BaseModel):
    ...
    user_profile: UserProfile = Field(default_factory=UserProfile)
```

### 3.2 Settings 使用 `userProfile`

主存储 key 使用 camelCase，符合现有 `settings.json` 风格：

```json
{
  "userProfile": {
    "language": "zh-CN",
    "tone": "direct"
  }
}
```

为兼容手写配置，可读取旧式 snake_case：

- `user_profile`
- `user_language`
- `user_tone`

但写入时统一写回 `userProfile`。

### 3.3 Settings API

```python
def get_user_profile(self) -> UserProfile: ...
def set_user_language(self, language: str) -> Path: ...
def set_user_tone(self, tone: str) -> Path: ...
```

`language` 规则：

- `auto`、`detect`、`default`、空字符串 -> 清空为 `""`
- `set_user_language("")` 是显式清除，不是 no-op；保存时移除 `userProfile.language`，如果 profile 没有其他字段则移除整个 `userProfile`
- 其他值原样 strip 后保存，例如 `zh-CN`、`en`

`tone` 规则：

- `auto`、`default`、空字符串 -> 清空为 `""`
- `set_user_tone("")` 是显式清除，不是 no-op；保存时移除 `userProfile.tone`，如果 profile 没有其他字段则移除整个 `userProfile`
- 常用值建议：`direct`、`friendly`、`formal`
- 不做严格枚举校验，避免限制用户自定义描述

## 4. Runtime Context 注入

### 4.1 RuntimeEnvelope

`RuntimeEnvelope` 增加 `user_profile`，`_render_envelope()` 只渲染非空字段：

```text
- User language: Chinese (Simplified) [zh-CN]
- User tone: direct
```

语言渲染优先将常见 tag 映射为自然语言，避免只给 LLM 一个 tag：

| tag | 渲染 |
|-----|------|
| `zh-CN` | `Chinese (Simplified) [zh-CN]` |
| `zh-TW` | `Chinese (Traditional) [zh-TW]` |
| `en` | `English [en]` |
| `ja` | `Japanese [ja]` |
| `ko` | `Korean [ko]` |

未知 tag 使用原值。

### 4.2 Current Task State

在 `_current_task_state()` 中追加用户画像指令：

```text
- User language preference: Chinese (Simplified) [zh-CN]
- Language instruction: Prefer responding in Chinese (Simplified) unless the user explicitly asks otherwise.
- User tone preference: direct
- Tone instruction: Keep the response tone direct.
```

当 `language=""` 时，保留 `BASE_SYSTEM_PROMPT` 的自动检测规则，不注入显式语言指令。

当 `tone=""` 时，不注入显式语气指令，沿用基础 prompt。

## 5. 数据流

```
.voidx/settings.json
  userProfile.language / userProfile.tone
    │
    ├─ Settings.get_user_profile()
    ├─ Settings.build_config()
    │    └─ Config(user_profile=UserProfile(...))
    │
    ├─ VoidXGraph(config)
    │    └─ _prepare_with_stream()
    │         └─ RuntimeContextBuilder(config=config)
    │              └─ Current Task State / Runtime State 注入用户画像
    │
    └─ run_subagent(config=config)
         └─ RuntimeContextBuilder(config=context_config)
              └─ 子 agent 注入同一用户画像
```

## 6. Slash 命令

新增两个快捷命令：

```text
/lang             显示当前语言偏好
/lang zh-CN       设置回复语言偏好
/lang en          设置英文偏好
/lang auto        恢复自动检测

/tone             显示当前语气偏好
/tone direct      设置直接风格
/tone friendly    设置友好风格
/tone formal      设置正式风格
/tone default     恢复默认风格
```

实现建议：

- 新增 `src/voidx/agent/slash/profile.py` mixin。
- `SlashHandler` 继承该 mixin。
- `COMMANDS` 注册 `/lang` 和 `/tone`。
- 命令写 settings 后，同时更新当前 `graph.config.user_profile`，保证当前会话立即生效。

## 7. 实现计划

| 步骤 | 描述 | 文件 |
|------|------|------|
| 1 | 新增 `UserProfile` 和 `Config.user_profile` | `src/voidx/config/models.py` |
| 2 | Settings 增加用户画像读写和 `build_config()` 传入 | `src/voidx/config/settings.py` |
| 3 | Runtime context 渲染 language/tone | `src/voidx/agent/runtime_context.py` |
| 4 | 子 agent 复用 `config.user_profile` | `src/voidx/agent/graph/subagent.py` |
| 5 | 新增 `/lang`、`/tone` 命令 | `src/voidx/agent/slash/profile.py`, `handler.py`, `ui/commands.py` |
| 6 | 增加 focused tests | `tests/test_config.py`, `tests/test_agent/test_runtime_context.py`, `tests/test_agent/test_slash_model.py`, `tests/test_agent/test_core_flow.py` |

## 8. 风险与权衡

| 风险 | 缓解 |
|------|------|
| 用户设置语言但本轮明确要求另一种语言 | 指令使用 `Prefer`，允许用户显式覆盖 |
| tag 不是标准 BCP 47 | 不严格校验；常见 tag 做自然语言映射，未知值原样传给 LLM |
| tone 值过于自由 | MVP 不限制；后续可在 UI 层提供常用候选 |
| 子 agent 忽略画像 | 画像进入 `Current Task State`，主 agent 和子 agent 都通过 `RuntimeContextBuilder` 注入 |

## 9. 后续扩展

后续用户画像可以继续扩展，但不在本阶段实现：

- `response_detail`
- `timezone`
- `locale`
- `code_explanation`
- `final_summary`
- `ask_style`
