"""规划范围测试：全量范围、各类修改操作的受影响天、逐级放宽、补位与每日上限。"""

import pytest

from app.agent.trip.planning.scope import MAX_WIDEN_LEVEL, REFILL_MIN_FREED_MINUTES, full_scope, partial_scope
from app.schemas.trip_plan_schema import (
    AddPoiOp,
    MoveItemsOp,
    Plan,
    PoiRef,
    RemoveItemsOp,
    ReplaceItemsOp,
    ReplaceSlot,
    ReplaceTarget,
    Requirements,
    SetDayLoadOp,
    SetDurationOp,
    SwapItemsOp,
)
from tests.trip.draft_data import draft_plan, draft_requirements

HEFANG = PoiRef(poi_id="hefang", name="河坊街")
TEA = PoiRef(poi_id="teamuseum", name="中国茶叶博物馆")


@pytest.fixture
def baseline() -> Plan:
    """三天草稿：第 1 天断桥、白堤、浙博；第 2 天雷峰塔、净慈寺、花港；第 3 天灵隐寺、岳王庙。"""
    return draft_plan()


def required_map(scope) -> dict[str, int | None]:
    """把必选约束转为 POI 到指定天的映射。"""
    return {ref.poi_id: ref.day for ref in scope.required}


# ---------------------------------------------------------------------------
# 全量范围
# ---------------------------------------------------------------------------


def test_full_scope_all_days_and_refill(baseline: Plan) -> None:
    """全量范围包含全部天，所有天都允许补位，没有固定天。"""
    scope = full_scope(draft_requirements(), baseline, stability=True)
    assert scope.mode == "full"
    assert scope.affected_days == [1, 2, 3]
    assert scope.allow_refill_days == [1, 2, 3]
    assert scope.fixed_days == []
    assert scope.stability is True


def test_full_scope_stability_keeps_baseline_days(baseline: Plan) -> None:
    """启用稳定性时原草稿每个景点都有保留奖励，指向原所在天。"""
    scope = full_scope(draft_requirements(), baseline, stability=True)
    assert scope.keep_bonus == {
        "duanqiao": 1, "baidi": 1, "zjmuseum": 1, "leifeng": 2, "jingci": 2, "huagang": 2, "lingyin": 3, "yuewang": 3,
    }


def test_full_scope_without_stability_has_no_keep_bonus(baseline: Plan) -> None:
    """未启用稳定性或没有原草稿时不给保留奖励。"""
    assert full_scope(draft_requirements(), baseline, stability=False).keep_bonus == {}
    assert full_scope(draft_requirements(), None, stability=True).keep_bonus == {}


def test_full_scope_drops_baseline_days_beyond_new_length(baseline: Plan) -> None:
    """天数缩短后，超出新天数的原景点不再给保留奖励。"""
    scope = full_scope(draft_requirements(days=2), baseline, stability=True)
    assert scope.affected_days == [1, 2]
    assert "lingyin" not in scope.keep_bonus
    assert "yuewang" not in scope.keep_bonus


def test_full_scope_excludes_avoid_and_requires_must_visit() -> None:
    """不去的景点进入排除列表，必去景点进入必选；指定天超出行程时不限定天。"""
    req = draft_requirements(
        days=2,
        avoid=[HEFANG],
        must_visit=[PoiRef(poi_id="leifeng", name="雷峰塔", day=2), PoiRef(poi_id="lingyin", name="灵隐寺", day=5)],
    )
    scope = full_scope(req, None, stability=False)
    assert scope.excluded_poi_ids == ["hefang"]
    assert required_map(scope) == {"leifeng": 2, "lingyin": None}


def test_full_scope_defaults_to_one_day_without_days() -> None:
    """需求未给天数时按 1 天处理。"""
    scope = full_scope(Requirements(), None, stability=False)
    assert scope.affected_days == [1]


# ---------------------------------------------------------------------------
# 删除
# ---------------------------------------------------------------------------


def test_remove_affects_only_its_day(baseline: Plan) -> None:
    """删除只影响所在天，其他天固定；被删景点排除。"""
    scope = partial_scope(draft_requirements(), baseline, [RemoveItemsOp(item_ids=["D1-2"])])
    assert scope.mode == "partial"
    assert scope.affected_days == [1]
    assert scope.fixed_days == [2, 3]
    assert scope.excluded_poi_ids == ["baidi"]
    assert scope.stability is True


