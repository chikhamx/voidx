---
name: macos-ocr-tool-2026-09-02
display_name: macOS OCR 工具调研与接入设计草案（已被独立 Rust CLI 方案取代）
description: 调研 Apple Vision OCR，并记录 voidx 在 macOS 上提供 OCR 能力的历史接入路线
doc_type: rfc
audience: human+llm
status: superseded
implementation_status: superseded
related_docs:
  - tools/macos-ocr/Cargo.toml
  - tools/macos-ocr/SKILL.md
  - tools/macos-ocr/src/lib.rs
  - src/voidx/agent/application/attachments.py
  - src/voidx/tooling/ports/tool.py
  - desktop/tauri/tauri.conf.json
---

# macOS OCR 工具调研与接入设计草案

## 1. 摘要

本文件是 2026-09-02 的历史 RFC。原提案曾计划把 Apple Vision OCR 接入 Python `ToolPlugin` 和附件 fallback；该路线已被 `tools/macos-ocr` 的独立 Rust CLI 方案取代，原有 Python/Tauri 接入内容仅保留为背景，不是当前实现规格。

**调研结论：**

- macOS 的首选 OCR 引擎是 Apple Vision 的 `VNRecognizeTextRequest`，而不是外部 `tesseract`、`osascript` 或网络 OCR 服务。
- Vision 的文字识别 API 从 macOS 10.15 可用；项目桌面端最低 macOS 为 12.0，系统版本满足要求。
- 原提案曾推荐 Python 后端通过 `pyobjc-framework-Vision` 接入，再视发布条件切换为 Tauri/Swift helper；该路线没有实施，已由 `tools/macos-ocr` 的独立 Rust CLI 取代。
- `ocr` 应是只读、文件范围受授权控制的工具。它必须复用 `authorized_path()`，不能因为“只是读取图片”而绕过 workspace、sandbox 或 grant 校验。
- “显式调用 `ocr`”和“非多模态模型收到图片时自动 OCR”是两个不同层次的功能。建议先确定稳定的显式工具契约，再以模型能力字段为前置条件实现自动 fallback；不要根据 provider/model 名称猜测视觉能力。

**当前状态（2026-09-03）：** 本 RFC 的 Python/Tauri 接入路线已被 `tools/macos-ocr` 独立 Rust CLI 取代。CLI 已完成 Apple Vision OCR、图像预处理、PDF、目录批处理、候选文字、布局排序、条码和 JSON/text/JSONL 输出；Apple Silicon arm64 图片、QR、PDF 和批量 smoke test 已通过；arm64/x86_64 release 构建和 universal binary 合并已通过，当前产物已完成 ad-hoc codesign。正式签名、notarization、DMG/Tauri 集成仍未完成。

## 2. 范围与非目标

### 2.1 原提案范围

- Apple Silicon 和 Intel macOS 桌面端的 Apple Vision OCR 可行性。
- Python 后端工具接口、路径授权、附件 fallback 的接入边界。
- Vision bridge 的依赖、打包、签名、线程、错误和隐私风险。
- 后续实现所需的测试和发布验收条件。

### 2.2 原提案非目标

- 原提案不实现 Python `ocr` 工具或附件自动 OCR fallback；当前独立 CLI 不接入 Python `ToolPlugin` 或附件链路。
- 本阶段不覆盖 Windows、Linux、PaddleOCR、RapidOCR、Tesseract 或跨平台统一引擎。
- 本阶段不改变当前多模态模型的图片输入协议。
- 原提案不承诺 HEIC、PDF、扫描文档版面分析、表格识别、手写识别或二维码识别；当前独立 CLI 已支持 PDF、二维码/条码和确定性的布局排序，但仍不支持 HEIC/HEIF、表格、表单或手写识别。
- 本阶段不把 Vision 的识别结果当作事实保证；OCR 文本必须带有低置信度和可能误识别的语义边界。

## 3. 原提案背景与当前实现状态

### 3.1 图片附件链路

当前实现已经有图片附件，但它服务的是模型图片输入，不是 OCR：

