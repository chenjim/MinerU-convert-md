import os
import io
import re
import sys
import uuid
import asyncio
import tempfile
import shutil
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Form, Query
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from app import config
from app import refiner
from app import mineru_pool


@asynccontextmanager
async def lifespan(app: FastAPI):
    if config.MINERU_WARM_POOL:
        try:
            await mineru_pool.start_pool()
        except Exception as e:
            print(f"[warn] mineru 常驻池启动失败，回退冷启动: {e}", file=sys.stderr, flush=True)
    yield
    await mineru_pool.stop_pool()


app = FastAPI(title="MinerU 文档转换服务", lifespan=lifespan)

_pending: dict[str, asyncio.Task] = {}
_task_status: dict[str, dict] = {}


def _set_status(file_id: str, status: str, phase: str = "", pct: int = 0):
    _task_status[file_id] = {"status": status, "phase": phase, "pct": pct}


_RUN_INFO_RE = re.compile(r'total_pages=(\d+).*?total_batches=(\d+)')
_BATCH_RE = re.compile(r'window batch (\d+)/(\d+)', re.I)
_PAGE_BAR_RE = re.compile(r'Processing pages:\s*(\d+)%.*?\|\s*(\d+)/(\d+)')
_STAGE_BAR_RE = re.compile(r'([A-Za-z][\w\s-]+?):\s*(\d+)%.*?\|\s*(\d+)/(\d+)')

# pipeline 后端实际执行顺序：Layout → Table → OCR(det→rec)，每个 batch 内独立重置。
# Layout 计数是页数，Table/OCR 计数是检测框（区域）数，单位不同需分别标注。
# 批内区间按各阶段实测耗时占比分配（OCR 检测最耗时），避免早期阶段跑太快、OCR 段卡住。
# (进度条关键字, 显示名, 计数单位, 批内起始比例, 批内结束比例)
_TABLE_BAND = 0.12
_STAGE_BANDS: list[tuple[str, str, str, float, float]] = [
    ("layout",  "布局分析", "页",   0.00, 0.20),
    ("table",   "表格识别", "区域", 0.20, 0.20 + _TABLE_BAND),
    ("ocr-det", "文字识别", "区域", 0.20 + _TABLE_BAND, 0.85),
    ("ocr-rec", "文字识别", "区域", 0.85, 1.00),
]


class _ProgressTracker:
    """把 MinerU 的 tqdm 进度条映射成平滑单调的全局百分比（10→85）。

    整体进度 = 已完成页数占比 + 当前 batch 内阶段进度 / batch 总数。
    Processing pages 提供跨 batch 的页进度，Layout/Table/OCR 提供批内细粒度进度；
    阶段文案直接采用进度条自身的计数（页 / 区域），不做页码估算。
    """

    def __init__(self, upper: int = 85) -> None:
        self.upper = upper
        self.total_pages = 1
        self.total_batches = 1
        self.pages_done = 0
        self.stage = 0.0
        self.table_seen = False
        self.active = False
        self.pct = 0

    def _overall(self) -> int:
        pages = min(1.0, self.pages_done / self.total_pages)
        overall = min(1.0, pages + self.stage / self.total_batches)
        return int(10 + overall * (self.upper - 10))

    def _emit(self, phase: str) -> dict:
        self.pct = max(self.pct, self._overall())
        return {"phase": phase, "pct": self.pct}

    def feed(self, line: str) -> Optional[dict]:
        t = line.strip()
        if not t:
            return None

        m = _RUN_INFO_RE.search(t)
        if m:
            self.total_pages = max(1, int(m.group(1)))
            self.total_batches = max(1, int(m.group(2)))
            self.active = True
            return None

        # 未见到本次任务的起始行前，忽略残留的进度行（常驻 worker 复用时的串扰）
        if not self.active:
            return None

        if _BATCH_RE.search(t):
            self.stage = 0.0
            self.table_seen = False
            return None

        if "model init done" in t.lower():
            self.pct = max(self.pct, 10)
            return {"phase": "模型加载完成", "pct": self.pct}

        m = _PAGE_BAR_RE.search(t)
        if m:
            self.pages_done = max(self.pages_done, int(m.group(2)))
            self.total_pages = max(self.total_pages, int(m.group(3)))
            self.stage = 0.0
            return self._emit(f"处理页面 {self.pages_done}/{self.total_pages} 页")

        m = _STAGE_BAR_RE.search(t)
        if m:
            name, rpct = m.group(1).lower(), int(m.group(2))
            n, total = int(m.group(3)), int(m.group(4))
            for keyword, display, unit, lo, hi in _STAGE_BANDS:
                if keyword in name:
                    if keyword == "table":
                        self.table_seen = True
                    elif not self.table_seen and keyword.startswith("ocr"):
                        # 本批没有表格阶段：OCR 起点前移，表格区间让给 OCR，避免空档跳变
                        lo = max(0.0, lo - _TABLE_BAND)
                    self.stage = max(self.stage, lo + (hi - lo) * rpct / 100)
                    return self._emit(f"{display} {n}/{total} {unit}")
            return None

        return None


