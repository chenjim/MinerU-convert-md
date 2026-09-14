import json, io, zipfile
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest


def test_index_returns_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "MinerU" in resp.text


def test_upload_rejects_unsupported_format(client):
    resp = client.post("/upload", files={"file": ("test.exe", b"data", "application/octet-stream")})
    assert resp.status_code == 400
    assert "不支持的文件格式" in resp.json()["detail"]


def test_upload_success(client, tmp_path):
    from app import config
    with patch("app.config.UPLOAD_DIR", tmp_path / "uploads"):
        resp = client.post("/upload", files={"file": ("test.pdf", b"%PDF", "application/pdf")})
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert "file_id" in body
        assert body["filename"] == "test.pdf"
        assert body["size"] == 4


def test_convert_returns_404_without_upload(client):
    resp = client.post("/convert/nonexistent")
    assert resp.status_code == 404


def test_convert_success(client, mock_subprocess, tmp_path):
    from app import config
    file_id = "conv_test"
    upload_dir = tmp_path / "uploads" / file_id
    upload_dir.mkdir(parents=True)
    (upload_dir / "test.pdf").write_bytes(b"%PDF")

    out_dir = tmp_path / "output" / file_id
    stem_dir = out_dir / "stem" / "auto"
    stem_dir.mkdir(parents=True)
    (stem_dir / "stem.md").write_text("# Converted", encoding="utf-8")

    with patch("app.config.UPLOAD_DIR", tmp_path / "uploads"):
        with patch("app.config.OUTPUT_DIR", tmp_path / "output"):
            resp = client.post(f"/convert/{file_id}")
            assert resp.status_code == 200
            body = resp.json()
            assert body["success"] is True
            assert body["file_id"] == file_id


def test_download_zip_includes_optimized_md(client, tmp_path):
    from app import config
    file_id = "zip_test"
    content_dir = tmp_path / "output" / file_id / "stem" / "auto"
    content_dir.mkdir(parents=True)
    (content_dir / "stem.md").write_text("# Original", encoding="utf-8")
    (content_dir / "stem_optimized.md").write_text("# Optimized", encoding="utf-8")

    with patch("app.config.OUTPUT_DIR", tmp_path / "output"):
        resp = client.get(f"/download/{file_id}?zip=true")
        assert resp.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        names = zf.namelist()
        assert "stem.md" in names
        assert "stem_optimized.md" in names


def test_download_specific_file(client, tmp_path):
    from app import config
    file_id = "single_test"
    content_dir = tmp_path / "output" / file_id / "stem" / "auto"
    content_dir.mkdir(parents=True)
    (content_dir / "stem.md").write_text("# Hello", encoding="utf-8")

    with patch("app.config.OUTPUT_DIR", tmp_path / "output"):
        resp = client.get(f"/download/{file_id}?filename=stem/auto/stem.md")
        assert resp.status_code == 200
        assert resp.text == "# Hello"


def test_download_returns_404_for_missing(client):
    resp = client.get("/download/nonexistent")
    assert resp.status_code == 404


def test_refine_returns_not_optimized_when_no_key(client, tmp_path):
    from app import config
    file_id = "refine_test"
    content_dir = tmp_path / "output" / file_id / "stem" / "auto"
    content_dir.mkdir(parents=True)
    (content_dir / "stem.md").write_text("# Test", encoding="utf-8")

    with patch("app.config.OUTPUT_DIR", tmp_path / "output"):
        with patch("app.config.LLM_API_KEY", ""):
            resp = client.get(f"/refine/{file_id}")
            assert resp.status_code == 200
            body = resp.json()
            assert body["optimized"] is False


def test_download_zip_excludes_log(client, tmp_path):
    from app import config
    file_id = "zip_log"
    content_dir = tmp_path / "output" / file_id / "stem" / "auto"
    content_dir.mkdir(parents=True)
    (content_dir / "stem.md").write_text("# Original", encoding="utf-8")
    (content_dir / "convert.log").write_text("raw stderr", encoding="utf-8")

    with patch("app.config.OUTPUT_DIR", tmp_path / "output"):
        resp = client.get(f"/download/{file_id}?zip=true")
        assert resp.status_code == 200
        names = zipfile.ZipFile(io.BytesIO(resp.content)).namelist()
        assert "stem.md" in names
        assert "convert.log" not in names


