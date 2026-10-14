"""认证接口的请求与响应契约。"""

from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)


class CredentialsRequest(BaseModel):
    """定义注册和登录共用的邮箱、密码凭证。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    email: EmailStr = Field(max_length=254, description="账户邮箱")
    password: SecretStr = Field(
        min_length=6,
        max_length=128,
        description="账户密码",
    )

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        """统一邮箱大小写，并检查规范化后的长度。

        Args:
            value: 已通过邮箱格式校验及基础规范化的邮箱。

        Returns:
            整体转为小写的账户邮箱。

        Raises:
            ValueError: 规范化后的邮箱超过长度限制。
        """
        normalized = value.lower()
        if len(normalized) > 254:
            raise ValueError("邮箱长度不能超过254个字符")
        return normalized


class RegisterRequest(CredentialsRequest):
    """定义注册请求。"""


class LoginRequest(CredentialsRequest):
    """定义登录请求。"""


class UserResponse(BaseModel):
    """返回用户公开信息。"""

    id: int = Field(gt=0, description="用户唯一标识")
    email: EmailStr = Field(max_length=254, description="账户邮箱")
    created_at: AwareDatetime = Field(description="注册时间")


class LoginResponse(BaseModel):
    """返回登录凭证及有效时长。"""

    access_token: str = Field(min_length=1, description="访问令牌")
    token_type: Literal["bearer"] = Field(default="bearer", description="令牌类型")
    expires_in: int = Field(gt=0, description="令牌签发时的有效秒数")


class TokenValidationResponse(BaseModel):
    """返回有效令牌对应的身份及到期时间。"""

    user_id: int = Field(gt=0, description="用户唯一标识")
    expires_at: AwareDatetime = Field(description="令牌到期时间")


class FieldError(BaseModel):
    """描述请求字段的校验错误。"""

    field: str = Field(description="错误字段路径")
    message: str = Field(description="字段错误说明")


class ErrorResponse(BaseModel):
    """定义认证及行程接口共用的统一错误响应。"""

    code: Literal[
        "EMAIL_ALREADY_EXISTS",
        "INVALID_CREDENTIALS",
        "INVALID_TOKEN",
        "TRIP_NOT_FOUND",
        "TRIP_BUSY",
        "DRAFT_VERSION_CONFLICT",
        "TRIP_NOT_CONFIRMABLE",
        "TRIP_TURN_FAILED",
        "VALIDATION_ERROR",
        "INTERNAL_ERROR",
    ] = Field(description="错误代码")
    message: str = Field(description="错误说明")
    errors: list[FieldError] = Field(
        default_factory=list,
        description="字段校验错误列表",
    )


class AccessTokenClaims(BaseModel):
    """校验签名验证后的声明类型及数据库可接受的取值范围。"""

    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    sub: str = Field(min_length=1, max_length=20, description="令牌对应的用户标识")
    jti: str = Field(min_length=1, max_length=64, description="令牌唯一标识")
    iss: str = Field(min_length=1, description="令牌签发方")
    aud: str = Field(min_length=1, description="令牌接收方")
    iat: int = Field(ge=0, le=253402300799, description="令牌签发时间戳")
    exp: int = Field(gt=0, le=253402300799, description="令牌到期时间戳")

    @field_validator("sub")
    @classmethod
    def validate_subject(cls, value: str) -> str:
        """仅接受 MySQL 无符号 BIGINT 范围内的规范正整数标识。"""
        if (
            not value.isascii() or not value.isdecimal() or value.startswith("0")
            or int(value) > 18446744073709551615
        ):
            raise ValueError("用户标识不合法")
        return value

    @model_validator(mode="after")
    def validate_time_order(self) -> "AccessTokenClaims":
        """要求令牌在签发之后到期。"""
        if self.exp <= self.iat:
            raise ValueError("令牌到期时间必须晚于签发时间")
        return self


class AuthenticatedUser(BaseModel):
    """表示通过签名、声明、撤销状态及用户存在性校验的身份。"""

    user_id: int = Field(gt=0, description="已认证用户标识")
    jti: str = Field(min_length=1, max_length=64, description="已认证令牌唯一标识")
    expires_at: AwareDatetime = Field(description="已认证令牌到期时间")
