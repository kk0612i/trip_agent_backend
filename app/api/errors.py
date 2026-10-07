"""在 API 边界将业务异常映射为统一响应。"""

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger

from app.core.errors import BusinessError
from app.schemas.auth_schema import ErrorResponse, FieldError

_BUSINESS_STATUS_CODES = {
    "EMAIL_ALREADY_EXISTS": 409, "INVALID_CREDENTIALS": 401, "INVALID_TOKEN": 401,
}
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
    """返回固定业务提示，并记录不包含凭证的拒绝事件。"""
    status_code = _BUSINESS_STATUS_CODES[exc.code]
    logger.bind(
        event="auth_rejected", status="rejected", error_type=type(exc).__name__,
    ).warning("认证操作未通过业务校验")
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
        field = next((name for name in location if name in ("email", "password")), "body")
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
