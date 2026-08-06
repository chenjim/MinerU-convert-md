#!/usr/bin/env python3
"""MinerU 视觉理解：本地文件转 Markdown 文本（通过 MinerU-convert-md HTTP 服务）。

原理：opencode 当前模型为纯文本模型，无法直接看图。本脚本将图片/文档上传到
MinerU 服务，经 OCR + 版面还原 + 表格/公式识别后转成 Markdown，再把文本返回
给 agent，从而让纯文本 LLM 间接获得视觉理解能力。

用法:
    python3 <skill>/scripts/vision.py <文件路径> [--output 保存路径]
    python3 <skill>/scripts/vision.py <文件> --timeout 3600 --base-url http://127.0.0.1:56784

参数:
    <文件路径>     PNG/JPG/PDF/DOCX/PPTX/XLSX，自动识别
    --output      把 Markdown 写入指定文件（长文档建议使用，防止 stdout 截断）
    --base-url    服务地址，默认 http://127.0.0.1:56784
    --timeout     轮询超时秒数，默认 1800（多页 PDF 可能需要更长）

前置条件:
    MinerU 服务已启动: cd /vol1/1000/Tools/MinerU-convert-md && docker compose up -d

输出:
    stdout 输出 Markdown 文本，进度日志写入 stderr。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path

ALLOWED = {".pdf", ".docx", ".pptx", ".xlsx", ".png", ".jpg", ".jpeg"}
DEFAULT_BASE_URL = "http://127.0.0.1:56784"
DEFAULT_TIMEOUT = 1800
POLL_INTERVAL = 3


def http_request(base_url: str, method: str, path: str, body: bytes = None,
                 headers: dict | None = None, timeout: int = 60) -> tuple[int, bytes]:
    req = urllib.request.Request(base_url.rstrip("/") + path, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        print(f"[error] 无法连接服务 {base_url}：{e}", file=sys.stderr)
        print("[error] 请先启动服务: cd /vol1/1000/Tools/MinerU-convert-md && docker compose up -d", file=sys.stderr)
        sys.exit(1)


def upload(base_url: str, path: Path) -> str:
    boundary = uuid.uuid4().hex
    body = b""
    body += f"--{boundary}\r\n".encode()
    body += (f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
             f"Content-Type: application/octet-stream\r\n\r\n").encode()
    body += path.read_bytes()
    body += f"\r\n--{boundary}--\r\n".encode()

    status, data = http_request(
        base_url, "POST", "/upload", body,
        {"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    if status != 200:
        print(f"[error] 上传失败 HTTP {status}: {data.decode(errors='replace').strip()}", file=sys.stderr)
        sys.exit(1)
    return json.loads(data)["file_id"]


def start_convert(base_url: str, file_id: str) -> None:
    status, data = http_request(base_url, "POST", f"/convert/{file_id}")
    if status != 200:
        print(f"[error] 启动转换失败 HTTP {status}: {data.decode(errors='replace').strip()}", file=sys.stderr)
        sys.exit(1)


def poll_status(base_url: str, file_id: str, timeout: int) -> dict:
    deadline = time.time() + timeout
    last_phase = ""
    while time.time() < deadline:
        status, data = http_request(base_url, "GET", f"/status/{file_id}")
        if status != 200:
            print(f"[error] 查询状态失败 HTTP {status}: {data.decode(errors='replace').strip()}", file=sys.stderr)
            sys.exit(1)
        info = json.loads(data)
        st = info.get("status")
        if st == "done":
            print("[done] 转换完成", file=sys.stderr)
            return info
        if st == "error":
            print(f"[error] 转换失败: {info.get('phase', 'unknown error')}", file=sys.stderr)
            sys.exit(1)
        phase = info.get("phase", "")
        pct = info.get("pct", 0)
        if phase != last_phase:
            print(f"[progress] {pct}% {phase}", file=sys.stderr)
            last_phase = phase
        time.sleep(POLL_INTERVAL)
    print(f"[error] 转换超时（{timeout}s），可通过 --timeout 调大", file=sys.stderr)
    sys.exit(1)


def download_markdown(base_url: str, file_id: str) -> bytes:
    status, data = http_request(base_url, "GET", f"/download/{file_id}", timeout=600)
    if status != 200:
        print(f"[error] 下载结果失败 HTTP {status}: {data.decode(errors='replace').strip()}", file=sys.stderr)
        sys.exit(1)
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="MinerU 视觉理解：文件转 Markdown")
    parser.add_argument("file", help="PNG/JPG/PDF/DOCX/PPTX/XLSX 文件路径")
    parser.add_argument("--output", help="保存 Markdown 到指定文件")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"服务地址，默认 {DEFAULT_BASE_URL}")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"轮询超时秒数，默认 {DEFAULT_TIMEOUT}")
    args = parser.parse_args()

    path = Path(args.file).expanduser().resolve()
    if not path.exists():
        print(f"[error] 文件不存在: {path}", file=sys.stderr)
        return 1
    ext = path.suffix.lower()
    if ext not in ALLOWED:
        print(f"[error] 不支持的格式 {ext}，支持: {', '.join(sorted(ALLOWED))}", file=sys.stderr)
        return 1

    print(f"[upload] {path} -> {args.base_url}", file=sys.stderr)
    file_id = upload(args.base_url, path)
    print(f"[convert] file_id={file_id}", file=sys.stderr)

    start_convert(args.base_url, file_id)
    poll_status(args.base_url, file_id, args.timeout)

    md = download_markdown(args.base_url, file_id).decode("utf-8", errors="replace")

    empty_imgs = len(re.findall(r'!\[\]\(([^)]+)\)', md))
    if empty_imgs:
        print(f"[warn] {empty_imgs} 张图片未能生成文字描述（架构图/抽象图无法 OCR，需视觉模型兜底）", file=sys.stderr)

    if args.output:
        out = Path(args.output).expanduser().resolve()
        out.write_text(md, encoding="utf-8")
        print(f"[saved] {out}", file=sys.stderr)
    else:
        sys.stdout.write(md)
        if not md.endswith("\n"):
            sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
