"""草稿组装测试：景点信息、交通、午餐、固定天、提示信息和变化摘要。"""

from datetime import date, timedelta

import pytest

from app.agent.trip.planning.assemble import INSIDE_LUNCH_START, build_plan, change_summary, day_date
from app.agent.trip.planning.rules import LUNCH_MINUTES
from app.agent.trip.planning.scope import full_scope, partial_scope
from app.agent.trip.planning.solver import solve
from app.agent.trip.planning.travel import build_travel_matrix
from app.agent.trip.planning.validate import validate_plan
from app.schemas.trip_plan_schema import (
    Candidate,
    DayWeather,
    LunchBreak,
    Plan,
    PlanScope,
    PoiPhoto,
    RemoveItemsOp,
    Requirements,
    SolvedDay,
    SolvedVisit,
    SolveOutput,
    TransportPref,
    TravelLeg,
)
from tests.trip.draft_data import amap_poi, draft_plan, draft_requirements, spot_profile
from tests.trip.planning_data import HANGZHOU, SMALL_IDS, SPOTS, START_DATE, make_input

PHOTOS = [PoiPhoto(url="https://img.example.com/a.jpg", title="正门")]


def candidates_for(ids: list[str]) -> dict[str, Candidate]:
    """为测试景点构造候选，游玩时长取测试景点的时长。"""
    result = {}
    for pid in ids:
        spot = SPOTS[pid]
        result[pid] = Candidate(
            poi=amap_poi(pid, rating=4.7, address=f"西湖区{spot.name}路 1 号", photos=PHOTOS),
            profile=spot_profile(pid),
            duration_minutes=spot.duration,
            interest_score=5,
            rating_score=9.4,
        )
    return result


def travel_for(ids: list[str]) -> dict[str, TravelLeg]:
    """按测试景点坐标估算交通矩阵。"""
    return build_travel_matrix([(i, SPOTS[i].lng, SPOTS[i].lat) for i in ids], TransportPref())


def hand_output() -> SolveOutput:
    """手工求解结果：第 1 天三个景点且白堤后午餐，第 2 天灵隐寺园内午餐，第 3 天未出现。"""
    return SolveOutput(
        status="ok",
        days=[
            SolvedDay(day=1, visits=[
                SolvedVisit(poi_id="duanqiao", start_min=0, end_min=60),
                SolvedVisit(poi_id="baidi", start_min=70, end_min=130, lunch_after=True),
                SolvedVisit(poi_id="zjmuseum", start_min=200, end_min=320),
            ]),
            SolvedDay(day=2, visits=[
                SolvedVisit(poi_id="lingyin", start_min=0, end_min=240, lunch_inside=True),
            ]),
        ],
        objective={"total": 123, "travel": -4},
    )


HAND_IDS = ["duanqiao", "baidi", "zjmuseum", "lingyin"]


@pytest.fixture
def hand_plan() -> Plan:
    """由手工求解结果组装的三天草稿，无天气。"""
    req = draft_requirements(days=3)
    return build_plan(
        requirements=req, baseline=None, scope=full_scope(req, None, stability=False), output=hand_output(),
        candidates=candidates_for(HAND_IDS), travel=travel_for(HAND_IDS), weather={},
    )


def test_day_date() -> None:
    """第几天换算为日期。"""
    assert day_date(date(2026, 10, 20), 1) == date(2026, 10, 20)
    assert day_date(date(2026, 10, 30), 3) == date(2026, 11, 1)


def test_items_carry_poi_details(hand_plan: Plan) -> None:
    """草稿项带有高德评分、地址、图片和属性中的分类。"""
    item = hand_plan.days[0].items[2]
    assert item.item_id == "D1-3"
    assert item.poi_id == "zjmuseum"
    assert item.name == "浙江省博物馆孤山馆区"
    assert item.rating == 4.7
    assert item.address == "西湖区浙江省博物馆孤山馆区路 1 号"
    assert item.photos == PHOTOS
    assert item.subtype == "museum"
    assert item.main_category == "culture_art"
    assert item.indoor == "indoor"
    assert (item.start_min, item.end_min, item.duration_minutes) == (200, 320, 120)
    assert (item.lng, item.lat) == (SPOTS["zjmuseum"].lng, SPOTS["zjmuseum"].lat)


def test_item_ids_and_dates(hand_plan: Plan) -> None:
    """草稿项按天和顺序编号，天按出发日期顺延，城市和日期来自需求。"""
    assert [[i.item_id for i in d.items] for d in hand_plan.days] == [["D1-1", "D1-2", "D1-3"], ["D2-1"], []]
    assert [d.date for d in hand_plan.days] == [START_DATE + timedelta(days=n) for n in range(3)]
    assert hand_plan.city == HANGZHOU
    assert hand_plan.start_date == START_DATE
    assert hand_plan.objective == {"total": 123, "travel": -4}


