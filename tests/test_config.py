import os
from app import config


def test_default_values():
    assert config.MAX_FILE_SIZE == 100 * 1024 * 1024
    assert config.MINERU_BACKEND == "pipeline"
    assert config.LLM_BASE_URL == "https://api.deepseek.com"
    assert ".pdf" in config.ALLOWED_EXTENSIONS
    assert ".docx" in config.ALLOWED_EXTENSIONS


def test_env_values():
    assert config.LLM_API_KEY == os.getenv("LLM_API_KEY", "")
    assert config.LLM_MODEL == os.getenv("LLM_MODEL", "deepseek-chat")
    assert config.OUTPUT_MAX_SIZE == int(os.getenv("OUTPUT_MAX_SIZE", str(500 * 1024 * 1024)))
