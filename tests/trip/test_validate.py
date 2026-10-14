"""独立验算测试：合法行程通过，逐类篡改后都能报出违规。"""

import pytest

from app.agent.trip.planning.solver import solve
from app.agent.trip.planning.validate import validate_plan
from app.schemas.trip_plan_schema import (
    LunchBreak,
    Plan,
    PlanDay,
    PlanScope,
    PoiRef,
    Requirements,
    TransportPref,
    TravelLeg,
)
from tests.trip.planning_data import SMALL_IDS, SPOTS, assemble_plan, make_input

MUST = PoiRef(poi_id="leifeng", name="雷峰塔", day=1)
AVOID = PoiRef(poi_id="lingyin", name="灵隐寺")


def has(errors: list[str], fragment: str) -> bool:
    """判断违规列表中是否有包含指定片段的描述。"""
    return any(fragment in e for e in errors)


@pytest.fixture(scope="module")
def req() -> Requirements:
    """普通节奏、雷峰塔第 1 天必去、灵隐寺不去。"""
    return Requirements(must_visit=[MUST], avoid=[AVOID])


@pytest.fixture(scope="module")
def plan(req: Requirements) -> Plan:
    """由求解器生成的 2 天合法行程。"""
    out = solve(make_input(SMALL_IDS, req, [1, 2], required=req.must_visit))
    assert out.status == "ok"
    return assemble_plan(out, req)


@pytest.fixture(scope="module")
def inside_plan() -> Plan:
    """宋城园内午餐的单日行程（休闲且更轻松，最晚 420 结束）。"""
    inside_req = Requirements(pace="relaxed", day_loads={1: "lighter"})
    out = solve(make_input(["songcheng"], inside_req, [1], required=[PoiRef(poi_id="songcheng", name="宋城")]))
    return assemble_plan(out, inside_req)


def copy(plan: Plan) -> Plan:
    """深拷贝行程以便篡改。"""
    return plan.model_copy(deep=True)


def busiest(plan: Plan) -> PlanDay:
    """返回景点最多的一天。"""
    return max(plan.days, key=lambda d: len(d.items))


def lunch_day(plan: Plan) -> PlanDay:
    """返回有景点外午餐的一天。"""
    day = next(d for d in plan.days if d.lunch is not None and d.lunch.inside_item_id is None)
    return day


# ---------------------------------------------------------------------------
# 合法行程
# ---------------------------------------------------------------------------


def test_solver_plan_passes(plan: Plan, req: Requirements) -> None:
    assert validate_plan(plan, req) == []
    # 前置条件：后续篡改测试依赖的结构
    assert len(busiest(plan).items) >= 4
    assert lunch_day(plan) is not None


def test_inside_lunch_plan_passes(inside_plan: Plan) -> None:
    inside_req = Requirements(pace="relaxed", day_loads={1: "lighter"})
    assert inside_plan.days[0].lunch.inside_item_id == "D1-1"
    assert validate_plan(inside_plan, inside_req) == []


# ---------------------------------------------------------------------------
# 1. 重复与编号
# ---------------------------------------------------------------------------


def test_duplicate_poi_across_days(plan: Plan, req: Requirements) -> None:
    bad = copy(plan)
    src = bad.days[0].items[0]
    dst = bad.days[1].items[0]
    dst.poi_id = src.poi_id
    assert has(validate_plan(bad, req), "重复安排")


def test_item_id_not_consecutive(plan: Plan, req: Requirements) -> None:
    bad = copy(plan)
    bad.days[0].items[1].item_id = "D1-9"
    assert has(validate_plan(bad, req), "编号应为 D1-2")


def test_item_id_wrong_day(plan: Plan, req: Requirements) -> None:
    bad = copy(plan)
    bad.days[1].items[0].item_id = "D1-1"
    assert has(validate_plan(bad, req), "编号应为 D2-1")


# ---------------------------------------------------------------------------
# 2. 时间与数量
# ---------------------------------------------------------------------------


def test_end_after_day_limit(plan: Plan, req: Requirements) -> None:
    bad = copy(plan)
    last = bad.days[0].items[-1]
    last.end_min = 610
    errors = validate_plan(bad, req)
    assert has(errors, "不在 [0, 600] 内")


def test_end_after_lighter_limit(plan: Plan, req: Requirements) -> None:
    # 同一行程按“第 1 天更轻松”核对：最晚 510、最多 4 个
    lighter = req.model_copy(update={"day_loads": {1: "lighter"}})
    bad = copy(plan)
    last = bad.days[0].items[-1]
    shift = 515 - last.end_min
    if shift > 0:
        last.start_min += shift
        last.end_min += shift
    assert has(validate_plan(bad, lighter), "不在 [0, 510] 内")


def test_occupied_minutes_mismatch(plan: Plan, req: Requirements) -> None:
    bad = copy(plan)
    bad.days[0].items[-1].duration_minutes += 10
    assert has(validate_plan(bad, req), "占用")


def test_too_many_items(plan: Plan, req: Requirements) -> None:
    relaxed = req.model_copy(update={"pace": "relaxed", "day_loads": {1: "lighter", 2: "lighter"}})
    # 休闲（3 个）且更轻松（减 1）：每天最多 2 个
    assert has(validate_plan(plan, relaxed), "超过上限 2")


