"""行程会话测试的 SQLite 替身数据库、假图执行器和应用环境。"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

# 导入全部相关模型，使元数据包含所有表。
import app.models.poi_profile  # noqa: F401
import app.models.revoked_token  # noqa: F401
import app.models.trip  # noqa: F401
import app.models.trip_plan_version  # noqa: F401
import app.models.trip_turn  # noqa: F401
from app.core.config import Settings, get_settings
from app.models.base import Base
from app.models.user import User
from app.schemas.auth_schema import AuthenticatedUser
from main import create_app
from tests.trip.trip_session_fakes import TEST_JWT_SECRET, FakeTurnRunner


@pytest.fixture
def trip_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """隔离真实配置，并在测试后恢复全局日志与配置缓存。

    不设为 autouse，避免影响同目录下其他业务的测试。
    """
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
async def trip_db(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """按 ORM 元数据创建 SQLite 替身，同时验证模型在 SQLite 上可用。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'trip.db'}")
    async with engine.begin() as connection:
        await connection.execute(text("PRAGMA journal_mode=WAL"))
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def session_factory(trip_db: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """与应用相同配置的会话工厂。"""
    return async_sessionmaker(trip_db, expire_on_commit=False)


@pytest.fixture
def fake_runner() -> FakeTurnRunner:
    """可替换处理函数的假图执行器。"""
    return FakeTurnRunner()


@pytest_asyncio.fixture
async def make_user(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[[str], Awaitable[AuthenticatedUser]]:
    """直接写入用户并返回已认证身份，供 Service 层测试使用。"""

    async def create(email: str) -> AuthenticatedUser:
        async with session_factory() as session, session.begin():
            user = User(email=email, password_hash="$argon2id$test", created_at=datetime(2026, 10, 1))
            session.add(user)
            await session.flush()
            user_id = user.id
        return AuthenticatedUser(
            user_id=user_id, jti=f"jti-{user_id}", expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    return create


@pytest_asyncio.fixture
async def client(
    trip_environment: None,
    session_factory: async_sessionmaker[AsyncSession],
    fake_runner: FakeTurnRunner,
) -> AsyncIterator[AsyncClient]:
    """通过真实 ASGI 应用测试接口，图执行器替换为假实现。"""
    app = create_app()
    async with app.router.lifespan_context(app):
        app.state.session_factory = session_factory
        app.state.turn_runner = fake_runner
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://testserver",
        ) as http_client:
            yield http_client


@pytest_asyncio.fixture
async def login(client: AsyncClient) -> Callable[[str], Awaitable[dict[str, str]]]:
    """注册并登录指定邮箱，返回 Bearer 请求头。"""

    async def register_and_login(email: str) -> dict[str, str]:
        credentials = {"email": email, "password": "trip-test-password"}
        assert (await client.post("/api/auth/register", json=credentials)).status_code == 201
        logged_in = await client.post("/api/auth/login", json=credentials)
        assert logged_in.status_code == 200
        return {"Authorization": f"Bearer {logged_in.json()['access_token']}"}

    return register_and_login


@pytest_asyncio.fixture
async def headers(login: Callable[[str], Awaitable[dict[str, str]]]) -> dict[str, str]:
    """默认测试用户的请求头。"""
    return await login("trip-owner@example.com")
