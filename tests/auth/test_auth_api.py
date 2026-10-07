"""通过 HTTP 接口验证账户与认证行为。"""

from datetime import UTC, datetime

import jwt
import pytest
from httpx import AsyncClient

from tests.auth.auth_data import TEST_JWT_SECRET, make_token


@pytest.mark.asyncio
async def test_register_returns_public_user_with_utc_time(client: AsyncClient) -> None:
    """注册成功返回公开用户信息，密码按 Argon2id 存储。"""
    response = await client.post(
        "/api/auth/register",
        json={"email": " User@EXAMPLE.COM ", "password": " secret "},
    )

    assert response.status_code == 201
    payload = response.json()
    assert set(payload) == {"id", "email", "created_at"}
    assert payload["id"] > 0
    assert payload["email"] == "user@example.com"
    assert datetime.fromisoformat(payload["created_at"]).utcoffset() == UTC.utcoffset(None)
    assert "secret" not in response.text


@pytest.mark.asyncio
async def test_duplicate_email_returns_conflict(client: AsyncClient) -> None:
    """邮箱规范化后重复注册返回统一冲突错误。"""
    payload = {"email": "User@example.com", "password": "password-a"}
    assert (await client.post("/api/auth/register", json=payload)).status_code == 201
    payload["email"] = "USER@EXAMPLE.COM"

    response = await client.post("/api/auth/register", json=payload)

    assert response.status_code == 409
    assert response.json() == {
        "code": "EMAIL_ALREADY_EXISTS", "message": "邮箱已注册", "errors": [],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["register", "login"])
@pytest.mark.parametrize("payload", [
    {"email": "invalid-secret-email", "password": "private-secret-password"},
    {"email": "user@example.com", "password": "private-secret-" * 20},
    {"email": "user@example.com", "password": {"value": "private-secret-password"}},
    {"email": "user@example.com", "password": "private-secret-password", "user_id": 1},
    {"email": "user@example.com", "password": "private-secret-password", "private-secret-key": 1},
    {"email": "user@example.com"},
])
async def test_invalid_credentials_input_never_echoes_secrets(
    client: AsyncClient, payload: dict, endpoint: str,
) -> None:
    """422 只保留安全字段说明，不回显原始值或异常上下文。"""
    response = await client.post(f"/api/auth/{endpoint}", json=payload)

    assert response.status_code == 422
    result = response.json()
    assert set(result) == {"code", "message", "errors"}
    assert result["code"] == "VALIDATION_ERROR"
    assert result["errors"]
    assert all(set(error) == {"field", "message"} for error in result["errors"])
    assert "private-secret" not in response.text
    assert "invalid-secret" not in response.text
    if endpoint == "login":
        assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
async def test_registered_user_can_login_with_signed_token(client: AsyncClient) -> None:
    """注册用户可使用原样密码登录，取得不可缓存的签名令牌。"""
    credentials = {"email": "User@example.com", "password": " secret "}
    registered = await client.post("/api/auth/register", json=credentials)
    credentials["email"] = "USER@EXAMPLE.COM"

    response = await client.post("/api/auth/login", json=credentials)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.json()
    assert set(payload) == {"access_token", "token_type", "expires_in"}
    assert payload["token_type"] == "bearer"
    assert payload["expires_in"] == 1800
    claims = jwt.decode(
        payload["access_token"], TEST_JWT_SECRET, algorithms=["HS256"],
        issuer="trip-agents", audience="trip-agents-api",
    )
    assert claims["sub"] == str(registered.json()["id"])
    assert claims["exp"] - claims["iat"] == 1800
    assert claims["jti"]
    assert "password" not in claims


@pytest.mark.asyncio
@pytest.mark.parametrize("email", ["absent@example.com", "user@example.com"])
async def test_unknown_email_and_wrong_password_have_same_error(
    client: AsyncClient, email: str,
) -> None:
    """不存在的邮箱和错误密码都返回相同的 401 及 Bearer 挑战头。"""
    assert (await client.post(
        "/api/auth/register", json={"email": "user@example.com", "password": " secret "},
    )).status_code == 201

    response = await client.post(
        "/api/auth/login", json={"email": email, "password": "secret"},
    )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {
        "code": "INVALID_CREDENTIALS", "message": "邮箱或密码错误", "errors": [],
    }


@pytest.mark.asyncio
async def test_valid_token_returns_authenticated_identity(
    client: AsyncClient, logged_in_user: tuple[int, str],
) -> None:
    """令牌校验返回签名身份和 UTC 到期时间。"""
    user_id, token = logged_in_user

    response = await client.get("/api/auth/token", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"user_id", "expires_at"}
    assert payload["user_id"] == user_id
    expires_at = datetime.fromisoformat(payload["expires_at"])
    assert expires_at.utcoffset() == UTC.utcoffset(None)
    assert 1790 < (expires_at - datetime.now(UTC)).total_seconds() <= 1800