def test_main_category_mismatch(plan: Plan, req: Requirements) -> None:
    bad = copy(plan)
    item = bad.days[0].items[0]
    item.main_category = "entertainment"
    assert has(validate_plan(bad, req), "主体验类")


# ---------------------------------------------------------------------------
# 3. 交通
# ---------------------------------------------------------------------------


def test_first_item_with_travel(plan: Plan, req: Requirements) -> None:
    bad = copy(plan)
    bad.days[0].items[0].travel_from_prev = TravelLeg(mode="walk", minutes=10, distance_km=0.5, fee_yuan=0)
    assert has(validate_plan(bad, req), "首个景点")


def test_missing_travel(plan: Plan, req: Requirements) -> None:
    bad = copy(plan)
    bad.days[0].items[1].travel_from_prev = None
    assert has(validate_plan(bad, req), "缺少从")


def test_travel_minutes_mismatch(plan: Plan, req: Requirements) -> None:
    bad = copy(plan)
    bad.days[0].items[1].travel_from_prev.minutes += 1
    assert has(validate_plan(bad, req), "重算应为")


def test_travel_mode_mismatch(plan: Plan, req: Requirements) -> None:
    bad = copy(plan)
    leg = bad.days[0].items[1].travel_from_prev
    leg.mode = "taxi" if leg.mode == "walk" else "walk"
    assert has(validate_plan(bad, req), "重算应为")


def test_next_start_too_early(plan: Plan, req: Requirements) -> None:
    bad = copy(plan)
    day = busiest(bad)
    prev, cur = day.items[-2], day.items[-1]
    dur = cur.end_min - cur.start_min
    cur.start_min = prev.end_min
    cur.end_min = cur.start_min + dur
    assert has(validate_plan(bad, req), "早于上一景点结束")


def test_lunch_between_requires_extra_hour(plan: Plan, req: Requirements) -> None:
    bad = copy(plan)
    day = lunch_day(bad)
    pos = next(i for i, it in enumerate(day.items) if it.end_min == day.lunch.start_min)
    prev, cur = day.items[pos], day.items[pos + 1]
    # 午餐在两者之间且不重叠，但下一景点比“结束 + 午餐 + 交通”早 1 分钟
    dur = cur.end_min - cur.start_min
    cur.start_min = prev.end_min + 60 + cur.travel_from_prev.minutes - 1
    cur.end_min = cur.start_min + dur
    errors = validate_plan(bad, req)
    assert has(errors, "加交通和午餐")
    assert not has(errors, "重叠")


def test_walk_only_with_taxi(plan: Plan, req: Requirements) -> None:
    walk_req = req.model_copy(update={"transport": TransportPref(walk_only=True, max_walk_minutes=180)})
    bad = copy(plan)
    bad.days[0].items[1].travel_from_prev.mode = "taxi"
    assert has(validate_plan(bad, walk_req), "只允许步行")


def test_unreachable_leg(plan: Plan, req: Requirements) -> None:
    bad = copy(plan)
    item = bad.days[0].items[1]
    item.lng, item.lat = 0.0, 0.0
    assert has(validate_plan(bad, req), "不可达")


def test_walk_only_far_leg_unreachable(plan: Plan, req: Requirements) -> None:
    walk_req = req.model_copy(update={"transport": TransportPref(walk_only=True, max_walk_minutes=10)})
    assert has(validate_plan(plan, walk_req), "不可达")


# ---------------------------------------------------------------------------
# 4. 午餐
# ---------------------------------------------------------------------------


def test_missing_lunch(plan: Plan, req: Requirements) -> None:
    bad = copy(plan)
    lunch_day(bad).lunch = None
    assert has(validate_plan(bad, req), "没有安排午餐")


def test_lunch_overlaps_item(plan: Plan, req: Requirements) -> None:
    bad = copy(plan)
    day = lunch_day(bad)
    day.lunch = LunchBreak(start_min=day.lunch.start_min - 10, end_min=day.lunch.start_min + 50)
    assert has(validate_plan(bad, req), "重叠")


def test_lunch_outside_window(plan: Plan, req: Requirements) -> None:
    bad = copy(plan)
    day = lunch_day(bad)
    day.lunch = LunchBreak(start_min=220, end_min=280)
    assert has(validate_plan(bad, req), "午餐开始于 220")


def test_lunch_wrong_length(plan: Plan, req: Requirements) -> None:
    bad = copy(plan)
    day = lunch_day(bad)
    day.lunch = LunchBreak(start_min=day.lunch.start_min, end_min=day.lunch.start_min + 30)
    assert has(validate_plan(bad, req), "时长不是 60 分钟")


def test_inside_lunch_flag_missing(inside_plan: Plan) -> None:
    inside_req = Requirements(pace="relaxed", day_loads={1: "lighter"})
    bad = copy(inside_plan)
    bad.days[0].items[0].lunch_inside = False
    errors = validate_plan(bad, inside_req)
    assert has(errors, "未标记 lunch_inside")
    assert has(errors, "占用")


