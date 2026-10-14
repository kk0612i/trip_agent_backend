"""行程规划领域的数据结构：城市、景点、天气、需求、草稿、修改操作和求解输入输出。

时间统一使用"相对 9:00 的分钟数"：0 表示 9:00，600 表示 19:00。
"""

import datetime
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# 固定标签词表
# ---------------------------------------------------------------------------

MainCategory = Literal[
    "history", "culture_art", "religion", "nature", "leisure", "entertainment", "urban",
]
"""主体验类，用于每日集中惩罚。"""

Subtype = Literal[
    "museum", "art_gallery", "exhibition", "memorial", "former_residence",
    "ancient_architecture", "historic_site", "ancient_town", "historic_street",
    "temple", "church",
    "mountain", "lake_river", "seaside", "forest_wetland", "botanical_garden",
    "garden", "urban_park", "square", "zoo_aquarium",
    "theme_park",
    "landmark_building", "commercial_street", "university", "other",
]
"""细分体验类，用于全程重复惩罚。"""

# 细分体验到主体验类的固定映射，主体验类不由模型直接决定。
SUBTYPE_MAIN_CATEGORY: dict[str, str] = {
    "museum": "culture_art", "art_gallery": "culture_art", "exhibition": "culture_art",
    "university": "culture_art",
    "memorial": "history", "former_residence": "history", "ancient_architecture": "history",
    "historic_site": "history", "ancient_town": "history", "historic_street": "history",
    "temple": "religion", "church": "religion",
    "mountain": "nature", "lake_river": "nature", "seaside": "nature",
    "forest_wetland": "nature", "botanical_garden": "nature",
    "garden": "leisure", "urban_park": "leisure", "square": "leisure", "zoo_aquarium": "leisure",
    "theme_park": "entertainment",
    "landmark_building": "urban", "commercial_street": "urban", "other": "urban",
}

PreferenceTag = MainCategory | Subtype
"""偏好标签：主体验类或细分体验类。"""

SelectorTag = MainCategory | Subtype | Literal["indoor", "outdoor"]
"""选择器和替换目标可用的标签。"""

IndoorType = Literal["indoor", "outdoor", "mixed"]
Pace = Literal["relaxed", "normal", "packed"]
DayPart = Literal["morning", "afternoon"]
DayLoad = Literal["lighter", "fuller"]
TravelMode = Literal["walk", "taxi"]

DAY_WINDOW_MINUTES = 600
"""每天游玩时间窗 9:00-19:00 的总分钟数。"""

MAX_TRIP_DAYS = 7
"""单次行程最多天数，限制求解规模；只支持 1～7 天。"""

MAX_ITEMS_PER_DAY = 5
"""每天最多安排的景点数，任何节奏和松紧调整都不超过该值。"""

MAX_CANDIDATES = MAX_TRIP_DAYS * MAX_ITEMS_PER_DAY
"""进入求解的候选景点上限（35）。"""

MIN_CANDIDATE_RATING = 4.5
"""候选景点默认的最低高德评分；不足时按评分降序补入较低评分的景点。"""


class StrictModel(BaseModel):
    """领域模型基类，拒绝未声明字段。"""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# 外部数据
# ---------------------------------------------------------------------------


class CityInfo(StrictModel):
    """高德解析后的城市。"""

    adcode: str = Field(min_length=6, max_length=6, description="城市行政区划编码")
    name: str = Field(min_length=1, description="城市名称")
    lng: float = Field(description="城市中心经度")
    lat: float = Field(description="城市中心纬度")


class PoiPhoto(StrictModel):
    """景点图片，供前端展示。"""

    url: str = Field(min_length=1, description="图片地址，统一为 https")
    title: str = Field(default="", description="图片标题，可空")