def test_travel_legs_from_matrix(hand_plan: Plan) -> None:
    """首个景点没有交通，其余从交通矩阵中按上一个景点取边。"""
    travel = travel_for(HAND_IDS)
    items = hand_plan.days[0].items
    assert items[0].travel_from_prev is None
    assert items[1].travel_from_prev == travel["duanqiao|baidi"]
    assert items[2].travel_from_prev == travel["baidi|zjmuseum"]
    assert hand_plan.days[1].items[0].travel_from_prev is None


def test_missing_travel_edge_is_none() -> None:
    """交通矩阵缺少对应边时交通为空。"""
    req = draft_requirements(days=1)
    output = SolveOutput(status="ok", days=[SolvedDay(day=1, visits=[
        SolvedVisit(poi_id="duanqiao", start_min=0, end_min=60),
        SolvedVisit(poi_id="baidi", start_min=70, end_min=130),
    ])])
    plan = build_plan(
        requirements=req, baseline=None, scope=full_scope(req, None, stability=False), output=output,
        candidates=candidates_for(["duanqiao", "baidi"]), travel={}, weather={},
    )
    assert plan.days[0].items[1].travel_from_prev is None


def test_lunch_after_visit(hand_plan: Plan) -> None:
    """景点结束后午餐时，午餐从该景点结束开始，持续 60 分钟。"""
    assert hand_plan.days[0].lunch == LunchBreak(start_min=130, end_min=130 + LUNCH_MINUTES)


def test_lunch_inside_visit(hand_plan: Plan) -> None:
    """园内午餐统一展示为 12:00-13:00，并记录所在景点。"""
    item = hand_plan.days[1].items[0]
    assert item.lunch_inside is True
    assert hand_plan.days[1].lunch == LunchBreak(
        start_min=INSIDE_LUNCH_START, end_min=INSIDE_LUNCH_START + LUNCH_MINUTES, inside_item_id="D2-1",
    )


def test_unsolved_day_is_empty(hand_plan: Plan) -> None:
    """求解结果中没有的天为空日程，没有午餐。"""
    assert hand_plan.days[2].items == []
    assert hand_plan.days[2].lunch is None


def test_fixed_days_copied_from_baseline() -> None:
    """固定天原样沿用原草稿，只更新天气；受影响天使用求解结果。"""
    baseline = draft_plan()
    req = draft_requirements(days=3)
    scope = PlanScope(mode="partial", affected_days=[2], fixed_days=[1, 3])
    sunny = DayWeather(date=START_DATE, day_weather="晴", rain_level=0)
    output = SolveOutput(status="ok", days=[SolvedDay(day=2, visits=[
        SolvedVisit(poi_id="hefang", start_min=0, end_min=90),
    ])])
    plan = build_plan(
        requirements=req, baseline=baseline, scope=scope, output=output,
        candidates=candidates_for(["hefang"]), travel={}, weather={START_DATE: sunny},
    )
    assert plan.days[0].items == baseline.days[0].items
    assert plan.days[0].weather == sunny
    assert plan.days[2].items == baseline.days[2].items
    assert plan.days[2].weather is None
    assert [i.poi_id for i in plan.days[1].items] == ["hefang"]
    # 原草稿不被修改
    assert baseline.days[0].weather is None


def test_warnings_without_weather(hand_plan: Plan) -> None:
    """没有任何天气数据时提示暂未获取到天气预报。"""
    assert hand_plan.warnings == ["第1、2、3天暂未获取到天气预报，出发前请留意天气。"]


def test_warnings_rain_and_out_of_forecast() -> None:
    """降雨日列出含户外部分的景点；超出预报范围的天单独提示。"""
    req = draft_requirements(days=3)
    weather = {
        START_DATE: DayWeather(date=START_DATE, day_weather="小雨", rain_level=1),
        START_DATE + timedelta(days=1): DayWeather(date=START_DATE + timedelta(days=1), day_weather="晴", rain_level=0),
    }
    plan = build_plan(
        requirements=req, baseline=None, scope=full_scope(req, None, stability=False), output=hand_output(),
        candidates=candidates_for(HAND_IDS), travel=travel_for(HAND_IDS), weather=weather,
    )
    assert plan.days[0].weather.day_weather == "小雨"
    assert plan.warnings == [
        "第1天预报小雨，断桥残雪、白堤含户外部分，注意带伞。",
        "第3天超出天气预报范围，出发前请留意天气。",
    ]


