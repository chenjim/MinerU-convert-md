from unittest.mock import patch, MagicMock

from app import refiner


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
    assert "OCR" in refiner.PROMPT
    assert "Markdown" in refiner.PROMPT
    assert "嵌套列表" in refiner.PROMPT
