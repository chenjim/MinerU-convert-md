FROM python:3.11-slim AS base

WORKDIR /app

RUN sed -i "s@http://deb.debian.org@http://mirrors.tuna.tsinghua.edu.cn@g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0t64 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    wget \
    g++ \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple uv

RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple --extra-index-url https://mirrors.tuna.tsinghua.edu.cn/pytorch/whl/cpu torch torchvision

RUN uv pip install --system --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple "mineru[pipeline]" six pytest pytest-asyncio

COPY app/ ./app/

RUN mkdir -p /app/output

EXPOSE 56784

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "56784"]
