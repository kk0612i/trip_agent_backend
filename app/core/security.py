"""使用成熟密码及令牌库提供认证基础能力。"""

from datetime import UTC, datetime
from uuid import uuid4

import jwt

from app.core.config import Settings
from app.core.errors import InvalidToken
from app.schemas.auth_schema import AccessTokenClaims


class TokenCodec:
    """使用服务端固定算法和配置签发访问令牌。"""

    algorithm = "HS256"

    def __init__(self, settings: Settings) -> None:
        """接收签名配置，拒绝空密钥和长度不足的密钥。"""
        secret = settings.auth_jwt_secret.get_secret_value()
        if len(secret.encode("utf-8")) < 32:
            raise ValueError("AUTH_JWT_SECRET 必须配置为至少 32 字节的随机密钥")
        self._secret = secret
        self._issuer = settings.auth_jwt_issuer
        self._audience = settings.auth_jwt_audience
        self.ttl_seconds = settings.auth_token_ttl_seconds

    def issue(self, user_id: int) -> str:
        """为已验证密码的用户签发具有独立 jti 的访问令牌。"""
        issued_at = int(datetime.now(UTC).timestamp())
        return jwt.encode(
            {
                "sub": str(user_id), "jti": uuid4().hex,
                "iss": self._issuer, "aud": self._audience,
                "iat": issued_at, "exp": issued_at + self.ttl_seconds,
            },
            self._secret, algorithm=self.algorithm,
        )

    def decode(self, token: str) -> AccessTokenClaims:
        """验证固定算法、签名、签发方、接收方、时间及必需声明。"""
        try:
            payload = jwt.decode(
                token, self._secret, algorithms=[self.algorithm],
                issuer=self._issuer, audience=self._audience,
                options={
                    "require": ["sub", "jti", "iss", "aud", "iat", "exp"],
                    "strict_aud": True,
                },
            )
            return AccessTokenClaims.model_validate(payload)
        except (jwt.InvalidTokenError, ValueError, OverflowError):
            raise InvalidToken from None
