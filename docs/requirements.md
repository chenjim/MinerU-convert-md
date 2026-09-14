# MinerU-convert-md 需求说明

本文档描述项目需求，并与当前实现保持一致。实现变更后需同步更新本文档的“最终状态”。

## 1. 目标

提供一个 HTTP 服务，把常见文档/图片转换成结构化 Markdown，并可选地调用大模型做：
- 图片 alt 描述（视觉模型 VLM）
- 整篇 Markdown 排版优化（文本 LLM）

面向纯 CPU 环境，布局还原、表格、公式（LaTeX）、阅读顺序由 MinerU 负责。

## 2. 技术栈与运行环境

| 项 | 值 |
| --- | --- |
| 服务框架 | FastAPI + Uvicorn |
| 转换引擎 | MinerU `mineru[pipeline]==3.4.5`（vendored 源码 tag `mineru-3.4.4-released`） |
| OCR | PP-OCRv6（MinerU 内置 `pytorchocr`，无需 PaddlePaddle） |
| 运行方式 | Docker Compose，纯 CPU |
| 端口 | 56784 |
| Python | 3.11 |

## 3. 支持格式与限制

- 输入格式：PDF / DOCX / PPTX / XLSX / PNG / JPG（`.jpeg`）
- 单文件大小上限：100 MB（`MAX_FILE_SIZE`）
- 输出目录总量上限：500 MB（`OUTPUT_MAX_SIZE`），超出按时间逐出

## 4. 架构

```
浏览器(单页)
   │  /upload  /convert  /status  /download
   ▼
FastAPI (app/main.py)
   ├── mineru_pool.py   常驻 mineru-api worker 池（模型只加载一次）
   ├── refiner.py       文本 LLM(refine) 与 视觉模型(describe_image)
   └── config.py        配置读取
```

- **转换**：`mineru` 子进程通过 `--api-url` 复用常驻 worker；worker 的 stderr（tqdm）被解析为进度。
- **后处理**：转换完成后，按开关执行图片 alt 描述（并发）与 LLM 重排。

## 5. 功能需求

### 5.1 上传
- 接收 multipart 文件，校验扩展名与大小，返回 `file_id`。

### 5.2 转换
- 启动异步转换任务；同一 `file_id` 正在转换时返回 409。
- MinerU 解析方法由 `MINERU_METHOD` 决定，默认 `auto` 并**透传**给 MinerU，由其分类器判断 txt/ocr（可识别扫描件、乱码字体）。

### 5.3 后处理开关（相互独立）
| 开关 | 默认 | 说明 |
| --- | --- | --- |
| 补充图片描述（alt）`alt` | 关 | 用 VLM 为每张图片生成描述，写入 Markdown 图片替代文本（alt） |
| 大模型优化排版 `optimize` | 关 | 用文本 LLM 对整篇 Markdown 纠错与重排 |

- `alt` 与 `optimize` 独立：可只补 alt、只优化排版、两者都要、或都不要。
- 任一后处理开启时，MinerU 阶段进度上限为 85；都关闭时为 95。

### 5.4 图片描述（alt）
- 并发上限 `VLM_MAX_CONCURRENCY`（默认 5）。
- 同一图片引用去重，只识别一次；相同引用的所有位置统一填充。
- 描述长度上限 200 字（`DESCRIBE_PROMPT`）。
- 单次请求超时 60s，单图最多重试 3 次；SDK 内部重试关闭（`max_retries=0`），避免超时被放大。

### 5.5 结果文件
- 原始结果：MinerU 产出目录（含 Markdown 与 `images/`）。
- 后处理结果：`*_optimized.md`（补好 alt 或 LLM 重排后的文本）。
- 下载优先返回 `*_optimized.md`；zip 打包排除 `.json`/`.pdf`/`.log`。

### 5.6 进度显示
- 全局百分比单调递增，区间：`上传/排队 5 → 模型加载 8 → 阶段 10~upper → 后处理 → 100`。
- 阶段区间按实测耗时占比重分（每 batch 内重置）：

| 阶段 | 关键字 | 区间 | 单位 |
| --- | --- | --- | --- |
| 布局分析 | `layout` | 0.00–0.20 | 页 |
| 表格识别 | `table` | 0.20–0.32 | 区域 |
| 文字识别 | `ocr-det` | 0.32–0.85 | 区域 |
| 文字识别 | `ocr-rec` | 0.85–1.00 | 区域 |

- 无表格阶段时，OCR 起点前移（让出表格区间），避免空档跳变。
- 图片 alt 阶段：85→93；LLM 重排：95；完成 100。

### 5.7 前端界面
- 单页应用，拖拽/选择文件 → 上传 → 转换 → 轮询进度 → 下载 zip。
- 深色模式：默认跟随系统（`prefers-color-scheme`），右上角按钮可手动切换，选择存入 `localStorage`；首屏用内联脚本预设主题，避免闪白。