1. 前端通过 `attachments.saveImage` 保存图片到 workspace 下的 `.voidx/attachments`。
2. 前端使用 `[image-<stem>]` 作为用户消息中的附件 token。
3. `src/voidx/agent/application/attachments.py` 解析 token，当前识别的扩展名是 `.png`、`.jpg`、`.jpeg`、`.gif`、`.webp`。
4. 对图片大小的现有限制是 `MAX_IMAGE_ATTACHMENT_BYTES = 5_000_000`。
5. `build_user_message_payload()` 当前将图片构造成带 data URL 的结构化 `image_url` 消息；如果图片被跳过，则只产生 warning。
6. `turn_runner.py` 会把结构化内容作为 `HumanMessage` 送入模型；后续上下文重建会移除图片消息。

这意味着：如果一个不支持图片输入的模型收到当前结构化 `image_url`，失败可能发生在模型请求之前或 provider 层，不能仅靠增加一个可选工具解决。

### 3.2 模型能力现状

`src/voidx/llm/domain/model.py` 的 `ModelConfig` 目前没有 `supports_vision`、`image_input` 或等价字段；`ProviderSpec` 也没有视觉能力声明。因此当前代码无法可靠判断某个 provider/model 是否支持图片。

自动 fallback 至少需要一个明确的能力来源：

- `ModelConfig.image_input` 或 `supports_vision`；
- provider/model catalog 的显式能力声明；或
- 用户对未知模型的显式配置。

不能把模型名称中的 `vision`、`vl` 等字符串作为唯一判断依据。

### 3.3 工具和授权现状

工具契约是 `ToolPlugin`：提供 `id`、`description`、`parameters_schema()` 和异步 `execute()`，返回 `ToolResult`。工具注册使用显式闭集：

- `src/voidx/tooling/builtin/plugins.py` 实例化内置插件；
- `src/voidx/bootstrap/tooling.py` 维护 `TOOL_CAPABILITIES` 和 `_CATALOG_ORDER`；
- `ToolRegistry` 要求注册工具具有能力元数据；
- `src/voidx/tooling/policy/permission/rules.py` 负责能力分类、路径提取和权限规则；
- `FileScopedPlugin` 为文件型工具注入 `FileToolContext` 和授权服务。

`ocr` 必须作为文件型只读工具接入这些闭集。它不应加入 execution-only 工具集合，也不应通过 shell 调用 `sips`、`osascript` 或其他外部程序来规避文件授权。

### 3.4 桌面打包现状

- `desktop/tauri/tauri.conf.json` 把 `resources/backend/` 作为应用资源，macOS 最低版本为 12.0，目标包含 `app` 和 `dmg`。
- `desktop/build.sh` 先构建 Python backend image，再由 Tauri 打包；macOS 运行时按架构使用 `aarch64-apple-darwin` 或 `x86_64-apple-darwin` backend image。
- `scripts/build_desktop_backend.py` 会把 Python runtime、site-packages 和当前 wheel 合并到 backend image。

如果使用 PyObjC，依赖和其扩展模块必须进入 macOS backend image，并经过应用签名和 notarization 验证；如果使用 Swift helper，helper 的二进制、架构、签名和资源路径也必须进入同一发布验证链路。

## 4. Apple Vision 调研结果

### 4.1 相关 API

Apple Vision 提供的是系统框架，不需要在应用内分发 OCR 模型：

- `VNImageRequestHandler` 接收一个图片输入并执行一个或多个 Vision request；官方 API 支持 `CGImage`、`CIImage`、图片数据和 URL 等输入形式。
- `VNRecognizeTextRequest` 查找并识别图片中的文字区域，结果是 `VNRecognizedTextObservation` 集合。
- `VNRecognizedTextObservation` 同时包含识别出的文字区域和文字候选；可以通过 `topCandidates(_:)` 获取候选。
- `VNRecognizedText` 提供 top candidate 的 `string`、归一化的 `confidence`，并支持按字符串范围计算文字 bounding box。
- `VNRecognizedTextObservation` 继承 `VNRectangleObservation`，可提供归一化文字区域的几何信息。

关键 API 的 macOS availability：

| API | macOS availability | 项目 macOS 12 是否满足 |
|---|---:|---:|
| `VNImageRequestHandler` | 10.13+ | 是 |
| `VNRecognizeTextRequest` | 10.15+ | 是 |
| `VNRecognizedTextObservation` | 10.15+ | 是 |
| `VNRecognizedText` | 10.15+ | 是 |

