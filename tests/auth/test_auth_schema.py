"""认证请求边界与响应公开字段的验证。"""

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.auth_schema import (
    ErrorResponse,
    FieldError,
    LoginRequest,
    LoginResponse,
    RegisterRequest,
    TokenValidationResponse,
    UserResponse,
)


@pytest.mark.parametrize("request_model", [RegisterRequest, LoginRequest])
@pytest.mark.parametrize("password", [" secret ", "x" * 6, "x" * 128])
def test_credentials_normalize_email_and_preserve_password(request_model, password):
    """注册、登录共用邮箱规范化，且完整保留密码及长度边界。"""
    request = request_model.model_validate_json(
        json.dumps({"email": " User@EXAMPLE.COM ", "password": password})
    )

    assert request.email == "user@example.com"
    assert request.password.get_secret_value() == password
    assert password not in repr(request)
    assert json.loads(request.model_dump_json())["password"] == "**********"


@pytest.mark.parametrize("request_model", [RegisterRequest, LoginRequest])
@pytest.mark.parametrize(
    "overrides",
    [
        {"email": "not-an-email"},
        {"email": "a" * 64 + "@" + ".".join(["b" * 63] * 3) + ".com"},
        {"password": "x" * 5},
        {"password": "x" * 129},
        {"password": None},
        {"user_id": 1},
    ],
)
def test_credentials_reject_invalid_input(request_model, overrides):
    """拒绝非法邮箱、密码以及客户端额外提交的身份字段。"""
    payload = {"email": "user@example.com", "password": "example-password"}
    payload.update(overrides)

    with pytest.raises(ValidationError) as exc_info:
        request_model.model_validate_json(json.dumps(payload))

    assert "example-password" not in str(exc_info.value)


@pytest.mark.parametrize("request_model", [RegisterRequest, LoginRequest])
def test_credentials_accept_email_at_length_limit(request_model):
    """符合格式且总长度为254个字符的邮箱可用于注册和登录。"""
    email = "a" * 64 + "@" + ".".join(["b" * 63, "b" * 63, "b" * 61])
    request = request_model(email=email, password="example-password")

    assert len(request.email) == 254
    assert request.email == email


def test_user_response_excludes_password_hash_and_serializes_time():
    """用户公开响应不包含密码哈希，并输出带时区的时间。"""
    response = UserResponse.model_validate(
        {
            "id": 1,
            "email": "user@example.com",
            "created_at": datetime(2026, 10, 7, tzinfo=UTC),
            "password_hash": "private-password-hash",
        }
    )

    assert json.loads(response.model_dump_json()) == {
        "id": 1,
        "email": "user@example.com",
        "created_at": "2026-10-07T00:00:00Z",
    }


@pytest.mark.parametrize(
    ("response_model", "payload"),
    [
        (UserResponse, {"id": 1, "email": "user@example.com"}),
        (TokenValidationResponse, {"user_id": 1}),
    ],
)
def test_responses_require_timezone(response_model, payload):
    """拒绝没有时区的注册时间和令牌到期时间。"""
    field = "created_at" if response_model is UserResponse else "expires_at"
    payload = {**payload, field: datetime(2026, 10, 7)}

    with pytest.raises(ValidationError):
        response_model.model_validate(payload)


def test_login_response_contract():
    """登录凭证采用 Bearer 类型，并返回有效时长。"""
    response = LoginResponse(access_token="signed-token", expires_in=1800)

    assert json.loads(response.model_dump_json()) == {
        "access_token": "signed-token",
        "token_type": "bearer",
        "expires_in": 1800,
    }


@pytest.mark.parametrize(
    "overrides",
    [{"access_token": ""}, {"expires_in": 0}, {"token_type": "basic"}],
)
def test_login_response_rejects_invalid_credentials(overrides):
    """拒绝空令牌、非正有效时长和错误令牌类型。"""
    payload = {"access_token": "signed-token", "expires_in": 1800}
    payload.update(overrides)

    with pytest.raises(ValidationError):
        LoginResponse.model_validate(payload)


def test_token_validation_response_serializes_expiry():
    """令牌校验响应包含用户标识和带时区的到期时间。"""
    response = TokenValidationResponse(
        user_id=1,
        expires_at=datetime(2026, 10, 7, 0, 30, tzinfo=UTC),
    )

    assert json.loads(response.model_dump_json()) == {
        "user_id": 1,
        "expires_at": "2026-10-07T00:30:00Z",
    }


def test_error_response_supports_field_errors():
    """校验失败可包含字段错误，普通业务错误默认没有字段错误。"""
    response = ErrorResponse(
        code="VALIDATION_ERROR",
        message="请求参数不合法",
        errors=[FieldError(field="password", message="密码长度不合法")],
    )

    assert json.loads(response.model_dump_json())["errors"] == [
        {"field": "password", "message": "密码长度不合法"}
    ]
    assert ErrorResponse(code="INVALID_TOKEN", message="令牌无效").errors == []
