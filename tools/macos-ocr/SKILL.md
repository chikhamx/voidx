---
name: macos-ocr
description: 使用 macOS Apple Vision OCR CLI 识别图片、PDF、目录和条码
enabled: true
---

# macOS OCR 技能说明

## 用途

使用本技能读取本机图片或 PDF 中的文字，也可以识别条码。CLI 只读、离线：

- 结果从 stdout 读取；诊断信息从 stderr 读取。
- 仅支持 macOS 12.0 及以上。
- 不接入网络 OCR、shell OCR、Tesseract 或附件自动 OCR。
- 不支持表格、表单、手写识别和 HEIC/HEIF。

## 调用方式

```bash
OCR_BIN="${VOIDX_MACOS_OCR_BIN:-tools/macos-ocr/bin/voidx-macos-ocr}"
"$OCR_BIN" --input "$IMAGE_PATH"
```

路径包含空格时必须保留引号。除 `--help` 外，调用必须提供 `--input` 或 `--input-dir`，二者不能同时使用。

## 输入

### 单个或多个文件

```bash
"$OCR_BIN" --input image.png
"$OCR_BIN" --input first.png --input second.jpg
```

`--input` 可以重复。支持的图片扩展名为：`bmp`、`gif`、`jpeg`/`jpg`、`png`、`tif`/`tiff`、`webp`。PDF 也通过 `--input` 传入；HEIC/HEIF 不可用。

### 目录

```bash
"$OCR_BIN" --input-dir scans
```

`--input-dir` 只读取目录的直接子文件，不递归；只处理支持的扩展名，并按大小写不敏感的稳定路径顺序处理。

### PDF 页码

```bash
"$OCR_BIN" --input document.pdf --page 2
"$OCR_BIN" --input document.pdf --page 1-3 --page 5
```

- `--page` 可以重复，页码从 `1` 开始。
- 重复页码会去重，并按升序处理。
- 只能对通过 `--input` 传入的 PDF 使用，不能用于目录或图片。

## 图像预处理

预处理顺序固定为：EXIF orientation → region 裁剪 → 缩放 → 灰度 → 对比度 → 锐化 → 旋转。选项可以组合：

| 参数 | 说明 |
|---|---|
| `--preprocess auto` | 选择自动模式；当前没有额外自动处理。 |
| `--scale 1..8` | 整数倍缩放，默认 `1`。 |
| `--grayscale` | 转为灰度图。 |
| `--contrast -2..2` | 调整对比度，默认不调整。 |
| `--sharpen` | 锐化图像。 |
| `--rotate auto\|0\|90\|180\|270` | 旋转图像；默认 `auto`，当前等同于 `0`。 |

### 区域识别

`--region` 使用归一化坐标 `x,y,width,height`，原点在左上角；每个值必须在 `0..1` 内，矩形不能越界。可以重复传入：

```bash
"$OCR_BIN" --input page.png \
  --region 0,0,0.5,1 \
  --region 0.5,0,0.5,1
```

每个区域产生一个独立结果 item。`--regions` 只控制是否返回文字和条码的 `bounding_box`，不是 ROI 开关：

```bash
"$OCR_BIN" --input page.png --region 0,0,0.5,1 --regions
```

## Vision 文字识别

| 参数 | 说明 |
|---|---|
| `--language <id>` | 识别语言，可重复，例如 `en-US`、`zh-Hans`；未指定时由 Vision 自动检测。 |
| `--recognition-level fast\|accurate` | 速度/质量；默认 `accurate`。 |
| `--candidates 1..10` | 每个文字块返回的候选数；默认 `1`。大于 `1` 时才输出 `candidates`。 |
| `--layout lines\|paragraphs\|columns\|reading-order` | 文字块排序方式；默认不重排。 |

```bash
"$OCR_BIN" --input document.png \
  --language zh-Hans --language en-US \
  --recognition-level accurate \
  --candidates 3 \
  --layout reading-order \
  --regions
```

布局模式含义：

- `lines`、`reading-order`：按行排序。
- `paragraphs`：聚合相邻行。
- `columns`：按列、再按行排序。

## 条码识别

```bash
"$OCR_BIN" --input label.png \
  --barcodes \
  --barcode-symbology QR \
  --regions
```

