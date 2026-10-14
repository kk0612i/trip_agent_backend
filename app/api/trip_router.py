"""行程会话接口。"""

from typing import Annotated

from fastapi import APIRouter, Path

from app.api.deps import CurrentUserDep, TripChatServiceDep, TripServiceDep
from app.schemas.auth_schema import ErrorResponse
from app.schemas.trip_schema import (
    ConfirmRequest, ConfirmResponse, MessageRequest, MessageResponse, TripDetail, TripSummary, TurnView,
)

router = APIRouter(prefix="/api/trips", tags=["行程"])

# 路径中的行程标识，限制在 MySQL 无符号 BIGINT 可比较的正整数范围内。
TripIdPath = Annotated[int, Path(gt=0, le=9223372036854775807, description="行程标识")]

_AUTH_ERRORS = {401: {"model": ErrorResponse}}
_TRIP_ERRORS = {**_AUTH_ERRORS, 404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}}


@router.post("/", status_code=201, responses=_AUTH_ERRORS)
async def create_trip(current_user: CurrentUserDep, trip_service: TripServiceDep) -> TripDetail:
    """创建处于需求收集状态的新行程。"""
    return await trip_service.create_trip(current_user)


@router.get("/", responses=_AUTH_ERRORS)
async def list_trips(current_user: CurrentUserDep, trip_service: TripServiceDep) -> list[TripSummary]:
    """按创建倒序返回当前用户最近的行程。"""
    return await trip_service.list_trips(current_user)


@router.get("/{trip_id}", responses=_TRIP_ERRORS)
async def get_trip(
    trip_id: TripIdPath, current_user: CurrentUserDep, trip_service: TripServiceDep,
) -> TripDetail:
    """返回行程详情及当前草稿。"""
    return await trip_service.get_trip(current_user, trip_id)


@router.get("/{trip_id}/turns", responses=_TRIP_ERRORS)
async def list_turns(
    trip_id: TripIdPath, current_user: CurrentUserDep, trip_service: TripServiceDep,
) -> list[TurnView]:
    """按时间顺序返回行程的对话消息。"""
    return await trip_service.list_turns(current_user, trip_id)


@router.post("/{trip_id}/messages", responses={
    **_TRIP_ERRORS, 409: {"model": ErrorResponse}, 503: {"model": ErrorResponse},
})
async def send_message(
    trip_id: TripIdPath, request: MessageRequest,
    current_user: CurrentUserDep, trip_chat_service: TripChatServiceDep,
) -> MessageResponse:
    """发送一条用户消息并返回本轮回复；同一 turn_id 重试返回已保存的结果。"""
    return await trip_chat_service.handle_message(current_user, trip_id, request)


@router.post("/{trip_id}/confirm", responses={**_TRIP_ERRORS, 409: {"model": ErrorResponse}})
async def confirm_trip(
    trip_id: TripIdPath, request: ConfirmRequest,
    current_user: CurrentUserDep, trip_service: TripServiceDep,
) -> ConfirmResponse:
    """确认前端当前展示的草稿版本，重复确认同一版本时幂等成功。"""
    return await trip_service.confirm(current_user, trip_id, request)