def test_remove_level0_pins_remaining_items(baseline: Plan) -> None:
    """0 级时受影响天未被操作的原景点固定在原天，并有保留奖励。"""
    scope = partial_scope(draft_requirements(), baseline, [RemoveItemsOp(item_ids=["D1-2"])])
    assert required_map(scope) == {"duanqiao": 1, "zjmuseum": 1}
    assert scope.keep_bonus == {"duanqiao": 1, "zjmuseum": 1}
    assert [ref.name for ref in scope.required] == ["断桥残雪", "浙江省博物馆孤山馆区"]


def test_remove_frees_minutes_and_allows_refill(baseline: Plan) -> None:
    """删除 60 分钟的景点刚好达到补位门槛，当天允许补位且不设景点数上限。"""
    scope = partial_scope(draft_requirements(), baseline, [RemoveItemsOp(item_ids=["D1-2"])])
    assert REFILL_MIN_FREED_MINUTES == 60
    assert scope.freed_minutes == {1: 60}
    assert scope.allow_refill_days == [1]
    assert scope.day_max_items == {}


def test_remove_short_item_does_not_refill() -> None:
    """空出不足 60 分钟时不补位，当天景点数上限为留下的景点数。"""
    baseline = draft_plan(durations={"baidi": 45})
    scope = partial_scope(draft_requirements(), baseline, [RemoveItemsOp(item_ids=["D1-2"])])
    assert scope.freed_minutes == {1: 45}
    assert scope.allow_refill_days == []
    assert scope.day_max_items == {1: 2}


def test_remove_multiple_items_sums_freed_minutes() -> None:
    """同一天删除多个景点时空出时间累加，合计达到门槛即可补位。"""
    baseline = draft_plan(durations={"duanqiao": 30, "baidi": 40})
    scope = partial_scope(draft_requirements(), baseline, [RemoveItemsOp(item_ids=["D1-1", "D1-2"])])
    assert scope.freed_minutes == {1: 70}
    assert scope.allow_refill_days == [1]
    assert required_map(scope) == {"zjmuseum": 1}


def test_remove_on_two_days_affects_both(baseline: Plan) -> None:
    """删除分布在两天的景点时两天都受影响，各自计算空出时间。"""
    scope = partial_scope(draft_requirements(), baseline, [RemoveItemsOp(item_ids=["D1-2", "D3-1"])])
    assert scope.affected_days == [1, 3]
    assert scope.fixed_days == [2]
    assert scope.freed_minutes == {1: 60, 3: 180}
    assert scope.excluded_poi_ids == ["baidi", "lingyin"]


def test_excluded_includes_avoid_and_skips_avoided_baseline_items(baseline: Plan) -> None:
    """避开列表中的景点被排除，即使仍在受影响天的草稿中也不再保留或固定。"""
    req = draft_requirements(avoid=[PoiRef(poi_id="zjmuseum", name="浙江省博物馆孤山馆区"), HEFANG])
    scope = partial_scope(req, baseline, [RemoveItemsOp(item_ids=["D1-2"])])
    assert scope.excluded_poi_ids == ["baidi", "hefang", "zjmuseum"]
    assert scope.keep_bonus == {"duanqiao": 1}
    assert required_map(scope) == {"duanqiao": 1}


# ---------------------------------------------------------------------------
# 替换
# ---------------------------------------------------------------------------


def test_replace_with_tags_creates_slot(baseline: Plan) -> None:
    """按标签替换生成当天的数量约束，不空出时间也不补位，上限为留下的加替换数量。"""
    op = ReplaceItemsOp(item_ids=["D2-2"], target=ReplaceTarget(tags=["museum"]))
    scope = partial_scope(draft_requirements(), baseline, [op])
    assert scope.affected_days == [2]
    assert scope.fixed_days == [1, 3]
    assert scope.replace_slots == [ReplaceSlot(day=2, tags=["museum"], count=1)]
    assert scope.excluded_poi_ids == ["jingci"]
    assert scope.freed_minutes == {}
    assert scope.allow_refill_days == []
    assert scope.day_max_items == {2: 3}
    assert required_map(scope) == {"leifeng": 2, "huagang": 2}


def test_replace_count_larger_adds_to_first_day(baseline: Plan) -> None:
    """替换后数量多于被替换数时，差额加到首个出现的天。"""
    op = ReplaceItemsOp(item_ids=["D2-2", "D3-2"], target=ReplaceTarget(count=3))
    scope = partial_scope(draft_requirements(), baseline, [op])
    assert scope.replace_slots == [ReplaceSlot(day=2, count=2), ReplaceSlot(day=3, count=1)]
    assert scope.day_max_items == {2: 4, 3: 2}


