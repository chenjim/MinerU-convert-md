# MinerU-convert-md

基于 MinerU 3.4.4 + FastAPI 的文档转 Markdown HTTP 服务。

- 支持格式：PDF / DOCX / PPTX / XLSX / PNG / JPG → Markdown
- 纯 CPU 运行，后端 PP-OCRv6，约 34.5M 模型
- 布局还原、表格识别、公式 LaTeX、阅读顺序重组
- 端口 56784

## 快速启动

```bash
docker compose up -d
curl http://localhost:56784/
```

## 运行测试

```bash
docker compose up -d
docker exec mineru-convert-md python3 -m pytest tests/ -v
```

修改代码后无需重构建，`docker compose restart` 即可生效。

## 参考源码

`vendor/mineru/` — MinerU 官方仓库 tag `mineru-3.4.4-released`
