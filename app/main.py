import os
import io
import uuid
import asyncio
import tempfile
import shutil
import zipfile
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException, Form, Query
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from app import config
from app import refiner

app = FastAPI(title="MinerU 文档转换服务")

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

    files = list(save_dir.iterdir())
    if not files:
        raise HTTPException(status_code=404, detail="上传文件不存在")
    src_path = str(files[0])

    out_dir = config.OUTPUT_DIR / file_id
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        result = await convert_file(src_path, str(out_dir))
        await asyncio.to_thread(trim_output)
        await asyncio.to_thread(trim_uploads)
        return {"success": True, "file_id": file_id, "output": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"转换失败: {str(e)}")

async def convert_file(src_path: str, out_dir: str) -> dict:
    cmd = [
        "mineru", "-p", src_path, "-o", out_dir,
        "--backend", config.MINERU_BACKEND,
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"mineru 失败: {stderr.decode().strip() or 'unknown error'}")

    files = sorted(
        str(f.relative_to(out_dir))
        for f in Path(out_dir).rglob("*")
        if f.is_file()
    )
    return {"files": files}

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
        name = md_files[0].name
        return FileResponse(str(md_files[0]), filename=name)

    raise HTTPException(status_code=404, detail="未找到输出文件")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=56784)