def test_replace_count_smaller_cuts_in_order(baseline: Plan) -> None:
    """替换后数量少于被替换数时，按出现顺序从各天扣减，扣到 0 的天不生成约束。"""
    op = ReplaceItemsOp(item_ids=["D2-2", "D3-2"], target=ReplaceTarget(count=1))
    scope = partial_scope(draft_requirements(), baseline, [op])
    assert scope.replace_slots == [ReplaceSlot(day=3, count=1)]
    assert scope.day_max_items == {2: 2, 3: 2}


def test_replace_with_poi_pins_target_and_softens_day(baseline: Plan) -> None:
    """指定替换景点时不生成数量约束，目标固定到被替换项所在天，当天原景点改为软保留。"""
    op = ReplaceItemsOp(item_ids=["D2-2"], target=ReplaceTarget(poi=TEA))
    scope = partial_scope(draft_requirements(), baseline, [op])
    assert scope.replace_slots == []
    assert required_map(scope) == {"teamuseum": 2}
    assert scope.required[0].name == "中国茶叶博物馆"
    assert scope.keep_bonus == {"leifeng": 2, "huagang": 2}
    assert scope.day_max_items == {2: 3}


# ---------------------------------------------------------------------------
# 新增
# ---------------------------------------------------------------------------


def test_add_with_day_affects_that_day(baseline: Plan) -> None:
    """指定天新增时只影响该天，新景点固定到该天，原景点软保留不固定。"""
    scope = partial_scope(draft_requirements(), baseline, [AddPoiOp(poi=HEFANG.model_copy(update={"day": 3}))])
    assert scope.affected_days == [3]
    assert scope.fixed_days == [1, 2]
    assert required_map(scope) == {"hefang": 3}
    assert scope.required[0].name == "河坊街"
    assert scope.keep_bonus == {"lingyin": 3, "yuewang": 3}
    assert scope.day_max_items == {3: 3}


def test_add_without_day_affects_all_days(baseline: Plan) -> None:
    """未指定天新增时全部天参与求解，新景点不限定天。"""
    scope = partial_scope(draft_requirements(), baseline, [AddPoiOp(poi=HEFANG)])
    assert scope.affected_days == [1, 2, 3]
    assert scope.fixed_days == []
    assert required_map(scope) == {"hefang": None}
    assert len(scope.keep_bonus) == 8
    # 不限定天的新景点不计入任何一天的新增数量
    assert scope.day_max_items == {1: 3, 2: 3, 3: 2}


def test_must_visit_stays_pinned_on_soft_day(baseline: Plan) -> None:
    """必去景点即使在软保留的天也固定在原天。"""
    req = draft_requirements(must_visit=[PoiRef(poi_id="lingyin", name="灵隐寺")])
    scope = partial_scope(req, baseline, [AddPoiOp(poi=HEFANG.model_copy(update={"day": 3}))])
    assert required_map(scope) == {"hefang": 3, "lingyin": 3}


# ---------------------------------------------------------------------------
# 移动与交换
# ---------------------------------------------------------------------------


def test_move_affects_source_and_target(baseline: Plan) -> None:
    """移动影响原天和目标天，移出的景点固定到目标天，原天空出时间。"""
    scope = partial_scope(draft_requirements(), baseline, [MoveItemsOp(item_ids=["D1-1"], to_day=3)])
    assert scope.affected_days == [1, 3]
    assert scope.fixed_days == [2]
    assert scope.excluded_poi_ids == []
    assert scope.freed_minutes == {1: 60}
    assert scope.allow_refill_days == [1]
    # 原天未软化，留下的景点固定；目标天软化，原景点不固定
    assert required_map(scope) == {"duanqiao": 3, "baidi": 1, "zjmuseum": 1}
    assert scope.keep_bonus == {"baidi": 1, "zjmuseum": 1, "lingyin": 3, "yuewang": 3}
    assert scope.day_max_items == {3: 3}


def test_move_to_same_day_is_noop(baseline: Plan) -> None:
    """移动到原所在天不产生影响，所有天固定。"""
    scope = partial_scope(draft_requirements(), baseline, [MoveItemsOp(item_ids=["D1-1"], to_day=1)])
    assert scope.affected_days == []
    assert scope.fixed_days == [1, 2, 3]
    assert scope.required == []