def test_convert_writes_stderr_log(tmp_path):
    import asyncio
    from app import main
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    chunks = [b"model init done\nLayout Predict:  50%|#####| 1/2 [00:00<00:00]\n", b""]

    async def fake_read(_n):
        return chunks.pop(0)

    process = MagicMock()
    process.stderr.read = AsyncMock(side_effect=fake_read)
    process.wait = AsyncMock(return_value=0)

    with patch("app.main.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock:
        mock.return_value = process
        result = asyncio.run(
            main._convert_with_progress("log_test", str(tmp_path / "a.pdf"), str(out_dir))
        )

    assert result == {"files": []}
    log = (out_dir / "convert.log").read_text(encoding="utf-8")
    assert "model init done" in log
    assert "Layout Predict" in log


def test_run_convert_skips_postprocess_when_disabled(tmp_path):
    import asyncio
    from app import main
    called = {"refine": False}

    async def fake_convert(file_id, src_path, out_dir, upper=85):
        return {"files": []}

    async def fake_refine(file_id, md_path, **kwargs):
        called["refine"] = True

    with patch("app.main._convert_with_progress", new=fake_convert):
        with patch("app.main._refine_md", new=fake_refine):
            with patch("app.main.trim_output"), patch("app.main.trim_uploads"):
                asyncio.run(main._run_convert("t_default", "x.pdf", str(tmp_path), optimize=False, alt=False))

    assert called["refine"] is False
    assert main._task_status["t_default"]["status"] == "done"


def test_run_convert_runs_alt_only_without_optimize(tmp_path):
    import asyncio
    from app import main
    out_dir = tmp_path / "out"
    stem_dir = out_dir / "stem" / "auto"
    stem_dir.mkdir(parents=True)
    (stem_dir / "stem.md").write_text("# x", encoding="utf-8")
    seen = {}

    async def fake_convert(file_id, src_path, out_dir, upper=85):
        return {"files": []}

    async def fake_refine(file_id, md_path, **kwargs):
        seen.update(kwargs)

    with patch("app.main._convert_with_progress", new=fake_convert):
        with patch("app.main._refine_md", new=fake_refine):
            with patch("app.main.trim_output"), patch("app.main.trim_uploads"):
                asyncio.run(main._run_convert("t_alt", "x.pdf", str(out_dir), optimize=False, alt=True))

    assert seen == {"add_alt": True, "do_refine": False}


def test_run_convert_runs_refine_when_optimize(tmp_path):
    import asyncio
    from app import main
    out_dir = tmp_path / "out"
    stem_dir = out_dir / "stem" / "auto"
    stem_dir.mkdir(parents=True)
    (stem_dir / "stem.md").write_text("# x", encoding="utf-8")
    called = {"refine": False}

    async def fake_convert(file_id, src_path, out_dir, upper=85):
        return {"files": []}

    async def fake_refine(file_id, md_path, **kwargs):
        called["refine"] = True

    with patch("app.main._convert_with_progress", new=fake_convert):
        with patch("app.main._refine_md", new=fake_refine):
            with patch("app.main.trim_output"), patch("app.main.trim_uploads"):
                asyncio.run(main._run_convert("t_opt", "x.pdf", str(out_dir), optimize=True))

    assert called["refine"] is True


def test_refine_md_caps_image_concurrency_and_dedupes(tmp_path, monkeypatch):
    import asyncio
    import threading
    import time
    from app import main, config

    monkeypatch.setattr(config, "VLM_MAX_CONCURRENCY", 3)

    # 10 处引用、9 张唯一图片，其中 img0.jpg 重复一次
    lines = []
    for i in range(9):
        (tmp_path / f"img{i}.jpg").write_bytes(b"x")
        lines.append(f"![](img{i}.jpg)")
    lines.append("![](img0.jpg)")
    md = tmp_path / "doc.md"
    md.write_text("\n".join(lines), encoding="utf-8")

    state = {"active": 0, "peak": 0, "calls": 0}
    guard = threading.Lock()

    def fake_describe(path):
        with guard:
            state["active"] += 1
            state["calls"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.02)
        with guard:
            state["active"] -= 1
        return "图片描述"

    with patch("app.refiner.describe_image", side_effect=fake_describe), \
         patch("app.refiner.refine", side_effect=lambda t: t):
        asyncio.run(main._refine_md("t_conc", str(md)))

    assert state["calls"] == 9          # 去重后只识别 9 张唯一图片
    assert 2 <= state["peak"] <= 3      # 有并发且不超过上限
    out = (tmp_path / "doc_optimized.md").read_text(encoding="utf-8")
    assert out.count("![图片描述](img0.jpg)") == 2  # 重复引用全部替换
    assert "![](" not in out


def test_refine_md_alt_only_persists_without_refine(tmp_path):
    import asyncio
    from app import main

    (tmp_path / "a.jpg").write_bytes(b"x")
    md = tmp_path / "doc.md"
    md.write_text("![](a.jpg)\n正文", encoding="utf-8")

    def fake_describe(path):
        return "一张图"

    with patch("app.refiner.describe_image", side_effect=fake_describe), \
         patch("app.refiner.refine", side_effect=AssertionError("不应调用 refine")):
        asyncio.run(main._refine_md("t_alt_only", str(md), add_alt=True, do_refine=False))

    out = (tmp_path / "doc_optimized.md").read_text(encoding="utf-8")
    assert out == "![一张图](a.jpg)\n正文"


def test_refine_md_skips_alt_when_disabled(tmp_path):
    import asyncio
    from app import main

    (tmp_path / "a.jpg").write_bytes(b"x")
    md = tmp_path / "doc.md"
    md.write_text("![](a.jpg)", encoding="utf-8")

    with patch("app.refiner.describe_image", side_effect=AssertionError("不应识图")), \
         patch("app.refiner.refine", return_value=None):
        asyncio.run(main._refine_md("t_no_alt", str(md), add_alt=False, do_refine=True))

    assert not (tmp_path / "doc_optimized.md").exists()



def test_download_blocks_path_traversal(client, tmp_path):
    from app import config
    file_id = "trav_test"
    content_dir = tmp_path / "output" / file_id / "stem" / "auto"
    content_dir.mkdir(parents=True)
    (content_dir / "stem.md").write_text("# ok", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("top-secret", encoding="utf-8")

    with patch("app.config.OUTPUT_DIR", tmp_path / "output"):
        assert client.get(f"/download/{file_id}?filename=stem/auto/stem.md").status_code == 200
        for bad in ("../../secret.txt", "../../../etc/passwd", "/etc/passwd"):
            r = client.get(f"/download/{file_id}", params={"filename": bad})
            assert r.status_code == 404, bad
            assert "top-secret" not in r.text


def test_convert_rejects_when_pending_full(client, tmp_path):
    from app import config, main
    fid = "cap_test"
    up = tmp_path / "uploads" / fid
    up.mkdir(parents=True)
    (up / "a.pdf").write_bytes(b"x")

    with patch("app.config.UPLOAD_DIR", tmp_path / "uploads"), \
         patch("app.config.OUTPUT_DIR", tmp_path / "output"), \
         patch("app.config.MAX_PENDING_TASKS", 5), \
         patch.object(main, "_pending", {f"p{i}": object() for i in range(5)}):
        resp = client.post(f"/convert/{fid}")
        assert resp.status_code == 429


def test_trim_output_keeps_active_tasks(tmp_path):
    from app import config, main
    out = tmp_path / "output"
    active, old = out / "active_id", out / "old_id"
    for d in (active, old):
        (d / "stem").mkdir(parents=True)
        (d / "stem" / "big.md").write_bytes(b"x" * 1000)

    main._task_status["active_id"] = {"status": "converting", "phase": "", "pct": 50}
    try:
        with patch("app.config.OUTPUT_DIR", out), patch("app.config.OUTPUT_MAX_SIZE", 500):
            main.trim_output()
    finally:
        main._task_status.pop("active_id", None)

    assert active.exists()      # 转换中，保留
    assert not old.exists()     # 超限，逐出