def test_inside_lunch_not_covering_window(inside_plan: Plan) -> None:
    inside_req = Requirements(pace="relaxed", day_loads={1: "lighter"})
    bad = copy(inside_plan)
    item = bad.days[0].items[0]
    item.start_min, item.end_min = 0, 260
    item.duration_minutes = 200
    assert has(validate_plan(bad, inside_req), "未覆盖午餐时段")


def test_inside_lunch_short_visit(inside_plan: Plan) -> None:
    inside_req = Requirements(pace="relaxed", day_loads={1: "lighter"})
    bad = copy(inside_plan)
    item = bad.days[0].items[0]
    item.duration_minutes = 170
    item.end_min = item.start_min + 170 + 60
    assert has(validate_plan(bad, inside_req), "不允许园内午餐")


def test_inside_lunch_unknown_item(inside_plan: Plan) -> None:
    inside_req = Requirements(pace="relaxed", day_loads={1: "lighter"})
    bad = copy(inside_plan)
    bad.days[0].lunch.inside_item_id = "D1-7"
    errors = validate_plan(bad, inside_req)
    assert has(errors, "不在当天行程中")


# ---------------------------------------------------------------------------
# 5. 必去与不去
# ---------------------------------------------------------------------------


def test_must_visit_missing(plan: Plan, req: Requirements) -> None:
    more = req.model_copy(update={"must_visit": [MUST, PoiRef(poi_id="xixi", name="西溪湿地")]})
    assert has(validate_plan(plan, more), "必去景点 西溪湿地")


def test_must_visit_wrong_day(plan: Plan, req: Requirements) -> None:
    moved = req.model_copy(update={"must_visit": [MUST.model_copy(update={"day": 2})]})
    assert has(validate_plan(plan, moved), "指定第2天，实际在第1天")


def test_avoid_present(plan: Plan, req: Requirements) -> None:
    present = plan.days[0].items[0]
    more = req.model_copy(update={"avoid": [AVOID, PoiRef(poi_id=present.poi_id, name=present.name)]})
    assert has(validate_plan(plan, more), f"不去的景点 {present.name}")


# ---------------------------------------------------------------------------
# 6. 规划范围
# ---------------------------------------------------------------------------


def test_scope_excluded_present(plan: Plan, req: Requirements) -> None:
    present = plan.days[1].items[0].poi_id
    scope = PlanScope(mode="partial", affected_days=[2], excluded_poi_ids=[present])
    assert has(validate_plan(plan, req, scope=scope), "已排除的景点")


def test_scope_required_missing(plan: Plan, req: Requirements) -> None:
    scope = PlanScope(mode="partial", affected_days=[2], required=[PoiRef(poi_id="xixi", name="西溪湿地", day=2)])
    assert has(validate_plan(plan, req, scope=scope), "本次必须入选的景点 西溪湿地")


def test_scope_required_wrong_day(plan: Plan, req: Requirements) -> None:
    scope = PlanScope(mode="partial", affected_days=[2], required=[MUST.model_copy(update={"day": 2})])
    assert has(validate_plan(plan, req, scope=scope), "本次必须入选的景点 雷峰塔 指定第2天")


def test_scope_fixed_day_changed(plan: Plan, req: Requirements) -> None:
    scope = PlanScope(mode="partial", affected_days=[2], fixed_days=[1])
    assert validate_plan(plan, req, baseline=plan, scope=scope) == []
    baseline = copy(plan)
    baseline.days[0].items[-1].start_min += 5
    assert has(validate_plan(plan, req, baseline=baseline, scope=scope), "固定的第1天")
    assert has(validate_plan(plan, req, scope=scope), "缺少基准行程")


def test_partial_resolve_end_to_end(plan: Plan, req: Requirements) -> None:
    """局部重排第 2 天：排除一个原景点，固定第 1 天，组装后整体验算通过。"""
    day1 = plan.days[0]
    day2_ids = [it.poi_id for it in plan.days[1].items]
    removed = day2_ids[0]
    candidates = [i for i in SMALL_IDS + ["teamuseum", "hefang"] if i not in {it.poi_id for it in day1.items}]
    candidates = [i for i in dict.fromkeys(candidates) if i != removed]
    fixed_counts: dict[str, int] = {}
    for it in day1.items:
        fixed_counts[it.subtype] = fixed_counts.get(it.subtype, 0) + 1
    scope = PlanScope(
        mode="partial",
        affected_days=[2],
        fixed_days=[1],
        excluded_poi_ids=[removed],
        keep_bonus={pid: 2 for pid in day2_ids if pid != removed},
    )
    inp = make_input(
        candidates, req, [2],
        keep_bonus=scope.keep_bonus,
        fixed_subtype_counts=fixed_counts,
    )
    out = solve(inp)
    assert out.status == "ok"
    new_plan = assemble_plan(out, req, extra_days=[day1])
    assert validate_plan(new_plan, req, baseline=plan, scope=scope) == []
    kept = {v.poi_id for v in out.days[0].visits}
    assert set(scope.keep_bonus) <= kept
    assert removed not in kept
