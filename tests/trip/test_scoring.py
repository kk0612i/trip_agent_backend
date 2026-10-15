"""候选打分测试：兴趣匹配分、评分兜底、评分分、热度奖励、候选组装和求解输入转换。"""

import pytest

from app.agent.trip.planning.scoring import (
    DEFAULT_RATING_SCORE,
    INTEREST_DISLIKED,
    INTEREST_NEUTRAL,
    INTEREST_UNMATCHED,
    build_candidate,
    interest_score,
    rating_fallback,
    rating_score,
    tier_bonus,
    to_solver_poi,
)
from app.schemas.trip_plan_schema import AmapPoi, ObjectiveWeights, PoiProfile
from tests.trip.draft_data import amap_poi, spot_profile

MUSEUM = PoiProfile(poi_id="m", subtype="museum", indoor="indoor", duration_minutes=120, source="rule")


def rated(rating: float | None) -> AmapPoi:
    """构造指定评分的景点。"""
    return AmapPoi(poi_id=f"p{rating}", name="景点", lng=120.15, lat=30.25, rating=rating)


# ---------------------------------------------------------------------------
# 兴趣分
# ---------------------------------------------------------------------------


def test_interest_neutral_without_preferences() -> None:
    """未设置偏好时为中性分。"""
    assert interest_score(MUSEUM, {}) == INTEREST_NEUTRAL == 5.0


@pytest.mark.parametrize(
    ("preferences", "expected"),
    [
        ({"museum": 1}, 7.0),
        ({"museum": 2}, 10.0),
        ({"culture_art": 1}, 7.0),
        ({"culture_art": 2}, 10.0),
        ({"museum": 1, "culture_art": 2}, 10.0),
        ({"museum": 2, "culture_art": 1}, 10.0),
    ],
)
def test_interest_takes_highest_matched_weight(preferences: dict[str, int], expected: float) -> None:
    """细分体验和主体验类都可命中，取命中的最高权重对应的分数。"""
    assert interest_score(MUSEUM, preferences) == expected


def test_interest_unmatched_is_low() -> None:
    """设置了偏好但没有命中时为低分。"""
    assert interest_score(MUSEUM, {"nature": 2, "temple": 1}) == INTEREST_UNMATCHED == 2.0


def test_interest_weight_above_two_capped() -> None:
    """权重超过 2 时按 2 计。"""
    assert interest_score(MUSEUM, {"museum": 5}) == 10.0


def test_interest_zero_weight_is_unmatched() -> None:
    """权重为 0 视为未命中。"""
    assert interest_score(MUSEUM, {"museum": 0}) == INTEREST_UNMATCHED


@pytest.mark.parametrize("preferences", [{"museum": -1}, {"culture_art": -1}, {"museum": -1, "nature": 1}])
def test_interest_disliked_is_lowest(preferences: dict[str, int]) -> None:
    """命中不喜欢的类型且没有命中喜欢的类型时为最低分。"""
    assert interest_score(MUSEUM, preferences) == INTEREST_DISLIKED == 0.0


def test_interest_only_dislikes_keeps_others_neutral() -> None:
    """只设置了不喜欢时，其他类型保持中性分，不当作未命中。"""
    assert interest_score(MUSEUM, {"temple": -1}) == INTEREST_NEUTRAL


def test_interest_liked_wins_over_disliked() -> None:
    """细分体验和主体验类一喜一恶时，以喜欢为准。"""
    assert interest_score(MUSEUM, {"culture_art": -1, "museum": 1}) == 7.0


# ---------------------------------------------------------------------------
# 评分
# ---------------------------------------------------------------------------


def test_rating_fallback_median_doubled() -> None:
    """兜底分为候选评分中位数乘 2。"""
    assert rating_fallback([rated(4.0), rated(4.6), rated(4.8)]) == pytest.approx(9.2)


def test_rating_fallback_even_count() -> None:
    """偶数个评分取中间两数平均。"""
    assert rating_fallback([rated(4.0), rated(5.0)]) == pytest.approx(9.0)


def test_rating_fallback_ignores_missing_and_zero() -> None:
    """缺失和为 0 的评分不参与中位数。"""
    assert rating_fallback([rated(None), rated(0.0), rated(4.5)]) == pytest.approx(9.0)


def test_rating_fallback_default_without_ratings() -> None:
    """候选都没有评分时使用默认值。"""
    assert rating_fallback([rated(None), rated(0.0)]) == DEFAULT_RATING_SCORE == 7.0
    assert rating_fallback([]) == DEFAULT_RATING_SCORE


def test_rating_score_converts_to_ten_scale() -> None:
    """5 分制评分换算为 10 分制。"""
    assert rating_score(rated(4.5), 6.0) == pytest.approx(9.0)


