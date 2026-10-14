"""高德客户端的解析、缓存、重试和错误处理测试，使用 MockTransport 不访问网络。"""

from collections.abc import Callable
from datetime import date
from typing import Any

import httpx
import pytest
from loguru import logger

from app.tools.amap import AmapClient, AmapError, city_adcode_of, rain_level_of

TEST_KEY = "amap-test-key-should-not-leak"
BASE_URL = "https://amap.test/v3"

Handler = Callable[[httpx.Request], httpx.Response]


def make_client(handler: Handler, **options: Any) -> tuple[AmapClient, httpx.AsyncClient]:
    """创建使用 MockTransport 的客户端，默认关闭退避等待。"""
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5)
    options.setdefault("retry_backoff_seconds", 0)
    client = AmapClient(http_client, api_key=TEST_KEY, base_url=BASE_URL, **options)
    return client, http_client


def ok(**body: Any) -> httpx.Response:
    """构造高德成功响应。"""
    return httpx.Response(200, json={"status": "1", "info": "OK", "infocode": "10000", **body})


def fail(infocode: str, info: str) -> httpx.Response:
    """构造高德业务失败响应。"""
    return httpx.Response(200, json={"status": "0", "info": info, "infocode": infocode})


def raw_poi(**overrides: Any) -> dict[str, Any]:
    """构造与真实返回格式一致的 POI，空值字段为空列表。"""
    poi = {
        "id": "B023B02842",
        "name": "灵隐寺",
        "location": "120.101406,30.240826",
        "adcode": "330106",
        "address": "法云弄1号",
        "type": "风景名胜;风景名胜;国家级景点",
        "typecode": "110202",
        "parent": "B023B05MJE",
        "biz_ext": {"cost": [], "rating": "4.8", "open_time": "06:30-18:00"},
    }
    poi.update(overrides)
    return poi


class TestCityAdcodeOf:
    """区县编码到城市编码的推算。"""

    @pytest.mark.parametrize(
        ("adcode", "expected"),
        [
            ("330106", "330100"),
            ("330100", "330100"),
            ("110105", "110000"),
            ("120101", "120000"),
            ("310115", "310000"),
            ("500235", "500000"),
            ("441900", "441900"),
            ("460400", "460400"),
            ("429021", "429021"),
            ("469001", "469001"),
        ],
    )
    def test_maps_district_to_city(self, adcode: str, expected: str) -> None:
        """普通区县取前四位，直辖市取前两位，省直辖县级单位原样返回。"""
        assert city_adcode_of(adcode) == expected

    @pytest.mark.parametrize("adcode", ["", "33010", "3301060", "abcdef"])
    def test_rejects_invalid(self, adcode: str) -> None:
        """非 6 位数字编码抛出 ValueError。"""
        with pytest.raises(ValueError):
            city_adcode_of(adcode)


@pytest.mark.parametrize(
    ("text", "level"),
    [
        ("晴", 0), ("多云", 0), ("阴", 0), ("小雪", 0), ("暴雪", 0), ("雾", 0), ("", 0),
        ("小雨", 1), ("阵雨", 1), ("雷阵雨", 1), ("毛毛雨/细雨", 1), ("雨夹雪", 1), ("冻雨", 1), ("雨", 1),
        ("雷阵雨并伴有冰雹", 1),
        ("中雨", 2), ("小到中雨", 2), ("小雨-中雨", 2), ("强阵雨", 2), ("强雷阵雨", 2),
        ("大雨", 3), ("暴雨", 3), ("大暴雨", 3), ("特大暴雨", 3), ("中到大雨", 3), ("中雨-大雨", 3),
        ("大雨-暴雨", 3), ("极端降雨", 3),
    ],
)
def test_rain_level_of(text: str, level: int) -> None:
    """各类天气描述映射到正确的降雨等级，组合描述取较高等级。"""
    assert rain_level_of(text) == level


