"""博查 Web Search 客户端，用于查询景点游玩时长等补充资料。

接口字段已按真实返回核对：成功时 HTTP 200 且 JSON ``code`` 为整数 200，结果位于
``data.webPages.value``，每项含 ``name``、``url``、``snippet``、``summary``（请求
``summary=true`` 时返回）、``siteName``、``datePublished``（ISO 8601 字符串）。
失败时返回对应 HTTP 状态码，JSON 形如 ``{"code": "401", "message": "Invalid API KEY"}``。
"""

import asyncio
import time
from typing import Any

import httpx
from loguru import logger

from app.schemas.trip_plan_schema import WebSearchResult

# 单次搜索允许的返回条数范围（博查文档：1 至 50）。
_MAX_COUNT = 50


class BochaError(Exception):
    """博查接口调用失败，消息不含密钥和查询原文。

    Attributes:
        reason: 失败原因，如接口返回的 message 或异常类型名。
        status_code: HTTP 状态码；未收到响应时为空。
        retryable: 是否属于可重试的暂时性错误。
    """

    def __init__(self, *, reason: str, status_code: int | None = None, retryable: bool = False) -> None:
        """记录失败信息并生成异常消息。

        Args:
            reason: 失败原因。
            status_code: HTTP 状态码，可空。
            retryable: 是否可重试。
        """
        self.reason = reason
        self.status_code = status_code
        self.retryable = retryable
        status = f" http_status={status_code}" if status_code is not None else ""
        super().__init__(f"博查请求失败:{status} reason={reason}")


def _text(value: Any) -> str:
    """将字段转为去除首尾空白的字符串，非字符串视为空串。"""
    return value.strip() if isinstance(value, str) else ""


def _parse_result(raw: Any) -> WebSearchResult | None:
    """将单条网页结果转为 ``WebSearchResult``，缺少地址时返回空。"""
    if not isinstance(raw, dict):
        return None
    url = _text(raw.get("url"))
    if not url:
        return None
    return WebSearchResult(
        title=_text(raw.get("name")),
        url=url,
        # 摘要优先使用博查生成的 summary，缺失时退回网页片段 snippet。
        snippet=_text(raw.get("summary")) or _text(raw.get("snippet")),
        site_name=_text(raw.get("siteName")),
        published_at=_text(raw.get("datePublished")) or None,
    )


