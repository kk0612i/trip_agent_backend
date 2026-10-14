"""CP-SAT 行程求解器测试：必选、上限、午餐、替换、雨天、保留奖励、不可行、局部求解、时间压缩和规模。"""

import time

import pytest

from app.agent.trip.planning.rules import (
    LUNCH_EARLIEST_START,
    LUNCH_LATEST_START,
    LUNCH_MINUTES,
    LUNCH_WINDOW_END,
)
from app.agent.trip.planning.solver import solve
from app.agent.trip.planning.validate import validate_plan
from app.schemas.trip_plan_schema import (
    PoiRef,
    ReplaceSlot,
    Requirements,
    SolveDay,
    SolveInput,
    SolveOutput,
    TransportPref,
)
from tests.trip.planning_data import SMALL_IDS, SPOTS, assemble_plan, make_input, random_instance

OBJECTIVE_KEYS = {
    "visits", "value", "travel_minutes", "day_category_penalty",
    "subtype_penalty", "rain_penalty", "keep_bonus", "objective",
}


def ref(poi_id: str, day: int | None = None) -> PoiRef:
    """构造测试景点引用。"""
    return PoiRef(poi_id=poi_id, name=SPOTS[poi_id].name if poi_id in SPOTS else poi_id, day=day)


def day_of(out: SolveOutput, poi_id: str) -> int | None:
    """返回景点所在的天，未入选为空。"""
    for d in out.days:
        if any(v.poi_id == poi_id for v in d.visits):
            return d.day
    return None


def check_solution(inp: SolveInput, out: SolveOutput) -> None:
    """只依据求解输入核对输出的硬约束，适用于任意实例（含随机实例）。"""
    assert out.status == "ok"
    assert OBJECTIVE_KEYS <= set(out.objective)
    pois = {p.poi_id: p for p in inp.pois}
    days = {d.day: d for d in inp.days}
    assert [d.day for d in out.days] == [d.day for d in inp.days]
    seen: set[str] = set()
    for solved in out.days:
        limit = days[solved.day]
        visits = solved.visits
        assert len(visits) <= limit.max_items
        for v in visits:
            assert v.poi_id not in seen
            seen.add(v.poi_id)
            dur = pois[v.poi_id].duration_minutes
            assert 0 <= v.start_min
            assert v.end_min == v.start_min + dur + (LUNCH_MINUTES if v.lunch_inside else 0)
            assert v.end_min <= limit.end_limit_min
            if v.lunch_after:
                assert LUNCH_EARLIEST_START <= v.end_min <= LUNCH_LATEST_START
            if v.lunch_inside:
                assert v.start_min <= LUNCH_EARLIEST_START and v.end_min >= LUNCH_WINDOW_END
        for prev, cur in zip(visits, visits[1:]):
            leg = inp.travel[f"{prev.poi_id}|{cur.poi_id}"]
            gap = leg.minutes + (LUNCH_MINUTES if prev.lunch_after else 0)
            assert cur.start_min >= prev.end_min + gap
        lunches = sum(v.lunch_after or v.lunch_inside for v in visits)
        assert lunches <= 1
        if inp.lunch_enabled:
            spans = any(v.start_min < LUNCH_EARLIEST_START for v in visits) and any(
                v.end_min > LUNCH_WINDOW_END for v in visits
            )
            if spans:
                assert lunches == 1
        else:
            assert lunches == 0
    for r in inp.required:
        assert r.poi_id in seen


def test_small_instance_ok_and_validates() -> None:
    req = Requirements()
    inp = make_input(SMALL_IDS, req, [1, 2])
    out = solve(inp)
    check_solution(inp, out)
    # 8 个景点、两天各 5 个上限，全部值得入选
    assert out.objective["visits"] == 8
    assert out.objective["value"] == round(sum(SPOTS[i].value for i in SMALL_IDS) / 10)
    assert validate_plan(assemble_plan(out, req), req) == []


def test_required_with_day_and_without_day() -> None:
    req = Requirements()
    # 河坊街收益为负，不强制时不会入选；西溪湿地不指定天
    inp = make_input(
        SMALL_IDS + ["xixi"], req, [1, 2],
        poi_overrides={"hefang": {"value": -500}},
        required=[ref("hefang", 2), ref("xixi")],
    )
    out = solve(inp)
    check_solution(inp, out)
    assert day_of(out, "hefang") == 2
    assert day_of(out, "xixi") in (1, 2)


def test_required_poi_not_in_candidates_is_infeasible() -> None:
    req = Requirements()
    inp = make_input(SMALL_IDS, req, [1], required=[ref("lingyin")])
    assert solve(inp).status == "infeasible"


