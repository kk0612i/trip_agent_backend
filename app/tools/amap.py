"""高德 Web 服务客户端：城市解析、景点搜索、景点详情和天气预报。

接口字段已按真实返回核对：
- 空值字段统一返回空列表 ``[]`` 而不是空串或 null，例如 ``parent``、``biz_ext.cost``。
- ``biz_ext.rating`` 为字符串，如 ``"4.8"``；缺失时为 ``[]``。
- 直辖市在行政区查询中 level 为 ``province``（如北京 110000），同时会返回
  level 为 ``city`` 的“北京城区”（110100），后者不是独立城市，需要过滤。
- 天气预报 ``extensions=all`` 返回包括当天在内共 4 天。
- QPS 超限返回 status=0、infocode=10021（CUQPS_HAS_EXCEEDED_THE_LIMIT）等。
"""

import asyncio
import math
import time
from datetime import date
from typing import Any

import httpx
from loguru import logger

from app.schemas.trip_plan_schema import AmapPoi, CityInfo, DayWeather, PoiPhoto

# 直辖市行政区划编码前两位：北京、天津、上海、重庆。
_MUNICIPALITY_PREFIXES = frozenset({"11", "12", "31", "50"})

# 可短暂等待后重试的 infocode（依据高德“错误码说明”）：
# 10014 云图服务 QPS 超限；10015 单机 QPS 限流；10016 服务器负载过高；
# 10019 服务总 QPS 超限；10020 Key 接口 QPS 超限；10021 账号接口 QPS 超限。
# 10004（1 分钟内访问量超限被封停至下一分钟）短退避无法恢复，10003/10044 日配额超限
# 当天无法恢复，均不重试。
_RETRYABLE_INFOCODES = frozenset({"10014", "10015", "10016", "10019", "10020", "10021"})

# 单页最大条数，高德建议 offset 不超过 25。
_MAX_PAGE_SIZE = 25
# 每个 POI 保留的图片数上限。
_MAX_PHOTOS = 5

# 进程内缓存条目上限，超过后先清理过期条目，仍超出则淘汰最早写入的条目。
_MAX_CACHE_ENTRIES = 2048

_CacheKey = tuple[str, tuple[tuple[str, str], ...]]