class BochaClient:
    """博查 Web Search 异步客户端，可在进程内并发复用。

    ``http_client`` 由应用生命周期创建和关闭，本类不创建或关闭连接。请求超时由
    外部客户端的 ``timeout`` 统一设置（应使用配置 ``bocha_timeout_seconds``）。

    重试策略：网络超时、连接错误、HTTP 5xx 和 429 视为暂时性错误，最多尝试
    ``max_attempts`` 次（默认 3 次，即最多 2 次退避重试），第 n 次失败后等待
    ``retry_backoff_seconds * 2**(n-1)`` 秒；其他失败直接抛出 ``BochaError``。
    搜索为只读查询，重试不涉及幂等问题。
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        api_key: str,
        base_url: str,
        max_attempts: int = 3,
        retry_backoff_seconds: float = 0.5,
    ) -> None:
        """保存依赖和重试参数，不执行任何外部 I/O。

        Args:
            http_client: 外部管理生命周期、已设置超时的异步 HTTP 客户端。
            api_key: 博查 API 密钥。
            base_url: 接口根地址，如 ``https://api.bochaai.com/v1``。
            max_attempts: 暂时性错误下的最大尝试次数（含首次）。
            retry_backoff_seconds: 首次重试前的等待秒数，之后按 2 倍递增；测试可设为 0。

        Raises:
            ValueError: 尝试次数小于 1 或退避秒数为负。
        """
        if max_attempts < 1:
            raise ValueError("尝试次数必须大于 0")
        if retry_backoff_seconds < 0:
            raise ValueError("退避秒数不能为负")
        self._http_client = http_client
        self._api_key = api_key
        self._url = f"{base_url.rstrip('/')}/web-search"
        self._max_attempts = max_attempts
        self._retry_backoff_seconds = retry_backoff_seconds

    async def web_search(self, query: str, *, count: int = 5, freshness: str = "noLimit") -> list[WebSearchResult]:
        """搜索网页并返回带摘要的结果。

        Args:
            query: 搜索词。
            count: 返回条数，1 至 50。
            freshness: 时间范围，如 ``noLimit``、``oneYear``、``oneMonth``、``oneWeek``、``oneDay``。

        Returns:
            网页结果列表，缺少地址的条目被丢弃；无结果时为空列表。

        Raises:
            ValueError: 搜索词为空或条数越界。
            BochaError: 接口调用失败或重试耗尽。
        """
        query = query.strip()
        if not query:
            raise ValueError("搜索词不能为空")
        if not 1 <= count <= _MAX_COUNT:
            raise ValueError("count 须在 1 至 50 之间")
        payload = {"query": query, "summary": True, "count": count, "freshness": freshness}
        headers = {"Authorization": f"Bearer {self._api_key}"}

        for attempt in range(1, self._max_attempts + 1):
            started = time.perf_counter()
            try:
                response = await self._http_client.post(self._url, json=payload, headers=headers)
                data = self._check_response(response)
            except BochaError as exc:
                error = exc
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                error = BochaError(reason=type(exc).__name__, retryable=True)
                error.__cause__ = exc
            except httpx.HTTPError as exc:
                error = BochaError(reason=type(exc).__name__)
                error.__cause__ = exc
            else:
                results = self._parse_results(data)
                logger.bind(
                    event="bocha_request", status="ok", attempt=attempt, duration_ms=_elapsed_ms(started),
                ).info(f"博查搜索成功 query_len={len(query)} results={len(results)}")
                return results

            duration_ms = _elapsed_ms(started)
            error_type = f"http_{error.status_code}" if error.status_code is not None else error.reason
            if error.retryable and attempt < self._max_attempts:
                logger.bind(
                    event="bocha_request", status="retry", attempt=attempt,
                    duration_ms=duration_ms, error_type=error_type,
                ).warning(f"博查搜索暂时失败，准备重试 query_len={len(query)}")
                await asyncio.sleep(self._retry_backoff_seconds * 2 ** (attempt - 1))
                continue
            logger.bind(
                event="bocha_request", status="error", attempt=attempt,
                duration_ms=duration_ms, error_type=error_type,
            ).error(f"博查搜索失败 query_len={len(query)}")
            raise error
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _check_response(response: httpx.Response) -> dict[str, Any]:
        """校验 HTTP 状态和业务 code，返回成功的 JSON。

        Raises:
            BochaError: HTTP 错误、响应不是 JSON 对象或业务 code 不为 200。
        """
        status_code = response.status_code
        if status_code >= 500 or status_code == 429:
            raise BochaError(reason="服务端繁忙或限流", status_code=status_code, retryable=True)
        try:
            data = response.json()
        except ValueError as exc:
            if status_code != 200:
                raise BochaError(reason="HTTP 状态异常", status_code=status_code) from exc
            raise BochaError(reason="响应不是有效 JSON", status_code=status_code) from exc
        if not isinstance(data, dict):
            raise BochaError(reason="响应结构异常", status_code=status_code)
        if status_code != 200 or str(data.get("code")) != "200":
            # 错误响应的 message 为固定说明（如 Invalid API KEY），不含密钥。
            reason = _text(data.get("message")) or _text(data.get("msg")) or "未知错误"
            raise BochaError(reason=reason, status_code=status_code)
        return data

    @staticmethod
    def _parse_results(data: dict[str, Any]) -> list[WebSearchResult]:
        """解析 ``data.webPages.value``，各层缺失时返回空列表。"""
        body = data.get("data")
        pages = body.get("webPages") if isinstance(body, dict) else None
        values = pages.get("value") if isinstance(pages, dict) else None
        if not isinstance(values, list):
            return []
        return [result for raw in values if (result := _parse_result(raw)) is not None]


def _elapsed_ms(started: float) -> int:
    """返回从 ``started`` 起经过的毫秒数。"""
    return int((time.perf_counter() - started) * 1000)