class AmapPoi(StrictModel):
    """高德地点搜索返回的景点。"""

    poi_id: str = Field(min_length=1, description="高德 POI 标识")
    name: str = Field(min_length=1, description="景点名称")
    lng: float = Field(description="经度")
    lat: float = Field(description="纬度")
    adcode: str = Field(default="", description="所属区县编码")
    city_adcode: str = Field(default="", description="所属城市编码，按区县编码推算")
    address: str = Field(default="", description="地址")
    amap_type: str = Field(default="", description="高德分类名称，如 风景名胜;公园广场;公园")
    typecode: str = Field(default="", description="高德分类编码")
    rating: float | None = Field(default=None, ge=0, le=5, description="高德评分，缺失或无法解析为空")
    parent_id: str | None = Field(default=None, description="父级 POI 标识，用于去重")
    photos: list[PoiPhoto] = Field(default_factory=list, description="高德图片，最多保留 5 张")


class DayWeather(StrictModel):
    """单日天气预报。"""

    date: datetime.date = Field(description="日期")
    day_weather: str = Field(description="白天天气描述")
    night_weather: str = Field(default="", description="夜间天气描述")
    rain_level: int = Field(ge=0, le=3, description="降雨等级：0 无雨，1 小雨，2 中雨，3 大雨及以上")


class WebSearchResult(StrictModel):
    """网页搜索结果。"""

    title: str = Field(description="网页标题")
    url: str = Field(description="网页地址")
    snippet: str = Field(default="", description="摘要")
    site_name: str = Field(default="", description="站点名称")
    published_at: str | None = Field(default=None, description="发布时间原文")


# ---------------------------------------------------------------------------
# 景点属性与候选
# ---------------------------------------------------------------------------


class PoiProfile(StrictModel):
    """景点自身属性，按 POI 全局缓存，与会话无关。"""

    poi_id: str = Field(description="高德 POI 标识")
    subtype: Subtype = Field(description="细分体验类")
    indoor: IndoorType = Field(description="室内外属性")
    duration_minutes: int = Field(ge=20, le=360, description="建议游玩分钟数")
    source: Literal["llm", "rule"] = Field(description="属性来源")

    @property
    def main_category(self) -> str:
        """返回固定映射得到的主体验类。"""
        return SUBTYPE_MAIN_CATEGORY[self.subtype]


class Candidate(StrictModel):
    """进入求解的候选景点及本次会话的打分。"""

    poi: AmapPoi = Field(description="高德景点")
    profile: PoiProfile = Field(description="景点属性")
    template_hits: int = Field(default=1, ge=0, description="命中的搜索模板数，作为热度信号")
    must_template_hit: bool = Field(default=False, description="是否命中“必去景点”模板")
    duration_minutes: int = Field(ge=10, le=600, description="本次使用的游玩分钟数，含用户指定")
    interest_score: float = Field(ge=0, le=10, description="兴趣匹配分")
    rating_score: float = Field(ge=0, le=10, description="评分质量分")
    tier_bonus: float = Field(default=0, ge=0, description="热度档奖励")


# ---------------------------------------------------------------------------
# 用户需求（持久化）
# ---------------------------------------------------------------------------


class PoiRef(StrictModel):
    """已经过高德核实的景点引用。"""

    poi_id: str = Field(description="高德 POI 标识")
    name: str = Field(description="景点名称")
    day: int | None = Field(default=None, ge=1, le=MAX_TRIP_DAYS, description="指定的第几天，可空")


class TransportPref(StrictModel):
    """交通偏好。"""

    walk_only: bool = Field(default=False, description="是否只允许步行")
    max_walk_minutes: int = Field(default=40, ge=5, le=180, description="单段步行最长分钟数")


class Requirements(StrictModel):
    """用户已确认的规划需求。"""

    city: CityInfo | None = Field(default=None, description="目的地城市")
    start_date: date | None = Field(default=None, description="出发日期")
    days: int | None = Field(default=None, ge=1, le=MAX_TRIP_DAYS, description="游玩天数")
    pace: Pace = Field(default="normal", description="游玩节奏")
    preferences: dict[str, int] = Field(
        default_factory=dict, description="偏好标签到权重（1 喜欢，2 非常喜欢）",
    )
    must_visit: list[PoiRef] = Field(default_factory=list, description="必去景点")
    avoid: list[PoiRef] = Field(default_factory=list, description="不去的景点")
    transport: TransportPref = Field(default_factory=TransportPref, description="交通偏好")
    duration_overrides: dict[str, int] = Field(
        default_factory=dict, description="用户指定的 POI 游玩分钟数",
    )
    day_loads: dict[int, DayLoad] = Field(default_factory=dict, description="指定天的松紧调整")

    def missing_fields(self) -> list[str]:
        """返回首次规划前仍缺失的必填项名称。"""
        missing = []
        if self.city is None:
            missing.append("city")
        if self.start_date is None:
            missing.append("start_date")
        if self.days is None:
            missing.append("days")
        return missing


