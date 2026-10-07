"""集中配置 Loguru、标准 logging 衔接和安全日志输出。"""

import json
import logging
import sys
import traceback
from collections.abc import Callable
from typing import Any

from loguru import logger

from app.core.config import Settings

# 只输出明确允许的上下文字段，避免整个请求或工具结果进入日志。
_CONTEXT_FIELDS = (
    "event", "request_id", "run_id", "entry_point", "status", "duration_ms",
    "attempt", "error_type", "method", "route", "status_code", "node", "tool", "call_id",
)


class InterceptHandler(logging.Handler):
    """将标准 logging 记录转发到唯一的 Loguru 输出链路。"""

    def emit(self, record: logging.LogRecord) -> None:
        """保留原始级别、异常和业务字段并转发记录。"""
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame = logging.currentframe()
        depth = 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1

        fields = {name: getattr(record, name) for name in _CONTEXT_FIELDS if hasattr(record, name)}
        logger.bind(**fields).opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def _make_formatter(settings: Settings, *, structured: bool) -> Callable[[dict[str, Any]], str]:
    """创建输出格式器，在最终输出处脱敏消息、上下文和异常。"""
    secrets = sorted(
        {
            secret.get_secret_value()
            for secret in (
                settings.amap_api_key, settings.deepseek_api_key,
                settings.mysql_password, settings.auth_jwt_secret,
            )
            if secret.get_secret_value()
        },
        key=len,
        reverse=True,
    )

    def redact(value: Any) -> Any:
        """递归移除配置中已知的密钥及密码。"""
        if isinstance(value, dict):
            return {str(key): redact(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [redact(item) for item in value]
        if not isinstance(value, (str, int, float, bool, type(None))):
            value = str(value)
        if isinstance(value, str):
            for secret in secrets:
                value = value.replace(secret, "***")
        return value

    def format_record(record: dict[str, Any]) -> str:
        """将一条记录格式化为 JSON 或开发环境文本。"""
        extra = record["extra"]
        payload = {
            "timestamp": record["time"].isoformat(),
            "level": record["level"].name,
            "logger": record["name"],
            "process_id": record["process"].id,
            "message": record["message"],
            "event": extra.get("event", "log_message"),
            "request_id": extra.get("request_id", "-"),
            "run_id": extra.get("run_id", "-"),
            "entry_point": extra.get("entry_point", "-"),
        }
        payload.update({name: extra[name] for name in _CONTEXT_FIELDS if name in extra})
        exception = record["exception"]
        if exception:
            payload["error_type"] = exception.type.__name__
            if extra.get("sanitize_exception"):
                # API 异常可能含 SQL 参数或原始输入，仅记录调用位置与异常类型。
                frames = traceback.extract_tb(exception.traceback)
                locations = "".join(
                    f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}\n'
                    for frame in frames
                )
                payload["exception"] = (
                    "Traceback (most recent call last):\n" + locations
                    + f"{exception.type.__name__}: 请求处理失败\n"
                )
            else:
                # 不采集局部变量；普通异常仍由调用日志的明确边界负责记录。
                payload["exception"] = "".join(
                    traceback.format_exception(exception.type, exception.value, exception.traceback)
                )

        if structured:
            # 先脱敏再编码，包含换行、引号的密钥也能被替换，且不破坏 JSON。
            output = json.dumps(redact(payload), ensure_ascii=False)
        else:
            fields = " ".join(f"{name}={payload[name]}" for name in _CONTEXT_FIELDS if name in payload)
            output = redact(
                f"{record['time']:%Y-%m-%d %H:%M:%S.%f} | {payload['level']:<8} | "
                f"{payload['logger']} | {fields} | {payload['message']}"
            )
            if exception:
                output += "\n" + redact(payload["exception"])

        record["extra"]["_formatted"] = output
        return "{extra[_formatted]}\n"

    return format_record


def setup_logging(settings: Settings) -> None:
    """由应用或任务入口初始化日志，可重复调用而不产生重复输出。

    Args:
        settings: 日志和运行环境配置。
    """
    logger.remove()
    structured = settings.app_env == "production" or settings.log_format == "json"
    options = {
        "level": settings.log_level,
        "enqueue": settings.log_enqueue,
        "backtrace": False,
        "diagnose": False,
        "colorize": False,
    }
    logger.add(sys.stderr, format=_make_formatter(settings, structured=structured), **options)
    if settings.log_file_enabled:
        settings.log_directory.mkdir(parents=True, exist_ok=True)
        # 单 worker 使用文件创建日期命名，由 Loguru 在创建和轮转时填充日期。
        logger.add(
            settings.log_directory / "backend-{time:YYYY-MM-DD}.log",
            format=_make_formatter(settings, structured=True),
            rotation=settings.log_rotation,
            retention=settings.log_retention,
            encoding="utf-8",
            **options,
        )

    logging.basicConfig(
        handlers=[InterceptHandler()], level=logger.level(settings.log_level).no, force=True
    )
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        standard_logger = logging.getLogger(name)
        standard_logger.handlers.clear()
        standard_logger.propagate = True
        standard_logger.setLevel(logging.NOTSET)