def test_warnings_taxi_fee_unknown() -> None:
    """有打车交通时提示费用未列出。"""
    ids = ["leifeng", "lingyin"]
    travel = travel_for(ids)
    assert travel["leifeng|lingyin"].mode == "taxi"
    req = draft_requirements(days=1)
    output = SolveOutput(status="ok", days=[SolvedDay(day=1, visits=[
        SolvedVisit(poi_id="leifeng", start_min=0, end_min=90),
        SolvedVisit(poi_id="lingyin", start_min=90 + travel["leifeng|lingyin"].minutes, end_min=450, lunch_inside=True),
    ])])
    plan = build_plan(
        requirements=req, baseline=None, scope=full_scope(req, None, stability=False), output=output,
        candidates=candidates_for(ids), travel=travel, weather={},
    )
    assert "打车时间按直线距离估算，费用暂无可靠数据未列出。" in plan.warnings


def test_solver_output_assembles_to_valid_plan() -> None:
    """真实求解结果组装后通过独立验算。"""
    req = Requirements(city=HANGZHOU, start_date=START_DATE, days=2)
    inp = make_input(SMALL_IDS, req, [1, 2])
    output = solve(inp)
    assert output.status == "ok"
    plan = build_plan(
        requirements=req, baseline=None, scope=full_scope(req, None, stability=False), output=output,
        candidates=candidates_for(SMALL_IDS), travel=inp.travel, weather={},
    )
    assert validate_plan(plan, req) == []
    assert sum(len(d.items) for d in plan.days) == sum(len(d.visits) for d in output.days)


# ---------------------------------------------------------------------------
# 变化摘要
# ---------------------------------------------------------------------------


def test_change_summary_first_plan_is_none() -> None:
    """首次规划没有变化摘要。"""
    assert change_summary(None, draft_plan(), None) is None


def test_change_summary_no_change() -> None:
    """内容相同时说明只重新核算了时间。"""
    assert change_summary(draft_plan(), draft_plan(), None) == "行程内容没有变化，仅重新核算了时间。"


def test_change_summary_remove_without_refill() -> None:
    """删除后空出不足 60 分钟时说明未补位。"""
    baseline = draft_plan(durations={"baidi": 45})
    scope = partial_scope(draft_requirements(), baseline, [RemoveItemsOp(item_ids=["D1-2"])])
    plan = draft_plan({
        1: [("duanqiao", 0), ("zjmuseum", 250)],
        2: [("leifeng", 0), ("jingci", 100), ("huagang", 180)],
        3: [("lingyin", 0), ("yuewang", 250)],
    })
    assert change_summary(baseline, plan, scope) == (
        "第1天：移除 白堤。\n第1天空出约 45 分钟，不足 60 分钟，未补入新景点。"
    )


def test_change_summary_refill_and_move() -> None:
    """补位、移入和调整时长都按天列出。"""
    baseline = draft_plan()
    scope = partial_scope(draft_requirements(), baseline, [RemoveItemsOp(item_ids=["D1-2"])])
    plan = draft_plan({
        1: [("duanqiao", 0), ("hefang", 70), ("zjmuseum", 250)],
        2: [("leifeng", 0), ("jingci", 100), ("huagang", 180)],
        3: [("lingyin", 0), ("yuewang", 250)],
    })
    assert change_summary(baseline, plan, scope) == (
        "第1天：移除 白堤；新增 河坊街。\n第1天空出约 60 分钟，已补入 河坊街。"
    )
    moved = draft_plan({
        1: [("baidi", 70), ("zjmuseum", 250)],
        2: [("leifeng", 0), ("jingci", 100), ("huagang", 180)],
        3: [("duanqiao", 0), ("lingyin", 100), ("yuewang", 300)],
    }, durations={"leifeng": 150})
    assert change_summary(baseline, moved, None) == (
        "第2天：调整时长 雷峰塔。\n第3天：移入 断桥残雪（原第1天）。"
    )


def test_change_summary_fewer_days() -> None:
    """天数减少时说明天数变化和不再保留的景点。"""
    baseline = draft_plan()
    plan = draft_plan({1: [("duanqiao", 0)], 2: [("leifeng", 0), ("lingyin", 120)]})
    summary = change_summary(baseline, plan, None)
    assert summary is not None
    lines = summary.split("\n")
    assert lines[0] == "行程从 3 天调整为 2 天。"
    assert "原第3天的 岳王庙 已不在行程中。" in lines
    assert "第2天：移除 净慈寺、花港观鱼；移入 灵隐寺（原第3天）。" in lines