# ---------------------------------------------------------------------------
# 行程草稿
# ---------------------------------------------------------------------------


class TravelLeg(StrictModel):
    """两景点之间的交通估算。"""

    mode: TravelMode = Field(description="交通方式")
    minutes: int = Field(ge=0, description="耗时分钟数")
    distance_km: float = Field(ge=0, description="球面直线距离公里数")
    fee_yuan: float | None = Field(default=None, ge=0, description="费用，未知为空")


class PlanItem(StrictModel):
    """草稿中的一个景点安排。"""

    item_id: str = Field(description="草稿内稳定引用，如 D1-2")
    poi_id: str = Field(description="高德 POI 标识")
    name: str = Field(description="景点名称")
    lng: float = Field(description="经度")
    lat: float = Field(description="纬度")
    subtype: Subtype = Field(description="细分体验类")
    main_category: MainCategory = Field(description="主体验类")
    indoor: IndoorType = Field(description="室内外属性")
    start_min: int = Field(ge=0, le=DAY_WINDOW_MINUTES, description="开始时间，相对 9:00 的分钟数")
    end_min: int = Field(ge=0, le=DAY_WINDOW_MINUTES, description="结束时间，相对 9:00 的分钟数")
    duration_minutes: int = Field(ge=10, description="游玩分钟数，不含园内午餐")
    lunch_inside: bool = Field(default=False, description="是否在景点内用午餐（占用 60 分钟）")
    travel_from_prev: TravelLeg | None = Field(default=None, description="从上一个景点过来的交通")
    rating: float | None = Field(default=None, ge=0, le=5, description="高德评分，必去景点可能为空")
    address: str = Field(default="", description="地址")
    photos: list[PoiPhoto] = Field(default_factory=list, description="景点图片")


class LunchBreak(StrictModel):
    """自由用餐时间。"""

    start_min: int = Field(ge=0, le=DAY_WINDOW_MINUTES, description="开始时间，相对 9:00")
    end_min: int = Field(ge=0, le=DAY_WINDOW_MINUTES, description="结束时间，相对 9:00")
    inside_item_id: str | None = Field(default=None, description="在某景点内用餐时的景点引用")


class PlanDay(StrictModel):
    """一天的安排。"""

    day: int = Field(ge=1, le=MAX_TRIP_DAYS, description="第几天")
    date: datetime.date = Field(description="日期")
    weather: DayWeather | None = Field(default=None, description="天气预报，超出预报范围为空")
    items: list[PlanItem] = Field(default_factory=list, description="按时间顺序的景点")
    lunch: LunchBreak | None = Field(default=None, description="午餐空档，当天未跨午餐时段为空")


class Plan(StrictModel):
    """完整行程草稿内容。"""

    city: CityInfo = Field(description="城市")
    start_date: date = Field(description="出发日期")
    days: list[PlanDay] = Field(description="每日安排")
    objective: dict[str, int] = Field(default_factory=dict, description="目标函数各项取值，便于解释和调参")
    warnings: list[str] = Field(default_factory=list, description="提示信息")


# ---------------------------------------------------------------------------
# 已解析的修改操作（程序内部使用，均已校验）
# ---------------------------------------------------------------------------


class RemoveItemsOp(StrictModel):
    """删除草稿中的景点。"""

    op: Literal["remove"] = Field(default="remove", description="操作类型")
    item_ids: list[str] = Field(min_length=1, description="被删除的草稿引用")


