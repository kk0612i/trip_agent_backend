"""认证接口测试的独立数据库和应用环境。"""

import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from main import create_app
from tests.auth.auth_data import TEST_JWT_SECRET


@pytest.fixture(autouse=True)
def isolated_auth_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """隔离真实配置，并在测试后恢复全局日志与配置缓存。"""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("AUTH_JWT_SECRET", TEST_JWT_SECRET)
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("LOG_ENQUEUE", "false")
    get_settings.cache_clear()
    root = logging.getLogger()
    previous_handlers, previous_level = root.handlers[:], root.level
    yield
    logger.complete()
    logger.remove()
    root.handlers, root.level = previous_handlers, previous_level
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def auth_db(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """创建与已有表契约对应的 SQLite 替身，不连接业务数据库。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}")
    async with engine.begin() as connection:
        await connection.execute(text("PRAGMA journal_mode=WAL"))
        # 测试数据库拒绝明文及其他哈希算法，让接口行为验证密码存储约束。
        await connection.execute(text("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email VARCHAR(254) NOT NULL,
                password_hash VARCHAR(255) NOT NULL
                    CHECK (password_hash LIKE '$argon2id$%' AND LENGTH(password_hash) > 40),
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uk_users_email UNIQUE (email)
            )
        """))
        await connection.execute(text("""
            CREATE TABLE revoked_tokens (
                jti VARCHAR(64) PRIMARY KEY,
                expires_at DATETIME NOT NULL
            )
        """))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def client(auth_db: AsyncEngine) -> AsyncIterator[AsyncClient]:
    """通过真实 ASGI 应用测试接口，每个请求使用独立数据库会话。"""
    app = create_app()
    async with app.router.lifespan_context(app):
        app.state.session_factory = async_sessionmaker(auth_db, expire_on_commit=False)
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://testserver",
        ) as http_client:
            yield http_client


@pytest_asyncio.fixture
async def logged_in_user(client: AsyncClient) -> tuple[int, str]:
    """通过注册和登录取得当前测试专用的账户及令牌。"""
    credentials = {"email": "user@example.com", "password": "private-test-password"}
    registered = await client.post("/api/auth/register", json=credentials)
    assert registered.status_code == 201
    logged_in = await client.post("/api/auth/login", json=credentials)
    assert logged_in.status_code == 200
    return registered.json()["id"], logged_in.json()["access_token"]
