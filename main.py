"""FastAPI 应用装配与启动入口。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter
from uuid import uuid4

import uvicorn
from fastapi import FastAPI
from loguru import logger

from app.core.config import get_settings
from app.core.log import setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """在应用生命周期中加载配置、初始化日志并等待日志写入完成。"""
    settings = get_settings()
    setup_logging(settings)
    app.state.settings = settings
    app.title = settings.app_name
    started_at = perf_counter()
    with logger.contextualize(entry_point="api", run_id=uuid4().hex):
        logger.bind(event="application_started", status="success").info("应用启动完成")
        try:
            yield
        finally:
            logger.bind(
                event="application_stopped",
                status="completed",
                duration_ms=round((perf_counter() - started_at) * 1000, 2),
            ).info("应用关闭完成")
            await logger.complete()


def create_app() -> FastAPI:
    """创建应用并注册生命周期，导入模块时不读取配置或连接外部服务。"""
    return FastAPI(title="Trip Agents", lifespan=lifespan)


app = create_app()


def main() -> None:
    """使用缓存配置启动 Uvicorn 服务。"""
    settings = get_settings()
    setup_logging(settings)
    uvicorn.run(
        "main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.app_env == "development" and settings.app_debug,
        log_config=None,
    )


if __name__ == "__main__":
    main()
