"""请求级数据库会话与认证依赖装配。"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidToken
from app.repository.revoked_token_repository import RevokedTokenRepository
from app.repository.trip_plan_version_repository import TripPlanVersionRepository
from app.repository.trip_repository import TripRepository
from app.repository.trip_turn_repository import TripTurnRepository
from app.repository.user_repository import UserRepository
from app.schemas.auth_schema import AuthenticatedUser
from app.services.auth_service import AuthService
from app.services.trip_chat_service import TripChatService
from app.services.trip_service import TripService


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


def get_trip_service(session: SessionDep) -> TripService:
    """注入本请求的会话及行程相关 Repository。"""
    return TripService(
        session=session,
        trip_repo=TripRepository(session),
        trip_plan_version_repo=TripPlanVersionRepository(session),
        trip_turn_repo=TripTurnRepository(session),
    )


TripServiceDep = Annotated[TripService, Depends(get_trip_service)]


def get_trip_chat_service(request: Request) -> TripChatService:
    """注入会话工厂和图执行器；一轮对话跨多个事务，不复用请求会话。"""
    settings = request.app.state.settings
    return TripChatService(
        request.app.state.session_factory,
        request.app.state.turn_runner,
        lock_seconds=settings.trip_turn_lock_seconds,
        timeout_seconds=settings.agent_timeout_seconds,
    )


TripChatServiceDep = Annotated[TripChatService, Depends(get_trip_chat_service)]