### 5.8 安全
- **路径穿越防护**：`/download` 的 `filename` 解析后必须仍位于任务目录内，且必须是文件，否则 404。
- **任务上限**：同时排队/转换中的任务数不超过 `MAX_PENDING_TASKS`（默认 5），超出返回 429。
- **清理保护**：`trim_output`/`trim_uploads` 跳过仍在转换/排队中的任务目录。
- 部署建议：外网只经反向代理（HTTPS + 代理层鉴权/限流）。应用内暂未做鉴权，公网暴露前需在代理层加认证（如 NPM Access List / Basic Auth）。

## 6. 接口契约

| 方法 | 路径 | 参数 | 返回 |
| --- | --- | --- | --- |
| GET | `/health` | - | `{status, service}` |
| GET | `/` | - | 前端页面（HTML） |
| POST | `/upload` | `file`(multipart) | `{success, file_id, filename, size}` |
| POST | `/convert/{file_id}` | `optimize`(bool, 默认 false)、`alt`(bool, 默认 false) | `{success, file_id}` |
| GET | `/status/{file_id}` | - | `{file_id, status, phase, pct, result?}` |
| GET | `/refine/{file_id}` | - | `{optimized, filename?}` 或 `{optimized:false, detail}` |
| GET | `/download/{file_id}` | `filename`(可选)、`zip`(bool) | 文件 / zip |

- `status` 取值：`pending` / `converting` / `done` / `error`。

## 7. 配置项（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MINERU_BACKEND` | `pipeline` | MinerU 后端 |
| `MINERU_METHOD` | `auto` | `auto`/`txt`/`ocr`，auto 透传 MinerU 分类器 |
| `MINERU_WARM_POOL` | `1` | 常驻 worker 池开关（`0` 关闭，回退冷启动） |
| `MINERU_MAX_CONCURRENCY` | `3` | worker 数（最大并发转换数） |
| `MINERU_API_PORT_BASE` | `51250` | worker 起始端口 |
| `LLM_API_KEY` | 空 | 文本 LLM 密钥 |
| `LLM_BASE_URL` | `https://api.deepseek.com` | 文本 LLM 地址 |
| `LLM_MODEL` | `deepseek-chat` | 文本 LLM 模型 |
| `VLM_API_KEY` | 回退 `LLM_API_KEY` | 视觉模型密钥（本地 ollama 可填任意值） |
| `VLM_BASE_URL` | 回退 `LLM_BASE_URL` | 视觉模型地址 |
| `VLM_MODEL` | 回退 `LLM_MODEL` | 视觉模型 |
| `VLM_MAX_CONCURRENCY` | `5` | 识图并发数 |
| `OUTPUT_MAX_SIZE` | 500MB | 输出目录上限 |
| `MAX_PENDING_TASKS` | `5` | 同时排队/转换中的任务上限 |

> 文本 LLM 与视觉模型分离：识图可指向本地 ollama（如 `VLM_BASE_URL=http://192.168.31.165:11434/v1`、`VLM_MODEL=qwen3.5:2b`），排版优化仍用远端。

## 8. 目录结构

```
app/
  main.py          FastAPI 应用、进度追踪、转换编排
  mineru_pool.py   常驻 mineru-api worker 池与预热
  refiner.py       文本 LLM(refine) / 视觉模型(describe_image)
  config.py        配置
  templates/index.html  前端单页
tests/             pytest 单元测试
vendor/mineru/     MinerU 官方源码（参考）
uploads/           上传文件（转换结束后清理）
output/            转换结果（含 convert.log）
```

## 9. 运行与测试

```bash
docker compose up -d
docker exec mineru-convert-md python3 -m pytest tests/ -v
```

- 代码目录已挂载，改代码后 `docker compose restart` 生效。
- 改动 `.env` 需 `docker compose up -d` 重建容器（`restart` 不重读 env_file）。

## 10. 变更记录（最终状态）

- 进度映射由固定区间改为按实测耗时占比重分，OCR 拆分为 det/rec，修复“前期进度过快、OCR 段卡住”。
- 常驻 mineru-api worker 池 + 启动预热，转换复用已加载模型。
- MinerU 转换 stderr 落盘 `output/<file_id>/convert.log`。
- 图片 alt 识图改为最多 5 路并发 + 同图去重。
- 文本 LLM 与视觉模型拆分为两套配置（`LLM_*` / `VLM_*`）；识图切本地 ollama `qwen3.5:2b`。
- 新增前端「补充图片描述（alt）」开关（默认关），与「大模型优化排版」解耦；`/convert` 增加 `alt` 参数。
- 前端新增深色模式：跟随系统 + 手动切换，`localStorage` 记忆。
- 安全加固：修复 `/download` 的 `filename` 路径穿越；`MAX_PENDING_TASKS=5` 队列上限（超出 429）；`trim_output`/`trim_uploads` 跳过在用任务。（应用内 Bearer 鉴权曾实现，后按需求移除；鉴权改由反向代理层承担。）
- 识图输出上限 200 字，`max_tokens` 10240，单请求超时 60s，关闭 SDK 内部重试。
- PDF `auto` 方法透传 MinerU 分类器（移除应用内 pypdf 启发式）。
