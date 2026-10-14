"""行程规划纯计算测试的共享数据与辅助函数：杭州西湖周边景点、求解输入构造和行程组装。"""

import random
from dataclasses import dataclass
from datetime import date, timedelta

from app.agent.trip.planning.rules import LUNCH_EARLIEST_START, LUNCH_MINUTES, day_limits
from app.agent.trip.planning.travel import build_travel_matrix, estimate_leg
from app.schemas.trip_plan_schema import (
    SUBTYPE_MAIN_CATEGORY,
    CityInfo,
    LunchBreak,
    Plan,
    PlanDay,
    PlanItem,
    Requirements,
    SolveDay,
    SolveInput,
    SolveOutput,
    SolverPoi,
    TransportPref,
)

HANGZHOU = CityInfo(adcode="330100", name="杭州", lng=120.1551, lat=30.2741)
START_DATE = date(2026, 10, 20)


@dataclass(frozen=True)
class SpotInfo:
    """测试用景点。"""

    poi_id: str
    name: str
    lng: float
    lat: float
    subtype: str
    indoor: str
    duration: int
    value: int


# 杭州西湖周边真实景点的近似坐标（高德坐标系）
SPOTS: dict[str, SpotInfo] = {
    s.poi_id: s
    for s in [
        SpotInfo("duanqiao", "断桥残雪", 120.1513, 30.2593, "lake_river", "outdoor", 60, 90),
        SpotInfo("baidi", "白堤", 120.1460, 30.2560, "lake_river", "outdoor", 60, 70),
        SpotInfo("zjmuseum", "浙江省博物馆孤山馆区", 120.1442, 30.2531, "museum", "indoor", 120, 80),
        SpotInfo("yuewang", "岳王庙", 120.1365, 30.2600, "memorial", "mixed", 60, 75),
        SpotInfo("leifeng", "雷峰塔", 120.1490, 30.2312, "ancient_architecture", "mixed", 90, 95),
        SpotInfo("jingci", "净慈寺", 120.1497, 30.2290, "temple", "mixed", 60, 60),
        SpotInfo("huagang", "花港观鱼", 120.1430, 30.2320, "garden", "outdoor", 90, 70),
        SpotInfo("lingyin", "灵隐寺", 120.1013, 30.2420, "temple", "mixed", 180, 100),
        SpotInfo("xixi", "西溪湿地", 120.0700, 30.2730, "forest_wetland", "outdoor", 240, 85),
        SpotInfo("hefang", "河坊街", 120.1700, 30.2420, "historic_street", "outdoor", 90, 65),
        SpotInfo("teamuseum", "中国茶叶博物馆", 120.1270, 30.2350, "museum", "indoor", 90, 55),
        SpotInfo("songcheng", "宋城", 120.0980, 30.1750, "theme_park", "outdoor", 300, 90),
    ]
}

# 2 天 8 个景点的基础小实例
SMALL_IDS = ["duanqiao", "baidi", "zjmuseum", "yuewang", "leifeng", "jingci", "huagang", "hefang"]


def solver_poi(spot: SpotInfo, **overrides) -> SolverPoi:
    """把测试景点转为求解候选，标签包含细分体验、主体验类和室内外属性。"""
    main = SUBTYPE_MAIN_CATEGORY[spot.subtype]
    data = {
        "poi_id": spot.poi_id,
        "duration_minutes": spot.duration,
        "value": spot.value,
        "main_category": main,
        "subtype": spot.subtype,
        "indoor": spot.indoor,
        "tags": [spot.subtype, main, spot.indoor],
    }
    data.update(overrides)
    return SolverPoi(**data)


def solve_days(requirements: Requirements, days: list[int], rain: dict[int, int] | None = None) -> list[SolveDay]:
    """按需求的节奏与松紧生成求解用的天。"""
    rain = rain or {}
    result = []
    for d in days:
        end_limit, max_items = day_limits(requirements, d)
        result.append(SolveDay(day=d, rain_level=rain.get(d, 0), end_limit_min=end_limit, max_items=max_items))
    return result


