"""局部修改、意图解析和回复渲染测试共用的手工草稿：杭州西湖周边三日游。"""

from datetime import timedelta

from app.schemas.trip_plan_schema import (
    SUBTYPE_MAIN_CATEGORY,
    AmapPoi,
    Plan,
    PlanDay,
    PlanItem,
    PoiProfile,
    Requirements,
)
from tests.trip.planning_data import HANGZHOU, SPOTS, START_DATE

# 每天的（景点, 开始分钟）；时间只用于选择器的上午/下午判断，不要求可行。
DRAFT_LAYOUT: dict[int, list[tuple[str, int]]] = {
    1: [("duanqiao", 0), ("baidi", 70), ("zjmuseum", 250)],
    2: [("leifeng", 0), ("jingci", 100), ("huagang", 180)],
    3: [("lingyin", 0), ("yuewang", 250)],
}


def plan_item(spot_id: str, item_id: str, start_min: int, *, duration: int | None = None) -> PlanItem:
    """把测试景点转为草稿中的一项，游玩时长缺省取测试景点的时长。"""
    spot = SPOTS[spot_id]
    minutes = duration if duration is not None else spot.duration
    return PlanItem(
        item_id=item_id,
        poi_id=spot.poi_id,
        name=spot.name,
        lng=spot.lng,
        lat=spot.lat,
        subtype=spot.subtype,
        main_category=SUBTYPE_MAIN_CATEGORY[spot.subtype],
        indoor=spot.indoor,
        start_min=start_min,
        end_min=start_min + minutes,
        duration_minutes=minutes,
    )


def draft_plan(
    layout: dict[int, list[tuple[str, int]]] | None = None, *, durations: dict[str, int] | None = None,
) -> Plan:
    """按布局构造草稿，草稿引用为 D{天}-{序号}。

    Args:
        layout: 每天的（景点, 开始分钟），缺省为 DRAFT_LAYOUT。
        durations: 覆盖的游玩分钟数。
    """
    layout = DRAFT_LAYOUT if layout is None else layout
    durations = durations or {}
    days = [
        PlanDay(
            day=number,
            date=START_DATE + timedelta(days=number - 1),
            items=[
                plan_item(spot_id, f"D{number}-{pos}", start, duration=durations.get(spot_id))
                for pos, (spot_id, start) in enumerate(entries, start=1)
            ],
        )
        for number, entries in sorted(layout.items())
    ]
    return Plan(city=HANGZHOU, start_date=START_DATE, days=days)


def draft_requirements(days: int = 3, **extra: object) -> Requirements:
    """与草稿一致的需求：杭州、默认三天。"""
    return Requirements(city=HANGZHOU, start_date=START_DATE, days=days, **extra)


def amap_poi(spot_id: str, **overrides: object) -> AmapPoi:
    """把测试景点转为高德景点，默认杭州、评分 4.6。"""
    spot = SPOTS[spot_id]
    data: dict = {
        "poi_id": spot.poi_id,
        "name": spot.name,
        "lng": spot.lng,
        "lat": spot.lat,
        "adcode": "330106",
        "city_adcode": HANGZHOU.adcode,
        "address": f"{spot.name}地址",
        "amap_type": "风景名胜",
        "rating": 4.6,
    }
    data.update(overrides)
    return AmapPoi(**data)


def spot_profile(spot_id: str, **overrides: object) -> PoiProfile:
    """测试景点的属性，来源为 rule。"""
    spot = SPOTS[spot_id]
    data: dict = {
        "poi_id": spot.poi_id,
        "subtype": spot.subtype,
        "indoor": spot.indoor,
        "duration_minutes": spot.duration,
        "source": "rule",
    }
    data.update(overrides)
    return PoiProfile(**data)
