"""交通估算规则测试：公式数值、步行阈值、步行上限、只步行和不生成边的情况。"""

import pytest
from haversine import Unit, haversine

from app.agent.trip.planning.travel import (
    build_travel_matrix,
    estimate_leg,
    taxi_minutes,
    walk_minutes,
)
from app.schemas.trip_plan_schema import TransportPref

# 断桥残雪附近作为基准点
BASE_LNG = 120.1513
BASE_LAT = 30.2593
# 沿经线 1 公里对应的纬度差（haversine 库使用的平均地球半径）
KM_PER_DEG_LAT = haversine((0, 0), (1, 0), unit=Unit.KILOMETERS)


def north_of(km: float) -> tuple[float, float]:
    """返回基准点正北 km 公里处的 (经度, 纬度)。"""
    return BASE_LNG, BASE_LAT + km / KM_PER_DEG_LAT


def leg_to(km: float, transport: TransportPref | None = None):
    """估算从基准点到正北 km 公里处的交通。"""
    lng, lat = north_of(km)
    return estimate_leg(BASE_LNG, BASE_LAT, lng, lat, transport or TransportPref())


@pytest.mark.parametrize(
    ("km", "walk", "taxi"),
    [
        (0.0, 5, 15),
        (1.0, 23, 19),  # 步行 17.33→18+5；打车 3.36→4+15
        (1.5, 31, 21),  # 步行恰为 26.0，不能因浮点误差取成 27
        (10.0, 179, 49),  # 步行 173.33→174+5；打车 33.6→34+15
    ],
)
def test_formula_values(km: float, walk: int, taxi: int) -> None:
    assert walk_minutes(km) == walk
    assert taxi_minutes(km) == taxi


def test_distance_matches_haversine() -> None:
    leg = leg_to(1.2)
    assert leg is not None
    assert leg.distance_km == pytest.approx(1.2, abs=1e-3)


def test_walk_below_threshold_taxi_above() -> None:
    below = leg_to(1.49)
    above = leg_to(1.51)
    assert below is not None and below.mode == "walk"
    assert below.minutes == walk_minutes(1.49)
    assert below.fee_yuan == 0
    assert above is not None and above.mode == "taxi"
    assert above.minutes == taxi_minutes(1.51)
    # 当前未配置可信城市费率，打车费用为空
    assert above.fee_yuan is None


def test_walk_over_user_limit_switches_to_taxi() -> None:
    # 1.4 公里步行 25+5=30 分钟，超过 25 分钟上限
    leg = leg_to(1.4, TransportPref(max_walk_minutes=25))
    assert leg is not None
    assert leg.mode == "taxi"
    assert leg.minutes == taxi_minutes(1.4)


def test_walk_only_ignores_distance_threshold() -> None:
    # 3 公里步行 52+5=57 分钟，只步行且上限 60 时仍步行
    leg = leg_to(3.0, TransportPref(walk_only=True, max_walk_minutes=60))
    assert leg is not None
    assert leg.mode == "walk"
    assert leg.minutes == 57


def test_walk_only_over_limit_unreachable() -> None:
    assert leg_to(3.0, TransportPref(walk_only=True, max_walk_minutes=40)) is None
    # 步行上限放宽到 180，但超过单段 120 分钟上限仍不可达
    assert leg_to(8.0, TransportPref(walk_only=True, max_walk_minutes=180)) is None


@pytest.mark.parametrize(
    ("lng", "lat"),
    [(0.0, 30.0), (120.0, 0.0), (72.9, 30.0), (136.1, 30.0), (120.0, 2.9), (120.0, 54.1), (float("nan"), 30.0)],
)
def test_invalid_coordinates(lng: float, lat: float) -> None:
    assert estimate_leg(BASE_LNG, BASE_LAT, lng, lat, TransportPref()) is None
    assert estimate_leg(lng, lat, BASE_LNG, BASE_LAT, TransportPref()) is None


def test_leg_over_max_minutes_not_generated() -> None:
    # 30 公里打车 100.8→101+15=116 分钟，可达；32 公里 107.52→108+15=123 分钟，不可达
    near = leg_to(30.0)
    assert near is not None and near.minutes == 116
    assert leg_to(32.0) is None


def test_build_travel_matrix() -> None:
    a = ("a", BASE_LNG, BASE_LAT)
    b = ("b", *north_of(0.5))
    c = ("c", *north_of(5.0))
    bad = ("bad", 0.0, 0.0)
    far = ("far", *north_of(40.0))
    matrix = build_travel_matrix([a, b, c, bad, far], TransportPref())

    # a、b、c 两两可达；far 距离其余点至少 35 公里，打车超过 120 分钟；bad 坐标无效
    assert set(matrix) == {"a|b", "b|a", "a|c", "c|a", "b|c", "c|b"}
    assert matrix["a|b"].mode == "walk"
    assert matrix["a|c"].mode == "taxi"
    assert matrix["a|b"] == matrix["b|a"]


def test_build_travel_matrix_full_size() -> None:
    points = [(f"p{i}", *north_of(i * 0.3)) for i in range(6)]
    matrix = build_travel_matrix(points, TransportPref())
    assert len(matrix) == 6 * 5
    pairs = [key.split("|") for key in matrix]
    assert all(src != dst for src, dst in pairs)
