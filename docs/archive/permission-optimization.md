# 权限系统优化：plan 模式复用 read-only + 静态黑名单精简 + deny 转 ask

> **Status: Done** — Archived on 2026-07-12.

## 目标

1. plan 模式直接复用 read-only 沙箱逻辑，消除 `mode_overlay_denial_reason` 的重复实现。
2. 非 read-only 沙箱下，只对极度危险命令（静态黑名单）直接 deny，其余原 deny 全部转为 ask，改善用户体验。

## 架构

权限引擎 `authorize_tool_call` 的决策链路保持不变（sandbox_precheck → session → strategy → approval），但：
- sandbox_precheck 内部，plan 模式等价 read-only 处理。
- sandbox_precheck 的 workspace-write 分支中，git_policy 和 shell_policy 的"未注册/动态语法/复合操作符/路径越界"从 deny 改为 defer（交给后续 strategy/approval 走 ask）。
- 静态黑名单（极度危险命令）保留 deny。

## 技术栈

- Python 3.11+，dataclass + pydantic
- 权限引擎：`src/voidx/permission/engine.py`
- Git 策略：`src/voidx/permission/git_policy.py`
- Shell 策略：`src/voidx/permission/shell_policy.py`
- 路径授权：`src/voidx/permission/grants.py`
- 沙箱路径检查：`src/voidx/permission/sandbox.py`
- Bash 危险命令：`src/voidx/tools/bash/safety.py`（`_BLOCKED` 列表，已存在）
- Git 危险子命令：`src/voidx/tools/git.py`（`_DENIED_SUBCOMMANDS`，已存在）

## 文件结构

| 文件 | 责任 |
|------|------|
| `src/voidx/permission/engine.py` | plan 模式等价 read-only；移除 mode_overlay；workspace-write 下 sandbox_precheck 的 deny 改 defer |
| `src/voidx/permission/git_policy.py` | git_policy 区分"黑名单 deny"（危险配置）和"可 ask 的拒绝"（未注册命令）；git_sandbox_precheck 将后者转 defer |
| `src/voidx/permission/shell_policy.py` | shell_sandbox_precheck 将动态语法/复合操作符/未注册命令从 deny 改 defer；嵌套解释器保留 deny |
| `src/voidx/permission/grants.py` | resolve_access 中路径穿越保留 deny；路径越界（workspace 外非穿越）从 deny 改 defer |
| `src/voidx/permission/sandbox.py` | check_sandbox_filepath 路径越界从 deny 改 defer（返回 None 让后续走 ask） |
| `src/tests/test_agent/test_permission.py` | 更新 plan 模式和 workspace-write 下 deny→ask 的断言 |
| `src/tests/test_agent/test_permission_phase6.py` | 更新 shell policy 相关断言 |
| `src/tests/test_tools/test_git_tool_destructive.py` | 确认极度危险命令仍 deny |
| `src/tests/test_tools/test_file_tools_redesign.py` | 更新路径越界相关断言 |

## 静态黑名单定义（保留 deny）

### Bash（`bash/safety.py` `_BLOCKED`，已存在，不改）
- sudo、chmod 7xx、chown、chgrp、mkfs、dd to /dev/、write to /dev/sd*、reboot、shutdown、poweroff、init 0/6、fork bomb、force push to main/master、curl|bash、wget|bash

### Git（`git_policy.py` `_DANGEROUS_CONFIG_PREFIXES`，已存在，不改）
- alias.、core.askpass、core.editor、core.fsmonitor、core.hookspath、core.pager、core.sshcommand、credential.helper、filter.、gpg.program、gpg.ssh.program、include.、protocol.、sequence.editor

### Git（`tools/git.py` `_DENIED_SUBCOMMANDS`，已存在，不改）
- filter-branch、gc、prune、fsck
- reset --hard、clean -x/--force/-fd

### Shell（`shell_policy.py`）
- 嵌套解释器（bash/sh/zsh/fish/cmd/powershell/pwsh/python/node/ruby/perl）→ 保留 deny

### 路径（`grants.py`）
- 路径穿越（`..` 越界，`_normalize_path` 返回 None）→ 保留 deny

## 从 deny 改为 ask（defer）的项

### Git（`git_policy.py`）
- 空命令 → defer
- shlex 解析失败 → defer
- 未注册全局选项 → defer
- 未注册全局配置（非危险前缀） → defer
- 未注册子命令 → defer
- worktree 写操作 → defer

