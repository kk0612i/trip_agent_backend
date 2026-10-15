"""评估用的固定会话场景：当天日期、需求、草稿和待回答问题全部冻结，保证标注答案可复现。

评估集里的相对日期（“下周五”“后天”）都按 EVAL_TODAY 换算；修改草稿时的引用编号（D2-3 等）
以本文件的 HZ3D_LAYOUT 为准。修改这里会使已有标注失效，需要同步更新 cases/ 下的用例。
"""

from collections.abc import Callable
from datetime import date, timedelta

from app.schemas.trip_plan_schema import (
    SUBTYPE_MAIN_CATEGORY,
    CityInfo,
    DayWeather,
    Plan,
    PlanDay,
    PlanItem,
    PoiRef,
    Requirements,
)
from app.schemas.trip_schema import PendingOption, PendingQuestion, TripContext, TurnRecord

EVAL_TODAY = date(2026, 10, 15)
"""评估使用的“今天”：2026-10-15，周四。"""

HZ_START = date(2026, 10, 20)
"""杭州草稿的出发日期：2026-10-20，周二。"""

HANGZHOU = CityInfo(adcode="330100", name="杭州", lng=120.1551, lat=30.2741)
SHANGHAI = CityInfo(adcode="310100", name="上海", lng=121.4737, lat=31.2304)

# 景点：（poi_id, 名称, 经度, 纬度, 细分体验, 室内外）
_SPOTS: dict[str, tuple[str, float, float, str, str]] = {
    "duanqiao": ("断桥残雪", 120.1513, 30.2593, "lake_river", "outdoor"),
    "baidi": ("白堤", 120.1460, 30.2560, "lake_river", "outdoor"),
    "zjmuseum": ("浙江省博物馆孤山馆区", 120.1442, 30.2531, "museum", "indoor"),
    "yuewang": ("岳王庙", 120.1365, 30.2600, "memorial", "mixed"),
    "leifeng": ("雷峰塔", 120.1490, 30.2312, "ancient_architecture", "mixed"),
    "jingci": ("净慈寺", 120.1497, 30.2290, "temple", "mixed"),
    "huagang": ("花港观鱼", 120.1430, 30.2320, "garden", "outdoor"),
    "teamuseum": ("中国茶叶博物馆", 120.1270, 30.2350, "museum", "indoor"),
    "lingyin": ("灵隐寺", 120.1013, 30.2420, "temple", "mixed"),
    "xixi": ("西溪湿地", 120.0700, 30.2730, "forest_wetland", "outdoor"),
}

# 每天的（景点, 开始分钟, 游玩分钟）；分钟相对 9:00。第 2 天小雨。
HZ3D_LAYOUT: dict[int, list[tuple[str, int, int]]] = {
    1: [("duanqiao", 0, 60), ("baidi", 70, 60), ("zjmuseum", 250, 120), ("yuewang", 390, 60)],
    2: [("leifeng", 0, 90), ("jingci", 100, 60), ("huagang", 240, 90), ("teamuseum", 360, 90)],
    3: [("lingyin", 0, 180), ("xixi", 270, 240)],
}
_HZ3D_WEATHER = {1: ("晴", 0), 2: ("小雨", 1), 3: ("多云", 0)}


def _item(spot_id: str, item_id: str, start: int, minutes: int) -> PlanItem:
    """把场景景点转为草稿项。"""
    name, lng, lat, subtype, indoor = _SPOTS[spot_id]
    return PlanItem(
        item_id=item_id, poi_id=spot_id, name=name, lng=lng, lat=lat, subtype=subtype,
        main_category=SUBTYPE_MAIN_CATEGORY[subtype], indoor=indoor,
        start_min=start, end_min=start + minutes, duration_minutes=minutes, rating=4.7,
    )


