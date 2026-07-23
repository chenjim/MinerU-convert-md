import json, io, zipfile
from pathlib import Path
from unittest.mock import patch

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