@pytest.mark.asyncio
async def test_resolve_city_keeps_city_level_only() -> None:
    """只返回地级市，区县结果不返回，请求参数正确且带 key。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok(districts=[
            {"name": "杭州市", "adcode": "330100", "level": "city", "center": "120.209903,30.246566"},
            {"name": "西湖区", "adcode": "330106", "level": "district", "center": "120.130396,30.259242"},
            {"name": "浙江省", "adcode": "330000", "level": "province", "center": "120.15,30.26"},
        ])

    client, http_client = make_client(handler)
    async with http_client:
        cities = await client.resolve_city(" 杭州市 ")

    assert [(c.adcode, c.name, c.lng, c.lat) for c in cities] == [("330100", "杭州市", 120.209903, 30.246566)]
    request = seen[0]
    assert request.url.path == "/v3/config/district"
    assert request.url.params["keywords"] == "杭州市"
    assert request.url.params["subdistrict"] == "0"
    assert request.url.params["key"] == TEST_KEY


@pytest.mark.asyncio
async def test_resolve_city_municipality_and_district_only() -> None:
    """直辖市取 province 级条目并过滤“北京城区”；只有区县结果时返回空列表。"""
    responses = {
        "北京": [
            {"name": "北京市", "adcode": "110000", "level": "province", "center": "116.407387,39.904179"},
            {"name": "北京城区", "adcode": "110100", "level": "city", "center": "116.405285,39.904989"},
        ],
        "西湖区": [
            {"name": "西湖区", "adcode": "330106", "level": "district", "center": "120.130396,30.259242"},
            {"name": "西湖区", "adcode": "360103", "level": "district", "center": "115.876998,28.657005"},
        ],
        "不存在": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return ok(districts=responses[request.url.params["keywords"]])

    client, http_client = make_client(handler)
    async with http_client:
        beijing = await client.resolve_city("北京")
        district = await client.resolve_city("西湖区")
        missing = await client.resolve_city("不存在")
        blank = await client.resolve_city("  ")

    assert [(c.adcode, c.name) for c in beijing] == [("110000", "北京市")]
    assert district == []
    assert missing == []
    assert blank == []


@pytest.mark.asyncio
async def test_search_pois_parses_fields_and_drops_invalid() -> None:
    """正常解析 POI 字段，评分缺失为空，坐标无效的 POI 被丢弃。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok(count="5", pois=[
            raw_poi(),
            raw_poi(id="B1", name="无评分列表", parent=[], biz_ext={"cost": [], "rating": []}),
            raw_poi(id="B2", name="空评分", biz_ext={"rating": ""}, address=[]),
            raw_poi(id="B3", name="无扩展", biz_ext=[], adcode="110105"),
            raw_poi(id="B4", name="坐标空", location=[]),
            raw_poi(id="B5", name="坐标坏", location="abc,30.1"),
            raw_poi(id="B6", name="坐标越界", location="200.0,30.1"),
        ])

    client, http_client = make_client(handler)
    async with http_client:
        pois = await client.search_pois("330100", "热门景点", types="110000", page=2, page_size=10)

    params = seen[0].url.params
    assert seen[0].url.path == "/v3/place/text"
    assert params["city"] == "330100"
    assert params["citylimit"] == "true"
    assert params["extensions"] == "all"
    assert params["types"] == "110000"
    assert params["keywords"] == "热门景点"
    assert params["page"] == "2"
    assert params["offset"] == "10"

    assert [poi.poi_id for poi in pois] == ["B023B02842", "B1", "B2", "B3"]
    first = pois[0]
    assert first.name == "灵隐寺"
    assert (first.lng, first.lat) == (120.101406, 30.240826)
    assert first.rating == 4.8
    assert first.parent_id == "B023B05MJE"
    assert first.adcode == "330106"
    assert first.city_adcode == "330100"
    assert first.typecode == "110202"
    assert first.amap_type == "风景名胜;风景名胜;国家级景点"
    assert first.address == "法云弄1号"
    assert [poi.rating for poi in pois[1:]] == [None, None, None]
    assert pois[1].parent_id is None
    assert pois[2].address == ""
    assert pois[3].city_adcode == "110000"


@pytest.mark.asyncio
async def test_search_pois_validates_paging() -> None:
    """分页参数越界时不发请求，直接抛 ValueError。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("不应发起请求")

    client, http_client = make_client(handler)
    async with http_client:
        with pytest.raises(ValueError):
            await client.search_pois("330100", "景点", page_size=26)
        with pytest.raises(ValueError):
            await client.search_pois("330100", "景点", page=0)
        with pytest.raises(ValueError):
            await client.search_pois("330100", " ")


@pytest.mark.asyncio
async def test_find_pois_by_name_without_types() -> None:
    """按名称查找不带分类限制，条数由 limit 控制。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok(pois=[raw_poi(id="B023B13L9M", name="杭州西湖风景名胜区")])

    client, http_client = make_client(handler)
    async with http_client:
        pois = await client.find_pois_by_name("330100", "西湖", limit=3)

    assert [poi.name for poi in pois] == ["杭州西湖风景名胜区"]
    assert "types" not in seen[0].url.params
    assert seen[0].url.params["offset"] == "3"
    assert seen[0].url.params["keywords"] == "西湖"


