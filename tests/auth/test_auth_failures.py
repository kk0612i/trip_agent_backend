"""认证接口在数据库冲突、提交失败和并发请求下的行为。"""

import asyncio
import json
import sqlite3

import pytest
from httpx import AsyncClient
from pymysql.err import IntegrityError as MySQLIntegrityError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import get_settings
from app.core.log import setup_logging
from tests.auth.db_faults import fail_next_commit, fail_next_insert


@pytest.mark.asyncio
@pytest.mark.parametrize("database", ["mysql", "sqlite"])
async def test_unique_constraint_race_maps_to_email_conflict_and_rolls_back(
    client: AsyncClient, auth_db: AsyncEngine, database: str,
) -> None:
    """预检查通过后的唯一冲突仍返回 409，回滚后同邮箱可再次注册。"""
    error = (
        MySQLIntegrityError(1062, "Duplicate entry 'user@example.com' for key 'users.uk_users_email'")
        if database == "mysql" else sqlite3.IntegrityError("UNIQUE constraint failed: users.email")
    )
    credentials = {"email": "user@example.com", "password": "private-test-password"}
    with fail_next_insert(auth_db, "users", error):
        failed = await client.post("/api/auth/register", json=credentials)

    assert failed.status_code == 409
    assert failed.json()["code"] == "EMAIL_ALREADY_EXISTS"
    assert (await client.post("/api/auth/register", json=credentials)).status_code == 201


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["register", "logout"])
async def test_failed_commit_is_rolled_back_and_logged_without_secrets(
    client: AsyncClient, auth_db: AsyncEngine, logged_in_user: tuple[int, str],
    endpoint: str, capsys: pytest.CaptureFixture[str],
) -> None:
    """提交失败不返回成功，事务可回滚，异常只在边界记录一次安全堆栈。"""
    token = logged_in_user[1]
    credentials = {"email": "other@example.com", "password": "private-test-password"}
    kwargs = {"json": credentials} if endpoint == "register" else {
        "headers": {"Authorization": f"Bearer {token}"},
    }
    # pytest 会在异步 fixture 与测试阶段切换 stderr，按当前捕获流重新装配输出。
    setup_logging(get_settings())
    capsys.readouterr()
    with fail_next_commit(auth_db, "users" if endpoint == "register" else "revoked_tokens"):
        failed = await client.post(f"/api/auth/{endpoint}", **kwargs)
    output = capsys.readouterr().err

    assert failed.status_code == 500
    assert failed.json() == {"code": "INTERNAL_ERROR", "message": "服务暂时不可用", "errors": []}
    assert "--- Logging error" not in output, output[output.rfind("Traceback"):][-1800:]
    assert "private-database-detail" not in output + failed.text
    assert "private-test-password" not in output + failed.text
    assert token not in output + failed.text
    records = [json.loads(line) for line in output.splitlines()]
    errors = [record for record in records if record["event"] == "http_request_failed"]
    assert len(errors) == 1
    assert errors[0]["level"] == "ERROR"
    assert errors[0]["error_type"] == "RuntimeError"
    assert errors[0]["request_id"] == failed.headers["X-Request-ID"]
    assert errors[0]["entry_point"] == "api"
    assert errors[0]["duration_ms"] >= 0
    assert "Traceback" in errors[0]["exception"]
    assert not any(record["event"] in ("auth_registered", "auth_logged_out") for record in records)
    if endpoint == "register":
        assert (await client.post("/api/auth/login", json=credentials)).status_code == 401
        assert (await client.post("/api/auth/register", json=credentials)).status_code == 201
    else:
        assert (await client.get("/api/auth/token", **kwargs)).status_code == 200
        assert (await client.post("/api/auth/logout", **kwargs)).status_code == 204


