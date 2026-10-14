"""候选集构建测试：景区嵌套去重、名称清理和按评分筛选候选。"""

from types import SimpleNamespace

from app.agent.trip.planning.planning_nodes import (
    NESTED_AREA_MIN_CHILDREN,
    _clean_name,
    prune_nested,
    select_candidate_ids,
)
from app.schemas.trip_plan_schema import (
    MAX_CANDIDATES,
    MIN_CANDIDATE_RATING,
    AmapPoi,
    PlanScope,
    PoiProfile,
    PoiRef,
    ReplaceSlot,
)


def poi(pid: str, rating: float | None = 4.6, parent: str | None = None, name: str | None = None) -> AmapPoi:
    """构造杭州景点。"""
    return AmapPoi(
        poi_id=pid, name=name or pid, lng=120.15, lat=30.25, city_adcode="330100", rating=rating, parent_id=parent,
    )


def state(pois: list[AmapPoi], scope: PlanScope | None = None, hits: dict[str, int] | None = None, baseline=None):
    """构造只含候选筛选所需字段的规划状态。"""
    return {
        "scope": scope or PlanScope(mode="full", affected_days=[1, 2, 3]),
        "pois": {p.poi_id: p for p in pois},
        "template_hits": hits or {},
        "baseline": baseline,
    }


def baseline(days: dict[int, list[str]]):
    """构造只含天和景点标识的草稿替身。"""
    return SimpleNamespace(days=[
        SimpleNamespace(day=day, items=[SimpleNamespace(poi_id=pid) for pid in ids]) for day, ids in days.items()
    ])


class TestCleanName:
    """所属景区前缀清理。"""

    def test_strips_area_prefix(self) -> None:
        assert _clean_name(poi("a", name="杭州西湖风景名胜区-曲院风荷")).name == "曲院风荷"
        assert _clean_name(poi("b", name="千岛湖景区-梅峰岛")).name == "梅峰岛"
        assert _clean_name(poi("c", name="杭州西湖风景名胜区宝石山")).name == "宝石山"

    def test_keeps_other_names(self) -> None:
        assert _clean_name(poi("a", name="中国茶叶博物馆-龙井馆区")).name == "中国茶叶博物馆-龙井馆区"
        assert _clean_name(poi("b", name="灵隐寺")).name == "灵隐寺"
        assert _clean_name(poi("c", name="西湖风景名胜区-")).name == "西湖风景名胜区-"
        assert _clean_name(poi("d", name="杭州西湖风景名胜区")).name == "杭州西湖风景名胜区"
        assert _clean_name(poi("e", name="西湖风景名胜区A")).name == "西湖风景名胜区A"
        assert _clean_name(poi("f", name="千岛湖景区游船")).name == "千岛湖景区游船"


class TestPruneNested:
    """景区与内部景点的去重。"""

    def test_parent_with_many_children_is_dropped(self) -> None:
        kids = [poi(f"k{i}", parent="area") for i in range(NESTED_AREA_MIN_CHILDREN)]
        result = prune_nested({p.poi_id: p for p in [poi("area"), *kids, poi("other")]})
        assert set(result) == {*(k.poi_id for k in kids), "other"}

    def test_parent_with_few_children_is_kept(self) -> None:
        kids = [poi(f"k{i}", parent="area") for i in range(NESTED_AREA_MIN_CHILDREN - 1)]
        result = prune_nested({p.poi_id: p for p in [poi("area"), *kids]})
        assert set(result) == {"area"}

    def test_children_without_parent_in_candidates_are_kept(self) -> None:
        result = prune_nested({p.poi_id: p for p in [poi("k1", parent="missing"), poi("self", parent="self")]})
        assert set(result) == {"k1", "self"}