def hz3d_plan() -> Plan:
    """杭州西湖三日草稿，引用编号为 D{天}-{序号}。"""
    days = []
    for number, entries in sorted(HZ3D_LAYOUT.items()):
        current = HZ_START + timedelta(days=number - 1)
        text, rain = _HZ3D_WEATHER[number]
        days.append(PlanDay(
            day=number,
            date=current,
            weather=DayWeather(date=current, day_weather=text, rain_level=rain),
            items=[
                _item(spot_id, f"D{number}-{pos}", start, minutes)
                for pos, (spot_id, start, minutes) in enumerate(entries, start=1)
            ],
        ))
    return Plan(city=HANGZHOU, start_date=HZ_START, days=days)


def _hz3d_requirements() -> Requirements:
    """与杭州草稿一致的需求：3 天、适中节奏、喜欢历史和自然、必去灵隐寺。"""
    return Requirements(
        city=HANGZHOU, start_date=HZ_START, days=3, pace="normal",
        preferences={"history": 1, "nature": 1},
        must_visit=[PoiRef(poi_id="lingyin", name="灵隐寺")],
    )


_HZ3D_TURNS = [
    TurnRecord(role="user", content="下周二去杭州玩三天，喜欢历史和自然风景，灵隐寺一定要去"),
    TurnRecord(role="assistant", content="已为你生成杭州 3 天的行程草稿，第 1 天环湖北线，第 2 天南线，第 3 天灵隐和西溪。"),
]


def _collecting_empty() -> TripContext:
    """刚创建的会话，什么需求都没有。"""
    return TripContext(trip_id=1, user_id=1, status="collecting", today=EVAL_TODAY)


def _collecting_city_only() -> TripContext:
    """只知道去杭州，正在追问日期和天数（自由回答）。"""
    return TripContext(
        trip_id=1, user_id=1, status="collecting", today=EVAL_TODAY,
        requirements=Requirements(city=HANGZHOU),
        pending=PendingQuestion(kind="missing_info", text="好的，去杭州。请问计划哪天出发、玩几天？"),
        recent_turns=[
            TurnRecord(role="user", content="我想去杭州玩"),
            TurnRecord(role="assistant", content="好的，去杭州。请问计划哪天出发、玩几天？"),
        ],
    )


def _collecting_shanghai_partial() -> TripContext:
    """上海、2 天、休闲、特别喜欢博物馆，缺出发日期。"""
    return TripContext(
        trip_id=1, user_id=1, status="collecting", today=EVAL_TODAY,
        requirements=Requirements(city=SHANGHAI, days=2, pace="relaxed", preferences={"museum": 2}),
        pending=PendingQuestion(kind="missing_info", text="请问哪天出发？"),
        recent_turns=[
            TurnRecord(role="user", content="去上海玩两天，节奏慢一点，特别喜欢博物馆"),
            TurnRecord(role="assistant", content="请问哪天出发？"),
        ],
    )


def _drafting_hz3d() -> TripContext:
    """已有杭州三日草稿，可修改。"""
    plan = hz3d_plan()
    return TripContext(
        trip_id=1, user_id=1, status="drafting", today=EVAL_TODAY,
        requirements=_hz3d_requirements(), draft=plan, draft_version=1, recent_turns=list(_HZ3D_TURNS),
    )


# 以下待回答问题的文字和选项按线上生成逻辑构造，保证评估的是真实会出现的状态：
# - 景点名歧义：IntentResolver.resolve 捕获 AmbiguousPoi，自由回答，没有选项；
# - 城市歧义：IntentResolver._apply_city，选项为候选城市并携带对应需求；
# - 无可行解：planning_nodes 诊断节点，选项为“允许打车”“允许调整其他天的安排”并携带需求和操作。


def _drafting_hz3d_pending_poi() -> TripContext:
    """草稿状态，新增景点名称有歧义，等待用户说出完整名称（无选项）。"""
    context = _drafting_hz3d()
    question = "你说的“西湖博物馆”是指 西湖博物馆总馆、杭州西湖博物馆南山路馆、西湖美术馆 中的哪一个？请说出完整名称。"
    context.pending = PendingQuestion(kind="ambiguous", text=question)
    context.recent_turns = [
        *_HZ3D_TURNS,
        TurnRecord(role="user", content="第一天加一个西湖博物馆"),
        TurnRecord(role="assistant", content=question),
    ]
    return context


