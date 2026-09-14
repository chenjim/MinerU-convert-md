import os
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE = 100 * 1024 * 1024
OUTPUT_MAX_SIZE = int(os.getenv("OUTPUT_MAX_SIZE", str(500 * 1024 * 1024)))
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx", ".png", ".jpg", ".jpeg"}
# 同时存在于队列/执行中的转换任务上限，超出返回 429
MAX_PENDING_TASKS = int(os.getenv("MAX_PENDING_TASKS", "5"))
MINERU_BACKEND = os.getenv("MINERU_BACKEND", "pipeline")
MINERU_METHOD = os.getenv("MINERU_METHOD", "auto")

# 常驻 mineru-api 工作进程池：复用已加载模型，避免每次转换冷启动
MINERU_WARM_POOL = os.getenv("MINERU_WARM_POOL", "1") != "0"
MINERU_MAX_CONCURRENCY = int(os.getenv("MINERU_MAX_CONCURRENCY", "3"))
MINERU_API_PORT_BASE = int(os.getenv("MINERU_API_PORT_BASE", "51250"))

# 文本 LLM（整篇 Markdown 排版优化）
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")

# 视觉模型 VLM（图片 alt 描述），可独立于文本 LLM 配置
VLM_API_KEY = os.getenv("VLM_API_KEY", LLM_API_KEY)
VLM_BASE_URL = os.getenv("VLM_BASE_URL", LLM_BASE_URL)
VLM_MODEL = os.getenv("VLM_MODEL", LLM_MODEL)
# 识图最大并发数，避免 100+ 张图串行等待
VLM_MAX_CONCURRENCY = int(os.getenv("VLM_MAX_CONCURRENCY", "5"))