class ReplaceTarget(StrictModel):
    """替换目标。"""

    poi: PoiRef | None = Field(default=None, description="指定替换成的景点")
    tags: list[str] = Field(default_factory=list, description="替换景点需满足的标签")
    count: int | None = Field(default=None, ge=1, description="替换后的数量，空表示与被替换数量相同")


class ReplaceItemsOp(StrictModel):
    """替换草稿中的景点。"""

    op: Literal["replace"] = Field(default="replace", description="操作类型")
    item_ids: list[str] = Field(min_length=1, description="被替换的草稿引用")
    target: ReplaceTarget = Field(default_factory=ReplaceTarget, description="替换目标")


class AddPoiOp(StrictModel):
    """新增景点。"""

    op: Literal["add"] = Field(default="add", description="操作类型")
    poi: PoiRef = Field(description="新增景点，day 为指定天")


class MoveItemsOp(StrictModel):
    """把景点移动到另一天。"""

    op: Literal["move"] = Field(default="move", description="操作类型")
    item_ids: list[str] = Field(min_length=1, description="被移动的草稿引用")
    to_day: int = Field(ge=1, le=MAX_TRIP_DAYS, description="目标天")


class SwapItemsOp(StrictModel):
    """交换两个景点所在的天。"""

    op: Literal["swap"] = Field(default="swap", description="操作类型")
    a: str = Field(description="第一个草稿引用")
    b: str = Field(description="第二个草稿引用")


class SetDurationOp(StrictModel):
    """修改游玩时长。"""

    op: Literal["set_duration"] = Field(default="set_duration", description="操作类型")
    item_id: str = Field(description="草稿引用")
    minutes: int = Field(ge=10, le=600, description="游玩分钟数")


class SetDayLoadOp(StrictModel):
    """调整某天的松紧。"""

    op: Literal["day_load"] = Field(default="day_load", description="操作类型")
    day: int = Field(ge=1, le=MAX_TRIP_DAYS, description="第几天")
    level: DayLoad = Field(description="更轻松或更充实")


class UndoOp(StrictModel):
    """撤销到上一个草稿版本。"""

    op: Literal["undo"] = Field(default="undo", description="操作类型")


EditOp = Annotated[
    RemoveItemsOp | ReplaceItemsOp | AddPoiOp | MoveItemsOp | SwapItemsOp
    | SetDurationOp | SetDayLoadOp | UndoOp,
    Field(discriminator="op"),
]


# ---------------------------------------------------------------------------
# 规划范围与求解输入输出
# ---------------------------------------------------------------------------


class ReplaceSlot(StrictModel):
    """替换约束：某天新入选且满足条件的景点数量下限。"""

    day: int = Field(ge=1, le=MAX_TRIP_DAYS, description="第几天")
    tags: list[str] = Field(default_factory=list, description="需满足的标签，空表示不限")
    poi_id: str | None = Field(default=None, description="指定景点；指定时 count 视为 1")
    count: int = Field(ge=1, description="数量下限")


class PlanScope(StrictModel):
    """本次规划的影响范围。"""

    mode: Literal["full", "partial"] = Field(description="全量或局部")
    affected_days: list[int] = Field(description="需要求解的天")
    fixed_days: list[int] = Field(default_factory=list, description="保持不变的天")
    excluded_poi_ids: list[str] = Field(default_factory=list, description="禁止入选的 POI")
    required: list[PoiRef] = Field(default_factory=list, description="必须入选的 POI，day 为指定天")
    replace_slots: list[ReplaceSlot] = Field(default_factory=list, description="替换约束")
    keep_bonus: dict[str, int] = Field(
        default_factory=dict, description="受影响天中原有景点到原所在天，保留时给奖励",
    )
    allow_refill_days: list[int] = Field(default_factory=list, description="允许补入新景点的受影响天")
    day_max_items: dict[int, int] = Field(
        default_factory=dict, description="不允许补位的受影响天的景点数上限，覆盖节奏上限",
    )
    freed_minutes: dict[int, int] = Field(default_factory=dict, description="受影响天因删除或移出空出的游玩分钟数")
    stability: bool = Field(default=False, description="全量重排时是否启用稳定性奖励")


