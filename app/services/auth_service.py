"""账户认证业务和事务边界。"""

import re
from datetime import UTC, datetime
from time import perf_counter

from anyio import to_thread
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import EmailAlreadyExists, InvalidCredentials, InvalidToken
from app.core.security import TokenCodec
from app.repository.revoked_token_repository import RevokedTokenRepository
from app.repository.user_repository import UserRepository
from app.schemas.auth_schema import (
    AuthenticatedUser, LoginRequest, LoginResponse, RegisterRequest, UserResponse,
)


def _is_unique_conflict(
    exc: IntegrityError, *, mysql_key: str, sqlite_columns: str,
) -> bool:
    """只识别目标唯一键冲突，避免将其他完整性错误误判为业务冲突。"""
    arguments = getattr(exc.orig, "args", ())
    if len(arguments) >= 2 and arguments[0] == 1062:
        key_match = re.search(r"for key ['`]([^'`]+)['`]\s*$", str(arguments[1]))
        return key_match is not None and key_match.group(1).rsplit(".", 1)[-1] == mysql_key
    # SQLite 替身使用相同唯一性契约，但错误消息以列名标识冲突。
    return arguments == (f"UNIQUE constraint failed: {sqlite_columns}",)


class AuthService:
    """协调账户业务，每个请求使用独立会话和 Repository。"""

    def __init__(
        self,
        session: AsyncSession,
        user_repo: UserRepository,
        revoked_token_repo: RevokedTokenRepository,
        password_hasher: PasswordHasher,
        token_codec: TokenCodec,
        dummy_password_hash: str,
    ) -> None:
        """接收本次工作单元的依赖，不创建外部连接。"""
        self.session = session
        self.user_repo = user_repo
        self.revoked_token_repo = revoked_token_repo
        self.password_hasher = password_hasher
        self.token_codec = token_codec
        self.dummy_password_hash = dummy_password_hash

    async def register(self, request: RegisterRequest) -> UserResponse:
        """保存密码哈希，并在事务提交成功后返回公开账户信息。"""
        started_at = perf_counter()
        password_hash = await to_thread.run_sync(
            self.password_hasher.hash, request.password.get_secret_value(),
        )
        try:
            async with self.session.begin():
                if await self.user_repo.get_by_email(str(request.email)) is not None:
                    raise EmailAlreadyExists
                user = await self.user_repo.add(
                    str(request.email), password_hash,
                    datetime.now(UTC).replace(tzinfo=None, microsecond=0),
                )
                response = UserResponse(
                    id=user.id, email=user.email,
                    created_at=user.created_at.replace(tzinfo=UTC),
                )
        except IntegrityError as exc:
            if _is_unique_conflict(exc, mysql_key="uk_users_email", sqlite_columns="users.email"):
                raise EmailAlreadyExists from None
            raise
        logger.bind(
            event="auth_registered", status="success",
            duration_ms=round((perf_counter() - started_at) * 1000, 2),
        ).info("账户注册完成")
        return response

    async def authenticate(self, token: str) -> AuthenticatedUser:
        """从验证后的 sub 获取身份，每次认证都查询用户和撤销状态。"""
        started_at = perf_counter()
        claims = self.token_codec.decode(token)
        async with self.session.begin():
            user = await self.user_repo.get_by_id(int(claims.sub))
            revoked = await self.revoked_token_repo.get_by_jti(claims.jti)
            if user is None or revoked is not None:
                raise InvalidToken
        identity = AuthenticatedUser(
            user_id=user.id, jti=claims.jti,
            expires_at=datetime.fromtimestamp(claims.exp, UTC),
        )
        logger.bind(
            event="auth_token_validated", status="success",
            duration_ms=round((perf_counter() - started_at) * 1000, 2),
        ).info("访问令牌校验完成")
        return identity

    async def logout(self, current_user: AuthenticatedUser) -> None:
        """提交当前令牌的撤销状态，提交失败时不报告退出成功。"""
        started_at = perf_counter()
        try:
            async with self.session.begin():
                await self.revoked_token_repo.add(
                    current_user.jti,
                    current_user.expires_at.astimezone(UTC).replace(tzinfo=None),
                )
        except IntegrityError as exc:
            if _is_unique_conflict(exc, mysql_key="PRIMARY", sqlite_columns="revoked_tokens.jti"):
                raise InvalidToken from None
            raise
        logger.bind(
            event="auth_logged_out", status="success",
            duration_ms=round((perf_counter() - started_at) * 1000, 2),
        ).info("账户退出完成")

    async def login(self, request: LoginRequest) -> LoginResponse:
        """验证账户密码后签发令牌，密码计算不占用数据库事务。"""
        started_at = perf_counter()
        async with self.session.begin():
            user = await self.user_repo.get_by_email(str(request.email))
        password_hash = user.password_hash if user is not None else self.dummy_password_hash
        try:
            await to_thread.run_sync(
                self.password_hasher.verify, password_hash, request.password.get_secret_value(),
            )
        except VerifyMismatchError:
            raise InvalidCredentials from None
        except (InvalidHashError, VerificationError) as exc:
            logger.bind(
                event="auth_password_hash_invalid", status="rejected", error_type=type(exc).__name__,
            ).warning("账户密码哈希无法验证")
            raise InvalidCredentials from None
        if user is None:
            raise InvalidCredentials
        response = LoginResponse(
            access_token=self.token_codec.issue(user.id),
            expires_in=self.token_codec.ttl_seconds,
        )
        logger.bind(
            event="auth_logged_in", status="success",
            duration_ms=round((perf_counter() - started_at) * 1000, 2),
        ).info("账户登录完成")
        return response
