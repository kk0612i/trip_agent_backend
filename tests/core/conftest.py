"""配置和日志测试的隔离环境。"""

import logging
from collections.abc import Iterator

import pytest
from loguru import logger

from app.core.config import Settings, get_settings


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """隔离环境变量、配置缓存和全局日志配置。"""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()
    root = logging.getLogger()
    previous_handlers = root.handlers[:]
    previous_level = root.level
    uvicorn_loggers = [logging.getLogger(name) for name in (
        "uvicorn", "uvicorn.error", "uvicorn.access", "fastapi",
    )]
    previous_loggers = [(item, item.handlers[:], item.level, item.propagate) for item in uvicorn_loggers]
    yield
    logger.complete()
    logger.remove()
    root.handlers = previous_handlers
    root.setLevel(previous_level)
    for item, handlers, level, propagate in previous_loggers:
        item.handlers = handlers
        item.setLevel(level)
        item.propagate = propagate
    get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    """提供同步写入 JSON 的基础日志配置。"""
    return Settings(_env_file=None, log_format="json", log_enqueue=False)
