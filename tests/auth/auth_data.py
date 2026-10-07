"""认证测试的合成签名材料与令牌构造器。"""

from datetime import UTC, datetime
from typing import Any

import jwt

TEST_JWT_SECRET = "test-auth-signing-key-with-at-least-64-bytes-for-algorithm-checks-1234"


def make_token(
    user_id: int,
    *,
    overrides: dict[str, Any] | None = None,
    missing_claim: str | None = None,
    secret: str | None = TEST_JWT_SECRET,
    algorithm: str = "HS256",
) -> str:
    """用独立 JWT 库生成有效或故意异常的外部输入。"""
    now = int(datetime.now(UTC).timestamp())
    claims = {
        "sub": str(user_id), "jti": "external-test-token",
        "iss": "trip-agents", "aud": "trip-agents-api", "iat": now, "exp": now + 1800,
    }
    claims.update(overrides or {})
    if missing_claim is not None:
        del claims[missing_claim]
    return jwt.encode(claims, secret, algorithm=algorithm)
