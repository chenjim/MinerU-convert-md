from openai import OpenAI
from app import config

PROMPT = """你是一名文档排版专家。请对以下由 OCR/文档转换生成的 Markdown 文本进行全面优化：

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
- 保留原文图片引用不变
- 只输出修正后的 Markdown，不要额外说明"""


def get_client() -> OpenAI | None:
    if not config.LLM_API_KEY:
        return None
    return OpenAI(api_key=config.LLM_API_KEY, base_url=config.LLM_BASE_URL)


def refine(markdown_text: str) -> str | None:
    client = get_client()
    if client is None:
        return None

    try:
        response = client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[
                {"role": "system", "content": PROMPT},
                {"role": "user", "content": markdown_text},
            ],
            temperature=0.1,
            max_tokens=8192,
        )
        return response.choices[0].message.content or None
    except Exception:
        return None
