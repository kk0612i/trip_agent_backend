"""在 API 边界将业务异常映射为统一响应。"""

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger

from app.core.errors import BusinessError
from app.schemas.auth_schema import ErrorResponse, FieldError

_BUSINESS_STATUS_CODES = {
    "EMAIL_ALREADY_EXISTS": 409, "INVALID_CREDENTIALS": 401, "INVALID_TOKEN": 401,
    "TRIP_NOT_FOUND": 404, "TRIP_BUSY": 409, "DRAFT_VERSION_CONFLICT": 409,
    "TRIP_NOT_CONFIRMABLE": 409, "TRIP_TURN_FAILED": 503,
}
# 认证以外的业务错误使用各自的日志事件名，认证错误保持 auth_rejected。
_BUSINESS_EVENTS = {
    "TRIP_NOT_FOUND": "trip_rejected", "TRIP_BUSY": "trip_rejected",
    "DRAFT_VERSION_CONFLICT": "trip_rejected", "TRIP_NOT_CONFIRMABLE": "trip_rejected",
    "TRIP_TURN_FAILED": "trip_unavailable",
}
# 只有这些固定字段名可以出现在校验错误响应中。
_SAFE_FIELD_NAMES = ("email", "password", "turn_id", "content", "draft_version", "trip_id")
_VALIDATION_MESSAGES = {
    "missing": "缺少必填字段",
    "extra_forbidden": "包含不允许的字段",
    "string_too_short": "字段长度过短",
    "string_too_long": "字段长度过长",
    "too_short": "字段长度过短",
    "too_long": "字段长度过长",
    "json_invalid": "请求体不是合法的 JSON",
}


async def business_error_handler(request: Request, exc: BusinessError) -> JSONResponse:
    """返回固定业务提示，并记录不包含凭证和用户输入的拒绝事件。"""
    status_code = _BUSINESS_STATUS_CODES[exc.code]
    logger.bind(
        event=_BUSINESS_EVENTS.get(exc.code, "auth_rejected"),
        status="failed" if status_code >= 500 else "rejected",
        error_type=type(exc).__name__,
    ).warning("业务操作未通过校验" if status_code < 500 else "业务依赖暂时不可用")
    payload = ErrorResponse(code=exc.code, message=exc.message)
    return JSONResponse(
        status_code=status_code, content=payload.model_dump(mode="json"),
        headers={"WWW-Authenticate": "Bearer"} if status_code == 401 else None,
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """仅投影固定字段路径和安全说明，移除 input、ctx 和原始异常消息。"""
    errors = []
    for error in exc.errors():
        location = error.get("loc", ())
        # 额外字段的名字也来自原始输入，不能直接写入响应。
        field = next((name for name in location if name in _SAFE_FIELD_NAMES), "body")
        errors.append(FieldError(
            field=field,
            message=_VALIDATION_MESSAGES.get(error["type"], "字段值不合法"),
        ))
    logger.bind(event="request_validation_failed", status="rejected").warning("请求参数校验失败")
    payload = ErrorResponse(code="VALIDATION_ERROR", message="请求参数不合法", errors=errors)
    return JSONResponse(status_code=422, content=payload.model_dump(mode="json"))


def unexpected_error_response(
    request: Request, exc: Exception, *, duration_ms: float,
) -> JSONResponse:
    """在唯一的意外异常边界记录安全堆栈，并隐藏数据库及内部错误详情。"""
    logger.bind(
        event="http_request_failed", status="failed", duration_ms=duration_ms,
        error_type=type(exc).__name__, method=request.method,
        route=getattr(request.scope.get("route"), "path", "unmatched"),
        status_code=500, sanitize_exception=True,
    ).opt(exception=exc).error("HTTP 请求处理失败")
    payload = ErrorResponse(code="INTERNAL_ERROR", message="服务暂时不可用")
    return JSONResponse(status_code=500, content=payload.model_dump(mode="json"))