@pytest.mark.asyncio
async def test_weather_forecast_uses_day_weather() -> None:
    """解析 4 天预报，降雨等级按白天天气计算。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/weather/weatherInfo"
        assert request.url.params["extensions"] == "all"
        return ok(forecasts=[{"city": "杭州市", "adcode": "330100", "casts": [
            {"date": "2026-10-14", "dayweather": "多云", "nightweather": "小雨"},
            {"date": "2026-10-15", "dayweather": "中到大雨", "nightweather": "阴"},
            {"date": "2026-10-16", "dayweather": "雷阵雨", "nightweather": "晴"},
            {"date": "坏日期", "dayweather": "晴", "nightweather": "晴"},
        ]}])

    client, http_client = make_client(handler)
    async with http_client:
        days = await client.weather_forecast("330100")

    assert [(d.date, d.day_weather, d.night_weather, d.rain_level) for d in days] == [
        (date(2026, 10, 14), "多云", "小雨", 0),
        (date(2026, 10, 15), "中到大雨", "阴", 3),
        (date(2026, 10, 16), "雷阵雨", "晴", 1),
    ]


@pytest.mark.asyncio
async def test_weather_forecast_empty() -> None:
    """无预报数据时返回空列表。"""
    client, http_client = make_client(lambda request: ok(forecasts=[]))
    async with http_client:
        assert await client.weather_forecast("330100") == []


@pytest.mark.asyncio
async def test_poi_detail_found_and_missing() -> None:
    """详情存在时返回 POI，不存在时返回空。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/place/detail"
        if request.url.params["id"] == "B023B02842":
            return ok(count="1", pois=[raw_poi()])
        return ok(count="0", pois=[])

    client, http_client = make_client(handler)
    async with http_client:
        found = await client.poi_detail("B023B02842")
        missing = await client.poi_detail("B000000000")

    assert found is not None and found.rating == 4.8
    assert missing is None


@pytest.mark.asyncio
async def test_cache_hit_skips_request() -> None:
    """相同路径和参数的第二次查询命中缓存，不同参数仍会请求。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return ok(pois=[raw_poi()])

    client, http_client = make_client(handler)
    async with http_client:
        first = await client.search_pois("330100", "景点", types="110000")
        second = await client.search_pois("330100", "景点", types="110000")
        assert calls == 1
        assert first == second
        await client.search_pois("330100", "景点", types="110000", page=2)
        assert calls == 2


@pytest.mark.asyncio
async def test_cache_disabled_when_ttl_zero() -> None:
    """ttl 为 0 时每次都请求。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return ok(pois=[])

    client, http_client = make_client(handler, cache_ttl_seconds=0)
    async with http_client:
        await client.search_pois("330100", "景点")
        await client.search_pois("330100", "景点")
    assert calls == 2


@pytest.mark.asyncio
async def test_qps_limit_retries_then_succeeds() -> None:
    """QPS 超限后重试成功，成功结果写入缓存，失败不写缓存。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return fail("10021", "CUQPS_HAS_EXCEEDED_THE_LIMIT")
        return ok(pois=[raw_poi()])

    client, http_client = make_client(handler)
    async with http_client:
        pois = await client.search_pois("330100", "景点")
        await client.search_pois("330100", "景点")

    assert [poi.poi_id for poi in pois] == ["B023B02842"]
    assert calls == 3


@pytest.mark.asyncio
async def test_timeout_retries_then_succeeds() -> None:
    """网络超时属于暂时性错误，重试后成功。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return ok(districts=[])

    client, http_client = make_client(handler)
    async with http_client:
        assert await client.resolve_city("杭州") == []
    assert calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("infocode", "info"), [("10001", "INVALID_USER_KEY"), ("10003", "DAILY_QUERY_OVER_LIMIT")])
