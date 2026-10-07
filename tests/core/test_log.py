"""实际日志输出、异常、文件轮转和上下文隔离验证。"""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

import pytest
from loguru import logger

from app.core.config import Settings
from app.core.log import setup_logging


def test_level_filter_and_repeated_setup(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    """验证初始化两次仍只输出一次，并按级别过滤。"""
    setup_logging(settings)
    setup_logging(settings)
    logger.debug("应被过滤")
    logger.bind(event="operation_completed", status="success").info("完成")
    output = capsys.readouterr().err.splitlines()
    assert len(output) == 1
    payload = json.loads(output[0])
    assert payload["event"] == "operation_completed"
    assert payload["level"] == "INFO"
    assert payload["status"] == "success"


def test_standard_logging_and_uvicorn(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    """验证 Uvicorn 和标准日志的级别、关联标识及结构化字段。"""
    setup_logging(settings)
    with logger.contextualize(request_id="request-1", entry_point="api"):
        logging.getLogger("uvicorn.error").warning(
            "重试第 %s 次", 1,
            extra={"event": "external_call_retry", "attempt": 1, "status": "retrying"},
        )
        logging.getLogger("tests.standard").info("请求完成")
    payloads = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert len(payloads) == 2
    assert payloads[0]["level"] == "WARNING"
    assert payloads[0]["attempt"] == 1
    assert payloads[0]["message"] == "重试第 1 次"
    assert all(item["request_id"] == "request-1" for item in payloads)
    assert all(item["entry_point"] == "api" for item in payloads)


def test_exception_redaction_and_field_allowlist(capsys: pytest.CaptureFixture[str]) -> None:
    """验证异常堆栈和消息中的密钥被脱敏，原始请求不会被输出。"""
    secret = 'secret-密码"\nkey'
    settings = Settings(_env_file=None, deepseek_api_key=secret, log_format="json", log_enqueue=False)
    setup_logging(settings)
    try:
        raise RuntimeError("调用失败：" + secret)
    except RuntimeError:
        logger.bind(
            event="external_call_failed", status="failed", request_id="request-error",
            request_body="不应输出的完整请求",
        ).exception("外部调用失败 {}", secret)
    payload = json.loads(capsys.readouterr().err)
    assert payload["level"] == "ERROR"
    assert payload["error_type"] == "RuntimeError"
    assert "Traceback" in payload["exception"]
    assert secret not in payload["exception"]
    assert secret not in payload["message"]
    assert "request_body" not in payload


@pytest.mark.asyncio
async def test_concurrent_context_isolation(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    """验证并发任务的关联标识不串用，离开上下文后恢复默认值。"""
    setup_logging(settings)

    async def operation(request_id: str) -> None:
        """交错输出模拟请求日志。"""
        with logger.contextualize(request_id=request_id, entry_point="api"):
            await asyncio.sleep(0)
            logger.bind(event="operation_completed").info(request_id)

    await asyncio.gather(operation("request-a"), operation("request-b"))
    logger.info("上下文外")
    payloads = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert {item["request_id"] for item in payloads[:2]} == {"request-a", "request-b"}
    assert all(item["request_id"] == item["message"] for item in payloads[:2])
    assert payloads[-1]["request_id"] == "-"


def test_file_rotation_and_queued_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """验证队列实际落盘、日期文件名和小阈值轮转。"""
    today = datetime.now().strftime("%Y-%m-%d")
    setup_logging(Settings(
        _env_file=None, log_file_enabled=True, log_dir=tmp_path,
        log_rotation="1 KB", log_enqueue=True,
    ))
    for index in range(15):
        logger.bind(event="rotation_test", attempt=index).info("文件写入 {}", "x" * 100)
    logger.complete()
    files = list(tmp_path.glob(f"backend-{today}*.log"))
    assert (tmp_path / f"backend-{today}.log").is_file()
    assert len(files) > 1
    payloads = [json.loads(line) for path in files for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(payloads) == 15
    assert {item["attempt"] for item in payloads} == set(range(15))
    assert len(capsys.readouterr().err.splitlines()) == 15


def test_production_always_json(capsys: pytest.CaptureFixture[str]) -> None:
    """验证生产环境即使选择文本格式，也保持结构化输出。"""
    setup_logging(Settings(_env_file=None, app_env="production", log_format="text", log_enqueue=False))
    logger.info("生产日志")
    assert json.loads(capsys.readouterr().err)["message"] == "生产日志"
