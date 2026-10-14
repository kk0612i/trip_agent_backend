"""FastAPI 应用装配与启动入口。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from secrets import token_urlsafe
from time import perf_counter
from uuid import uuid4

import httpx
import uvicorn
from anyio import to_thread
from argon2 import PasswordHasher, Type
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.trip.trip_graph import TripTurnRunner, UnavailableTurnRunner
from app.agent.trip.trip_state import TripDeps
from app.api.auth_router import router as auth_router
from app.api.errors import business_error_handler, validation_error_handler
from app.api.middleware import log_request
from app.api.trip_router import router as trip_router
from app.core.config import Settings, get_settings
from app.core.db import create_db_engine, create_session_factory
from app.core.errors import BusinessError
from app.core.llm import create_chat_model
from app.core.log import setup_logging
from app.core.security import TokenCodec
from app.tools.amap import AmapClient
from app.tools.bocha import BochaClient


def build_trip_deps(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    amap_http: httpx.AsyncClient,
    bocha_http: httpx.AsyncClient,
) -> TripDeps:
    """装配行程对话图的运行依赖；未配置博查密钥时问答不提供联网搜索。"""
    amap = AmapClient(
        amap_http,
        api_key=settings.amap_api_key.get_secret_value(),
        base_url=str(settings.amap_base_url),
        max_concurrency=settings.amap_max_concurrency,
        cache_ttl_seconds=settings.amap_cache_ttl_seconds,
    )
    bocha_key = settings.bocha_api_key.get_secret_value()
    bocha = BochaClient(bocha_http, api_key=bocha_key, base_url=str(settings.bocha_base_url)) if bocha_key else None
    return TripDeps(
        llm=create_chat_model(settings, temperature=0),
        qa_llm=create_chat_model(settings, temperature=0.3),
        amap=amap,
        bocha=bocha,
        session_factory=session_factory,
        solver_time_limit_seconds=settings.trip_solver_time_limit_seconds,
        max_tool_calls=settings.agent_max_tool_calls,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """加载配置、装配共享资源，并在关闭时释放引擎和日志队列。"""
    settings = get_settings()
    setup_logging(settings)
    app.state.settings = settings
    app.title = settings.app_name
    app.state.token_codec = TokenCodec(settings)
    app.state.password_hasher = PasswordHasher(type=Type.ID)
    # 不存在的账户也执行一次哈希验证，减少按耗时枚举邮箱的差异。
    app.state.dummy_password_hash = await to_thread.run_sync(
        app.state.password_hasher.hash, token_urlsafe(32),
    )
    engine = create_db_engine(settings)
    app.state.session_factory = create_session_factory(engine)
    amap_http = httpx.AsyncClient(timeout=settings.amap_timeout_seconds)
    bocha_http = httpx.AsyncClient(timeout=settings.bocha_timeout_seconds)
    if settings.deepseek_api_key.get_secret_value() and settings.amap_api_key.get_secret_value():
        deps = build_trip_deps(settings, app.state.session_factory, amap_http, bocha_http)
        app.state.turn_runner = TripTurnRunner(deps, recursion_limit=settings.agent_recursion_limit)
    else:
        logger.bind(event="trip_agent_disabled", status="skipped").warning("未配置模型或高德密钥，行程对话不可用")
        app.state.turn_runner = UnavailableTurnRunner()
    started_at = perf_counter()
    with logger.contextualize(entry_point="api", run_id=uuid4().hex):
        logger.bind(event="application_started", status="success").info("应用启动完成")
        try:
            yield
        finally:
            await amap_http.aclose()
            await bocha_http.aclose()
            await engine.dispose()
            logger.bind(
                event="application_stopped",
                status="completed",
                duration_ms=round((perf_counter() - started_at) * 1000, 2),
            ).info("应用关闭完成")
            await logger.complete()


def create_app() -> FastAPI:
    """创建应用并注册生命周期，导入模块时不读取配置或连接外部服务。"""
    application = FastAPI(title="Trip Agents", lifespan=lifespan)
    application.add_exception_handler(BusinessError, business_error_handler)
    application.add_exception_handler(RequestValidationError, validation_error_handler)
    application.middleware("http")(log_request)
    application.include_router(auth_router)
    application.include_router(trip_router)
    return application


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