@pytest.mark.asyncio
async def test_logout_immediately_revokes_only_the_presented_token(
    client: AsyncClient, logged_in_user: tuple[int, str],
) -> None:
    """退出返回空响应，旧令牌立即失效，其他登录令牌继续有效。"""
    _, token = logged_in_user
    other_login = await client.post(
        "/api/auth/login",
        json={"email": "user@example.com", "password": "private-test-password"},
    )
    other_token = other_login.json()["access_token"]
    assert other_token != token
    headers = {"Authorization": f"Bearer {token}"}

    response = await client.post("/api/auth/logout", headers=headers)

    assert response.status_code == 204
    assert response.content == b""
    for method, path in [("GET", "/api/auth/token"), ("POST", "/api/auth/logout")]:
        rejected = await client.request(method, path, headers=headers)
        assert rejected.status_code == 401
        assert rejected.headers["WWW-Authenticate"] == "Bearer"
        assert rejected.json()["code"] == "INVALID_TOKEN"
    assert (await client.get(
        "/api/auth/token", headers={"Authorization": f"Bearer {other_token}"},
    )).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["token", "logout"])
@pytest.mark.parametrize("authorization", [None, "Basic abc", "Bearer", "Bearer forged-secret-token"])
async def test_protected_endpoints_reject_missing_or_malformed_bearer(
    client: AsyncClient, endpoint: str, authorization: str | None,
) -> None:
    """所有受保护接口对缺失和异常认证头返回统一 401。"""
    response = await client.request(
        "GET" if endpoint == "token" else "POST", f"/api/auth/{endpoint}",
        headers={"Authorization": authorization} if authorization is not None else {},
    )
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["code"] == "INVALID_TOKEN"
    assert "forged-secret" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["token", "logout"])
@pytest.mark.parametrize("overrides", [
    {"iss": "other-issuer"}, {"aud": "other-audience"}, {"aud": ["trip-agents-api"]},
    {"iat": 253402300799}, {"iat": -1}, {"iat": "1"}, {"iat": 1.0}, {"iat": False},
    {"exp": 1}, {"exp": 10**200}, {"exp": "253402300799"}, {"exp": 253402300799.0},
    {"exp": True}, {"exp": None},
    {"sub": ""}, {"sub": "0"}, {"sub": "01"}, {"sub": "-1"}, {"sub": 1},
    {"sub": "18446744073709551616"}, {"sub": "١"}, {"sub": "999999"},
    {"jti": ""}, {"jti": "x" * 65}, {"jti": 1},
])
async def test_protected_endpoints_reject_invalid_signed_claims(
    client: AsyncClient, logged_in_user: tuple[int, str], endpoint: str, overrides: dict,
) -> None:
    """即使签名正确，也拒绝不匹配、过期、类型异常或无法对应用户的声明。"""
    token = make_token(logged_in_user[0], overrides=overrides)
    response = await client.request(
        "GET" if endpoint == "token" else "POST", f"/api/auth/{endpoint}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["code"] == "INVALID_TOKEN"
    assert token not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_claim", ["sub", "jti", "iss", "aud", "iat", "exp"])
async def test_required_token_claim_cannot_be_omitted(
    client: AsyncClient, logged_in_user: tuple[int, str], missing_claim: str,
) -> None:
    """所有规定声明必须存在，不能依靠 JWT 库的可选校验隐式放行。"""
    token = make_token(logged_in_user[0], missing_claim=missing_claim)
    response = await client.get("/api/auth/token", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.asyncio
@pytest.mark.parametrize(("algorithm", "secret"), [
    ("HS256", "another-test-key-with-at-least-32-bytes"),
    ("HS384", TEST_JWT_SECRET), ("HS512", TEST_JWT_SECRET),
    ("none", None),
])
async def test_untrusted_signature_or_algorithm_is_rejected(
    client: AsyncClient, logged_in_user: tuple[int, str], algorithm: str, secret: str | None,
) -> None:
    """拒绝其他密钥、客户端指定的其他算法和无签名令牌。"""
    token = make_token(logged_in_user[0], algorithm=algorithm, secret=secret)
    response = await client.get("/api/auth/token", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["register", "login"])
@pytest.mark.parametrize("body", [b'{"password":"private-json-secret",', b'["private-json-secret"]'])
async def test_malformed_or_wrong_shape_json_has_safe_validation_response(
    client: AsyncClient, endpoint: str, body: bytes,
) -> None:
    """损坏的 JSON 及错误的顶层结构也统一返回不含原始输入的 422。"""
    response = await client.post(
        f"/api/auth/{endpoint}", content=body, headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert "private-json-secret" not in response.text
