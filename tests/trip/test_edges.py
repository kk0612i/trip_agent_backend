"""图路由测试：根图解析后的去向，规划子图求解后的扩大搜索、放宽范围、诊断和校验。"""

import pytest

from app.agent.trip.planning.planning_edges import SLOT_SPARE_CANDIDATES, UNDERFILLED_ITEMS, route_after_solve
from app.agent.trip.planning.planning_nodes import MAX_SEARCH_ROUNDS
from app.agent.trip.trip_edges import route_after_resolve
from app.schemas.trip_plan_schema import (
    PlanScope,
    PoiProfile,
    ReplaceSlot,
    SolvedDay,
    SolvedVisit,
    SolveOutput,
)
from tests.trip.draft_data import draft_plan

# ---------------------------------------------------------------------------
# 根图
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("route", "expected"),
    [("plan", "plan"), ("qa", "qa"), ("reply", "render"), ("undo", "render"), (None, "render")],
)
def test_route_after_resolve(route: str | None, expected: str) -> None:
    """规划和问答各走对应节点，其余（直接回复、撤销、缺失）都去渲染。"""
    state = {} if route is None else {"route": route}
    assert route_after_resolve(state) == expected


# ---------------------------------------------------------------------------
# 规划子图
# ---------------------------------------------------------------------------

MUSEUM_SLOT = ReplaceSlot(day=2, tags=["museum"], count=1)


def museum(pid: str) -> PoiProfile:
    """博物馆属性。"""
    return PoiProfile(poi_id=pid, subtype="museum", indoor="indoor", duration_minutes=90, source="rule")


def temple(pid: str) -> PoiProfile:
    """寺庙属性。"""
    return PoiProfile(poi_id=pid, subtype="temple", indoor="mixed", duration_minutes=90, source="rule")


def output(status: str = "infeasible", counts: dict[int, int] | None = None) -> SolveOutput:
    """构造求解结果，counts 为每天的访问数。"""
    days = [
        SolvedDay(day=day, visits=[SolvedVisit(poi_id=f"v{day}-{n}", start_min=0, end_min=60) for n in range(count)])
        for day, count in (counts or {}).items()
    ]
    return SolveOutput(status=status, days=days)


def partial(slots: list[ReplaceSlot] | None = None, refill: list[int] | None = None) -> PlanScope:
    """第 2 天受影响的局部范围。"""
    return PlanScope(
        mode="partial", affected_days=[2], fixed_days=[1, 3], replace_slots=slots or [], allow_refill_days=refill or [],
    )


def state(
    scope: PlanScope,
    solve_output: SolveOutput,
    *,
    profiles: dict[str, PoiProfile] | None = None,
    candidate_ids: list[str] | None = None,
    search_round: int = 0,
    widen_level: int = 0,
    force_full: bool = False,
    with_baseline: bool = True,
) -> dict:
    """手工构造规划子图状态。"""
    profiles = profiles or {}
    return {
        "scope": scope,
        "solve_output": solve_output,
        "baseline": draft_plan() if with_baseline else None,
        "profiles": profiles,
        "candidate_ids": list(profiles) if candidate_ids is None else candidate_ids,
        "search_round": search_round,
        "widen_level": widen_level,
        "force_full": force_full,
    }


def museums(n: int) -> dict[str, PoiProfile]:
    """n 个新的博物馆候选。"""
    return {f"m{i}": museum(f"m{i}") for i in range(n)}


def test_constants() -> None:
    """路由依赖的阈值。"""
    assert MAX_SEARCH_ROUNDS == 2
    assert UNDERFILLED_ITEMS == 2
    assert SLOT_SPARE_CANDIDATES == 2


# 成功
def test_ok_goes_to_validate() -> None:
    """求解成功且补位天不空时进入校验。"""
    assert route_after_solve(state(partial(refill=[2]), output("ok", {2: 2}))) == "validate"


def test_ok_without_refill_days_ignores_counts() -> None:
    """不允许补位的天即使景点少也直接校验。"""
    assert route_after_solve(state(partial(), output("ok", {2: 1}))) == "validate"


def test_ok_underfilled_expands() -> None:
    """允许补位的天少于 2 个景点时扩大搜索。"""
    assert route_after_solve(state(partial(refill=[2]), output("ok", {2: 1}))) == "expand"
    # 求解结果中缺少该天也视为过空
    assert route_after_solve(state(partial(refill=[2]), output("ok", {}))) == "expand"


def test_ok_underfilled_after_max_rounds_validates() -> None:
    """搜索轮次用完后即使过空也进入校验。"""
    st = state(partial(refill=[2]), output("ok", {2: 1}), search_round=MAX_SEARCH_ROUNDS)
    assert route_after_solve(st) == "validate"


def test_full_ok_underfilled_expands() -> None:
    """全量规划成功但有天过空时同样扩大搜索。"""
    scope = PlanScope(mode="full", affected_days=[1, 2], allow_refill_days=[1, 2])
    assert route_after_solve(state(scope, output("ok", {1: 3, 2: 1}), search_round=1)) == "expand"