async def test_non_retryable_error_raises_immediately(infocode: str, info: str) -> None:
    """key 无效、日配额超限等错误不重试，异常消息不含密钥。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return fail(infocode, info)

    client, http_client = make_client(handler)
    async with http_client:
        with pytest.raises(AmapError) as exc_info:
            await client.resolve_city("杭州")
        # 失败结果不缓存，再次调用仍会请求。
        with pytest.raises(AmapError):
            await client.resolve_city("杭州")

    assert calls == 2
    error = exc_info.value
    assert error.infocode == infocode
    assert error.reason == info
    assert error.retryable is False
    assert TEST_KEY not in str(error)


@pytest.mark.asyncio
async def test_server_error_exhausts_retries() -> None:
    """HTTP 5xx 重试耗尽后抛出 AmapError。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="busy")

    client, http_client = make_client(handler, max_attempts=3)
    async with http_client:
        with pytest.raises(AmapError) as exc_info:
            await client.weather_forecast("330100")

    assert calls == 3
    assert exc_info.value.status_code == 503
    assert exc_info.value.retryable is True
    assert TEST_KEY not in str(exc_info.value)


@pytest.mark.asyncio
async def test_connect_error_exhausts_retries() -> None:
    """连接错误重试耗尽后转为 AmapError，消息不含请求地址中的密钥。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError(f"cannot connect {request.url}", request=request)

    client, http_client = make_client(handler, max_attempts=2)
    async with http_client:
        with pytest.raises(AmapError) as exc_info:
            await client.poi_detail("B023B02842")

    assert calls == 2
    assert exc_info.value.reason == "ConnectError"
    assert TEST_KEY not in str(exc_info.value)


@pytest.mark.asyncio
async def test_http_client_error_not_retried() -> None:
    """HTTP 4xx 不重试。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403, text="forbidden")

    client, http_client = make_client(handler)
    async with http_client:
        with pytest.raises(AmapError) as exc_info:
            await client.resolve_city("杭州")
    assert calls == 1
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_request_logs_without_key() -> None:
    """日志记录事件、状态和尝试次数，不包含密钥和响应内容。"""
    records: list[dict[str, Any]] = []
    sink_id = logger.add(lambda message: records.append(message.record), level="DEBUG")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return fail("10021", "CUQPS_HAS_EXCEEDED_THE_LIMIT")
        return ok(pois=[raw_poi()])

    client, http_client = make_client(handler)
    try:
        async with http_client:
            await client.search_pois("330100", "景点")
    finally:
        logger.remove(sink_id)

    amap_records = [r for r in records if r["extra"].get("event") == "amap_request"]
    assert [(r["extra"]["status"], r["extra"]["attempt"]) for r in amap_records] == [("retry", 1), ("ok", 2)]
    assert amap_records[0]["level"].name == "WARNING"
    assert amap_records[0]["extra"]["error_type"] == "10021"
    assert all("duration_ms" in r["extra"] for r in amap_records)
    for record in amap_records:
        text = record["message"] + repr(record["extra"])
        assert TEST_KEY not in text
        assert "灵隐寺" not in text


@pytest.mark.asyncio
async def test_search_pois_parses_photos() -> None:
    """图片只保留有效地址，http 升级为 https，空列表标题转为空串，最多 5 张。"""
    photos = [
        {"provider": [], "title": [], "url": "http://store.is.autonavi.com/showpic/a"},
        {"provider": [], "title": "夏季", "url": "https://store.is.autonavi.com/showpic/b"},
        {"provider": [], "title": "无地址", "url": []},
        {"provider": [], "title": "坏地址", "url": "ftp://example.com/c"},
        "不是对象",
        *({"title": f"第{i}张", "url": f"http://img.test/{i}"} for i in range(6)),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return ok(count="3", pois=[
            raw_poi(photos=photos),
            raw_poi(id="B1", name="无图片", photos=[]),
            raw_poi(id="B2", name="图片字段异常", photos="oops"),
        ])

    client, http_client = make_client(handler)
    async with http_client:
        pois = await client.search_pois("330100", "热门景点", types="110000")

    first = pois[0]
    assert [(p.url, p.title) for p in first.photos] == [
        ("https://store.is.autonavi.com/showpic/a", ""),
        ("https://store.is.autonavi.com/showpic/b", "夏季"),
        ("https://img.test/0", "第0张"),
        ("https://img.test/1", "第1张"),
        ("https://img.test/2", "第2张"),
    ]
    assert pois[1].photos == []
    assert pois[2].photos == []