### 4.2 可配置能力

`VNRecognizeTextRequest` 支持以下与工具设计直接相关的配置：

- `recognitionLevel`：在速度和准确率之间选择；工具可暴露为 `fast` / `accurate`。
- `recognitionLanguages`：按优先级传入待识别语言。
- `automaticallyDetectsLanguage`：请求自动选择语言模型和语言纠正策略。
- `usesLanguageCorrection`：是否启用语言纠正。
- `customWords`：为词识别补充自定义词。
- `minimumTextHeight`：按相对图片高度过滤过小文字。
- `supportedRecognitionLanguages...`：可查询指定 request revision 支持的语言。

MVP 不应把所有 Vision 参数暴露给模型。首版只需要语言列表、识别级别和是否返回区域；`customWords`、`minimumTextHeight` 和 request revision 可留在 bridge 内部，待真实图片 benchmark 后再开放。

### 4.3 坐标与结果语义

Vision 的区域坐标是归一化坐标，不应直接当作前端像素坐标。bridge 必须统一输出协议并写测试：

- 内部保留 Vision 原始坐标，或在 bridge 边界转换为项目约定的 top-left 原点坐标；
- 若采用 top-left 原点，转换公式应明确写在实现和测试中：`y_top = 1 - (vision_y + vision_height)`；
- 图片 EXIF orientation 必须在 handler 输入阶段处理，否则文字和 bounding box 可能错位；
- 区域结果至少包含 `text`、`confidence` 和 `bounding_box`；需要角点时再增加 `corner_points`；
- 不能假定观察结果天然完成复杂多栏排版。MVP 只保证确定性的结果顺序，不承诺文档版面重建。

推荐的逻辑结果模型如下，实际 `ToolResult.output` 是否直接序列化为该 JSON 需在实现阶段确认：

```json
{
  "engine": "apple-vision",
  "text": "识别出的全文",
  "languages": ["zh-Hans", "en-US"],
  "recognition_level": "accurate",
  "blocks": [
    {
      "text": "一行文字",
      "confidence": 0.98,
      "bounding_box": {"x": 0.12, "y": 0.70, "width": 0.42, "height": 0.08}
    }
  ],
  "truncated": false
}
```

`confidence` 是 Vision 的归一化识别置信度，不应被解释为整张图片或整段文本的正确率。

### 4.4 离线和隐私边界

应用调用 Vision 时可以把本地图片 URL/数据直接交给系统框架，不需要把图片上传到第三方 OCR 服务。MVP 不增加网络请求，也不把图片复制到临时云端或外部进程。

仍需在实机上确认：

- 首次使用某些语言时系统是否会有额外模型准备行为；
- macOS 应用 sandbox、TCC 和安全作用域文件访问是否会影响 workspace 外的已授权路径；
- 应用签名和 notarization 后，PyObjC 扩展或 helper 是否能正常加载。

应用层的 `authorized_path()` 只能表达 voidx 自身的授权，不等同于 macOS 的 OS 级文件访问 entitlement；两层都必须通过测试。

## 5. 接入路线选型

### 5.1 方案 A：Python 后端直接使用 PyObjC（推荐先做技术 spike）

Python 进程通过 `pyobjc-framework-Vision` 导入 `Vision`，由一个小型 adapter 创建 `VNRecognizeTextRequest` 和 `VNImageRequestHandler`，再把 Foundation/ Vision 对象转换成纯 Python 数据。

**优点：**

- OCR 逻辑留在现有 Python backend，不需要 Tauri command 或新的 IPC 协议。
- 可以直接复用 `ToolPlugin`、`FileToolContext`、`ToolResult` 和现有测试基础设施。
- PyPI 上存在 `pyobjc-framework-Vision`；公开元数据要求 Python 版本范围覆盖项目的 Python 3.11，但最终版本、wheel 标签和依赖必须锁定后验证。
- Vision 本身由 macOS 提供，不需要随应用分发大型 OCR 模型。

**风险：**