@pytest.mark.asyncio
async def test_concurrent_registration_has_one_winner_and_isolated_logs(
    client: AsyncClient, capsys: pytest.CaptureFixture[str],
) -> None:
    """并发注册同邮箱只成功一次，每个请求的日志关联标识独立。"""
    setup_logging(get_settings())
    capsys.readouterr()
    credentials = {"email": "racing@example.com", "password": "private-test-password"}
    responses = await asyncio.gather(*(
        client.post("/api/auth/register", json=credentials) for _ in range(2)
    ))
    records = [json.loads(line) for line in capsys.readouterr().err.splitlines()]

    assert sorted(response.status_code for response in responses) == [201, 409]
    ids = {response.headers["X-Request-ID"] for response in responses}
    assert len(ids) == 2
    completed = [record for record in records if record["event"] == "http_request_completed"]
    assert {record["request_id"] for record in completed} == ids
    for response in responses:
        request_records = [record for record in records if record["request_id"] == response.headers["X-Request-ID"]]
        assert all(record["entry_point"] == "api" for record in request_records)
        assert any(record.get("status_code") == response.status_code for record in request_records)
    assert "private-test-password" not in json.dumps(records)
    assert (await client.post("/api/auth/login", json=credentials)).status_code == 200


@pytest.mark.asyncio
async def test_concurrent_logout_does_not_turn_revocation_into_server_error(
    client: AsyncClient, logged_in_user: tuple[int, str],
) -> None:
    """并发退出同一令牌不会产生 500，最终令牌不可再使用。"""
    headers = {"Authorization": f"Bearer {logged_in_user[1]}"}
    responses = await asyncio.gather(*(
        client.post("/api/auth/logout", headers=headers) for _ in range(2)
    ))
    assert sorted(response.status_code for response in responses) == [204, 401]
    assert (await client.get("/api/auth/token", headers=headers)).status_code == 401


@pytest.mark.asyncio
async def test_concurrent_logout_primary_key_conflict_is_rejected_safely(
    client: AsyncClient, auth_db: AsyncEngine, logged_in_user: tuple[int, str],
) -> None:
    """认证之后才出现的撤销主键冲突统一映射为令牌无效。"""
    headers = {"Authorization": f"Bearer {logged_in_user[1]}"}
    error = MySQLIntegrityError(1062, "Duplicate entry 'test-jti' for key 'revoked_tokens.PRIMARY'")
    with fail_next_insert(auth_db, "revoked_tokens", error):
        failed = await client.post("/api/auth/logout", headers=headers)
    assert failed.status_code == 401
    assert failed.headers["WWW-Authenticate"] == "Bearer"
    assert failed.json()["code"] == "INVALID_TOKEN"
    assert (await client.post("/api/auth/logout", headers=headers)).status_code == 204


@pytest.mark.asyncio
async def test_other_integrity_error_is_not_misreported_as_duplicate_email(
    client: AsyncClient, auth_db: AsyncEngine, capsys: pytest.CaptureFixture[str],
) -> None:
    """数据库的其他约束错误保留为内部错误，不误报邮箱冲突。"""
    setup_logging(get_settings())
    error = MySQLIntegrityError(1062, "Duplicate entry 'uk_users_email' for key 'users.PRIMARY'")
    credentials = {"email": "user@example.com", "password": "private-test-password"}
    with fail_next_insert(auth_db, "users", error):
        response = await client.post("/api/auth/register", json=credentials)
    assert response.status_code == 500
    assert response.json()["code"] == "INTERNAL_ERROR"
    assert (await client.post("/api/auth/register", json=credentials)).status_code == 201


@pytest.mark.asyncio
async def test_deleted_user_cannot_use_previously_valid_token(
    client: AsyncClient, auth_db: AsyncEngine, logged_in_user: tuple[int, str],
) -> None:
    """令牌签名仍有效时，已不存在的用户也不能访问受保护接口。"""
    user_id, token = logged_in_user
    async with auth_db.begin() as connection:
        await connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
    response = await client.get("/api/auth/token", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_unreadable_stored_password_hash_has_safe_login_error(
    client: AsyncClient, auth_db: AsyncEngine, logged_in_user: tuple[int, str],
) -> None:
    """密码哈希损坏时使用固定认证失败响应，不回显哈希或库异常。"""
    async with auth_db.begin() as connection:
        await connection.execute(
            text("UPDATE users SET password_hash = :hash WHERE id = :id"),
            {"hash": "$argon2id$" + "invalid-private-hash" * 4, "id": logged_in_user[0]},
        )
    response = await client.post(
        "/api/auth/login", json={"email": "user@example.com", "password": "private-test-password"},
    )
    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_CREDENTIALS"
    assert "invalid-private-hash" not in response.text