def _drafting_hz3d_pending_infeasible() -> TripContext:
    """草稿状态、只步行，第 2 天新增宋城后排不下，询问是否允许打车或调整其他天。"""
    context = _drafting_hz3d()
    walk_only = context.requirements.transport.model_copy(update={"walk_only": True})
    context.requirements = context.requirements.model_copy(update={"transport": walk_only})
    relaxed = context.requirements.model_copy(
        update={"transport": walk_only.model_copy(update={"walk_only": False})},
    )
    options = [
        PendingOption(label="允许打车", requirements=relaxed),
        PendingOption(label="允许调整其他天的安排", requirements=context.requirements, force_full=True),
    ]
    question = (
        "这次没能排出行程：第2天需要保留或新增的景点（雷峰塔、净慈寺、花港观鱼、中国茶叶博物馆、宋城）"
        "超过当天最多 4 个的上限；只步行且单段不超过 40 分钟，景点之间难以连通。"
        "可以回复序号选择：" + "；".join(f"{i}. {o.label}" for i, o in enumerate(options, start=1))
    )
    context.pending = PendingQuestion(kind="infeasible", text=question, options=options)
    context.recent_turns = [
        *_HZ3D_TURNS,
        TurnRecord(role="user", content="第二天加上宋城"),
        TurnRecord(role="assistant", content=question),
    ]
    return context


def _collecting_pending_city() -> TripContext:
    """收集阶段，城市名对应多个城市，等待用户选择（候选城市为构造示例）。"""
    cities = [
        CityInfo(adcode="220200", name="吉林市", lng=126.5496, lat=43.8378),
        CityInfo(adcode="220100", name="长春市", lng=125.3235, lat=43.8171),
        CityInfo(adcode="222400", name="延边朝鲜族自治州", lng=129.5091, lat=42.8913),
    ]
    options = [
        PendingOption(label=city.name, requirements=Requirements(city=city, days=3)) for city in cities
    ]
    question = "“吉林”对应多个城市，请回复序号：" + "；".join(
        f"{i}. {o.label}" for i, o in enumerate(options, start=1)
    )
    return TripContext(
        trip_id=1, user_id=1, status="collecting", today=EVAL_TODAY,
        requirements=Requirements(days=3),
        pending=PendingQuestion(kind="ambiguous", text=question, options=options),
        recent_turns=[
            TurnRecord(role="user", content="想去吉林玩三天"),
            TurnRecord(role="assistant", content=question),
        ],
    )


def _confirmed_hz3d() -> TripContext:
    """杭州三日行程已确认，只读。"""
    context = _drafting_hz3d()
    context.status = "confirmed"
    return context


SCENARIOS: dict[str, Callable[[], TripContext]] = {
    "collecting_empty": _collecting_empty,
    "collecting_city_only": _collecting_city_only,
    "collecting_shanghai_partial": _collecting_shanghai_partial,
    "drafting_hz3d": _drafting_hz3d,
    "collecting_pending_city": _collecting_pending_city,
    "drafting_hz3d_pending_poi": _drafting_hz3d_pending_poi,
    "drafting_hz3d_pending_infeasible": _drafting_hz3d_pending_infeasible,
    "confirmed_hz3d": _confirmed_hz3d,
}
"""场景名到构造函数；每次调用返回新对象，用例之间互不影响。"""


def build_context(scenario: str, recent_turns: list[TurnRecord] | None = None) -> TripContext:
    """按场景名构造会话快照。

    Args:
        scenario: 场景名，见 SCENARIOS。
        recent_turns: 用例自带的最近对话，非空时覆盖场景默认值。

    Returns:
        新的会话快照。

    Raises:
        KeyError: 场景名不存在。
    """
    context = SCENARIOS[scenario]()
    if recent_turns is not None:
        context.recent_turns = recent_turns
    return context