def make_input(
    ids: list[str],
    requirements: Requirements,
    days: list[int],
    *,
    rain: dict[int, int] | None = None,
    poi_overrides: dict[str, dict] | None = None,
    **kwargs,
) -> SolveInput:
    """构造求解输入：候选、交通矩阵和天均由测试景点和需求生成。"""
    poi_overrides = poi_overrides or {}
    pois = [solver_poi(SPOTS[i], **poi_overrides.get(i, {})) for i in ids]
    travel = build_travel_matrix([(i, SPOTS[i].lng, SPOTS[i].lat) for i in ids], requirements.transport)
    kwargs.setdefault("time_limit_seconds", 5)
    return SolveInput(
        days=solve_days(requirements, days, rain),
        pois=pois,
        travel=travel,
        **kwargs,
    )


def assemble_plan(
    out: SolveOutput,
    requirements: Requirements,
    durations: dict[str, int] | None = None,
    extra_days: list[PlanDay] | None = None,
) -> Plan:
    """把求解输出组装为行程草稿，交通从坐标重新估算。

    Args:
        out: 求解输出。
        requirements: 需求，提供交通偏好。
        durations: 覆盖的游玩分钟数，缺省用测试景点的时长。
        extra_days: 不在求解输出中的固定天，原样并入。
    """
    durations = durations or {}
    plan_days: list[PlanDay] = list(extra_days or [])
    for solved in out.days:
        items: list[PlanItem] = []
        lunch: LunchBreak | None = None
        prev: SpotInfo | None = None
        for pos, visit in enumerate(solved.visits, start=1):
            spot = SPOTS[visit.poi_id]
            item_id = f"D{solved.day}-{pos}"
            leg = None
            if prev is not None:
                leg = estimate_leg(prev.lng, prev.lat, spot.lng, spot.lat, requirements.transport)
            items.append(
                PlanItem(
                    item_id=item_id,
                    poi_id=spot.poi_id,
                    name=spot.name,
                    lng=spot.lng,
                    lat=spot.lat,
                    subtype=spot.subtype,
                    main_category=SUBTYPE_MAIN_CATEGORY[spot.subtype],
                    indoor=spot.indoor,
                    start_min=visit.start_min,
                    end_min=visit.end_min,
                    duration_minutes=durations.get(spot.poi_id, spot.duration),
                    lunch_inside=visit.lunch_inside,
                    travel_from_prev=leg,
                )
            )
            if visit.lunch_inside:
                lunch = LunchBreak(
                    start_min=LUNCH_EARLIEST_START,
                    end_min=LUNCH_EARLIEST_START + LUNCH_MINUTES,
                    inside_item_id=item_id,
                )
            elif visit.lunch_after:
                lunch = LunchBreak(start_min=visit.end_min, end_min=visit.end_min + LUNCH_MINUTES)
            prev = spot
        plan_days.append(
            PlanDay(day=solved.day, date=START_DATE + timedelta(days=solved.day - 1), items=items, lunch=lunch)
        )
    plan_days.sort(key=lambda d: d.day)
    return Plan(city=HANGZHOU, start_date=START_DATE, days=plan_days, objective=out.objective)


def random_instance(n_days: int, n_pois: int, seed: int, time_limit: float = 10) -> SolveInput:
    """生成杭州主城区约 12×12 公里范围内的随机实例，用于规模测试。"""
    rng = random.Random(seed)
    subtypes = list(SUBTYPE_MAIN_CATEGORY)
    indoors = ["indoor", "outdoor", "mixed"]
    pois: list[SolverPoi] = []
    points: list[tuple[str, float, float]] = []
    for n in range(n_pois):
        pid = f"P{n:03d}"
        subtype = rng.choice(subtypes)
        indoor = rng.choice(indoors)
        duration = rng.choice([45, 60, 90, 120, 150, 180, 240])
        main = SUBTYPE_MAIN_CATEGORY[subtype]
        pois.append(
            SolverPoi(
                poi_id=pid,
                duration_minutes=duration,
                value=rng.randint(40, 160),
                main_category=main,
                subtype=subtype,
                indoor=indoor,
                tags=[subtype, main, indoor],
            )
        )
        points.append((pid, 120.08 + rng.random() * 0.12, 30.20 + rng.random() * 0.11))
    requirements = Requirements(pace="normal")
    travel = build_travel_matrix(points, TransportPref())
    rain = {d: rng.choice([0, 0, 1, 2, 3]) for d in range(1, n_days + 1)}
    return SolveInput(
        days=solve_days(requirements, list(range(1, n_days + 1)), rain),
        pois=pois,
        travel=travel,
        time_limit_seconds=time_limit,
    )