async def _refine_md(file_id: str, md_path: str, add_alt: bool = True, do_refine: bool = True):
    """按开关补图片 alt（最多 VLM_MAX_CONCURRENCY 路并发）与 LLM 重排，结果存为 *_optimized.md。"""
    md = Path(md_path)
    original = text = md.read_text(encoding="utf-8")
    md_dir = md.parent

    if add_alt:
        # 去重：同一图片只识别一次
        refs = list(dict.fromkeys(re.findall(r'!\[\]\(([^)]+)\)', text)))
        if refs:
            total = len(refs)
            sem = asyncio.Semaphore(config.VLM_MAX_CONCURRENCY)
            lock = asyncio.Lock()
            descs: dict[str, str] = {}
            done = 0

            async def _one(ref: str) -> None:
                nonlocal done
                img_file = md_dir / ref
                desc = None
                if img_file.exists():
                    async with sem:
                        desc = await asyncio.to_thread(refiner.describe_image, str(img_file))
                async with lock:
                    if desc:
                        descs[ref] = desc
                    done += 1
                    _set_status(file_id, "converting", f"图片理解 {done}/{total} 张", int(85 + done / total * 8))

            await asyncio.gather(*(_one(ref) for ref in refs))
            # gather 期间只读文本，收集完再统一替换，避免并发改写
            for ref, desc in descs.items():
                text = text.replace(f"![]({ref})", f"![{desc}]({ref})")

    optimized = md.parent / (md.stem + "_optimized.md")
    if do_refine:
        _set_status(file_id, "converting", "LLM 优化排版中...", 95)
        result = await asyncio.to_thread(refiner.refine, text)
        if result:
            optimized.write_text(result, encoding="utf-8")
            return
    # 未做 LLM 重排（或重排失败）时，落盘补好 alt 的文本，避免描述丢失
    if text != original:
        optimized.write_text(text, encoding="utf-8")


async def _run_convert(file_id: str, src_path: str, out_dir: str, optimize: bool = False, alt: bool = False):
    _set_status(file_id, "converting", "排队中...", 5)
    # 有后处理（图片 alt / LLM 重排）时 MinerU 保留 10~85；否则独占 10~95
    post = optimize or alt
    upper = 85 if post else 95
    try:
        result = await _convert_with_progress(file_id, src_path, out_dir, upper=upper)
        _set_status(file_id, "converting", "整理输出...", upper)
        await asyncio.to_thread(trim_output)
        await asyncio.to_thread(trim_uploads)

        if post:
            md_files = sorted(Path(out_dir).rglob("*.md"))
            if md_files:
                await _refine_md(file_id, str(md_files[0]), add_alt=alt, do_refine=optimize)

        _set_status(file_id, "done", "转换完成", 100)
        _task_status[file_id]["result"] = result
    except Exception as e:
        _set_status(file_id, "error", str(e), 0)
    finally:
        _pending.pop(file_id, None)


def _merge_progress(file_id: str, p: dict) -> None:
    cur = _task_status.setdefault(file_id, {})
    if p.get("pct", 0) < cur.get("pct", 0):
        p["pct"] = cur["pct"]
    cur.update(p)


async def _run_mineru(cmd: list[str], log_file, on_line=None) -> tuple[int, str]:
    """启动 mineru 子进程，落盘 stderr，并按需回调每一行。"""
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
            log_file.write(text)
            log_file.flush()
            stderr_lines.append(text)
            if on_line is not None:
                for seg in re.split(r"[\r\n]+", text):
                    on_line(seg)

    reader = asyncio.create_task(_read_stderr())
    returncode = await process.wait()
    await reader
    return returncode, "".join(stderr_lines).strip()


async def _convert_with_progress(file_id: str, src_path: str, out_dir: str, upper: int = 85) -> dict:
    # auto 直接透传给 MinerU，由其 pdfium 分类器判断 txt/ocr（可识别乱码字体/扫描件）
    method = config.MINERU_METHOD

    base_cmd = [
        "mineru", "-p", src_path, "-o", out_dir,
        "--backend", config.MINERU_BACKEND,
        "-m", method,
    ]

    log_path = Path(out_dir) / "convert.log"
    log_file = open(log_path, "w", encoding="utf-8")

    tracker = _ProgressTracker(upper=upper)
    pool = mineru_pool.get_pool()
    worker = None
    try:
        if pool is not None:
            worker = await pool.acquire()
            _set_status(file_id, "converting", "模型加载...", 8)
            worker.bind(tracker, lambda p: _merge_progress(file_id, p), log_file)
            cmd = base_cmd + ["--api-url", worker.url]
            log_file.write(f"# cmd: {' '.join(cmd)} (warm worker :{worker.port})\n")
            log_file.flush()
            returncode, error_text = await _run_mineru(cmd, log_file)
        else:
            _set_status(file_id, "converting", "模型加载...", 8)
            log_file.write(f"# cmd: {' '.join(base_cmd)}\n")
            log_file.flush()
            returncode, error_text = await _run_mineru(
                base_cmd, log_file, on_line=lambda seg: _feed_line(file_id, tracker, seg)
            )
    finally:
        if worker is not None:
            worker.unbind()
            pool.release(worker)
        log_file.close()

    if returncode != 0:
        raise RuntimeError(f"mineru 失败: {error_text or 'unknown error'}")

    files = sorted(
        str(f.relative_to(out_dir))
        for f in Path(out_dir).rglob("*")
        if f.is_file() and f.name != "convert.log"
    )
    return {"files": files}


def _feed_line(file_id: str, tracker: "_ProgressTracker", seg: str) -> None:
    p = tracker.feed(seg)
    if p:
        _merge_progress(file_id, p)

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
async def convert(file_id: str, optimize: bool = Query(False), alt: bool = Query(False)):
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
    task = asyncio.create_task(_run_convert(file_id, src_path, str(out_dir), optimize, alt))
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
        skip_suffixes = {".json", ".pdf", ".log"}
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