class AmapError(Exception):
    """高德接口调用失败，消息只包含接口路径、infocode 和原因，不含密钥。

    Attributes:
        path: 请求的接口路径，如 ``/place/text``。
        infocode: 高德返回的 infocode；网络错误或 HTTP 错误时为空。
        reason: 失败原因，如高德返回的 info 或异常类型名。
        status_code: HTTP 状态码；未收到响应时为空。
        retryable: 是否属于可重试的暂时性错误。
    """

    def __init__(
        self,
        path: str,
        *,
        reason: str,
        infocode: str | None = None,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        """记录失败信息并生成不含密钥的异常消息。

        Args:
            path: 请求的接口路径。
            reason: 失败原因。
            infocode: 高德 infocode，可空。
            status_code: HTTP 状态码，可空。
            retryable: 是否可重试。
        """
        self.path = path
        self.reason = reason
        self.infocode = infocode
        self.status_code = status_code
        self.retryable = retryable
        parts = [f"高德请求失败: path={path}"]
        if status_code is not None:
            parts.append(f"http_status={status_code}")
        if infocode is not None:
            parts.append(f"infocode={infocode}")
        parts.append(f"reason={reason}")
        super().__init__(" ".join(parts))


def city_adcode_of(adcode: str) -> str:
    """由区县编码推算所属城市编码。

    直辖市（11/12/31/50 开头）返回前两位加 ``0000``；省直辖县级行政区
    （第 3、4 位为 ``90``，如神农架林区 429021）本身即城市级单位，原样返回；
    其他返回前四位加 ``00``。

    Args:
        adcode: 6 位行政区划编码。

    Returns:
        6 位城市编码。

    Raises:
        ValueError: 编码不是 6 位数字。
    """
    if len(adcode) != 6 or not adcode.isdigit():
        raise ValueError("行政区划编码必须是 6 位数字")
    if adcode[:2] in _MUNICIPALITY_PREFIXES:
        return adcode[:2] + "0000"
    if adcode[2:4] == "90":
        return adcode
    return adcode[:4] + "00"


def rain_level_of(weather_text: str) -> int:
    """根据高德天气描述计算降雨等级，组合描述（如“中到大雨”“小雨-中雨”）取较高等级。

    Args:
        weather_text: 天气描述，如“多云”“雷阵雨”“中雨-大雨”。

    Returns:
        0 无雨；1 小雨、阵雨、雷阵雨、毛毛雨、雨夹雪、冻雨等；2 中雨、强阵雨；
        3 大雨、暴雨、大暴雨、特大暴雨、极端降雨。纯降雪不计入降雨。
    """
    text = weather_text.strip()
    if "雨" not in text:
        return 0
    # “暴雨”同时覆盖大暴雨、特大暴雨；“大雨”覆盖“中到大雨”“中雨-大雨”。
    if any(word in text for word in ("大雨", "暴雨", "极端降雨")):
        return 3
    if any(word in text for word in ("中雨", "强阵雨", "强雷阵雨")):
        return 2
    return 1


def _text(value: Any) -> str:
    """将高德字段转为字符串，空列表等非字符串空值统一为空串。"""
    return value.strip() if isinstance(value, str) else ""


def _parse_location(value: Any) -> tuple[float, float] | None:
    """解析 ``"经度,纬度"`` 字符串，无效坐标返回空。"""
    parts = _text(value).split(",")
    if len(parts) != 2:
        return None
    try:
        lng, lat = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if not (math.isfinite(lng) and math.isfinite(lat)):
        return None
    if not (-180 <= lng <= 180 and -90 <= lat <= 90) or (lng == 0 and lat == 0):
        return None
    return lng, lat


def _parse_rating(biz_ext: Any) -> float | None:
    """从 ``biz_ext`` 中解析评分，缺失、空串、空列表或越界时返回空。"""
    if not isinstance(biz_ext, dict):
        return None
    raw = _text(biz_ext.get("rating"))
    if not raw:
        return None
    try:
        rating = float(raw)
    except ValueError:
        return None
    return rating if 0 <= rating <= 5 else None


def _parse_photos(value: Any) -> list[PoiPhoto]:
    """解析 ``photos``：只保留有地址的图片，http 统一升级为 https，最多 5 张。

    高德在标题缺失时返回空列表而不是空串，这里统一转为空串。
    """
    if not isinstance(value, list):
        return []
    photos: list[PoiPhoto] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        url = _text(raw.get("url"))
        if not url.startswith(("http://", "https://")):
            continue
        if url.startswith("http://"):
            url = "https://" + url.removeprefix("http://")
        photos.append(PoiPhoto(url=url, title=_text(raw.get("title"))))
        if len(photos) >= _MAX_PHOTOS:
            break
    return photos


def _parse_poi(raw: Any) -> AmapPoi | None:
    """将单个高德 POI 转为 ``AmapPoi``，缺少标识、名称或坐标无效时返回空。"""
    if not isinstance(raw, dict):
        return None
    poi_id = _text(raw.get("id"))
    name = _text(raw.get("name"))
    location = _parse_location(raw.get("location"))
    if not poi_id or not name or location is None:
        return None
    adcode = _text(raw.get("adcode"))
    city_adcode = city_adcode_of(adcode) if len(adcode) == 6 and adcode.isdigit() else ""
    return AmapPoi(
        poi_id=poi_id,
        name=name,
        lng=location[0],
        lat=location[1],
        adcode=adcode,
        city_adcode=city_adcode,
        address=_text(raw.get("address")),
        amap_type=_text(raw.get("type")),
        typecode=_text(raw.get("typecode")),
        rating=_parse_rating(raw.get("biz_ext")),
        parent_id=_text(raw.get("parent")) or None,
        photos=_parse_photos(raw.get("photos")),
    )


def _parse_city(raw: Any) -> CityInfo | None:
    """将行政区查询结果转为城市，只保留地级市和直辖市，其他级别返回空。"""
    if not isinstance(raw, dict):
        return None
    adcode = _text(raw.get("adcode"))
    level = _text(raw.get("level"))
    name = _text(raw.get("name"))
    if len(adcode) != 6 or not adcode.isdigit() or not name:
        return None
    is_municipality = adcode[:2] in _MUNICIPALITY_PREFIXES
    if is_municipality:
        # 直辖市本身 level 为 province；“北京城区”等 level 为 city 的条目不是独立城市。
        if level != "province" or not adcode.endswith("0000"):
            return None
    elif level != "city":
        return None
    location = _parse_location(raw.get("center"))
    if location is None:
        return None
    return CityInfo(adcode=adcode, name=name, lng=location[0], lat=location[1])


def _parse_day_weather(raw: Any) -> DayWeather | None:
    """将单日预报转为 ``DayWeather``，日期无效时返回空。"""
    if not isinstance(raw, dict):
        return None
    try:
        day = date.fromisoformat(_text(raw.get("date")))
    except ValueError:
        return None
    day_weather = _text(raw.get("dayweather"))
    return DayWeather(
        date=day,
        day_weather=day_weather,
        night_weather=_text(raw.get("nightweather")),
        rain_level=rain_level_of(day_weather),
    )


class AmapClient:
    """高德 Web 服务异步客户端，可在进程内并发复用。

    ``http_client`` 由应用生命周期创建和关闭，本类不创建或关闭连接。请求超时由
    外部客户端的 ``timeout`` 统一设置（应使用配置 ``amap_timeout_seconds``）。

    重试策略：网络超时、连接错误、HTTP 5xx 及 QPS 限流类 infocode 视为暂时性错误，
    最多尝试 ``max_attempts`` 次，第 n 次失败后等待 ``retry_backoff_seconds * 2**(n-1)``
    秒；key 无效、日配额超限、参数错误等其他错误直接抛出，不重试。所有接口均为只读
    查询，重试不涉及幂等问题。

    成功结果按（接口路径、排序后的参数，不含 key）缓存在进程内，``cache_ttl_seconds``
    为 0 时不缓存，失败结果不缓存。
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        api_key: str,
        base_url: str,
        max_concurrency: int = 3,
        cache_ttl_seconds: int = 3600,
        max_attempts: int = 3,
        retry_backoff_seconds: float = 0.5,
    ) -> None:
        """保存依赖和限制参数，不执行任何外部 I/O。

        Args:
            http_client: 外部管理生命周期、已设置超时的异步 HTTP 客户端。
            api_key: 高德 Web 服务 key。
            base_url: 接口根地址，如 ``https://restapi.amap.com/v3``。
            max_concurrency: 同时进行的请求数上限。
            cache_ttl_seconds: 成功结果缓存秒数，0 表示不缓存。
            max_attempts: 暂时性错误下的最大尝试次数（含首次）。
            retry_backoff_seconds: 首次重试前的等待秒数，之后按 2 倍递增；测试可设为 0。

        Raises:
            ValueError: 并发数或尝试次数小于 1，或缓存秒数、退避秒数为负。
        """
        if max_concurrency < 1 or max_attempts < 1:
            raise ValueError("并发数和尝试次数必须大于 0")
        if cache_ttl_seconds < 0 or retry_backoff_seconds < 0:
            raise ValueError("缓存秒数和退避秒数不能为负")
        self._http_client = http_client
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._cache_ttl_seconds = cache_ttl_seconds
        self._max_attempts = max_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        # 缓存值为（过期的单调时钟时间，已解析 JSON），按写入顺序保存以便淘汰。
        self._cache: dict[_CacheKey, tuple[float, dict[str, Any]]] = {}

    async def resolve_city(self, name: str) -> list[CityInfo]:
        """按名称解析城市，只返回地级市和直辖市。

        “杭州”“杭州市”都解析为 330100；“北京”解析为 110000（不返回“北京城区”）；
        区县、省份等其他级别不返回。

        Args:
            name: 城市名称或关键词。

        Returns:
            匹配的城市列表，按高德返回顺序去重；查不到时为空列表。

        Raises:
            AmapError: 接口调用失败。
        """
        keywords = name.strip()
        if not keywords:
            return []
        data = await self._request("/config/district", {"keywords": keywords, "subdistrict": "0"})
        cities: list[CityInfo] = []
        seen: set[str] = set()
        for raw in data.get("districts") or []:
            city = _parse_city(raw)
            if city is not None and city.adcode not in seen:
                seen.add(city.adcode)
                cities.append(city)
        return cities

    async def search_pois(
        self,
        city_adcode: str,
        keywords: str,
        *,
        types: str | None = None,
        page: int = 1,
        page_size: int = 25,
    ) -> list[AmapPoi]:
        """在指定城市内按关键词和分类搜索 POI，坐标无效的结果会被丢弃。

        Args:
            city_adcode: 城市编码，搜索限制在该城市内。
            keywords: 搜索关键词；提供 ``types`` 时可为空串。
            types: 高德分类编码，多个用 ``|`` 分隔，如 ``"110000"``。
            page: 页码，从 1 开始。
            page_size: 每页条数，1 至 25。

        Returns:
            解析后的 POI 列表，超出结果范围时为空列表。

        Raises:
            ValueError: 分页参数越界，或关键词和分类均为空。
            AmapError: 接口调用失败。
        """
        if page < 1 or not 1 <= page_size <= _MAX_PAGE_SIZE:
            raise ValueError("页码须大于 0，每页条数须在 1 至 25 之间")
        params = {
            "city": city_adcode,
            "citylimit": "true",
            # extensions=all 才会返回 biz_ext.rating。
            "extensions": "all",
            "offset": str(page_size),
            "page": str(page),
        }
        if keywords.strip():
            params["keywords"] = keywords.strip()
        if types:
            params["types"] = types
        if "keywords" not in params and "types" not in params:
            raise ValueError("关键词和分类不能同时为空")
        data = await self._request("/place/text", params)
        return [poi for raw in data.get("pois") or [] if (poi := _parse_poi(raw)) is not None]

    async def find_pois_by_name(self, city_adcode: str, name: str, *, limit: int = 5) -> list[AmapPoi]:
        """按名称在城市内查找 POI（不限分类），用于核实用户提到的景点。

        Args:
            city_adcode: 城市编码。
            name: 景点名称。
            limit: 最多返回条数，1 至 25。

        Returns:
            按高德相关度排序的 POI 列表；名称为空或查不到时为空列表。

        Raises:
            ValueError: ``limit`` 越界。
            AmapError: 接口调用失败。
        """
        if not 1 <= limit <= _MAX_PAGE_SIZE:
            raise ValueError("limit 须在 1 至 25 之间")
        if not name.strip():
            return []
        return await self.search_pois(city_adcode, name, page_size=limit)

    async def weather_forecast(self, city_adcode: str) -> list[DayWeather]:
        """查询城市天气预报（含当天共约 4 天），降雨等级按白天天气计算。

        Args:
            city_adcode: 城市编码。

        Returns:
            按日期排列的单日预报；无预报数据时为空列表。

        Raises:
            AmapError: 接口调用失败。
        """
        data = await self._request("/weather/weatherInfo", {"city": city_adcode, "extensions": "all"})
        forecasts = data.get("forecasts") or []
        if not forecasts or not isinstance(forecasts[0], dict):
            return []
        casts = forecasts[0].get("casts") or []
        return [day for raw in casts if (day := _parse_day_weather(raw)) is not None]

    async def poi_detail(self, poi_id: str) -> AmapPoi | None:
        """按 POI 标识查询详情。

        Args:
            poi_id: 高德 POI 标识。

        Returns:
            解析后的 POI；不存在或坐标无效时为空。

        Raises:
            AmapError: 接口调用失败。
        """
        if not poi_id.strip():
            return None
        data = await self._request("/place/detail", {"id": poi_id.strip()})
        for raw in data.get("pois") or []:
            poi = _parse_poi(raw)
            if poi is not None:
                return poi
        return None

    async def _request(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        """发起 GET 请求，处理缓存、并发限制、重试和日志。

        Args:
            path: 接口路径，如 ``/place/text``。
            params: 不含 key 的查询参数。

        Returns:
            status 为 1 的已解析 JSON。

        Raises:
            AmapError: 不可重试的错误，或重试耗尽。
        """
        cache_key: _CacheKey = (path, tuple(sorted(params.items())))
        cached = self._cache_get(cache_key)
        if cached is not None:
            logger.bind(event="amap_request", status="cache_hit").debug(f"高德请求命中缓存 {path}")
            return cached

        url = f"{self._base_url}{path}"
        query = {**params, "key": self._api_key}
        for attempt in range(1, self._max_attempts + 1):
            started = time.perf_counter()
            try:
                # 只在实际请求期间占用并发名额，退避等待不占用。
                async with self._semaphore:
                    response = await self._http_client.get(url, params=query)
                data = self._check_response(path, response)
            except AmapError as exc:
                error = exc
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                # 异常文本可能带请求地址，只保留类型名。
                error = AmapError(path, reason=type(exc).__name__, retryable=True)
                error.__cause__ = exc
            except httpx.HTTPError as exc:
                # 协议、地址等其他客户端错误重试无效，统一转为 AmapError。
                error = AmapError(path, reason=type(exc).__name__)
                error.__cause__ = exc
            else:
                logger.bind(
                    event="amap_request", status="ok", attempt=attempt, duration_ms=_elapsed_ms(started),
                ).info(f"高德请求成功 {path}")
                self._cache_put(cache_key, data)
                return data

            duration_ms = _elapsed_ms(started)
            error_type = error.infocode or (
                f"http_{error.status_code}" if error.status_code is not None else error.reason
            )
            if error.retryable and attempt < self._max_attempts:
                logger.bind(
                    event="amap_request", status="retry", attempt=attempt,
                    duration_ms=duration_ms, error_type=error_type,
                ).warning(f"高德请求暂时失败，准备重试 {path}")
                await asyncio.sleep(self._retry_backoff_seconds * 2 ** (attempt - 1))
                continue
            logger.bind(
                event="amap_request", status="error", attempt=attempt,
                duration_ms=duration_ms, error_type=error_type,
            ).error(f"高德请求失败 {path}")
            raise error
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _check_response(path: str, response: httpx.Response) -> dict[str, Any]:
        """校验 HTTP 状态和高德 status，返回成功的 JSON。

        Raises:
            AmapError: HTTP 错误、响应不是 JSON 对象或 status 不为 1。
        """
        if response.status_code >= 500:
            raise AmapError(path, reason="服务端错误", status_code=response.status_code, retryable=True)
        if response.status_code != 200:
            raise AmapError(path, reason="HTTP 状态异常", status_code=response.status_code)
        try:
            data = response.json()
        except ValueError as exc:
            raise AmapError(path, reason="响应不是有效 JSON", status_code=200) from exc
        if not isinstance(data, dict):
            raise AmapError(path, reason="响应结构异常", status_code=200)
        if str(data.get("status")) != "1":
            infocode = str(data.get("infocode") or "") or None
            raise AmapError(
                path,
                reason=_text(data.get("info")) or "未知错误",
                infocode=infocode,
                retryable=infocode in _RETRYABLE_INFOCODES,
            )
        return data

    def _cache_get(self, key: _CacheKey) -> dict[str, Any] | None:
        """读取未过期的缓存，过期条目顺带删除。"""
        if self._cache_ttl_seconds <= 0:
            return None
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, data = entry
        if expires_at <= time.monotonic():
            self._cache.pop(key, None)
            return None
        return data

    def _cache_put(self, key: _CacheKey, data: dict[str, Any]) -> None:
        """写入成功结果，超过条目上限时先清理过期条目再淘汰最早条目。"""
        if self._cache_ttl_seconds <= 0:
            return
        now = time.monotonic()
        self._cache.pop(key, None)
        self._cache[key] = (now + self._cache_ttl_seconds, data)
        if len(self._cache) > _MAX_CACHE_ENTRIES:
            for stale_key in [k for k, (expires_at, _) in self._cache.items() if expires_at <= now]:
                del self._cache[stale_key]
            while len(self._cache) > _MAX_CACHE_ENTRIES:
                del self._cache[next(iter(self._cache))]


def _elapsed_ms(started: float) -> int:
    """返回从 ``started`` 起经过的毫秒数。"""
    return int((time.perf_counter() - started) * 1000)
