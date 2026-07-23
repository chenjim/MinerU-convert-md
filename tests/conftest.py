import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

os.environ.setdefault("LLM_API_KEY", "")
os.environ.setdefault("OUTPUT_MAX_SIZE", str(500 * 1024 * 1024))

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def mock_subprocess():
    mock_process = MagicMock()
    mock_process.returncode = 0
    mock_process.communicate = AsyncMock(return_value=(b"", b""))

    with patch("app.main.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock:
        mock.return_value = mock_process
        yield mock


@pytest.fixture
def sample_md_dir(tmp_path):
    d = tmp_path / "output" / "test_id" / "stem" / "auto"
    d.mkdir(parents=True)
    (d / "stem.md").write_text("# Hello\n\nTest content", encoding="utf-8")
    return d