def test_swap_exchanges_days(baseline: Plan) -> None:
    """交换后两个景点分别固定到对方所在天，两天都软保留且不补位。"""
    scope = partial_scope(draft_requirements(), baseline, [SwapItemsOp(a="D1-2", b="D3-2")])
    assert scope.affected_days == [1, 3]
    assert required_map(scope) == {"baidi": 3, "yuewang": 1}
    assert scope.keep_bonus == {"duanqiao": 1, "zjmuseum": 1, "lingyin": 3}
    assert scope.freed_minutes == {}
    assert scope.allow_refill_days == []
    assert scope.day_max_items == {1: 3, 3: 2}


# ---------------------------------------------------------------------------
# 时长与松紧
# ---------------------------------------------------------------------------


def test_set_duration_pins_item_on_its_day(baseline: Plan) -> None:
    """调整时长的景点固定在原天并软化当天，其本身不计保留奖励。"""
    scope = partial_scope(draft_requirements(), baseline, [SetDurationOp(item_id="D2-1", minutes=150)])
    assert scope.affected_days == [2]
    assert required_map(scope) == {"leifeng": 2}
    assert scope.keep_bonus == {"jingci": 2, "huagang": 2}
    assert scope.freed_minutes == {}
    assert scope.day_max_items == {2: 3}


def test_day_load_lighter_caps_items(baseline: Plan) -> None:
    """更轻松时当天软保留、不补位，上限为原景点数。"""
    scope = partial_scope(draft_requirements(), baseline, [SetDayLoadOp(day=2, level="lighter")])
    assert scope.affected_days == [2]
    assert scope.required == []
    assert scope.allow_refill_days == []
    assert scope.day_max_items == {2: 3}


def test_day_load_fuller_allows_refill(baseline: Plan) -> None:
    """更充实时当天即使没有空出时间也允许补位。"""
    scope = partial_scope(draft_requirements(), baseline, [SetDayLoadOp(day=2, level="fuller")])
    assert scope.allow_refill_days == [2]
    assert scope.day_max_items == {}
    assert scope.freed_minutes == {}


# ---------------------------------------------------------------------------
# 逐级放宽
# ---------------------------------------------------------------------------


def test_max_widen_level_is_two() -> None:
    """自动放宽最多到 2 级，3 级只在用户同意全量重排时使用。"""
    assert MAX_WIDEN_LEVEL == 2


def test_level1_softens_affected_days(baseline: Plan) -> None:
    """1 级时原景点改为软保留：保留奖励不变，不再固定。"""
    scope = partial_scope(draft_requirements(), baseline, [RemoveItemsOp(item_ids=["D1-2"])], level=1)
    assert scope.affected_days == [1]
    assert scope.required == []
    assert scope.keep_bonus == {"duanqiao": 1, "zjmuseum": 1}


def test_level1_keeps_must_visit_pinned(baseline: Plan) -> None:
    """1 级时必去景点仍固定在原天。"""
    req = draft_requirements(must_visit=[PoiRef(poi_id="zjmuseum", name="浙江省博物馆孤山馆区")])
    scope = partial_scope(req, baseline, [RemoveItemsOp(item_ids=["D1-2"])], level=1)
    assert required_map(scope) == {"zjmuseum": 1}


def test_level2_adds_adjacent_days(baseline: Plan) -> None:
    """2 级时纳入相邻的天，相邻天原景点软保留且以原景点数为上限。"""
    scope = partial_scope(draft_requirements(), baseline, [RemoveItemsOp(item_ids=["D1-2"])], level=2)
    assert scope.affected_days == [1, 2]
    assert scope.fixed_days == [3]
    assert scope.required == []
    assert scope.keep_bonus == {"duanqiao": 1, "zjmuseum": 1, "leifeng": 2, "jingci": 2, "huagang": 2}
    assert scope.allow_refill_days == [1]
    assert scope.day_max_items == {2: 3}


def test_level2_middle_day_widens_both_sides(baseline: Plan) -> None:
    """中间一天受影响时 2 级纳入前后两天。"""
    scope = partial_scope(draft_requirements(), baseline, [RemoveItemsOp(item_ids=["D2-2"])], level=2)
    assert scope.affected_days == [1, 2, 3]
    assert scope.fixed_days == []


def test_level3_forces_full(baseline: Plan) -> None:
    """3 级时所有天参与求解，原景点软保留，被删景点仍排除。"""
    scope = partial_scope(draft_requirements(), baseline, [RemoveItemsOp(item_ids=["D1-2"])], level=3)
    assert scope.mode == "partial"
    assert scope.affected_days == [1, 2, 3]
    assert scope.fixed_days == []
    assert scope.required == []
    assert scope.excluded_poi_ids == ["baidi"]
    assert len(scope.keep_bonus) == 7
    assert scope.freed_minutes == {1: 60}