def test_daily_limits_respected() -> None:
    # 休闲节奏 + 第 1 天更轻松：第 1 天 (420, 3)，第 2 天 (510, 4)
    req = Requirements(pace="relaxed", day_loads={1: "lighter"})
    ids = SMALL_IDS + ["teamuseum", "lingyin"]
    inp = make_input(ids, req, [1, 2])
    out = solve(inp)
    check_solution(inp, out)
    by_day = {d.day: d.visits for d in out.days}
    assert len(by_day[1]) <= 3
    assert len(by_day[2]) <= 4
    assert all(v.end_min <= 420 for v in by_day[1])
    assert all(v.end_min <= 510 for v in by_day[2])
    assert validate_plan(assemble_plan(out, req), req) == []


def test_lunch_after_visit_when_day_spans_noon() -> None:
    req = Requirements()
    ids = ["duanqiao", "baidi", "zjmuseum"]
    overrides = {i: {"duration_minutes": 120} for i in ids}
    inp = make_input(ids, req, [1], poi_overrides=overrides, required=[ref(i) for i in ids])
    out = solve(inp)
    check_solution(inp, out)
    visits = out.days[0].visits
    # 三个 120 分钟景点：推迟到 11:30 开始可以免午餐，但第二阶段会压缩到最早，
    # 即第一个景点 9:30 开始、11:30 结束后午餐
    after = [v for v in visits if v.lunch_after]
    assert len(after) == 1
    assert after[0] is visits[0]
    assert visits[0].start_min == LUNCH_EARLIEST_START - 120
    assert not any(v.lunch_inside for v in visits)
    lunch_pos = visits.index(after[0])
    assert lunch_pos < len(visits) - 1
    nxt = visits[lunch_pos + 1]
    leg = inp.travel[f"{after[0].poi_id}|{nxt.poi_id}"]
    assert nxt.start_min >= after[0].end_min + LUNCH_MINUTES + leg.minutes
    plan = assemble_plan(out, req, durations={i: 120 for i in ids})
    assert validate_plan(plan, req) == []


def test_lunch_inside_long_visit() -> None:
    # 休闲且更轻松：当天最晚 420 结束；宋城 300 分钟必须上午开始并跨午餐，只能园内用餐
    req = Requirements(pace="relaxed", day_loads={1: "lighter"})
    inp = make_input(["songcheng"], req, [1], required=[ref("songcheng")])
    out = solve(inp)
    check_solution(inp, out)
    visit = out.days[0].visits[0]
    assert visit.lunch_inside
    assert visit.end_min - visit.start_min == 300 + LUNCH_MINUTES
    assert visit.start_min == 0
    assert validate_plan(assemble_plan(out, req), req) == []


def test_lunch_disabled_has_no_lunch() -> None:
    req = Requirements()
    inp = make_input(SMALL_IDS, req, [1, 2], lunch_enabled=False)
    out = solve(inp)
    check_solution(inp, out)


def test_replace_slot_by_tags_uses_new_pois_only() -> None:
    req = Requirements()
    ids = SMALL_IDS + ["teamuseum"]
    # 茶叶博物馆收益为负，只有替换约束会让它入选；浙博不在“新”景点中，不计入
    inp = make_input(
        ids, req, [1, 2],
        poi_overrides={"teamuseum": {"value": -300}},
        replace_slots=[ReplaceSlot(day=1, tags=["museum", "indoor"], count=1)],
        new_poi_ids=["teamuseum", "hefang"],
    )
    out = solve(inp)
    check_solution(inp, out)
    assert day_of(out, "teamuseum") == 1


def test_replace_slot_by_poi_id() -> None:
    req = Requirements()
    inp = make_input(
        SMALL_IDS, req, [1, 2],
        poi_overrides={"jingci": {"value": -300}},
        replace_slots=[ReplaceSlot(day=2, poi_id="jingci", count=1)],
    )
    out = solve(inp)
    check_solution(inp, out)
    assert day_of(out, "jingci") == 2


def test_replace_slot_without_match_is_infeasible() -> None:
    req = Requirements()
    inp = make_input(
        SMALL_IDS, req, [1],
        replace_slots=[ReplaceSlot(day=1, tags=["church"], count=1)],
        new_poi_ids=SMALL_IDS,
    )
    assert solve(inp).status == "infeasible"


def test_rain_moves_outdoor_to_dry_day() -> None:
    req = Requirements()
    ids = ["xixi", "zjmuseum", "teamuseum"]
    inp = make_input(ids, req, [1, 2], rain={1: 3})
    out = solve(inp)
    check_solution(inp, out)
    assert day_of(out, "xixi") == 2
    assert out.objective["rain_penalty"] == 0

    # 两天都下雨时仍会去，但要计入惩罚：户外 240 分钟 → 3 档 × 等级 3 × 权重 3
    both = make_input(["xixi"], req, [1, 2], rain={1: 3, 2: 3}, required=[ref("xixi")])
    out_both = solve(both)
    assert out_both.objective["rain_penalty"] == 27


