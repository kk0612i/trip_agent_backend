"""使用 MySQL 连接内临时表验证真实驱动，连接关闭即释放测试数据。"""

import os
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

import main
from app.core.config import get_settings
from app.core.db import create_db_engine
from tests.auth.auth_data import TEST_JWT_SECRET

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MYSQL_AUTH_INTEGRATION") != "1",
    reason="需显式开启 MySQL 集成验证及允许创建连接内临时表",
)


@pytest.mark.asyncio
async def test_auth_contract_with_mysql_temporary_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    """在真实 MySQL 驱动上执行注册、登录、校验与撤销完整流程。"""
    settings = get_settings().model_copy(update={
        "app_env": "test", "log_format": "json", "log_enqueue": False,
        "log_file_enabled": False, "auth_jwt_secret": SecretStr(TEST_JWT_SECRET),
    })
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    engine = create_db_engine(settings)
    try:
        async with engine.connect() as connection:
            # 同名临时表仅在本连接中覆盖查找目标，沿用实际表的字段与约束。
            for table in ("users", "revoked_tokens"):
                definition = (await connection.execute(text(f"SHOW CREATE TABLE {table}"))).one()[1]
                prefix = f"CREATE TABLE `{table}`"
                assert definition.startswith(prefix)
                temporary_definition = definition.replace(prefix, f"CREATE TEMPORARY TABLE `{table}`", 1)
                await connection.execute(text(temporary_definition))
            assert await connection.scalar(text("SELECT @@session.time_zone")) == "+00:00"
            await connection.commit()
            app = main.create_app()
            async with app.router.lifespan_context(app):
                # 同一连接仅用于本测试的顺序请求，不用于并发请求验证。
                app.state.session_factory = async_sessionmaker(connection, expire_on_commit=False)
                async with AsyncClient(
                    transport=ASGITransport(app=app, raise_app_exceptions=False),
                    base_url="http://mysql-integration",
                ) as client:
                    credentials = {"email": "mysql-auth-test@example.com", "password": " integration-secret "}
                    registered = await client.post("/api/auth/register", json=credentials)
                    assert registered.status_code == 201
                    assert set(registered.json()) == {"id", "email", "created_at"}
                    assert datetime.fromisoformat(registered.json()["created_at"]).utcoffset() == UTC.utcoffset(None)
                    duplicate = await client.post("/api/auth/register", json=credentials)
                    assert duplicate.status_code == 409
                    assert duplicate.json()["code"] == "EMAIL_ALREADY_EXISTS"
                    wrong = await client.post("/api/auth/login", json={**credentials, "password": "wrong-password"})
                    assert wrong.status_code == 401
                    assert wrong.headers["WWW-Authenticate"] == "Bearer"
                    logged_in = await client.post("/api/auth/login", json=credentials)
                    assert logged_in.status_code == 200
                    assert logged_in.headers["Cache-Control"] == "no-store"
                    headers = {"Authorization": f"Bearer {logged_in.json()['access_token']}"}
                    validated = await client.get("/api/auth/token", headers=headers)
                    assert validated.status_code == 200
                    assert validated.json()["user_id"] == registered.json()["id"]
                    exited = await client.post("/api/auth/logout", headers=headers)
                    assert exited.status_code == 204
                    assert exited.content == b""
                    assert (await client.get("/api/auth/token", headers=headers)).status_code == 401
    finally:
        await engine.dispose()
        get_settings.cache_clear()