- PyObjC 是 macOS 条件依赖，不能在 Linux/Windows import；模块导入必须延迟或隔离。
- Objective-C block、`NSArray`、`NSValue`、NSError 和 request results 的桥接行为需要在目标 Python runtime 中实测。
- `asyncio` 取消不能保证中止已经进入系统框架的同步 native request；只能先做并发限制和软超时。
- bundled Python/site-packages 内的 native extension 必须参与 codesign/notarization 验证。

### 5.2 方案 B：随 Tauri 应用分发 Swift helper

以 Swift/Objective-C 编写小型命令行 helper，使用 JSON Lines 或一次性 JSON 请求从 stdin 接收路径和选项，向 stdout 返回受约束的 JSON；Python adapter 负责启动、传输和错误映射。

**优点：**

- Vision API 和图片解码位于原生 macOS 进程，桥接语义更直接。
- helper 崩溃或卡死时可以由父进程做硬超时和进程回收。
- Python backend 不需要加载 PyObjC native extension。

**风险：**

- 新增 Swift 构建、双架构产物、资源路径、codesign、notarization 和升级维护链路。
- 每次 OCR 有进程启动和 JSON 序列化成本；需要处理 stderr、退出码、半包和协议版本。
- helper 必须随 app/dmg 正确分发，不能依赖用户机器上的 Xcode、Swift runtime 或开发目录。
- Python backend 与 Tauri bundle 的耦合更深，开发模式和发布模式需要两套路径测试。

### 5.3 方案 C：调用 `osascript`、`sips` 或外部 OCR 命令（不采用）

现有剪贴板图片捕获在 macOS 使用 `osascript` 是平台输入适配的局部做法，不应扩展为 OCR 主实现：

- AppleScript 没有适合 OCR 结果的稳定、类型化接口；
- `sips` 主要是图片处理，不是 OCR 引擎；
- 外部命令依赖用户环境，无法保证桌面包自包含；
- 命令启动、PATH、locale、权限和错误输出都会增加支持成本；
- 通过 shell 运行会扩大工具授权和安全边界。

### 5.4 推荐决策

**先采用方案 A 做 macOS 技术 spike，发布前设置方案 B 的切换门槛。**

方案 A 只有在以下条件全部满足后才可作为正式实现：

1. Python 3.11/桌面实际 runtime 能导入 Vision bridge；
2. macOS 12 Intel 和 Apple Silicon 都能识别中英文 fixture；
3. app/dmg 安装后的 native extension 能通过签名和 notarization 检查；
4. OCR 请求不会阻塞 gateway event loop，取消、异常和超时行为可接受；
5. 不需要新增不可接受的 OS 权限或网络依赖。

任一条件失败时，保留上层 Python port 和工具契约，改用方案 B；不回退到 shell OCR。

## 6. 建议的领域和工具接口

### 6.1 OCR engine port

建议新增一个与 Vision 解耦的内部接口，概念上包含：

```text
OcrOptions
  languages: list[str]
  recognition_level: "fast" | "accurate"
  automatically_detect_language: bool
  include_regions: bool
  max_output_chars: int

OcrDocument
  text: str
  blocks: list[OcrBlock]
  engine: str
  languages: list[str]
  recognition_level: str
  truncated: bool

OcrBlock
  text: str
  confidence: float | None
  bounding_box: NormalizedRect | None
```

`OcrEngine` 的实现可以是 `VisionOcrEngine`；测试使用 fake engine，不依赖 macOS。引擎调用应位于 worker thread 或独立 helper，不能在 asyncio 事件循环中直接执行同步 Vision request。

### 6.2 `ocr` 工具 schema 草案

工具 ID 固定为 snake_case 的 `ocr`，首版建议参数如下：

| 参数 | 类型 | 必填 | 建议语义 |
|---|---|---:|---|
| `file_path` | string | 是 | 要识别的本地图片路径 |
| `languages` | array[string] | 否 | Vision 语言标识，按优先级排列；空值使用自动检测 |
| `recognition_level` | enum | 否 | `accurate` 默认，或 `fast` |
| `include_regions` | boolean | 否 | 默认 `false`；为 true 时返回区域和置信度 |

MVP 不建议让模型传入任意 request revision、原始 Vision option、临时输出路径或 shell 命令。

