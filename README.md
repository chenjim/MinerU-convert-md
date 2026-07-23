# MinerU + PP-OCRv6 + LLM 纯 CPU 文档转 Markdown：封装 HTTP 服务实战

[TOC]

> 本文首发地址 <https://h89.cn/archives/664.html>

两个月前我写过一篇 [OCR 方案对比](https://h89.cn/archives/596.html)，把 PP-OCRv4、DeepSeek-OCR-2、Ollama deepseek-ocr、PaddleOCR-VL-1.5 在 RTX 4060 上跑了一圈。结论是复杂文档上 PaddleOCR-VL-1.5 最均衡，但代价也明显：CUDA 版本、DLL 缺失、Python 包冲突，每一步都是坑。

后来我想，能不能绕开 GPU，彻底不要显卡？找了一圈，最后盯上了 **MinerU 3.4.4 + PP-OCRv6**。纯 CPU 跑、Docker 一键启动、不需要 NVIDIA GPU，还能处理 PDF / Word / PPT / 图片。

于是就有了这个项目：用 FastAPI 把 MinerU 包成 HTTP 服务，顺手接 LLM 做 OCR 纠错和排版后处理。

项目开源在 Gitee： <https://gitee.com/chenjim/MinerU-convert-md>

![封面](https://blog-chenjim.oss-cn-shanghai.aliyuncs.com/2026/260723-mineru-cover.svg)

## 为什么选 MinerU + PP-OCRv6

MinerU 是 OpenDataLab 开源的文档解析引擎，就干一件事：把各种文档转成结构化的 Markdown / JSON，给 LLM、RAG、Agent 工作流用。

我之所以选它，原因挺实际：

- 它不只处理 PDF，DOCX、PPTX、XLSX、图片都能进。
- 表格转 HTML、公式转 LaTeX、多栏布局按阅读顺序重排、还能自动去掉页眉页脚。
- pipeline 后端默认走 PP-OCRv6，不需要 GPU。
- MinerU 自己用 PyTorch 重写了 OCR 推理（`pytorchocr`），直接加载 PP-OCRv6 的 safetensors 权重，不用装 PaddlePaddle 那套重依赖。

2026 年 6 月，MinerU 3.4 的 changelog 里说，pipeline 后端把 OCR 模型换成了 PP-OCRv6，在 OmniDocBench v1.6 上 OCR 准确率涨了约 11%，OCR 处理速度快了一倍。这是 MinerU 官方针对自家 pipeline 的测试数据，不是 PP-OCRv6 在通用 OCR 场景下的绝对指标。3.4.4 这个 tag 是 7 月 10 日发布的，我现在这个项目就是基于它。

PP-OCRv6 本身也很香。百度飞桨第六代 OCR，提供 Tiny(1.5M) / Small(7.7M) / Medium(34.5M) 三档模型，MinerU 默认用的是 **Medium（约 34.5M 参数）**。这个体量对 CPU 很友好，检测和识别模块分别基于 PPLCNetV4 + RepLKFPN + DBHead、PPLCNetV4 + LightSVTR，精度足够，加载也快。

| 后端 | 特点 | 是否需要 GPU | 适用场景 |
|------|------|:-----------:|----------|
| `pipeline` | 快、稳、相对 VLM 不会引入生成式幻觉 | 否 | 日常文档转换、批量处理 |
| `vlm-engine` | 高精度，支持 vLLM/LMDeploy/mlx | 建议 | 扫描版、手写、复杂版式 |
| `hybrid-engine` | 高精度、低幻觉 | 建议 | 对准确性要求极高的场景 |

我选 `pipeline` 后端，理由很简单：普通开发者/小团队手里没显卡，CPU 能跑起来才是真的能用。

## 这个项目做了什么

MinerU 本身是个命令行工具。我把它包成了一个 **HTTP 服务 + Web 界面**，并加了 LLM 后处理。

核心流程：

```
上传文件 → MinerU 转换 → LLM 精炼排版 → 下载 Markdown / Zip
```

支持格式：PDF / DOCX / PPTX / XLSX / PNG / JPG / JPEG。其中 Office 系格式（DOCX/PPTX/XLSX）由 MinerU 原生解析，不需要项目里再套 LibreOffice 之类的转换工具。

### 一键启动

```bash
docker compose up -d
```

访问： <http://localhost:56784>

端口 `56784` 是我专门挑的一个高位端口，避开 3000、8080 这种常和其他服务冲突的坑位。

### HTTP 接口

| 步骤 | 接口 | 方法 | 说明 |
|------|------|------|------|
| 1 | `/upload` | POST | 上传文件，返回 `file_id` |
| 2 | `/convert/{file_id}` | POST | 调 MinerU 转换为 Markdown |
| 3 | `/refine/{file_id}` | GET | LLM 优化排版，生成 `*_optimized.md`（当前为简化实现，会写文件；生产建议改为 POST） |
| - | `/download/{file_id}?zip=true` | GET | 下载 zip（含 md + 图片） |
| - | `/download/{file_id}` | GET | 下载单个 md 文件 |
| - | `/download/{file_id}?filename=...` | GET | 下载指定文件 |
| - | `/` | GET | Web 界面 |

前端会自动串联上传 → 转换 → 精炼三步。`/refine` 需要配 LLM，没配 API Key 时会跳过，不影响基础转换。

## 源码里的几个关键设计

项目结构很干净：

```
├── app/main.py          # FastAPI 应用
├── app/templates/index.html # Web 界面
├── app/config.py        # 配置项
├── app/refiner.py       # LLM 后处理
├── Dockerfile           # 镜像构建
├── docker-compose.yml   # 一键启动
├── vendor/mineru/       # MinerU 源码（tag mineru-3.4.4-released）
└── output/              # 转换结果
```

### 异步调用 MinerU CLI

`main.py` 里转换部分没有直接 import MinerU 的 Python API，而是用 `asyncio.create_subprocess_exec` 调用命令行：

```python
cmd = [
    "mineru", "-p", src_path, "-o", out_dir,
    "--backend", config.MINERU_BACKEND,
]
process = await asyncio.create_subprocess_exec(
    *cmd,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
)
```

这么做有两个好处：

1. **隔离性**：MinerU 内部模型加载、依赖比较复杂，子进程跑坏了不影响主服务。
2. **可控性**：stdout/stderr 直接拿到，方便调试和排错。

代价是转换大文件时子进程会占一定内存，但这个 trade-off 对 HTTP 服务来说划算。

### 文件大小与自动清理

配置里两条硬限制：

```python
MAX_FILE_SIZE = 100 * 1024 * 1024
OUTPUT_MAX_SIZE = int(os.getenv("OUTPUT_MAX_SIZE", str(500 * 1024 * 1024)))
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx", ".png", ".jpg", ".jpeg"}
```

单文件最大 100MB，输出目录默认上限 500MB。超过上限时，按修改时间删除最旧的输出目录：

```python
def trim_output():
    dirs = []
    total = 0
    for d in config.OUTPUT_DIR.iterdir():
        if not d.is_dir():
            continue
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        dirs.append((d.stat().st_mtime, d, size))
        total += size
    if total <= config.OUTPUT_MAX_SIZE:
        return
    for _, d, sz in sorted(dirs):
        shutil.rmtree(d, ignore_errors=True)
        total -= sz
        if total <= config.OUTPUT_MAX_SIZE:
            break
```

上传目录也会同步清理：只保留那些还有对应输出结果的上传文件。

这套机制对一个本地服务来说够了。你不需要手动删缓存，也不用担心硬盘被转换结果撑爆。

### CORS 全开

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

本地开发或内网使用时，CORS 全开最省事。但要注意两点：

1. `allow_origins=["*"]` 配合 `allow_credentials=True` 在浏览器带 cookie/token 的跨域请求里通常会失败。如果你确实需要跨域带凭据，应该配置具体域名，而不是 `*`。
2. 这个服务默认没有认证。如果放到公网，任何人都能上传文件、消耗资源、下载结果。所以只建议在本地或可信内网使用。

## LLM 后处理：不是炫技，是真有必要

MinerU 转换出来的 Markdown 已经能看，但 OCR 场景下总有一些"看着像对、其实错了"的小问题：

- 中英文混排时多空格或少空格
- 数字分组错误，比如 "10,20,30,40,50,60" 被识别成 "102030405060"
- 表格列对不齐
- 公式 LaTeX 语法小错误
- 多栏文档段落顺序错乱
- 标题层级被切得太碎

我在 `refiner.py` 里写了一个排版专家 Prompt，让 LLM 做四件事：

1. **OCR 纠错**：修正中英文、数字、标点、常见混淆（O→0、l→1 等）。
2. **结构重组**：修正阅读顺序、合并断裂段落、修正标题层级。
3. **内容格式化**：功能名加粗、状态描述转列表、表格重排、公式修复、去除页眉页脚页码。
4. **约束**：不凭空补充原文没有的内容，保留图片引用，只输出 Markdown。

调用用的是 OpenAI SDK 兼容接口，默认 DeepSeek：

```python
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
```

温度设成 0.1，max_tokens 8192，输出稳定、长度也够用。没配 Key 时自动跳过，不会报错。

之所以这么设计，是因为 LLM 在这里主要负责"格式修正"。但说实话，prompt 里"OCR 纠错"这一项已经涉及内容修改（比如改数字、改英文大小写、改标点），它不可能 100% 正确。

所以我保留了原始 Markdown，`_optimized.md` 只是一个可选输出。对数字、型号、代码、药品名这类容易改错的内容，建议用原始文件做最终校验。

## Docker 构建要点

`Dockerfile` 里几个关键决策：

```dockerfile
FROM python:3.11-slim AS base
```

用 Python 3.11 slim 镜像，体积可控。

```dockerfile
RUN sed -i "s@http://deb.debian.org@http://mirrors.tuna.tsinghua.edu.cn@g" /etc/apt/sources.list.d/debian.sources
```

Debian 源切到清华，加速 apt。

```dockerfile
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple --extra-index-url https://mirrors.tuna.tsinghua.edu.cn/pytorch/whl/cpu torch torchvision
```

安装 PyTorch CPU 版。容器基础镜像没有 GPU 驱动，这里加上清华 PyTorch 镜像源，实际会装到 CPU 构建。如果你想 double-check，可以在 Dockerfile 末尾加一句 `python -c "import torch; assert not torch.cuda.is_available()"`。

```dockerfile
RUN uv pip install --system --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple "mineru[pipeline]" six pytest pytest-asyncio
```

用 `uv` 安装 `mineru[pipeline]`，比 pip 快不少。`six` 是一些老依赖的兼容库，`pytest` 和 `pytest-asyncio` 用于运行测试。

```dockerfile
EXPOSE 56784
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "56784"]
```

容器内用 uvicorn 直接启动，端口 56784。

`docker-compose.yml` 里挂了好几个卷：

```yaml
volumes:
  - ./app:/app/app
  - ./uploads:/app/uploads
  - ./output:/app/output
  - ./tests:/app/tests
  - model_cache:/root/.cache/mineru
```

`app` 和 `tests` 挂出来方便本地改代码直接生效；`uploads` 和 `output` 挂出来方便看文件；`model_cache` 用命名卷，避免每次重建容器都重新下载模型。

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `MINERU_BACKEND` | `pipeline` | MinerU 后端 |
| `OUTPUT_MAX_SIZE` | `524288000` (500MB) | 输出目录上限 |
| `LLM_API_KEY` | 空 | LLM API Key（可选） |
| `LLM_BASE_URL` | `https://api.deepseek.com` | LLM Base URL |
| `LLM_MODEL` | `deepseek-chat` | LLM 模型 |

`.env` 文件里配 LLM 即可：

```bash
LLM_API_KEY=sk-your-api-key-here
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-v4-flash
```

## 和之前那四个方案比，它站在哪

上次那篇对比里，四个方案各有明显的长短板：

| 方案 | 优势 | 短板 |
|------|------|------|
| PP-OCRv4 | 速度极快 | 不管版式，公式表格全丢 |
| DeepSeek-OCR-2 | 结构好，能提图片 | 慢，显存占用高 |
| Ollama deepseek-ocr | 部署简单 | 丢了图片提取 |
| PaddleOCR-VL-1.5 | 功能均衡 | GPU/CUDA 折腾，Windows 上修行 |

现在这个 MinerU 方案的定位很清楚：**把"复杂文档结构化"这件事从 GPU 上搬下来，放到 CPU 和 Docker 里跑**。

它不是要替代 PaddleOCR-VL-1.5 的精度，也不是要跟 PP-OCRv4 比速度。它解决的是一个更实际的问题：如果你手里没有显卡、不想折腾 CUDA、或者只是想在服务器/ NAS 上跑一个稳定的文档转换服务，这个方案能直接用。

## 实测体验与适用边界

我在本机（i5-1240P / 30GB 内存）上跑了一次 `tests/image.jpg` 的转换：上传几乎瞬间完成，转换耗时约 24 秒。这张图本身是博文截图，约 74KB，内容以文字和表格为主。

但这只是一个单点数据，不能推导为通用结论。纯 CPU 下的实际耗时和文档页数、扫描密度、图片里的文字量、是否首次下载模型都有关系。内存方面，PP-OCRv6 medium 本身才 34.5M，但 MinerU 还要加载版面分析、表格识别、公式识别、阅读顺序等模型，整体占用会比单一 OCR 工具大。建议先在目标机器上跑一组基准，再决定部署方案。

适合的场景：

- 本地知识库预处理（RAG 前的文档清洗）
- 小团队内部文档转换服务
- 不想把文件传到云上的场景
- 有 LLM 后处理需求的转换链路

不太适合的场景：

- 超高并发（没做队列，直接起子进程）
- 超大文件（单文件 100MB 限制）
- 对 OCR 精度要求极高的古籍/手写/低质量扫描件（建议上 vlm-engine 或 hybrid-engine）

## 跑一遍测试

```bash
docker compose up -d
docker compose exec mineru-convert python3 -m pytest tests/ -v
```

测试代码在 `tests/` 目录，主要覆盖上传、转换、下载三个链路。我建议每次改完 `main.py` 都跑一遍，尤其是动了清理逻辑的时候。

## 总结

这个项目没做什么高深的东西，就是把 MinerU 的命令行能力包成了 HTTP 服务，顺手加了 LLM 后处理和自动清理。它刚好回答了我自己在 [OCR 方案对比](https://h89.cn/archives/596.html) 那篇文章最后留下的问题：能不能绕开 GPU，把复杂文档稳定地转成 Markdown？

现在答案是：能。纯 CPU、Docker 一键启动、不需要 NVIDIA GPU。

如果你也被 CUDA、DLL、Python 包冲突这些破事折腾过，直接拉下来跑一遍。源码不长，改起来也不费劲。

项目地址： <https://gitee.com/chenjim/MinerU-convert-md>

---

**参考链接**

- 前作：OCR 方案对比（本文引子） <https://h89.cn/archives/596.html>
- MinerU 官方仓库： <https://github.com/opendatalab/MinerU>
- MinerU 3.4.4 Release： <https://github.com/opendatalab/MinerU/releases/tag/mineru-3.4.4-released>
- PaddleOCR / PP-OCRv6： <https://github.com/PaddlePaddle/PaddleOCR>
- MinerU 技术报告： <https://arxiv.org/abs/2409.18839>
