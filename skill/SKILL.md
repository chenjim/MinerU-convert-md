---
name: mineru-convert-skill
description: 视觉理解能力。把本地图片（PNG/JPG）或文档（PDF/DOCX/PPTX/XLSX）通过 MinerU 服务转成 Markdown 文本，让纯文本 LLM 看懂图片/文档内容。适合"看这张图""理解这个文档""OCR 提取文字""识别截图里的表格/公式"等场景。
triggers:
  - 看这张图
  - 看看这张图片
  - 图片里有什么
  - 识别截图
  - OCR 提取
  - 理解这个文档
  - 阅读这个 PDF
  - 解析表格
  - 转换 Markdown
---

# MinerU Convert Skill — 视觉理解能力

> 当前 LLM 是纯文本模型，无法直接看图。本技能通过本地 MinerU 服务把图片/文档
> 转成 Markdown 文本（OCR + 版面还原 + 表格 + 公式），agent 读取文本后即可
> 理解内容并回答问题。

## 前置条件

MinerU 服务必须已启动（Docker 部署，端口 56784）：

```bash
docker compose -f /vol1/1000/Tools/MinerU-convert-md/docker-compose.yml up -d
```

验证服务可用：

```bash
curl -s http://127.0.0.1:56784/health   # 应返回 {"status":"ok",...}
```

## 使用脚本

```bash
# 输出 Markdown 到 stdout（短内容直接读）
python3 <skill_dir>/scripts/vision.py <文件路径>

# 长文档建议保存到文件，再读取文件内容，避免 stdout 截断
python3 <skill_dir>/scripts/vision.py <文件路径> --output /tmp/vision_result.md
```

- `<skill_dir>` 为本技能目录，脚本路径为 `scripts/vision.py`
- 支持格式：`.png .jpg .jpeg .pdf .docx .pptx .xlsx`
- 脚本自动完成：上传 → 启动转换 → 轮询进度（写 stderr）→ 下载 Markdown
- 常用参数：
  - `--output <文件>` 保存结果到文件
  - `--timeout <秒>` 轮询超时，默认 1800（多页 PDF 可调大）
  - `--base-url <地址>` 服务地址，默认 `http://127.0.0.1:56784`

## 工作流（agent 应当遵循）

1. 收到"看图/读文档"类请求，判断文件格式是否支持；不支持则告知可转格式。
2. 若服务未启动，先启动（见前置条件）。
3. 运行脚本转换；短内容直接读 stdout，长内容用 `--output` 存文件后读取。
4. 基于返回的 Markdown 文本，回答用户问题或执行后续任务。
5. 若输出含图片引用 `![](路径)` 且用户关心图中细节，可说明图片无法单独查看。

## 注意事项

- **耗时**：纯 CPU 服务。首次转换需加载模型（数秒~1分钟），单张图片约 10~60 秒，
  多页 PDF 可能数分钟。等待期间不要重复发起转换。
- **不要绕过脚本手拼 HTTP**：脚本已封装「上传→转换→轮询→下载」四步 + 轮询逻辑，
  避免重复且易错。
- **不要用 `docker exec` 直接跑 mineru**：转换由服务统一管理，直接跑会重复加载模型。
- **转换结果准确性**：由 MinerU OCR 决定，扫描件/复杂版面可能有个别错字；
  若用户需要更高精度，可结合上下文修正。
- **架构图/抽象图**：MinerU 会把整张图判定为 `figure` 块，跳过 OCR，正文中只有
  图片引用 `![](images/xxx.jpg)`，无法直接提取文字。服务端会调用视觉 LLM
  （`describe_image`，已带重试）生成 alt 描述 `![描述](图)`。若输出仍为
  `![]()`（视觉接口不可用），需告知用户：抽象图仅能理解图内文字，无法解析
  结构关系；或建议换 VLM 后端。
- **网络**：脚本通过 HTTP 调用服务，宿主机与 Docker 容器间端口已映射，正常可用。

## 与相关技能的关系

| 技能 | 定位 | 关系 |
|------|------|------|
| `mineru-convert-skill`（本技能） | 图片/文档 → Markdown 文本 | 视觉理解入口 |
| `jim-image-upload-oss` | 图片上传 OSS | 转换后如需发布图片可配合 |