默认输出应优先是可直接供模型阅读的文本；`include_regions=true` 时可以在同一受限输出中附加 JSON 区域信息。若结构化区域是产品功能，必须序列化进 `output`，不能只依赖不会进入模型上下文的本地 UI metadata。

### 6.3 工具注册和授权接入点

实现阶段需要同步修改以下闭集，不应只新增一个 plugin 文件：

- `src/voidx/tooling/builtin/ocr.py`：工具实现和 schema；
- `src/voidx/tooling/builtin/plugins.py`：实例化并使用文件范围 adapter；
- `src/voidx/bootstrap/tooling.py`：加入 `TOOL_CAPABILITIES` 和 `_CATALOG_ORDER`，能力为 `READ_ONLY`；
- `src/voidx/tooling/policy/filesystem/constants.py`：将 `ocr` 加入 `FILE_PATTERN_TOOLS`；
- `src/voidx/tooling/policy/permission/rules.py`：将 `ocr` 归入只读能力，并让 `file_paths_for_tool()` 提取 `file_path`；
- `src/voidx/agent/adapters/langgraph/runtime/tool_surface.py`：确认 `ocr` 不被 execution-only surface 排除；
- `pyproject.toml` / `uv.lock`：仅增加 macOS 条件依赖并锁定已验证版本；
- 对应的 backend 和工具测试目录：覆盖 schema、执行、权限和错误。

执行路径应类似：

```text
ocr.execute(args, ctx)
  -> authorized_path(ctx, file_path, write=False, require_exists=True)
  -> validate image type/size/pixels
  -> OcrEngine.recognize(normalized_path, options)
  -> bounded ToolResult
```

无论图片位于 workspace 内还是 workspace 外，都必须先经过 `authorized_path()`；工具不能自行 `Path.resolve()` 后绕过授权服务。

## 7. 图片附件自动 OCR 方案

### 7.1 两种行为

| 行为 | 作用 | 优点 | 主要风险 |
|---|---|---|---|
| 仅显式 `ocr` | 模型或用户主动调用工具读取图片 | 改动小，结果可解释，权限边界清晰 | 非多模态模型可能在调用工具前就收到无法处理的 `image_url` |
| 自动 fallback + 显式 `ocr` | 非视觉模型的附件在进入模型前变成 OCR 文本；工具仍可重复/指定语言识别 | 满足“非多模态模型可读附件”，保留高级调用能力 | 需要模型能力字段、OCR 延迟、结果持久化和失败语义 |

### 7.2 推荐的分阶段策略

建议最终采用“自动 fallback + 显式 `ocr`”，但分两阶段实施：

**阶段一：显式工具闭环**

- 先实现只读 `ocr`、Vision bridge 和授权/错误测试；
- 工具描述明确告诉模型何时调用 `ocr`；
- 当前多模态图片消息行为保持不变；
- 不在未知模型上静默删除图片或替换消息。

**阶段二：附件自动 fallback**

- 为 `ModelConfig`/provider catalog 增加显式图片输入能力；
- 在 `build_user_message_payload()` 或其上游确定模型能力后，非视觉模型不生成 `image_url`；
- 对图片附件调用 OCR，将图片 token 替换为有界文本，并保留附件路径引用；
- OCR 失败时发送清晰的文本错误，而不是把原始 `image_url` 偷渡给非视觉模型；
- 多模态模型继续使用原始图片输入，可选择同时提供 OCR 文本但默认不重复发送。

自动 fallback 的 OCR 结果必须持久化为可重放的用户消息内容或明确的附件派生记录。上下文压缩、重启和 transcript replay 不能因为原图不再进入上下文而丢失用户当时看到的 OCR 文本。

### 7.3 未决的未知能力策略

在引入能力字段前，未知模型不能被可靠分类。实现阶段需要明确以下一种策略：

- 保守地把未知模型视为不支持图片；或
- 保持现有图片输入行为并提示用户配置；或
- 仅对用户明确标记为 text-only 的模型启用 fallback。

推荐第三种作为迁移期策略，以免破坏当前已可用的多模态模型。

## 8. 限制、错误和安全设计

### 8.1 建议的 MVP 限制