### Shell（`shell_policy.py`）
- 动态语法（$、`` ` ``、<(、>(） → defer
- 复合操作符（;、|、&&、||、>、>>、<） → defer
- PowerShell 括号 → defer
- 无法解析的命令 → defer
- 未注册命令 → defer（已有 `_requires_user_approval` 部分覆盖）

### 路径（`grants.py` + `sandbox.py`）
- workspace 外非穿越路径 → defer（已有 defer 机制，但 `require_exists` 和写路径不存在的 deny 需改 defer）

## 任务清单

### Task 1: plan 模式复用 read-only 逻辑
- [ ] `engine.py` `sandbox_precheck_action`：在 `context.sandbox_mode == "read-only"` 判断前，增加 `context.interaction_mode == "plan"` 的等价处理（plan 模式走 read-only 能力集）
- [ ] `engine.py` 移除 `mode_overlay_denial_reason` 函数（第133-145行）
- [ ] `engine.py` `authorize_tool_call` 移除 mode_overlay 调用（第36-38行）
- [ ] 测试命令：`./test.py --backend -- src/tests/test_agent/test_permission.py -k "plan"`

### Task 2: git_policy 区分黑名单 deny 和可 ask 的拒绝
- [ ] `git_policy.py` `git_policy_for_args`：危险配置前缀（`_DANGEROUS_CONFIG_PREFIXES`）保留 deny reason 含 "dangerous"；其余拒绝 reason 改为可识别的非危险标记
- [ ] `git_policy.py` `git_sandbox_precheck`：当 `decision.reason` 含 "dangerous" 时返回 deny；否则返回 defer
- [ ] 测试命令：`./test.py --backend -- src/tests/test_agent/test_permission.py -k "git"`

### Task 3: shell_policy 将非黑名单拒绝改 defer
- [ ] `shell_policy.py` `shell_sandbox_precheck`：嵌套解释器保留 deny；动态语法/复合操作符/未注册命令/无法解析 改为 defer（返回 `"defer", reason`）
- [ ] `shell_policy.py` `_requires_user_approval` 扩展或重构，覆盖所有应走 ask 的 reason
- [ ] 测试命令：`./test.py --backend -- src/tests/test_agent/test_permission_phase6.py`

### Task 4: 路径越界从 deny 改 defer
- [ ] `grants.py` `resolve_access`：路径穿越（`_normalize_path` 返回 None）保留 deny；`require_exists` 文件不存在改 defer；写路径不存在改 defer
- [ ] `sandbox.py` `check_sandbox_filepath`：路径越界返回 None（让后续走 ask），而非 deny reason
- [ ] `engine.py` `sandbox_precheck_action` workspace-write 分支：`check_sandbox_filepath` 返回非 None 时改 defer
- [ ] 测试命令：`./test.py --backend -- src/tests/test_tools/test_file_tools_redesign.py`

### Task 5: 更新受影响测试
- [ ] `test_permission.py`：plan 模式下 write/git_write/bash_write 从 deny 改为 deny（read-only 等价，行为不变，但 source 从 "mode" 变 "sandbox"）
- [ ] `test_permission.py`：workspace-write 下 git 未注册命令从 deny 改 ask
- [ ] `test_permission_phase6.py`：shell 动态语法/复合操作符从 deny 改 ask
- [ ] `test_file_tools_redesign.py`：路径越界从 deny 改 ask/defer
- [ ] `test_git_tool_destructive.py`：确认 filter-branch/reset --hard/clean -fd 仍 deny
- [ ] 测试命令：`./test.py --backend -- src/tests/test_agent/test_permission.py src/tests/test_agent/test_permission_phase6.py src/tests/test_tools/test_git_tool_destructive.py src/tests/test_tools/test_file_tools_redesign.py`

### Task 6: 全量回归
- [ ] 测试命令：`./test.py --backend`

## 风险

1. **plan 模式行为变化**：plan 模式下 deny 的 source 从 "mode" 变 "sandbox"，错误消息从 "BLOCKED by plan mode" 变 "SANDBOX READ-ONLY"。需检查 UI 层是否有依赖该消息文本的逻辑。
2. **deny→defer 可能导致意外 ask 弹窗增多**：原本静默拒绝的命令现在会弹窗询问用户，短期内交互增多。但这是预期行为改善。
3. **路径越界改 defer 后，恶意路径可能走到 ask**：但路径穿越仍 deny，且 ask 需用户批准，风险可控。
4. **`_requires_user_approval` 重构**：现有逻辑只覆盖 3 种 reason，扩展时需确保不遗漏。
5. **git_sandbox_precheck 返回类型**：当前返回 `tuple[str, str | None]`，改 defer 后需确认 engine.py 能正确处理 defer 分支（已有 defer 处理逻辑在第40-41行）。
