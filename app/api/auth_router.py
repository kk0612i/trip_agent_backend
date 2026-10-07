"""账户认证接口。"""

from fastapi import APIRouter, Response

from app.api.deps import AuthServiceDep, CurrentUserDep
from app.schemas.auth_schema import (
    ErrorResponse, LoginRequest, LoginResponse, RegisterRequest, TokenValidationResponse, UserResponse,
)

router = APIRouter(prefix="/api/auth", tags=["认证"])


@router.post("/register", status_code=201, responses={
    409: {"model": ErrorResponse}, 422: {"model": ErrorResponse},
})
async def register(request: RegisterRequest, auth_service: AuthServiceDep) -> UserResponse:
    """创建账户并返回公开用户信息。"""
    return await auth_service.register(request)


@router.post("/login", responses={401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}})
async def login(
    request: LoginRequest, auth_service: AuthServiceDep,
) -> LoginResponse:
    """验证邮箱和密码并返回不可缓存的登录凭证。"""
    return await auth_service.login(request)


@router.get("/token", responses={401: {"model": ErrorResponse}})
async def validate_token(current_user: CurrentUserDep) -> TokenValidationResponse:
    """返回通过完整认证的用户标识与到期时间。"""
    return TokenValidationResponse(
        user_id=current_user.user_id, expires_at=current_user.expires_at,
    )


@router.post("/logout", status_code=204, responses={401: {"model": ErrorResponse}})
async def logout(current_user: CurrentUserDep, auth_service: AuthServiceDep) -> Response:
    """撤销当前 Bearer 令牌，成功后返回空响应。"""
    await auth_service.logout(current_user)
    return Response(status_code=204)