- 文件大小：复用当前 5 MB 限制；若 OCR 需要更高上限，另设 OCR 限制并说明原因，不隐式放大附件限制。
- 文件类型：首版沿用当前图片集合；每种格式都必须有真实 decoder smoke test。GIF 的多帧语义必须明确，建议只识别第一帧。
- 像素数和边长：新增显式上限，具体值在 macOS 12 实机 benchmark 后锁定；不能只依赖字节大小，因为高压缩图片可能有极大像素数。
- 输出长度：复用工具消息的 8,192 字符安全边界；超出时截断并返回 `truncated=true`，不能无提示丢失尾部。
- 并发：使用有界 semaphore；初版建议同一进程避免无限并发 OCR，具体并发数以 Intel/Apple Silicon benchmark 决定。
- 缓存：首版只考虑进程内、按路径 metadata + 选项区分的短生命周期缓存，不默认把 OCR 文本写入额外磁盘缓存。

### 8.2 错误分类

错误必须返回稳定、可操作的 `ToolResult`，不泄漏 Python traceback 或 Vision 对象 repr：

- `denied`：路径未通过 workspace/grant/sandbox 授权；
- `not_found`：路径不存在或不是普通文件；
- `unsupported_format`：扩展名或系统 decoder 不支持；
- `too_large`：字节、像素或输出超过限制；
- `decode_failed`：图片无法解码；
- `language_unsupported`：请求语言不在当前 Vision revision 支持范围内；
- `vision_unavailable`：非 macOS、bridge 未安装或系统 API 不可用；
- `timeout`：超过工具时间预算；
- `no_text`：请求成功但没有识别出文字；
- `internal`：其他可观测的实现错误。

`vision_unavailable` 应使用 `ToolResult.unavailable()`；路径拒绝应使用现有授权错误语义。不要在 Vision 不可用时自动联网或调用用户 PATH 中的 OCR 程序。

### 8.3 隐私和权限

- OCR 默认只读取图片，不写入原图目录，不生成用户可见的中间图片。
- 不新增网络请求；不要把图片内容写入日志、debug dump 或错误消息。
- 日志最多记录 engine、耗时、尺寸、截断状态和错误分类，不记录全文。
- workspace 外路径必须经过现有用户授权；macOS sandbox/TCC 若另有要求，必须在桌面实机验证。
- 若将 OCR 文本持久化到 transcript，必须在隐私说明中把它视为用户输入派生内容，而不是临时调试信息。

## 9. 验证计划

本文件阶段不执行以下命令；它们是实现阶段的验收入口。

### 9.1 不依赖 macOS 的单元测试

- fake `OcrEngine` 的 `ocr` 工具 schema、结果格式和输出截断；
- `file_path` 缺失、目录、非图片、坏图片、空文字和 engine error；
- workspace 内路径允许，workspace 外路径按 grant 允许/拒绝；
- 只读工具不创建、修改或删除文件；
- 工具注册闭集、能力标记、catalog 顺序和 tool surface；
- Vision 坐标转换、EXIF orientation、区域排序和 JSON 序列化；
- 自动 fallback 的 vision/text-only/unknown 三种能力路径及 replay。

建议先运行聚焦测试：

```bash
./test.py --backend -- src/tests/test_tooling src/tests/test_application/test_attachments.py -v
```

### 9.2 macOS Vision 集成测试

在至少 macOS 12 Intel 和 macOS 12 Apple Silicon 上运行：

- 中英文单行、多行、混排图片；
- 旋转 EXIF 图片、透明 PNG、低对比度图片；
- 无文字图片、损坏图片、边界大小图片；
- 同一图片 `fast` / `accurate` 和显式语言列表；
- `include_regions` 的置信度和归一化 bounding box；
- 连续调用和并发调用，确认无明显 native resource 泄漏；
- 取消、超时、bridge 缺失和 helper 崩溃（若采用方案 B）。

### 9.3 桌面发布验证

```bash
./test.py --backend
./test.py --desktop
./python.py scripts/package.py --format all --clean --verify
./desktop/build.sh --clean
```

发布验收还必须人工检查：