def test_keep_bonus_keeps_original_poi() -> None:
    req = Requirements()
    base = make_input(["leifeng", "jingci"], req, [1])
    one_item = [SolveDay(day=1, max_items=1)]
    plain = solve(base.model_copy(update={"days": one_item}))
    assert [v.poi_id for v in plain.days[0].visits] == ["leifeng"]

    kept = solve(base.model_copy(update={"days": one_item, "keep_bonus": {"jingci": 1}}))
    assert [v.poi_id for v in kept.days[0].visits] == ["jingci"]
    assert kept.objective["keep_bonus"] == 8

    # 原所在天不在本次求解范围时不生效
    other_day = solve(base.model_copy(update={"days": one_item, "keep_bonus": {"jingci": 2}}))
    assert [v.poi_id for v in other_day.days[0].visits] == ["leifeng"]


def test_walk_only_makes_far_required_pair_infeasible() -> None:
    must = [ref("lingyin", 1), ref("hefang", 1)]
    walk_req = Requirements(transport=TransportPref(walk_only=True, max_walk_minutes=40))
    inp = make_input(["lingyin", "hefang"], walk_req, [1], required=must)
    assert inp.travel == {}
    assert solve(inp).status == "infeasible"

    taxi_req = Requirements()
    ok = solve(make_input(["lingyin", "hefang"], taxi_req, [1], required=must))
    assert ok.status == "ok"
    assert len(ok.days[0].visits) == 2


def test_fixed_subtype_counts_in_partial_solve() -> None:
    req = Requirements()
    overrides = {"zjmuseum": {"value": 100}, "jingci": {"value": 90}}
    base = make_input(["zjmuseum", "jingci"], req, [3], poi_overrides=overrides)
    only_day3 = [SolveDay(day=3, max_items=1)]

    plain = solve(base.model_copy(update={"days": only_day3}))
    assert [v.poi_id for v in plain.days[0].visits] == ["zjmuseum"]
    assert plain.days[0].day == 3

    # 固定天已有 2 个博物馆，再选博物馆会超出免罚数量
    fixed = solve(base.model_copy(update={"days": only_day3, "fixed_subtype_counts": {"museum": 2}}))
    assert [v.poi_id for v in fixed.days[0].visits] == ["jingci"]
    assert fixed.objective["subtype_penalty"] == 0

    # 固定天本身已超出时，惩罚常数计入但不影响选择
    heavy = solve(base.model_copy(update={"days": only_day3, "fixed_subtype_counts": {"temple": 4}}))
    assert [v.poi_id for v in heavy.days[0].visits] == ["zjmuseum"]
    assert heavy.objective["subtype_penalty"] == 3 * 2


def test_day_category_penalty_spreads_categories() -> None:
    req = Requirements()
    # 两个湖景（nature）与两个文化类，每天 2 个：同类分到不同天可避免每日集中惩罚
    ids = ["duanqiao", "baidi", "zjmuseum", "teamuseum"]
    inp = make_input(ids, req, [1, 2])
    inp = inp.model_copy(update={"days": [SolveDay(day=1, max_items=2), SolveDay(day=2, max_items=2)]})
    out = solve(inp)
    check_solution(inp, out)
    assert out.objective["day_category_penalty"] == 0
    assert day_of(out, "duanqiao") != day_of(out, "baidi")


def test_phase_two_compacts_schedule() -> None:
    req = Requirements()
    ids = ["leifeng", "jingci", "huagang", "hefang"]
    inp = make_input(ids, req, [1], lunch_enabled=False)
    out = solve(inp)
    check_solution(inp, out)
    visits = out.days[0].visits
    assert len(visits) == 4
    assert visits[0].start_min == 0
    # 不安排午餐时，每段衔接都没有多余空档
    for prev, cur in zip(visits, visits[1:]):
        assert cur.start_min == prev.end_min + inp.travel[f"{prev.poi_id}|{cur.poi_id}"].minutes


def test_empty_candidates() -> None:
    req = Requirements()
    inp = make_input([], req, [1, 2])
    out = solve(inp)
    assert out.status == "ok"
    assert [d.visits for d in out.days] == [[], []]


def test_duplicate_poi_ids_rejected() -> None:
    req = Requirements()
    inp = make_input(["leifeng"], req, [1])
    inp = inp.model_copy(update={"pois": inp.pois * 2})
    with pytest.raises(ValueError):
        solve(inp)


@pytest.mark.parametrize("seed", [1, 2])
def test_scale_seven_days_45_candidates(seed: int) -> None:
    inp = random_instance(7, 45, seed=seed, time_limit=10)
    started = time.monotonic()
    out = solve(inp)
    elapsed = time.monotonic() - started
    check_solution(inp, out)
    # 每天至少 2 个景点才算有意义的可行解（空行程也可行）
    assert out.objective["visits"] >= 14
    # 第一阶段 10 秒、第二阶段至多 10/3 秒，另留建模开销
    assert elapsed < 10 + 10 / 3 + 3