- `--barcodes` 启用 Vision 条码检测，并在输出中加入 `barcodes`。
- `--barcode-symbology` 可以重复，且必须同时使用 `--barcodes`；匹配不区分大小写。
- 常用名称/别名包括 `QR`/`qrcode`、`Code128`、`Code39`、`EAN13`、`EAN8`、`PDF417`、`DataMatrix`、`Aztec`、`ITF14`、`Codabar`、`UPCE`、`I2of5`、`MSIPlessey`、`GS1DataBar`、`GS1DataBarExpanded`、`GS1DataBarLimited`。
- 非空条码 payload 会追加到全文 `text`。
- 没有文字但识别到条码时仍算成功，返回 `blocks: []`。

## 输出格式

| 参数 | 说明 |
|---|---|
| `--format json` | 默认格式。单个普通图片 job 且没有 page/region 时使用单图 JSON；其他情况使用批量 envelope。 |
| `--format text` | 只输出各 item 的 `text`，item 之间用换行分隔。 |
| `--format jsonl` | 每个 item 输出一行 JSON，不带批量 envelope。 |
| `--max-output-chars <正整数>` | 每个 item 的 `text` 最大字符数，默认 `100000`；只截断 `text`，并用 `truncated: true` 标记。 |

### 单图 JSON

```json
{
  "ok": true,
  "text": "识别结果",
  "blocks": [
    {
      "text": "识别结果",
      "confidence": 0.98
    }
  ],
  "confidence": 0.98,
  "truncated": false
}
```

使用 `--regions` 时，文字块和条码可带 `bounding_box`；使用 `--candidates 3` 时，文字块可带 `candidates`：

```json
{
  "text": "首选结果",
  "confidence": 0.98,
  "candidates": [
    {"text": "首选结果", "confidence": 0.98},
    {"text": "候选结果", "confidence": 0.71}
  ],
  "bounding_box": {"x": 0.1, "y": 0.2, "width": 0.4, "height": 0.1}
}
```

### 批量 JSON

批量输入、PDF、或使用 `--region` 时，JSON 使用 `items` envelope：

```json
{
  "ok": true,
  "items": [
    {
      "job": 1,
      "source": "document.pdf",
      "page": 2,
      "text": "第 2 页",
      "blocks": [
        {
          "text": "第 2 页",
          "confidence": 0.98
        }
      ],
      "confidence": 0.98,
      "truncated": false
    }
  ]
}
```

`page`、`region`、`barcodes` 按实际输入和参数按需出现。`job` 从 `1` 开始。

## 常用组合

### 纯文本

```bash
"$OCR_BIN" --input note.png --format text
```

### PDF 每页一行 JSON

```bash
"$OCR_BIN" --input document.pdf --page 1-3 --format jsonl --regions
```

### 目录批处理

```bash
"$OCR_BIN" --input-dir scans --format json
```

### 左右区域分别识别并保留坐标

```bash
"$OCR_BIN" --input wide.png \
  --region 0,0,0.5,1 \
  --region 0.5,0,0.5,1 \
  --regions \
  --format json
```

### 组合预处理、候选和长度限制

```bash
"$OCR_BIN" --input photo.png \
  --scale 2 --grayscale --contrast 0.5 --sharpen --rotate 90 \
  --candidates 3 --max-output-chars 1000
```

## 错误处理

错误始终以一行 JSON 写入 stdout，诊断文本写入 stderr；任意 job 或 region 失败都会终止整个 run，不返回部分成功结果。

| 退出码 | 错误码/含义 |
|---:|---|
| `0` | 成功；`--help` 也返回 `0`。 |
| `2` | `no_text`：没有识别到文字或非空条码 payload。 |
| `64` | `invalid_arguments`：参数或参数组合无效。 |
| `66` | `invalid_input`：输入路径、目录或页码无效。 |
| `69` | 平台能力不可用，例如非 macOS 上的 Vision/PDF。 |
| `70` | 其他处理错误，例如解码或 Vision/条码处理失败。 |
| `74` | `read_failed`：读取输入失败。 |

错误示例：

```json
{"ok":false,"error":{"code":"invalid_arguments","message":"..."}}
```
