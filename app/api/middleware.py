"""HTTP 请求关联标识与完成日志。"""

from collections.abc import Awaitable, Callable
from time import perf_counter
from uuid import uuid4

from fastapi import Request, Response
from loguru import logger

from app.api.errors import unexpected_error_response


async def log_request(
    request: Request, call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """为请求建立独立日志上下文，记录路由模板与耗时。"""
    started_at = perf_counter()
    request.state.request_id = uuid4().hex
    with logger.contextualize(request_id=request.state.request_id, entry_point="api"):
        try:
            response = await call_next(request)
        except Exception as exc:
            response = unexpected_error_response(
                request, exc, duration_ms=round((perf_counter() - started_at) * 1000, 2),
            )
        response.headers["X-Request-ID"] = request.state.request_id
        if request.url.path == "/api/auth/login":
            response.headers["Cache-Control"] = "no-store"
        if response.status_code >= 500:
            status = "failed"
        elif response.status_code >= 400:
            status = "rejected"
        else:
            status = "success"
        logger.bind(
            event="http_request_completed", method=request.method,
            route=getattr(request.scope.get("route"), "path", "unmatched"),
            status_code=response.status_code,
            status=status,
            duration_ms=round((perf_counter() - started_at) * 1000, 2),
        ).info("HTTP 请求处理完成")
        return response