@pytest.mark.parametrize("rating", [None, 0.0])
def test_rating_score_uses_fallback(rating: float | None) -> None:
    """评分缺失或为 0 时使用兜底分。"""
    assert rating_score(rated(rating), 6.5) == 6.5


# ---------------------------------------------------------------------------
# 热度奖励
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hits", "must", "expected"),
    [
        (0, False, 0.0),
        (1, False, 0.0),
        (2, False, 1.5),
        (3, False, 3.0),
        (6, False, 3.0),
        (1, True, 1.0),
        (3, True, 4.0),
    ],
)
def test_tier_bonus(hits: int, must: bool, expected: float) -> None:
    """每多命中一个模板加 1.5 分、封顶 3 分，命中“必去景点”模板再加 1 分。"""
    assert tier_bonus(hits, must) == expected


# ---------------------------------------------------------------------------
# 候选组装
# ---------------------------------------------------------------------------


def test_build_candidate_combines_scores() -> None:
    """候选包含景点、属性和三项打分，默认使用属性中的游玩时长。"""
    poi = amap_poi("zjmuseum", rating=4.5)
    candidate = build_candidate(
        poi, spot_profile("zjmuseum"),
        preferences={"museum": 1}, duration_overrides={}, template_hits=2, must_template_hit=True, rating_default=7.0,
    )
    assert candidate.poi == poi
    assert candidate.duration_minutes == 120
    assert candidate.interest_score == 7.0
    assert candidate.rating_score == pytest.approx(9.0)
    assert candidate.tier_bonus == 2.5
    assert candidate.template_hits == 2
    assert candidate.must_template_hit is True


def test_build_candidate_duration_override() -> None:
    """用户指定的游玩时长覆盖属性时长。"""
    candidate = build_candidate(
        amap_poi("zjmuseum"), spot_profile("zjmuseum"),
        preferences={}, duration_overrides={"zjmuseum": 200, "other": 30},
        template_hits=1, must_template_hit=False, rating_default=7.0,
    )
    assert candidate.duration_minutes == 200


def test_build_candidate_missing_rating_uses_default() -> None:
    """评分缺失时使用传入的兜底分，满分评分换算后不超过 10。"""
    missing = build_candidate(
        amap_poi("leifeng", rating=None), spot_profile("leifeng"),
        preferences={}, duration_overrides={}, template_hits=0, must_template_hit=False, rating_default=8.4,
    )
    full = build_candidate(
        amap_poi("leifeng", rating=5.0), spot_profile("leifeng"),
        preferences={}, duration_overrides={}, template_hits=0, must_template_hit=False, rating_default=8.4,
    )
    assert missing.rating_score == 8.4
    assert full.rating_score == 10.0


# ---------------------------------------------------------------------------
# 求解输入转换
# ---------------------------------------------------------------------------


def test_to_solver_poi_value_and_tags() -> None:
    """单点收益为兴趣、评分、热度和基础奖励之和乘放大倍数；标签为细分、主类和室内外。"""
    candidate = build_candidate(
        amap_poi("zjmuseum", rating=4.5), spot_profile("zjmuseum"),
        preferences={"museum": 1}, duration_overrides={"zjmuseum": 100}, template_hits=2, must_template_hit=False,
        rating_default=7.0,
    )
    weights = ObjectiveWeights()
    sp = to_solver_poi(candidate, weights)
    # 7 + 9 + 1.5 + 6 = 23.5，放大 10 倍
    assert sp.value == 235
    assert sp.poi_id == "zjmuseum"
    assert sp.duration_minutes == 100
    assert sp.main_category == "culture_art"
    assert sp.subtype == "museum"
    assert sp.indoor == "indoor"
    assert sp.tags == ["museum", "culture_art", "indoor"]


def test_to_solver_poi_mixed_counts_as_outdoor() -> None:
    """室内外结合的景点标签归入户外。"""
    candidate = build_candidate(
        amap_poi("lingyin", rating=4.8), spot_profile("lingyin"),
        preferences={}, duration_overrides={}, template_hits=1, must_template_hit=False, rating_default=7.0,
    )
    sp = to_solver_poi(candidate, ObjectiveWeights())
    assert sp.indoor == "mixed"
    assert sp.tags == ["temple", "religion", "outdoor"]


def test_to_solver_poi_respects_weights() -> None:
    """基础奖励和放大倍数来自目标权重。"""
    candidate = build_candidate(
        amap_poi("leifeng", rating=4.0), spot_profile("leifeng"),
        preferences={}, duration_overrides={}, template_hits=1, must_template_hit=False, rating_default=7.0,
    )
    # 5 + 8 + 0 + 2 = 15
    assert to_solver_poi(candidate, ObjectiveWeights(count_reward=2, score_scale=4)).value == 60
