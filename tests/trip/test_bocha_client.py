"""博查客户端的解析、重试和错误处理测试，使用 MockTransport 不访问网络。"""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from loguru import logger

from app.tools.bocha import BochaClient, BochaError

TEST_KEY = "bocha-test-key-should-not-leak"
BASE_URL = "https://bocha.test/v1"

Handler = Callable[[httpx.Request], httpx.Response]


def make_client(handler: Handler, **options: Any) -> tuple[BochaClient, httpx.AsyncClient]:
    """创建使用 MockTransport 的客户端，默认关闭退避等待。"""
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5)
    options.setdefault("retry_backoff_seconds", 0)
    return BochaClient(http_client, api_key=TEST_KEY, base_url=BASE_URL, **options), http_client


def ok(values: list[dict[str, Any]] | None) -> httpx.Response:
    """构造与真实结构一致的成功响应。"""
    web_pages = None if values is None else {"totalEstimatedMatches": 100, "value": values}
    return httpx.Response(200, json={
        "code": 200, "log_id": "x", "msg": None,
        "data": {"_type": "SearchResponse", "webPages": web_pages, "images": None, "videos": None},
    })


@pytest.mark.asyncio
async def test_web_search_parses_results() -> None:
    """请求格式正确；summary 优先，否则用 snippet；缺少地址的条目丢弃。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok([
            {
                "name": "西湖一般玩几个小时", "url": "https://a.example/1", "snippet": "片段",
                "summary": "西湖至少需要一天", "siteName": "百度知道",
                "datePublished": "2021-07-02T09:04:16+08:00",
            },
            {"name": "只有片段", "url": "https://a.example/2", "snippet": "片段二", "summary": None,
             "siteName": None, "datePublished": None},
            {"name": "没有地址", "url": "", "snippet": "x"},
        ])

    client, http_client = make_client(handler)
    async with http_client:
        results = await client.web_search(" 西湖 游玩时长 ", count=3, freshness="oneYear")

    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "https://bocha.test/v1/web-search"
    assert request.headers["Authorization"] == f"Bearer {TEST_KEY}"
    assert json.loads(request.content) == {
        "query": "西湖 游玩时长", "summary": True, "count": 3, "freshness": "oneYear",
    }
    assert [(r.title, r.url, r.snippet, r.site_name, r.published_at) for r in results] == [
        ("西湖一般玩几个小时", "https://a.example/1", "西湖至少需要一天", "百度知道", "2021-07-02T09:04:16+08:00"),
        ("只有片段", "https://a.example/2", "片段二", "", None),
    ]


@pytest.mark.asyncio
async def test_web_search_without_web_pages() -> None:
    """webPages 为空时返回空列表。"""
    client, http_client = make_client(lambda request: ok(None))
    async with http_client:
        assert await client.web_search("西湖") == []


@pytest.mark.asyncio
async def test_web_search_validates_input() -> None:
    """空搜索词或条数越界时不发请求。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("不应发起请求")

    client, http_client = make_client(handler)
    async with http_client:
        with pytest.raises(ValueError):
            await client.web_search("  ")
        with pytest.raises(ValueError):
            await client.web_search("西湖", count=0)
        with pytest.raises(ValueError):
            await client.web_search("西湖", count=51)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["429", "503", "timeout"])
async def test_transient_error_retries_then_succeeds(failure: str) -> None:
    """429、5xx 和超时重试后成功。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure == "timeout":
                raise httpx.ConnectTimeout("timed out", request=request)
            return httpx.Response(int(failure), json={"code": failure, "message": "busy"})
        return ok([{"name": "t", "url": "https://a.example/1", "snippet": "s"}])

    client, http_client = make_client(handler)
    async with http_client:
        results = await client.web_search("西湖")
    assert calls == 2
    assert [r.url for r in results] == ["https://a.example/1"]


@pytest.mark.asyncio
async def test_server_error_exhausts_retries() -> None:
    """5xx 持续失败时共尝试 3 次（2 次退避重试）后抛错。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, text="error")

    client, http_client = make_client(handler)
    async with http_client:
        with pytest.raises(BochaError) as exc_info:
            await client.web_search("西湖")
    assert calls == 3
    assert exc_info.value.status_code == 500
    assert exc_info.value.retryable is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (401, {"code": "401", "message": "Invalid API KEY"}),
        (403, {"code": "403", "message": "You do not have enough money"}),
        (400, {"code": "400", "message": "Missing parameter query"}),
        (200, {"code": 500, "msg": "inner error", "data": None}),
    ],
)
async def test_other_errors_not_retried(status_code: int, body: dict[str, Any]) -> None:
    """密钥无效、余额不足、参数错误及业务 code 异常不重试，消息不含密钥和查询原文。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code, json=body)

    client, http_client = make_client(handler)
    async with http_client:
        with pytest.raises(BochaError) as exc_info:
            await client.web_search("西湖私密查询")
    assert calls == 1
    assert exc_info.value.retryable is False
    assert exc_info.value.reason == (body.get("message") or body.get("msg"))
    assert TEST_KEY not in str(exc_info.value)
    assert "西湖私密查询" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_logs_query_length_only() -> None:
    """日志记录查询长度和状态，不记录查询原文与密钥。"""
    records: list[dict[str, Any]] = []
    sink_id = logger.add(lambda message: records.append(message.record), level="DEBUG")
    client, http_client = make_client(lambda request: ok([]))
    try:
        async with http_client:
            await client.web_search("西湖私密查询")
    finally:
        logger.remove(sink_id)

    bocha_records = [r for r in records if r["extra"].get("event") == "bocha_request"]
    assert len(bocha_records) == 1
    record = bocha_records[0]
    assert record["extra"]["status"] == "ok"
    assert record["extra"]["attempt"] == 1
    assert "query_len=6" in record["message"]
    text = record["message"] + repr(record["extra"])
    assert "西湖私密查询" not in text
    assert TEST_KEY not in text
