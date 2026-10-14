"""景点间交通估算：按球面直线距离推算步行或打车的耗时。

规则（用户给定，需精确实现）：
- 步行分钟 = ceil(d × 1.3 ÷ 4.5 × 60) + 5；
- 打车分钟 = ceil(d × 1.4 ÷ 25 × 60) + 15；
- 默认 d ≤ 1.5 公里且步行不超过用户上限时步行，否则打车；
- 只步行时不受 1.5 公里阈值限制，步行超出上限即不可达；
- 坐标无效或耗时超过 ``MAX_LEG_MINUTES`` 时不生成边。
"""

import math
from dataclasses import dataclass

from haversine import Unit, haversine

from app.agent.trip.planning.rules import MAX_LEG_MINUTES
from app.schemas.trip_plan_schema import TransportPref, TravelLeg

# 步行绕行系数与速度（公里/小时）
_WALK_DETOUR = 1.3
_WALK_SPEED_KMH = 4.5
_WALK_OVERHEAD_MINUTES = 5
# 打车绕行系数与速度（公里/小时）
_TAXI_DETOUR = 1.4
_TAXI_SPEED_KMH = 25
_TAXI_OVERHEAD_MINUTES = 15
# 默认优先步行的最大直线距离（公里）
WALK_PREFERRED_MAX_KM = 1.5

# 国内经纬度有效范围，超出视为坐标错误
_LNG_RANGE = (73.0, 136.0)
_LAT_RANGE = (3.0, 54.0)


@dataclass(frozen=True)
class TaxiFare:
    """可信的城市打车计价规则。

    Attributes:
        base_yuan: 起步价（元）。
        base_km: 起步价包含的公里数。
        per_km_yuan: 超出起步里程后每公里单价（元）。
    """

    base_yuan: float
    base_km: float
    per_km_yuan: float


# 城市编码到打车计价的映射；当前没有可信数据，保持为空，打车费用一律为空
TAXI_FARES: dict[str, TaxiFare] = {}


def _ceil_minutes(value: float) -> int:
    """对分钟数向上取整，先消除浮点误差，避免 26.000000000000004 被取成 27。"""
    return math.ceil(round(value, 9))


def walk_minutes(distance_km: float) -> int:
    """按直线距离估算步行分钟数。

    Args:
        distance_km: 球面直线距离（公里）。

    Returns:
        步行分钟数，含 5 分钟固定开销。
    """
    return _ceil_minutes(distance_km * _WALK_DETOUR / _WALK_SPEED_KMH * 60) + _WALK_OVERHEAD_MINUTES


def taxi_minutes(distance_km: float) -> int:
    """按直线距离估算打车分钟数。

    Args:
        distance_km: 球面直线距离（公里）。

    Returns:
        打车分钟数，含 15 分钟固定开销（候车、上下车）。
    """
    return _ceil_minutes(distance_km * _TAXI_DETOUR / _TAXI_SPEED_KMH * 60) + _TAXI_OVERHEAD_MINUTES


def _valid_coord(lng: float, lat: float) -> bool:
    """判断坐标是否在国内有效范围内且不为 0。"""
    if not (math.isfinite(lng) and math.isfinite(lat)):
        return False
    if lng == 0 or lat == 0:
        return False
    return _LNG_RANGE[0] <= lng <= _LNG_RANGE[1] and _LAT_RANGE[0] <= lat <= _LAT_RANGE[1]


def _taxi_fee(distance_km: float, city_adcode: str | None) -> float | None:
    """按可信城市费率估算打车费用，未配置费率时返回空。"""
    fare = TAXI_FARES.get(city_adcode) if city_adcode else None
    if fare is None:
        return None
    road_km = distance_km * _TAXI_DETOUR
    extra_km = max(road_km - fare.base_km, 0.0)
    return round(fare.base_yuan + extra_km * fare.per_km_yuan, 1)


def estimate_leg(
    a_lng: float,
    a_lat: float,
    b_lng: float,
    b_lat: float,
    transport: TransportPref,
    *,
    city_adcode: str | None = None,
) -> TravelLeg | None:
    """估算从 A 点到 B 点的一段交通。

    Args:
        a_lng: 起点经度。
        a_lat: 起点纬度。
        b_lng: 终点经度。
        b_lat: 终点纬度。
        transport: 用户交通偏好。
        city_adcode: 城市编码，用于查找打车费率；为空或未配置时打车费用为空。

    Returns:
        交通估算；坐标无效、只步行且超出步行上限、或耗时超过上限时返回 ``None``。
    """
    if not (_valid_coord(a_lng, a_lat) and _valid_coord(b_lng, b_lat)):
        return None
    # haversine 库的坐标顺序为 (纬度, 经度)
    distance = haversine((a_lat, a_lng), (b_lat, b_lng), unit=Unit.KILOMETERS)
    walk = walk_minutes(distance)
    distance_out = round(distance, 3)

    if transport.walk_only:
        if walk > transport.max_walk_minutes or walk > MAX_LEG_MINUTES:
            return None
        return TravelLeg(mode="walk", minutes=walk, distance_km=distance_out, fee_yuan=0)

    if distance <= WALK_PREFERRED_MAX_KM and walk <= transport.max_walk_minutes:
        if walk > MAX_LEG_MINUTES:
            return None
        return TravelLeg(mode="walk", minutes=walk, distance_km=distance_out, fee_yuan=0)

    taxi = taxi_minutes(distance)
    if taxi > MAX_LEG_MINUTES:
        return None
    return TravelLeg(
        mode="taxi",
        minutes=taxi,
        distance_km=distance_out,
        fee_yuan=_taxi_fee(distance, city_adcode),
    )


def build_travel_matrix(
    points: list[tuple[str, float, float]],
    transport: TransportPref,
    *,
    city_adcode: str | None = None,
) -> dict[str, TravelLeg]:
    """计算一组景点两两之间的有向交通边。

    距离和方式选择是对称的，每对景点只计算一次，再写入两个方向。

    Args:
        points: ``(poi_id, 经度, 纬度)`` 列表；重复的 poi_id 只保留首次出现。
        transport: 用户交通偏好。
        city_adcode: 城市编码，用于打车费率查找。

    Returns:
        键为 ``"from_id|to_id"`` 的有向边，不可达的边不出现，最多 N×(N−1) 条。
    """
    unique: list[tuple[str, float, float]] = []
    seen: set[str] = set()
    for poi_id, lng, lat in points:
        if poi_id in seen:
            continue
        seen.add(poi_id)
        unique.append((poi_id, lng, lat))

    matrix: dict[str, TravelLeg] = {}
    for i, (a_id, a_lng, a_lat) in enumerate(unique):
        for b_id, b_lng, b_lat in unique[i + 1:]:
            leg = estimate_leg(a_lng, a_lat, b_lng, b_lat, transport, city_adcode=city_adcode)
            if leg is None:
                continue
            matrix[f"{a_id}|{b_id}"] = leg
            matrix[f"{b_id}|{a_id}"] = leg.model_copy()
    return matrix
