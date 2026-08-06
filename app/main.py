import os
import io
import re
import uuid
import asyncio
import tempfile
import shutil
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Form, Query
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from app import config
from app import refiner

app = FastAPI(title="MinerU 文档转换服务")

_pending: dict[str, asyncio.Task] = {}
_task_status: dict[str, dict] = {}


def _set_status(file_id: str, status: str, phase: str = "", pct: int = 0):
    _task_status[file_id] = {"status": status, "phase": phase, "pct": pct}


_PHASE_RANGES: list[tuple[str, str, int, int]] = [
    ("Layout",    "布局分析",   10, 45),
    ("MFR",       "字体识别",   45, 55),
    ("ocr",       "文字识别",   55, 75),
    ("OCR",       "文字识别",   55, 75),
    ("Table",     "表格识别",   75, 82),
    ("Formula",   "公式识别",   82, 85),
]


def _parse_progress(line: str) -> Optional[dict]:
    t = line.strip()
    if not t:
        return None

    m = re.search(r'total_pages=(\d+)', t)
    if m:
        return {"pages_total": int(m.group(1))}

    if "model init done" in t.lower():
        return {"phase": "模型加载完成", "pct": 10}

    rpct = None
    n, total = None, None

    m = re.search(r'([A-Za-z][\w\s]+?):\s*(\d+)%.*?\|.*?\|\s*(\d+)/(\d+)\s*\[', t)
    if m:
        name = m.group(1).strip()
        rpct = int(m.group(2))
        n, total = int(m.group(3)), int(m.group(4))
    else:
        m = re.search(r'([A-Za-z][\w\s]+?):\s*(\d+)%', t)
        if m:
            name = m.group(1).strip()
            rpct = int(m.group(2))
        else:
            m = re.search(r'(?:^|\s)([A-Za-z][\w\s]{2,}?)\s+(\d+)/(\d+)(?:\s|$)', t)
            if m and not re.search(r'(?i)batch|submitting|window|doc_slices|cost|done!', m.group(1)):
                name = m.group(1).strip()
                n, total = int(m.group(2)), int(m.group(3))
                rpct = int(n / total * 100)

    if rpct is not None:
        pages = f" {n}/{total} 页" if n is not None and total is not None else ""
        for keyword, display, lo, hi in _PHASE_RANGES:
            if keyword in name:
                return {"phase": f"{display}{pages}", "pct": int(lo + rpct * (hi - lo) / 100)}
        return {"phase": f"{name}{pages}", "pct": int(10 + rpct * 0.75)}

    return None


async def _refine_md(file_id: str, md_path: str):
    md = Path(md_path)
    text = md.read_text(encoding="utf-8")
    md_dir = md.parent

    refs = re.findall(r'!\[\]\(([^)]+)\)', text)
    if refs:
        total = len(refs)
        for i, ref in enumerate(refs):
            img_file = md_dir / ref
            if img_file.exists():
                desc = await asyncio.to_thread(refiner.describe_image, str(img_file))
                if desc:
                    text = text.replace(f"![]({ref})", f"![{desc}]({ref})", 1)
            _set_status(file_id, "converting", f"图片理解 {i+1}/{total} 张", int(90 + (i+1)/total*5))

    _set_status(file_id, "converting", "LLM 优化排版中...", 95)
    result = await asyncio.to_thread(refiner.refine, text)
    if result:
        optimized = md.parent / (md.stem + "_optimized.md")
        optimized.write_text(result, encoding="utf-8")


async def _run_convert(file_id: str, src_path: str, out_dir: str):
    _set_status(file_id, "converting", "模型加载...", 3)
    try:
        result = await _convert_with_progress(file_id, src_path, out_dir)
        _set_status(file_id, "converting", "整理输出...", 85)
        await asyncio.to_thread(trim_output)
        await asyncio.to_thread(trim_uploads)

        md_files = sorted(Path(out_dir).rglob("*.md"))
        if md_files:
            await _refine_md(file_id, str(md_files[0]))

        _set_status(file_id, "done", "转换完成", 100)
        _task_status[file_id]["result"] = result
    except Exception as e:
        _set_status(file_id, "error", str(e), 0)
    finally:
        _pending.pop(file_id, None)