- 安装后的 `.app` 和 `.dmg` 能加载 Vision bridge；
- x86_64 和 arm64 backend image 与应用架构匹配；
- 最低 macOS 12 可启动并完成 OCR；
- codesign、notarization 和 Gatekeeper 安装路径无 native module/helper 加载错误；
- OCR 失败时不泄漏图片内容，也不偷偷转为网络或 shell OCR。

## 10. 实现顺序建议

1. 增加 `OcrEngine`、`OcrOptions` 和结果模型，先用 fake engine 写工具契约测试。
2. 完成 `VisionOcrEngine` 技术 spike，验证 PyObjC 在项目 Python runtime 中的导入、图片解码、语言和结果转换。
3. 将 `ocr` 接入文件工具授权和工具注册闭集，完成路径安全测试。
4. 增加 worker thread、有界并发、超时、输出截断和错误映射。
5. 在 macOS 12 Intel/Apple Silicon 上运行图片 fixture 和桌面 bundle smoke test。
6. 若 PyObjC 不满足发布门槛，替换为 Swift helper；保持 Python port、工具 schema 和 `ToolResult` 不变。
7. 单独设计并实现模型视觉能力字段和附件自动 fallback；在 fallback 通过 replay/多模态回归后再启用。

## 11. 开放问题

- [ ] MVP 是否只提供显式 `ocr`，还是第一版就必须包含自动 fallback？
- [ ] `ModelConfig` 的能力字段命名采用 `supports_vision`、`image_input` 还是 provider catalog 声明？
- [ ] 未知模型在迁移期采用哪种图片策略？
- [ ] PyObjC 技术 spike 的发布门槛是否允许切换到 Swift helper？
- [ ] 首版接受哪些格式：当前 PNG/JPEG/GIF/WebP，还是加入 HEIC/TIFF？GIF 是否只处理第一帧？
- [ ] OCR 的最大像素数、最大边长、并发数和硬超时分别是多少？
- [ ] 默认是否返回 bounding boxes？它们只服务工具结果，还是也服务前端可视化？
- [ ] OCR 派生文本是否写入 transcript；用户是否需要单独看到“图片已 OCR”的标识？
- [ ] workspace 外图片在 macOS sandbox/TCC 下是否需要额外 security-scoped bookmark？

## 12. 参考资料

### Apple 官方文档

- [Recognizing Text in Images](https://developer.apple.com/documentation/vision/recognizing-text-in-images)
- [`VNRecognizeTextRequest`](https://developer.apple.com/documentation/vision/vnrecognizetextrequest)
- [`VNImageRequestHandler`](https://developer.apple.com/documentation/vision/vnimagerequesthandler)
- [`VNRecognizedTextObservation`](https://developer.apple.com/documentation/vision/vnrecognizedtextobservation)
- [`VNRecognizedText`](https://developer.apple.com/documentation/vision/vnrecognizedtext)
- [`VNRectangleObservation`](https://developer.apple.com/documentation/vision/vnrectangleobservation)

Apple 文档确认了 request、handler、observation 和 recognized text 的 API 关系、macOS availability、语言配置、识别级别、置信度及区域结果；具体支持语言和 request revision 仍应在目标系统运行时查询，而不是在代码中永久硬编码。

### Python bridge

- [PyObjC Vision API notes](https://pyobjc.readthedocs.io/en/latest/apinotes/Vision.html)
- [`pyobjc-framework-Vision` on PyPI](https://pypi.org/project/pyobjc-framework-Vision/)

PyObjC 文档说明 Vision wrapper 通过 `import Vision` 使用；PyPI 元数据和 wheel 标签、版本范围需要在实施时重新锁定并以项目 Python runtime 实测结果为准。

### 项目内实现参考

- `src/voidx/agent/application/attachments.py`
- `src/voidx/agent/domain/turn/attachments.py`
- `src/voidx/tooling/application/authorization.py`
- `src/voidx/tooling/application/execution.py`
- `src/voidx/tooling/adapters/scoped_plugin.py`
- `src/voidx/tooling/builtin/plugins.py`
- `src/voidx/bootstrap/tooling.py`
- `src/voidx/tooling/policy/filesystem/constants.py`
- `src/voidx/tooling/policy/permission/rules.py`
- `desktop/tauri/tauri.conf.json`
- `desktop/build.sh`
- `scripts/build_desktop_backend.py`