class ObjectiveWeights(StrictModel):
    """目标函数权重，全部为整数以便 CP-SAT 建模。"""

    count_reward: int = Field(default=6, description="每入选一个景点的基础奖励")
    travel_divisor: int = Field(default=15, gt=0, description="交通分钟数除数")
    day_category_penalty: int = Field(default=2, description="每日同主体验类每多一个的惩罚")
    day_category_free: int = Field(default=1, description="每日同主体验类免罚数量")
    subtype_penalty: int = Field(default=3, description="全程同细分体验每多一个的惩罚")
    subtype_free: int = Field(default=2, description="全程同细分体验免罚数量")
    rain_penalty: int = Field(default=3, description="雨天户外访问的单位惩罚")
    keep_bonus: int = Field(default=8, description="修改时保留原景点的奖励")
    score_scale: int = Field(default=10, gt=0, description="浮点分数放大倍数")


class SolveDay(StrictModel):
    """求解器中的一天。"""

    day: int = Field(ge=1, le=MAX_TRIP_DAYS, description="第几天")
    rain_level: int = Field(default=0, ge=0, le=3, description="降雨等级，未知为 0")
    end_limit_min: int = Field(default=DAY_WINDOW_MINUTES, ge=60, le=DAY_WINDOW_MINUTES, description="当天最晚结束")
    max_items: int = Field(ge=1, le=10, description="当天景点数上限")


class SolverPoi(StrictModel):
    """求解器中的候选景点。"""

    poi_id: str = Field(description="高德 POI 标识")
    duration_minutes: int = Field(ge=10, le=600, description="游玩分钟数")
    value: int = Field(description="已放大的单点收益（兴趣+评分+热度+基础奖励）")
    main_category: MainCategory = Field(description="主体验类")
    subtype: Subtype = Field(description="细分体验类")
    indoor: IndoorType = Field(description="室内外属性")
    tags: list[str] = Field(default_factory=list, description="用于替换约束匹配的标签")


class SolveInput(StrictModel):
    """CP-SAT 求解输入，只包含受影响的天。"""

    days: list[SolveDay] = Field(min_length=1, description="待求解的天")
    pois: list[SolverPoi] = Field(description="候选景点")
    travel: dict[str, TravelLeg] = Field(description="有向交通边，键为 'from_id|to_id'，缺失表示不可达")
    required: list[PoiRef] = Field(default_factory=list, description="必须入选")
    replace_slots: list[ReplaceSlot] = Field(default_factory=list, description="替换约束")
    new_poi_ids: list[str] = Field(default_factory=list, description="替换约束中视为“新”的 POI")
    keep_bonus: dict[str, int] = Field(default_factory=dict, description="原景点到原所在天")
    fixed_subtype_counts: dict[str, int] = Field(default_factory=dict, description="固定天已占用的细分体验数")
    weights: ObjectiveWeights = Field(default_factory=ObjectiveWeights, description="目标权重")
    lunch_enabled: bool = Field(default=True, description="是否安排午餐空档")
    time_limit_seconds: float = Field(default=10, gt=0, description="求解时间上限")


class SolvedVisit(StrictModel):
    """求解结果中的一次访问。"""

    poi_id: str = Field(description="高德 POI 标识")
    start_min: int = Field(description="开始时间")
    end_min: int = Field(description="结束时间（含园内午餐）")
    lunch_inside: bool = Field(default=False, description="是否在景点内午餐")
    lunch_after: bool = Field(default=False, description="是否在该景点结束后午餐")


class SolvedDay(StrictModel):
    """求解结果中的一天。"""

    day: int = Field(description="第几天")
    visits: list[SolvedVisit] = Field(default_factory=list, description="按顺序的访问")


class SolveOutput(StrictModel):
    """CP-SAT 求解输出。"""

    status: Literal["ok", "infeasible", "timeout"] = Field(description="求解状态")
    days: list[SolvedDay] = Field(default_factory=list, description="每日结果")
    objective: dict[str, int] = Field(default_factory=dict, description="目标各项取值")
