from unittest.mock import patch, MagicMock

import pytest

from app import refiner


def _make_response(content):
    mock_response = MagicMock()
    mock_response.choices[0].message.content = content
    return mock_response


def test_get_client_returns_none_without_key():
    with patch("app.config.LLM_API_KEY", ""):
        assert refiner.get_client() is None


def test_get_client_returns_client_with_key():
    with patch("app.config.LLM_API_KEY", "sk-test"):
        with patch("app.config.LLM_BASE_URL", "https://test.url"):
            client = refiner.get_client()
            assert client is not None
            assert client.api_key == "sk-test"


def test_refine_returns_none_without_key():
    with patch("app.config.LLM_API_KEY", ""):
        assert refiner.refine("# Hello") is None


def test_refine_returns_none_on_api_error():
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = Exception("API error")

    with patch("app.config.LLM_API_KEY", "sk-test"):
        with patch("app.refiner.get_client", return_value=mock_client):
            assert refiner.refine("# Hello") is None


def test_refine_returns_content_on_success():
    mock_response = MagicMock()
    mock_response.choices[0].message.content = "# Refined"
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response

    with patch("app.config.LLM_API_KEY", "sk-test"):
        with patch("app.refiner.get_client", return_value=mock_client):
            result = refiner.refine("# Hello")
            assert result == "# Refined"


def test_refine_returns_none_on_empty_content():
    mock_response = MagicMock()
    mock_response.choices[0].message.content = None
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response

    with patch("app.config.LLM_API_KEY", "sk-test"):
        with patch("app.refiner.get_client", return_value=mock_client):
            assert refiner.refine("# Hello") is None


def test_prompt_contains_key_instructions():
    assert "OCR" in refiner.REFINE_PROMPT
    assert "Markdown" in refiner.REFINE_PROMPT
    assert "嵌套列表" in refiner.REFINE_PROMPT


def test_describe_image_returns_none_without_key(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"fake")
    with patch("app.config.VLM_API_KEY", ""):
        assert refiner.describe_image(str(img)) is None


def test_describe_image_retries_then_success(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"fake")
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = [
        Exception("timeout"),
        Exception("timeout"),
        _make_response("  一张架构图  "),
    ]

    with patch("app.config.VLM_API_KEY", "sk-test"), patch("app.config.VLM_MODEL", "vlm-test"):
        with patch("app.refiner.get_image_client", return_value=mock_client):
            with patch("app.refiner.time.sleep"):
                assert refiner.describe_image(str(img)) == "一张架构图"
    assert mock_client.chat.completions.create.call_count == 3
    assert mock_client.chat.completions.create.call_args.kwargs["timeout"] == 60
    assert mock_client.chat.completions.create.call_args.kwargs["model"] == "vlm-test"
    assert mock_client.chat.completions.create.call_args.kwargs["max_tokens"] == refiner.DESCRIBE_MAX_TOKENS
    assert refiner.DESCRIBE_MAX_TOKENS == 10240


def test_describe_image_retries_on_empty_content(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"fake")
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = [
        _make_response(None),
        _make_response(""),
        _make_response("内容"),
    ]

    with patch("app.config.VLM_API_KEY", "sk-test"):
        with patch("app.refiner.get_image_client", return_value=mock_client):
            with patch("app.refiner.time.sleep"):
                assert refiner.describe_image(str(img)) == "内容"
    assert mock_client.chat.completions.create.call_count == 3


def test_describe_image_returns_none_on_all_failures(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"fake")
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = Exception("API error")

    with patch("app.config.VLM_API_KEY", "sk-test"):
        with patch("app.refiner.get_image_client", return_value=mock_client):
            with patch("app.refiner.time.sleep"):
                assert refiner.describe_image(str(img)) is None
    assert mock_client.chat.completions.create.call_count == 3


def test_get_image_client_is_separate_from_text_client():
    with patch("app.config.VLM_API_KEY", "vlm-key"), \
         patch("app.config.VLM_BASE_URL", "http://local:11434/v1"), \
         patch("app.config.LLM_API_KEY", "llm-key"), \
         patch("app.config.LLM_BASE_URL", "https://remote"):
        vc = refiner.get_image_client()
        tc = refiner.get_client()
        assert vc.api_key == "vlm-key"
        assert str(vc.base_url).rstrip("/") == "http://local:11434/v1"
        assert tc.api_key == "llm-key"