class TestSelectCandidateIds:
    """按评分筛选候选。"""

    def test_drops_unrated_and_prefers_high_rating(self) -> None:
        pois = [poi("high", 4.7), poi("edge", MIN_CANDIDATE_RATING), poi("low", 4.2), poi("none", None)]
        assert select_candidate_ids(state(pois)) == ["high", "edge", "low"]

    def test_high_rated_sorted_by_hits_then_rating(self) -> None:
        pois = [poi("a", 4.9), poi("b", 4.5), poi("c", 4.7)]
        assert select_candidate_ids(state(pois, hits={"b": 3, "c": 1, "a": 1})) == ["b", "a", "c"]

    def test_fill_sorted_by_rating_desc(self) -> None:
        pois = [poi("x", 4.0), poi("y", 4.4), poi("z", 3.5), poi("top", 4.8)]
        # 低分补位只看评分，热度不能让低分景点排到前面。
        assert select_candidate_ids(state(pois, hits={"z": 5})) == ["top", "y", "x", "z"]

    def test_caps_at_max_candidates_and_high_first(self) -> None:
        high = [poi(f"h{i:02d}", 4.6) for i in range(MAX_CANDIDATES - 2)]
        low = [poi(f"l{i}", 4.0 + i / 10) for i in range(5)]
        ids = select_candidate_ids(state([*low, *high]))
        assert len(ids) == MAX_CANDIDATES == 35
        assert ids[-2:] == ["l4", "l3"]

    def test_required_kept_without_rating_and_counted_in_cap(self) -> None:
        must = poi("must", None)
        others = [poi(f"h{i:02d}", 4.8) for i in range(40)]
        scope = PlanScope(mode="full", affected_days=[1], required=[PoiRef(poi_id="must", name="must")])
        ids = select_candidate_ids(state([*others, must], scope))
        assert ids[0] == "must"
        assert len(ids) == MAX_CANDIDATES

    def test_excluded_and_fixed_day_pois_removed(self) -> None:
        pois = [poi("keep", 4.6), poi("nope", 4.9), poi("fixed", 4.9), poi("old", None), poi("new", 4.5)]
        scope = PlanScope(
            mode="partial", affected_days=[2], fixed_days=[1], excluded_poi_ids=["nope"], keep_bonus={"old": 2},
        )
        ids = select_candidate_ids(state(pois, scope, baseline=baseline({1: ["fixed"], 2: ["old"]})))
        # 受影响天的原景点即使没有评分也保留，以便软保留。
        assert ids == ["old", "keep", "new"]

    def test_keep_bonus_ignored_in_full_mode(self) -> None:
        scope = PlanScope(mode="full", affected_days=[1], keep_bonus={"old": 1})
        assert select_candidate_ids(state([poi("old", None), poi("a", 4.6)], scope)) == ["a"]

    def test_replace_slot_reserves_matching_pois(self) -> None:
        """替换成博物馆时，低热度、低评分的博物馆也不会被候选上限挤掉。"""
        popular = [poi(f"h{i:02d}", 4.8) for i in range(40)]
        museums = [poi("m1", 4.2, name="某某博物馆"), poi("m2", 4.6, name="另一博物馆"), poi("m3", None, name="无评分博物馆")]
        scope = PlanScope(
            mode="partial", affected_days=[1], replace_slots=[ReplaceSlot(day=1, tags=["museum"], count=1)],
        )
        ids = select_candidate_ids(state([*popular, *museums], scope, hits={p.poi_id: 3 for p in popular}))
        assert ids[:2] == ["m2", "m1"]
        assert "m3" not in ids
        assert len(ids) == MAX_CANDIDATES

    def test_replace_slot_uses_known_profile(self) -> None:
        """已补全属性的景点按属性标签匹配，而不是按名称规则。"""
        hall = poi("hall", 4.6, name="某某纪念馆")
        st = state([*(poi(f"h{i:02d}", 4.8) for i in range(40)), hall], PlanScope(
            mode="partial", affected_days=[1], replace_slots=[ReplaceSlot(day=1, tags=["museum"], count=1)],
        ))
        assert "hall" not in select_candidate_ids(st)
        st["profiles"] = {"hall": PoiProfile(poi_id="hall", subtype="museum", indoor="indoor", duration_minutes=90, source="llm")}
        assert select_candidate_ids(st)[0] == "hall"