# 替换约束的同类候选
def test_slot_short_expands() -> None:
    """替换约束的同类新候选少于“数量 + 2”时先扩大搜索。"""
    st = state(partial([MUSEUM_SLOT]), output(), profiles={**museums(2), "t1": temple("t1")})
    assert route_after_solve(st) == "expand"


def test_slot_enough_widens() -> None:
    """同类新候选充足时直接放宽范围。"""
    st = state(partial([MUSEUM_SLOT]), output(), profiles=museums(3))
    assert route_after_solve(st) == "widen"


def test_slot_count_raises_threshold() -> None:
    """数量下限越大需要的同类候选越多。"""
    slot = ReplaceSlot(day=2, tags=["museum"], count=2)
    assert route_after_solve(state(partial([slot]), output(), profiles=museums(3))) == "expand"
    assert route_after_solve(state(partial([slot]), output(), profiles=museums(4))) == "widen"


def test_slot_baseline_pois_not_new() -> None:
    """原草稿中的景点不算新候选。"""
    # zjmuseum 是原草稿第 1 天的博物馆
    profiles = {**museums(2), "zjmuseum": museum("zjmuseum")}
    assert route_after_solve(state(partial([MUSEUM_SLOT]), output(), profiles=profiles)) == "expand"
    # 没有原草稿时同一景点算新候选
    st = state(partial([MUSEUM_SLOT]), output(), profiles=profiles, with_baseline=False)
    assert route_after_solve(st) == "widen"


def test_slot_counts_only_selected_candidates_with_profiles() -> None:
    """只统计本次候选集中且已有属性的景点。"""
    profiles = museums(4)
    st = state(partial([MUSEUM_SLOT]), output(), profiles=profiles, candidate_ids=["m0", "m1", "x1", "x2"])
    assert route_after_solve(st) == "expand"


def test_slot_tag_match_uses_indoor_and_main_category() -> None:
    """标签按细分、主类和室内外匹配，室内外结合算户外。"""
    slot = ReplaceSlot(day=2, tags=["religion", "outdoor"], count=1)
    profiles = {f"t{i}": temple(f"t{i}") for i in range(3)}
    assert route_after_solve(state(partial([slot]), output(), profiles=profiles)) == "widen"
    indoor_slot = ReplaceSlot(day=2, tags=["religion", "indoor"], count=1)
    assert route_after_solve(state(partial([indoor_slot]), output(), profiles=profiles)) == "expand"


def test_slot_any_short_slot_expands() -> None:
    """多个替换约束中有一个同类候选不足就扩大搜索。"""
    slots = [MUSEUM_SLOT, ReplaceSlot(day=2, tags=["temple"], count=1)]
    st = state(partial(slots), output(), profiles={**museums(3), "t1": temple("t1")})
    assert route_after_solve(st) == "expand"


def test_slot_with_poi_ignored() -> None:
    """指定景点的替换约束不参与同类候选判断。"""
    slot = ReplaceSlot(day=2, poi_id="teamuseum", count=1)
    assert route_after_solve(state(partial([slot]), output())) == "widen"


def test_slot_empty_tags_match_everything() -> None:
    """不限标签的替换约束按全部新候选计数。"""
    slot = ReplaceSlot(day=2, count=1)
    profiles = {"m0": museum("m0"), "t0": temple("t0"), "t1": temple("t1")}
    assert route_after_solve(state(partial([slot]), output(), profiles=profiles)) == "widen"


def test_slot_short_after_widen_does_not_expand() -> None:
    """已经放宽过范围后不再因同类候选不足扩大搜索。"""
    st = state(partial([MUSEUM_SLOT]), output(), profiles=museums(1), widen_level=1)
    assert route_after_solve(st) == "widen"


def test_slot_short_without_rounds_left_widens() -> None:
    """搜索轮次用完时同类候选不足也只能放宽范围。"""
    st = state(partial([MUSEUM_SLOT]), output(), profiles=museums(1), search_round=MAX_SEARCH_ROUNDS)
    assert route_after_solve(st) == "widen"


def test_timeout_treated_as_failure() -> None:
    """求解超时与无解同样处理。"""
    st = state(partial([MUSEUM_SLOT]), output("timeout"), profiles=museums(1))
    assert route_after_solve(st) == "expand"


# 局部放宽
def test_partial_failure_widens_until_max_level() -> None:
    """局部无解时逐级放宽，到 2 级后诊断。"""
    assert route_after_solve(state(partial(), output(), widen_level=0)) == "widen"
    assert route_after_solve(state(partial(), output(), widen_level=1)) == "widen"
    assert route_after_solve(state(partial(), output(), widen_level=2)) == "diagnose"


def test_partial_force_full_failure_diagnoses() -> None:
    """用户已允许全量重排仍无解时直接诊断。"""
    assert route_after_solve(state(partial(), output(), force_full=True)) == "diagnose"


# 全量
def test_full_failure_expands_once_then_diagnoses() -> None:
    """全量无解时只多搜一轮，之后诊断。"""
    scope = PlanScope(mode="full", affected_days=[1, 2, 3])
    assert route_after_solve(state(scope, output(), search_round=0)) == "expand"
    assert route_after_solve(state(scope, output(), search_round=1)) == "diagnose"
    assert route_after_solve(state(scope, output(), search_round=2)) == "diagnose"
