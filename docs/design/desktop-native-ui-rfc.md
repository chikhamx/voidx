---
name: desktop-native-ui-rfc
display_name: RFC: 桌面端平台原生 UI 组件引入方案
description: 评估在 voidx 桌面端(Tauri 2)引入平台原生 UI 组件的各条技术路线,给出选型推荐与取舍
doc_type: rfc
audience: human
---

# RFC: 桌面端平台原生 UI 组件引入方案

## TL;DR

建议**不引入原生 UI 控件**,继续走 Tauri 标准形态(原生窗口壳 + Web 内容 + 插件调系统 API)。当前 `desktop/tauri/` 已是此架构,且 `frontend/` 的 TS SPA 同时服务 Web 与桌面,引入原生控件会破坏这套复用。真正值得做的增量是:(1) 打磨原生窗口装饰(macOS 标题栏融合、Win11 Mica 背景);(2) 按需补齐原生行为插件(托盘、全局快捷键、原生菜单)。本 RFC 不涉及具体实现,仅做路线选型。

## Why

- **触发原因**:用户希望桌面端"更像原生应用",疑问是否应直接使用 NSButton/Win32 Button 等平台原生控件。
- **现状**:voidx 桌面端是 Tauri 2 应用,窗口壳由各平台原生窗口 API 提供(macOS NSWindow / Windows Win32 / Linux GTK),内容区由系统 WebView 渲染 `frontend/dist` 的 TS SPA。`tauri.conf.json` 已配置 `titleBarStyle: "Overlay"`,`Cargo.toml` 已引入 `tauri-plugin-dialog`。
- **核心矛盾**:Tauri 的设计哲学是"壳原生、内容 Web",它**不提供原生 UI 控件库**。硬塞原生控件是逆设计,且会破坏 `frontend/` 在 Web/桌面间的复用。

## What Changes

本 RFC 为**选型决策**,不直接产生代码变化。若采纳推荐方案,后续会产生的行为变化:

- 用户感知:窗口标题栏、背景效果、托盘、快捷键更贴近系统原生;按钮/输入框等仍为 Web 渲染。
- 工程:新增少量 Tauri 插件依赖与 `tauri.conf.json` 配置,不改动 `frontend/` UI 组件实现。
- 不变:不引入 NSButton/Win32 控件,不换 UI 技术栈。

## Impact

| Area | Impact | Owner / Notes |
|------|--------|---------------|
| Product / User | 原生感增强(标题栏、背景、托盘),但 UI 控件仍是 Web | 桌面端 |
| Engineering | 新增插件依赖与配置,`desktop/tauri/Cargo.toml` + `capabilities/default.json` 变动 | desktop |
| API / Compatibility | 新增若干 `#[tauri::command]` 或插件 invoke,不破坏现有 `get_gateway_url`/`get_backend_status`/`restart_backend` | 无破坏性 |
| Data / Migration | N/A | |
| Ops / Support | 无新增运行时依赖(WebView 仍用系统自带) | |

## Options Considered

| Option | Summary | Pros | Cons | Decision |
|--------|---------|------|------|----------|
| A. 维持现状 + 增量(推荐) | 不引入原生控件;加原生窗口装饰 + 按需插件(托盘/快捷键/原生菜单/Mica) | 保留 `frontend/` Web/桌面复用;零破坏;Tauri 甜点区;成本低 | UI 控件非原生,极端场景(百万行滚动)性能不及原生 | ✅ Adopt |
| B. 嵌入原生视图到 WebView | 用 `objc2`/Win32 把 NSButton 等叠在 WebView 上 | 局部真原生控件 | 逆 Tauri 设计;跨平台三套实现;焦点/HiDPI/输入法坑;维护爆炸 | ❌ Reject |
| C. 换纯原生 UI 技术栈 | 改用 Slint/egui/Flutter Desktop/各平台原生 SDK | 全原生控件,一致性高 | 丢弃 `frontend/` 全部复用;重写三端 UI;与 Web 版分裂 | ❌ Reject |
| D. 仅原生窗口装饰,不加插件 | 只做标题栏/背景,不引入托盘等行为插件 | 最小改动 | 错过高性价比的原生行为能力 | ⚠️ 可作为 A 的子集,不单列 |

### 各方案展开

**A. 维持现状 + 增量(推荐)**
- 窗口壳已是原生(`titleBarStyle: "Overlay"`),可再加 macOS `hiddenTitle` + `trafficLightPosition`、Win11 Mica 背景、`@tauri-apps/plugin-window-state` 持久化。
- 原生行为走插件:`plugin-tray`、`plugin-global-shortcut`、`plugin-menu`(Tauri 2 新增,真系统菜单)、`plugin-notification`、`plugin-clipboard-manager`。
- 现有 `main.rs` 的 `#[tauri::command]` 模式(get_gateway_url 等)已是"原生能力出口"范例,新插件沿用此模式。
- 代价:UI 控件仍是 Web。但 voidx 是对话型工具,无百万行原生表格需求,Web 渲染足够。

**B. 嵌入原生视图到 WebView**
- macOS:拿 NSWindow,`addSubview:` 叠 NSView;Windows:拿 HWND,`CreateWindowEx` 子控件 + `WM_*` 消息。
- 致命问题:Tauri 无官方 API 支持此用法;焦点在 WebView 与原生视图间打架;HiDPI 缩放双轨;输入法候选框错位;三平台各写一套。维护成本远超收益。

**C. 换纯原生 UI 技术栈**
- Slint(Rust 原生跨平台)、egui(即时模式)、Flutter Desktop(自绘)、或各平台原生(SwiftUI/WinUI/GTK)。
- 致命问题:`frontend/` 的 26 个 TS 文件(含 rpc/services/ui 完整栈)全部作废,需为桌面重写一套,且与 Web 版长期分裂。voidx 的核心价值在 Python 后端 + 协议,不在 UI 控件原生与否。

## Decision

**Adopt Option A**:维持 Tauri 标准形态,仅做原生窗口装饰与按需原生行为插件增量。不引入原生 UI 控件,不换技术栈。

- 决策人:待评审
- 决策日期:Pending
- 判据:`frontend/` 复用价值 >> 原生控件一致性收益;voidx 无 Web 做不到的核心 UI 需求。

## Non-Goals

- 不在 WebView 内嵌入 NSButton/Win32 等原生控件(Option B 否决)。
- 不替换 UI 技术栈为 Slint/Flutter/原生 SDK(Option C 否决)。
- 不为"原生控件"重写 `frontend/` 的任何 UI 组件。
- 不改变 `frontend/` 同时服务 Web 与桌面的复用关系。
- 本 RFC 不输出实现规格(路径/命令/测试),落地时另起 implementation-spec。

## Rollout / Migration

N/A(选型决策,无代码变更)。后续若落地 Option A 增量,按插件逐个灰度:每个插件独立 PR,先 macOS 验证再跨平台。

## Open Questions

- [ ] 是否有"Web 死活做不了"的核心原生控件需求?若否,Option A 即终态;若有,需重开 RFC 评估 Option B/C。
- [ ] 托盘与全局快捷键是否为产品必需,还是可选增强?影响插件引入范围。
- [ ] Win11 Mica 背景与现有 `frontend/css/tokens.css` 主题如何协调,避免视觉割裂?
