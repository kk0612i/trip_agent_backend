"""高德与博查真实接口集成测试，需要 .env 中的真实密钥，并显式开启。

运行：``RUN_TRIP_INTEGRATION=1 uv run --no-sync pytest -q tests/integration/trip/test_external_apis.py``
"""

import os
from collections.abc import AsyncIterator
from datetime import date, timedelta

import httpx
import pytest
import pytest_asyncio

from app.core.config import get_settings
from app.tools.amap import AmapClient
from app.tools.bocha import BochaClient

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_TRIP_INTEGRATION") != "1",
    reason="需显式开启高德、博查真实接口验证，会消耗接口配额",
)


@pytest_asyncio.fixture
async def amap_client() -> AsyncIterator[AmapClient]:
    """使用真实配置创建高德客户端，测试结束关闭连接。"""
    settings = get_settings()
    if not settings.amap_api_key.get_secret_value():
        pytest.skip("未配置 AMAP_API_KEY")
    async with httpx.AsyncClient(timeout=settings.amap_timeout_seconds) as http_client:
        yield AmapClient(
            http_client,
            api_key=settings.amap_api_key.get_secret_value(),
            base_url=str(settings.amap_base_url),
            # 个人 key 的 QPS 较低，串行请求并保留默认退避，避免触发限流。
            max_concurrency=1,
            cache_ttl_seconds=0,
        )


@pytest_asyncio.fixture
async def bocha_client() -> AsyncIterator[BochaClient]:
    """使用真实配置创建博查客户端，测试结束关闭连接。"""
    settings = get_settings()
    if not settings.bocha_api_key.get_secret_value():
        pytest.skip("未配置 BOCHA_API_KEY")
    async with httpx.AsyncClient(timeout=settings.bocha_timeout_seconds) as http_client:
        yield BochaClient(
            http_client,
            api_key=settings.bocha_api_key.get_secret_value(),
            base_url=str(settings.bocha_base_url),
        )


@pytest.mark.asyncio
async def test_resolve_city(amap_client: AmapClient) -> None:
    """杭州解析为地级市 330100，北京解析为直辖市 110000，区县不返回。"""
    hangzhou = await amap_client.resolve_city("杭州")
    assert [city.adcode for city in hangzhou] == ["330100"]
    assert 119 < hangzhou[0].lng < 121 and 29 < hangzhou[0].lat < 31

    hangzhou_full = await amap_client.resolve_city("杭州市")
    assert [city.adcode for city in hangzhou_full] == ["330100"]

    beijing = await amap_client.resolve_city("北京")
    assert [(city.adcode, city.name) for city in beijing] == [("110000", "北京市")]

    assert await amap_client.resolve_city("西湖区") == []


@pytest.mark.asyncio
async def test_search_hot_spots(amap_client: AmapClient) -> None:
    """杭州热门景点返回坐标、分类和评分，城市编码均为 330100。"""
    pois = await amap_client.search_pois("330100", "热门景点", types="110000")
    assert len(pois) >= 10
    assert all(poi.city_adcode == "330100" for poi in pois)
    assert all(poi.typecode and poi.amap_type for poi in pois)
    assert all(118 < poi.lng < 121 and 29 < poi.lat < 31 for poi in pois)
    rated = [poi for poi in pois if poi.rating is not None]
    assert rated, "热门景点应至少有一条带评分"
    assert all(0 <= poi.rating <= 5 for poi in rated)


@pytest.mark.asyncio
async def test_find_west_lake_and_detail(amap_client: AmapClient) -> None:
    """按名称能找到西湖风景名胜区，详情接口返回同一 POI。"""
    pois = await amap_client.find_pois_by_name("330100", "西湖", limit=5)
    assert 0 < len(pois) <= 5
    scenic = next((poi for poi in pois if "西湖风景名胜区" in poi.name), None)
    assert scenic is not None
    assert scenic.typecode.startswith("11")

    detail = await amap_client.poi_detail(scenic.poi_id)
    assert detail is not None
    assert detail.poi_id == scenic.poi_id
    assert detail.city_adcode == "330100"


@pytest.mark.asyncio
async def test_weather_forecast(amap_client: AmapClient) -> None:
    """杭州天气预报返回连续多天，含当天前后的日期，降雨等级在合法范围。"""
    days = await amap_client.weather_forecast("330100")
    assert 3 <= len(days) <= 7
    assert all(day.day_weather for day in days)
    assert all(0 <= day.rain_level <= 3 for day in days)
    dates = [day.date for day in days]
    assert dates == sorted(dates)
    # 预报从当天（北京时间）开始，允许本机时区差一天。
    assert abs((dates[0] - date.today()).days) <= 1
    assert all(b - a == timedelta(days=1) for a, b in zip(dates, dates[1:]))


@pytest.mark.asyncio
async def test_bocha_search(bocha_client: BochaClient) -> None:
    """博查搜索返回带标题、地址和摘要的结果。"""
    results = await bocha_client.web_search("西湖 游玩时长", count=5)
    assert 0 < len(results) <= 5
    assert all(result.url.startswith("http") for result in results)
    assert all(result.title for result in results)
    assert any(result.snippet for result in results)
