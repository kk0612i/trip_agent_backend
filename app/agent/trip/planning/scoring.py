"""候选景点打分：兴趣匹配分、评分质量分和热度奖励。

兴趣分采用“标签 × 偏好权重”的确定性规则，不让模型打分，保证同样输入得到同样结果。
"""

from statistics import median

from app.schemas.trip_plan_schema import AmapPoi, Candidate, ObjectiveWeights, PoiProfile, SolverPoi

from app.agent.trip.planning.tags import item_tags

# 兴趣分：未设置偏好时为中性分，未命中偏好为低分，按命中权重给高分，命中“不喜欢”最低。
INTEREST_NEUTRAL = 5.0
INTEREST_UNMATCHED = 2.0
INTEREST_DISLIKED = 0.0
INTEREST_BY_WEIGHT = {1: 7.0, 2: 10.0}
# 缺少任何评分数据时使用的评分分。
DEFAULT_RATING_SCORE = 7.0


def interest_score(profile: PoiProfile, preferences: dict[str, int]) -> float:
    """按细分体验和主体验类匹配偏好，取命中的最高权重。

    负权重表示不喜欢：只在没有命中任何喜欢的标签时生效，给最低分（软约束，不排除景点）。
    只设置了不喜欢时，其余景点按中性分，不视为“未命中偏好”。

    Args:
        profile: 景点属性。
        preferences: 偏好标签到权重。

    Returns:
        0～10 的兴趣匹配分。
    """
    if not preferences:
        return INTEREST_NEUTRAL
    weights = [preferences[tag] for tag in (profile.subtype, profile.main_category) if tag in preferences]
    liked = max((w for w in weights if w > 0), default=0)
    if liked:
        return INTEREST_BY_WEIGHT[min(liked, 2)]
    if any(w < 0 for w in weights):
        return INTEREST_DISLIKED
    has_likes = any(w >= 0 for w in preferences.values())
    return INTEREST_UNMATCHED if has_likes else INTEREST_NEUTRAL


def rating_fallback(pois: list[AmapPoi]) -> float:
    """返回评分缺失时使用的评分分：候选评分中位数，候选都无评分时取默认值。"""
    ratings = [poi.rating for poi in pois if poi.rating]
    return median(ratings) * 2 if ratings else DEFAULT_RATING_SCORE


def rating_score(poi: AmapPoi, fallback: float) -> float:
    """高德 5 分制评分换算为 10 分制，缺失或为 0 时使用兜底分。"""
    return poi.rating * 2 if poi.rating else fallback


def tier_bonus(template_hits: int, must_template_hit: bool) -> float:
    """按命中的搜索模板数给热度奖励，命中“必去景点”模板额外加分。"""
    return min(3.0, max(0, template_hits - 1) * 1.5) + (1.0 if must_template_hit else 0.0)


def build_candidate(
    poi: AmapPoi,
    profile: PoiProfile,
    *,
    preferences: dict[str, int],
    duration_overrides: dict[str, int],
    template_hits: int,
    must_template_hit: bool,
    rating_default: float,
) -> Candidate:
    """组合景点、属性和本次会话的打分得到候选。

    Args:
        poi: 高德景点。
        profile: 景点属性。
        preferences: 偏好标签到权重。
        duration_overrides: 用户指定的游玩分钟数。
        template_hits: 命中的搜索模板数。
        must_template_hit: 是否命中“必去景点”模板。
        rating_default: 评分缺失时的评分分。

    Returns:
        候选景点。
    """
    return Candidate(
        poi=poi,
        profile=profile,
        template_hits=template_hits,
        must_template_hit=must_template_hit,
        duration_minutes=duration_overrides.get(poi.poi_id, profile.duration_minutes),
        interest_score=interest_score(profile, preferences),
        rating_score=min(10.0, rating_score(poi, rating_default)),
        tier_bonus=tier_bonus(template_hits, must_template_hit),
    )


def to_solver_poi(candidate: Candidate, weights: ObjectiveWeights) -> SolverPoi:
    """把候选转换为求解器输入，单点收益按 score_scale 放大取整。"""
    profile = candidate.profile
    raw = candidate.interest_score + candidate.rating_score + candidate.tier_bonus + weights.count_reward
    return SolverPoi(
        poi_id=candidate.poi.poi_id,
        duration_minutes=candidate.duration_minutes,
        value=round(raw * weights.score_scale),
        main_category=profile.main_category,
        subtype=profile.subtype,
        indoor=profile.indoor,
        tags=item_tags(profile.subtype, profile.main_category, profile.indoor),
    )
