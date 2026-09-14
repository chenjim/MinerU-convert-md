import base64
import re
import sys
import time
from pathlib import Path
from openai import OpenAI
from app import config

DESCRIBE_PROMPT = "请简要描述这张图片的内容，包括图中的文字。200字以内，只返回描述，不要额外说明。"

REFINE_PROMPT = """你是一名文档排版专家。请对以下由 OCR/文档转换生成的 Markdown 文本进行全面优化：

一、OCR 纠错
- 修正中英文混排、数字、标点符号、多余/缺失空格
- 修正连续数字错误分组，如 "10,20,30,40,50,60" 不会被识别为 "102030405060"
- 修正 OCR 常见混淆（如 O→0、l→1、冒号与分号混淆）

二、结构重组
- 修正阅读顺序：重新组织因多栏/复杂布局导致的段落错乱
- 合并因版面分析断裂的同一段落
- 修正标题层级，使其合理嵌套

三、内容格式化
- 冒号分隔的「功能名：描述」，将功能名加粗，独立成段
- 连续排列的状态/属性描述，转为嵌套列表
- 表格用标准 Markdown 表格语法重排，对齐行列
- 修正公式 LaTeX 语法错误
- 优化列表格式（有序/无序缩进正确）
- 去除页眉页脚页码等无关内容

四、约束
- 保留所有原始信息，不凭空补充原文没有的内容
- 保留原文图片引用不变（包括已添加的图片描述）
- 只输出修正后的 Markdown，不要额外说明"""


# 识图描述的输出 token 上限。目标模型是推理模型，reasoning 计入 max_tokens，
# 上限给小会只产出 reasoning、content 为空（表现为 "empty response"）。
DESCRIBE_MAX_TOKENS = 10240


def get_client() -> OpenAI | None:
    """文本 LLM 客户端，用于整篇 Markdown 排版优化。"""
    if not config.LLM_API_KEY:
        return None
    # max_retries=0：重试交给外层控制，避免 SDK 内部重试把单次超时放大数倍
    return OpenAI(api_key=config.LLM_API_KEY, base_url=config.LLM_BASE_URL, max_retries=0)


def get_image_client() -> OpenAI | None:
    """视觉模型客户端，用于图片 alt 描述（可与文本 LLM 分离，如本地 ollama）。"""
    if not config.VLM_API_KEY:
        return None
    return OpenAI(api_key=config.VLM_API_KEY, base_url=config.VLM_BASE_URL, max_retries=0)


def describe_image(image_path: str, retries: int = 3, timeout: float = 60) -> str | None:
    """生成图片 alt 描述。视觉接口偶发失败，自动重试 retries 次。"""
    client = get_image_client()
    if client is None:
        return None

    ext = Path(image_path).suffix.lower()
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}.get(ext, "image/jpeg")

    try:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
    except Exception:
        return None

    for attempt in range(1, retries + 1):
        try:
            resp = client.chat.completions.create(
                model=config.VLM_MODEL,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": DESCRIBE_PROMPT},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    ],
                }],
                temperature=0.1,
                max_tokens=DESCRIBE_MAX_TOKENS,
                timeout=timeout,
            )
            content = resp.choices[0].message.content
            if content and content.strip():
                return content.strip()
            raise ValueError("empty response")
        except Exception as e:
            print(f"[describe_image] attempt {attempt}/{retries} failed: {e}", file=sys.stderr, flush=True)
            if attempt < retries:
                time.sleep(2 * attempt)
    return None


def add_image_alt_text(md_text: str, md_dir: str) -> str:
    def _repl(m: re.Match) -> str:
        alt = m.group(1) or ""
        path = m.group(2)
        if alt:
            return m.group(0)
        full = str(Path(md_dir) / path)
        if Path(full).exists():
            desc = describe_image(full)
            if desc:
                alt = desc
        return f"![{alt}]({path})"

    return re.sub(r'!\[(.*?)\]\((.+?)\)', _repl, md_text)


def refine(markdown_text: str) -> str | None:
    client = get_client()
    if client is None:
        return None

    try:
        resp = client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[
                {"role": "system", "content": REFINE_PROMPT},
                {"role": "user", "content": markdown_text},
            ],
            temperature=0.1,
            max_tokens=8192,
        )
        return resp.choices[0].message.content or None
    except Exception:
        return None