def _detect_pdf_method(path: str) -> str:
    """Detect if PDF is text-based (txt) or scanned (ocr)."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(path)
        total = 0
        for page in reader.pages[:5]:
            total += len((page.extract_text() or "").strip())
        return "txt" if total > 100 else "ocr"
    except Exception:
        return "auto"


async def _convert_with_progress(file_id: str, src_path: str, out_dir: str) -> dict:
    method = config.MINERU_METHOD
    if method == "auto" and src_path.lower().endswith(".pdf"):
        method = _detect_pdf_method(src_path)

    cmd = [
        "mineru", "-p", src_path, "-o", out_dir,
        "--backend", config.MINERU_BACKEND,
        "-m", method,
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    stderr_lines: list[str] = []

    async def _read_stderr():
        while True:
            chunk = await process.stderr.read(4096)
            if not chunk:
                break
            text = chunk.decode(errors="replace")
            stderr_lines.append(text)
            for seg in re.split(r"[\r\n]+", text):
                seg = seg.strip()
                if not seg:
                    continue
                p = _parse_progress(seg)
                if p:
                    cur = _task_status.get(file_id, {})
                    if p.get("pct", 0) < cur.get("pct", 0):
                        p["pct"] = cur["pct"]
                    _task_status[file_id].update(p)

    reader = asyncio.create_task(_read_stderr())
    returncode = await process.wait()
    await reader
    error_text = "".join(stderr_lines).strip()

    if returncode != 0:
        raise RuntimeError(f"mineru 失败: {error_text or 'unknown error'}")

    files = sorted(
        str(f.relative_to(out_dir))
        for f in Path(out_dir).rglob("*")
        if f.is_file()
    )
    return {"files": files}

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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

def trim_uploads():
    ids_in_use = {d.name for d in config.OUTPUT_DIR.iterdir() if d.is_dir()}
    for d in sorted(config.UPLOAD_DIR.iterdir(), key=lambda d: d.stat().st_mtime):
        if d.is_dir() and d.name not in ids_in_use:
            shutil.rmtree(d, ignore_errors=True)

@app.get("/health")
async def health():
    return {"status": "ok", "service": "mineru-convert-md"}

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "templates" / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    ext = Path(file.filename).suffix.lower()
    if ext not in config.ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"不支持的文件格式: {ext}")

    cl = file.headers.get("content-length")
    if cl and int(cl) > config.MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="文件超过 100MB 限制")

    file_id = uuid.uuid4().hex
    save_dir = config.UPLOAD_DIR / file_id
    save_dir.mkdir(parents=True, exist_ok=True)
    save_path = save_dir / file.filename

    total = 0
    try:
        with open(save_path, "wb") as f:
            while True:
                chunk = await file.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > config.MAX_FILE_SIZE:
                    raise HTTPException(status_code=413, detail="文件超过 100MB 限制")
                f.write(chunk)
    except HTTPException:
        shutil.rmtree(save_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(save_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"文件读取失败: {str(e)}")

    return {"success": True, "file_id": file_id, "filename": file.filename, "size": total}

@app.post("/convert/{file_id}")
async def convert(file_id: str):
    save_dir = config.UPLOAD_DIR / file_id
    if not save_dir.exists():
        raise HTTPException(status_code=404, detail="文件不存在，请先上传")

    if _task_status.get(file_id, {}).get("status") in ("pending", "converting"):
        raise HTTPException(status_code=409, detail="转换正在进行中")

    files = list(save_dir.iterdir())
    if not files:
        raise HTTPException(status_code=404, detail="上传文件不存在")
    src_path = str(files[0])

    out_dir = config.OUTPUT_DIR / file_id
    out_dir.mkdir(parents=True, exist_ok=True)

    _set_status(file_id, "pending", "排队中", 0)
    task = asyncio.create_task(_run_convert(file_id, src_path, str(out_dir)))
    _pending[file_id] = task

    return {"success": True, "file_id": file_id}


@app.get("/status/{file_id}")
async def get_status(file_id: str):
    status = _task_status.get(file_id)
    if status is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"file_id": file_id, **status}

@app.get("/refine/{file_id}")
async def refine(file_id: str):
    file_dir = config.OUTPUT_DIR / file_id
    if not file_dir.exists():
        raise HTTPException(status_code=404, detail="文件不存在")

    md_files = sorted(file_dir.rglob("*.md"))
    if not md_files:
        raise HTTPException(status_code=404, detail="未找到输出文件")

    first_md = md_files[0]
    text = first_md.read_text(encoding="utf-8")
    result = await asyncio.to_thread(refiner.refine, text)

    if result is None:
        return {"optimized": False, "detail": "LLM 不可用或修正失败"}

    optimized_name = first_md.stem + "_optimized.md"
    optimized_path = first_md.parent / optimized_name
    optimized_path.write_text(result, encoding="utf-8")
    return {"optimized": True, "filename": optimized_name}

@app.get("/download/{file_id}")
async def download(file_id: str, filename: str = "", zip: bool = Query(False)):
    file_dir = config.OUTPUT_DIR / file_id
    if not file_dir.exists():
        raise HTTPException(status_code=404, detail="文件不存在")

    if zip:
        md_files = sorted(file_dir.rglob("*.md"))
        if not md_files:
            raise HTTPException(status_code=404, detail="未找到输出文件")
        root = md_files[0].parent
        buf = io.BytesIO()
        skip_suffixes = {".json", ".pdf"}
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(root.rglob("*")):
                if f.is_file() and f.suffix.lower() not in skip_suffixes:
                    zf.write(f, arcname=str(f.relative_to(root)))
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename={file_id}.zip"},
        )

    if filename:
        file_path = file_dir / filename
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="文件不存在")
        name = Path(filename).name
        return FileResponse(str(file_path), filename=name)

    md_files = sorted(file_dir.rglob("*.md"))
    if md_files:
        optimized = next((f for f in md_files if f.name.endswith("_optimized.md")), None)
        chosen = optimized or md_files[0]
        return FileResponse(str(chosen), filename=chosen.name)

    raise HTTPException(status_code=404, detail="未找到输出文件")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=56784)
