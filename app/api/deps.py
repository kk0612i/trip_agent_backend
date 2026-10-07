"""请求级数据库会话与认证依赖装配。"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidToken
from app.repository.revoked_token_repository import RevokedTokenRepository
from app.repository.user_repository import UserRepository
from app.schemas.auth_schema import AuthenticatedUser
from app.services.auth_service import AuthService


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """为单次请求创建会话，并在请求结束或失败时关闭。

    Yields:
        当前请求专用的异步数据库会话。
    """
    async with request.app.state.session_factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_auth_service(request: Request, session: SessionDep) -> AuthService:
    """注入共享密码哈希器及本请求的 Repository。"""
    return AuthService(
        session=session,
        user_repo=UserRepository(session),
        revoked_token_repo=RevokedTokenRepository(session),
        password_hasher=request.app.state.password_hasher,
        token_codec=request.app.state.token_codec,
        dummy_password_hash=request.app.state.dummy_password_hash,
    )


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]

bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    auth_service: AuthServiceDep,
) -> AuthenticatedUser:
    """统一校验 Bearer 令牌，身份只来自已验证的声明。"""
    if credentials is None:
        raise InvalidToken
    return await auth_service.authenticate(credentials.credentials)


CurrentUserDep = Annotated[AuthenticatedUser, Depends(get_current_user)]
