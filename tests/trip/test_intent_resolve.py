"""意图解析测试：规则快速通道、景点核实、选择器展开、就近选天和整轮解析的去向。"""

from datetime import date

import pytest

from app.agent.trip.intent_resolve import (
    CANCEL_REPLY,
    CONFIRMED_REPLY,
    OUT_OF_SCOPE_REPLY,
    AmbiguousPoi,
    IntentResolver,
    ResolveError,
    Resolution,
    fast_path_intent,
    match_poi,
    nearest_day,
    select_items,
)
from app.schemas.trip_intent_schema import IntentEditOp, IntentPoiMention, IntentPreference, TurnIntent
from app.schemas.trip_plan_schema import (
    AddPoiOp,
    AmapPoi,
    CityInfo,
    MoveItemsOp,
    Plan,
    PoiRef,
    RemoveItemsOp,
    ReplaceItemsOp,
    ReplaceTarget,
    Requirements,
    SetDayLoadOp,
    SetDurationOp,
    SwapItemsOp,
)
from app.schemas.trip_schema import PendingOption, PendingQuestion, TripContext
from tests.trip.draft_data import amap_poi, draft_plan, draft_requirements
from tests.trip.planning_data import HANGZHOU, START_DATE

TODAY = date(2026, 10, 15)
SUZHOU = CityInfo(adcode="320500", name="苏州", lng=120.5853, lat=31.2989)


class FakeAmap:
    """高德客户端替身：按名称返回预置景点和城市，并记录调用。"""

    def __init__(
        self,
        pois: dict[str, list[AmapPoi]] | None = None,
        cities: dict[str, list[CityInfo]] | None = None,
    ) -> None:
        """保存预置结果。"""
        self.pois = pois or {}
        self.cities = cities or {}
        self.poi_calls: list[tuple[str, str, int]] = []
        self.city_calls: list[str] = []

    async def find_pois_by_name(self, city_adcode: str, name: str, *, limit: int = 5) -> list[AmapPoi]:
        """按去掉首尾空白的名称返回预置景点。"""
        self.poi_calls.append((city_adcode, name, limit))
        return list(self.pois.get(name.strip(), []))

    async def resolve_city(self, name: str) -> list[CityInfo]:
        """按名称返回预置城市。"""
        self.city_calls.append(name)
        return list(self.cities.get(name, []))


def named(pid: str, name: str, city_adcode: str = "330100", lng: float = 120.15, lat: float = 30.25) -> AmapPoi:
    """构造指定名称的景点。"""
    return AmapPoi(poi_id=pid, name=name, lng=lng, lat=lat, city_adcode=city_adcode, rating=4.6)


def context(
    status: str = "drafting",
    requirements: Requirements | None = None,
    draft: Plan | None = None,
    previous: Plan | None = None,
    pending: PendingQuestion | None = None,
) -> TripContext:
    """构造会话快照，草稿状态默认带三天草稿。"""
    if draft is None and status != "collecting":
        draft = draft_plan()
    if requirements is None:
        requirements = draft_requirements() if status != "collecting" else Requirements()
    return TripContext(
        trip_id=1, user_id=1, status=status, requirements=requirements, draft=draft, previous_draft=previous,
        pending=pending, today=TODAY,
    )


def options_pending() -> PendingQuestion:
    """带两个选项的待回答问题。"""
    return PendingQuestion(
        kind="infeasible", text="请选择",
        options=[PendingOption(label="允许打车"), PendingOption(label="允许调整其他天的安排", force_full=True)],
    )


async def resolve(ctx: TripContext, intent: TurnIntent, amap: FakeAmap | None = None) -> Resolution:
    """用替身高德客户端解析一条意图。"""
    return await IntentResolver(ctx, amap or FakeAmap()).resolve(intent)


def modify(*ops: IntentEditOp, **fields: object) -> TurnIntent:
    """构造修改意图。"""
    return TurnIntent(kind="modify", ops=list(ops), **fields)


# ---------------------------------------------------------------------------
# 规则快速通道
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "option"),
    [("2", 2), ("选第三个", 3), ("第1项。", 1), ("选择 2", 2), (" 一 ", 1), ("第九条!", 9)],
)
def test_fast_path_option_number(message: str, option: int) -> None:
    """有带选项的待回答问题时，序号回复直接识别为选项。"""
    intent = fast_path_intent(context(pending=options_pending()), message)
    assert intent is not None
    assert intent.kind == "answer_pending"
    assert intent.pending_option == option


@pytest.mark.parametrize("message", ["10", "第2天", "选 2 和 3", "好的"])
def test_fast_path_option_not_matched(message: str) -> None:
    """不是单个序号的回复不走快速通道。"""
    assert fast_path_intent(context(pending=options_pending()), message) is None


def test_fast_path_option_requires_options() -> None:
    """没有待回答问题或问题不带选项时，数字不按选项处理。"""
    assert fast_path_intent(context(), "2") is None
    free = PendingQuestion(kind="missing_info", text="几天？")
    assert fast_path_intent(context(status="collecting", pending=free), "2") is None


@pytest.mark.parametrize("message", ["撤销", " 撤回 ", "恢复上一版", "撤销修改"])
def test_fast_path_undo(message: str) -> None:
    """有草稿时撤销词识别为撤销操作。"""
    intent = fast_path_intent(context(), message)
    assert intent is not None
    assert intent.kind == "modify"
    assert [op.op for op in intent.ops] == ["undo"]


def test_fast_path_undo_without_draft() -> None:
    """没有草稿时撤销词不走快速通道。"""
    assert fast_path_intent(context(status="collecting"), "撤销") is None


@pytest.mark.parametrize("message", ["确认", "确认行程", "就这样"])
def test_fast_path_confirm_words(message: str) -> None:
    """草稿阶段口头确认时提示点击确认按钮。"""
    intent = fast_path_intent(context(), message)
    assert intent is not None
    assert intent.kind == "chat"
    assert "确认行程" in intent.chat_reply


def test_fast_path_confirm_only_in_drafting() -> None:
    """已确认或没有草稿时，确认词不走快速通道。"""
    assert fast_path_intent(context(status="confirmed"), "确认") is None
    assert fast_path_intent(context(status="collecting"), "确认") is None


def test_fast_path_confirm_skipped_with_pending() -> None:
    """有待回答问题时“确定”可能是在回答问题，交给模型理解。"""
    assert fast_path_intent(context(pending=options_pending()), "确认") is None


def test_fast_path_other_text() -> None:
    """普通文字交给模型理解。"""
    assert fast_path_intent(context(), "把第二天的寺庙换成博物馆") is None


# ---------------------------------------------------------------------------
# 景点核实
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_match_poi_exact_name_preferred() -> None:
    """结果中有与名称完全一致的景点时优先返回它。"""
    amap = FakeAmap({"灵隐寺": [named("a", "灵隐寺景区"), named("b", "灵隐寺")]})
    poi = await match_poi(amap, HANGZHOU, " 灵隐寺 ")
    assert poi is not None and poi.poi_id == "b"
    assert amap.poi_calls == [("330100", " 灵隐寺 ", 5)]


@pytest.mark.asyncio
async def test_match_poi_first_contains_name() -> None:
    """首个结果名称包含用户说法时返回首个结果。"""
    amap = FakeAmap({"灵隐": [named("a", "灵隐寺"), named("b", "灵隐飞来峰")]})
    poi = await match_poi(amap, HANGZHOU, "灵隐")
    assert poi is not None and poi.poi_id == "a"


@pytest.mark.asyncio
async def test_match_poi_name_contains_first() -> None:
    """用户说法包含首个结果名称时返回首个结果。"""
    amap = FakeAmap({"杭州西湖断桥残雪": [named("a", "断桥残雪"), named("b", "白堤")]})
    poi = await match_poi(amap, HANGZHOU, "杭州西湖断桥残雪")
    assert poi is not None and poi.poi_id == "a"


@pytest.mark.asyncio
async def test_match_poi_single_distinct_name() -> None:
    """名称不直接对应但结果只有一个不同名称时返回首个结果。"""
    amap = FakeAmap({"飞来峰石刻": [named("a", "飞来峰造像"), named("b", "飞来峰造像")]})
    poi = await match_poi(amap, HANGZHOU, "飞来峰石刻")
    assert poi is not None and poi.poi_id == "a"


@pytest.mark.asyncio
async def test_match_poi_ambiguous() -> None:
    """名称不直接对应且有多个不同结果时抛出歧义，最多列出 3 个候选。"""
    amap = FakeAmap({"老街": [
        named("a", "清河坊历史街区"), named("b", "南宋御街"), named("c", "小河直街"), named("d", "大兜路"),
    ]})
    with pytest.raises(AmbiguousPoi) as info:
        await match_poi(amap, HANGZHOU, "老街")
    assert info.value.name == "老街"
    assert info.value.choices == ["清河坊历史街区", "南宋御街", "小河直街"]


@pytest.mark.asyncio
async def test_match_poi_filters_other_cities() -> None:
    """其他城市的结果被过滤，全部过滤后返回空。"""
    amap = FakeAmap({"拙政园": [named("a", "拙政园", city_adcode="320500")]})
    assert await match_poi(amap, HANGZHOU, "拙政园") is None
    assert await match_poi(FakeAmap(), HANGZHOU, "不存在") is None


# ---------------------------------------------------------------------------
# 选择器展开
# ---------------------------------------------------------------------------


def test_select_items_by_ids_dedup() -> None:
    """点名引用去重后按原顺序返回。"""
    op = IntentEditOp(op="remove", item_ids=["D2-1", "D1-1", "D2-1"])
    assert select_items(draft_plan(), op) == ["D2-1", "D1-1"]


def test_select_items_unknown_id() -> None:
    """引用不存在时报错并列出不存在的引用。"""
    with pytest.raises(ResolveError, match="草稿里没有 D9-9"):
        select_items(draft_plan(), IntentEditOp(op="remove", item_ids=["D1-1", "D9-9"]))


def test_select_items_empty_selector() -> None:
    """既没有引用也没有选择条件时报错。"""
    with pytest.raises(ResolveError, match="没听清"):
        select_items(draft_plan(), IntentEditOp(op="remove"))


def test_select_items_morning_afternoon_boundary() -> None:
    """上午为开始时间早于 12:00（180 分钟），恰好 180 分钟算下午。"""
    plan = draft_plan()
    assert select_items(plan, IntentEditOp(op="remove", day=1, part="morning")) == ["D1-1", "D1-2"]
    assert select_items(plan, IntentEditOp(op="remove", day=1, part="afternoon")) == ["D1-3"]
    assert select_items(plan, IntentEditOp(op="remove", day=2, part="morning")) == ["D2-1", "D2-2"]
    assert select_items(plan, IntentEditOp(op="remove", day=2, part="afternoon")) == ["D2-3"]


def test_select_items_by_tags() -> None:
    """按标签筛选，室内外结合的景点算作户外。"""
    plan = draft_plan()
    assert select_items(plan, IntentEditOp(op="remove", tags=["temple"])) == ["D2-2", "D3-1"]
    assert select_items(plan, IntentEditOp(op="remove", tags=["indoor"])) == ["D1-3"]
    assert select_items(plan, IntentEditOp(op="remove", day=2, tags=["outdoor"])) == ["D2-1", "D2-2", "D2-3"]
    assert select_items(plan, IntentEditOp(op="remove", day=3, part="morning", tags=["religion"])) == ["D3-1"]


def test_select_items_no_match_message() -> None:
    """没有匹配项时报错说明在哪里没有找到哪类景点。"""
    plan = draft_plan()
    with pytest.raises(ResolveError) as info:
        select_items(plan, IntentEditOp(op="remove", tags=["museum", "outdoor"]))
    assert str(info.value) == "行程中没有找到博物馆、户外的景点。"
    with pytest.raises(ResolveError) as info:
        select_items(plan, IntentEditOp(op="remove", day=1, part="afternoon", tags=["temple"]))
    assert str(info.value) == "第1天下午没有找到寺庙道观的景点。"
    with pytest.raises(ResolveError) as info:
        select_items(draft_plan({1: [("duanqiao", 0)]}), IntentEditOp(op="remove", day=1, part="afternoon"))
    assert str(info.value) == "第1天下午没有找到符合条件的景点。"


# ---------------------------------------------------------------------------
# 就近选天
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("spot", "day"), [("duanqiao", 1), ("hefang", 2), ("xixi", 3), ("teamuseum", 3)])
def test_nearest_day(spot: str, day: int) -> None:
    """选择距离当天景点重心最近（并考虑当天景点数）的一天。"""
    assert nearest_day(draft_plan(), amap_poi(spot)) == day


def test_nearest_day_prefers_less_busy_day() -> None:
    """距离相同时景点少的一天更优。"""
    plan = draft_plan({1: [("duanqiao", 0), ("duanqiao", 100)], 2: [("duanqiao", 0)]})
    assert nearest_day(plan, amap_poi("duanqiao")) == 2


def test_nearest_day_empty_day_for_far_poi() -> None:
    """景点离各天都很远时选择空着的一天（空日按 50 计分）。"""
    plan = draft_plan({1: [("duanqiao", 0)], 2: []})
    far = named("sh", "外滩", lng=121.49, lat=31.24)
    assert nearest_day(plan, far) == 2
    assert nearest_day(plan, amap_poi("baidi")) == 1


# ---------------------------------------------------------------------------
# 整轮解析：直接回复类
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_out_of_scope_keeps_pending() -> None:
    """超出范围的请求直接回复，并保留原待回答问题。"""
    pending = options_pending()
    res = await resolve(context(pending=pending), TurnIntent(kind="out_of_scope"))
    assert res.route == "reply"
    assert res.reply == OUT_OF_SCOPE_REPLY
    assert res.pending == pending


@pytest.mark.asyncio
async def test_chat_reply() -> None:
    """闲聊使用模型给的回复，没有时用默认回复。"""
    res = await resolve(context(), TurnIntent(kind="chat", chat_reply="你好呀"))
    assert (res.route, res.reply) == ("reply", "你好呀")
    res = await resolve(context(), TurnIntent(kind="chat"))
    assert res.reply.startswith("好的。")


@pytest.mark.asyncio
async def test_cancel_keeps_draft_and_clears_pending() -> None:
    """放弃本次修改时保持原样，并清除待回答问题。"""
    res = await resolve(context(pending=options_pending()), TurnIntent(kind="cancel"))
    assert res.route == "reply"
    assert res.reply == CANCEL_REPLY
    assert res.pending is None
    assert res.ops == []


@pytest.mark.asyncio
async def test_out_of_scope_with_requirements_still_applies() -> None:
    """一句话里既有需求又有范围外请求时，需求照常生效，范围外部分作为说明。"""
    intent = TurnIntent(kind="out_of_scope", pace="relaxed", unhandled=["帮我订酒店"])
    res = await resolve(context(), intent)
    assert res.route == "plan"
    assert res.requirements.pace == "relaxed"
    assert OUT_OF_SCOPE_REPLY in res.notes
    assert not any("暂时没法处理" in note for note in res.notes)


@pytest.mark.asyncio
async def test_chat_with_requirements_still_applies() -> None:
    """闲聊里带了需求变化时按需求处理。"""
    res = await resolve(context(), TurnIntent(kind="chat", chat_reply="好的", pace="packed"))
    assert res.route == "plan"
    assert res.requirements.pace == "packed"


@pytest.mark.asyncio
async def test_out_of_scope_with_requirements_when_confirmed() -> None:
    """已确认的行程不因夹带需求而进入规划，仍按范围外回复。"""
    res = await resolve(context(status="confirmed"), TurnIntent(kind="out_of_scope", pace="relaxed"))
    assert res.route == "reply"
    assert res.reply == OUT_OF_SCOPE_REPLY


@pytest.mark.asyncio
async def test_op_day_beyond_draft_rejected() -> None:
    """操作指向草稿之外的天时提示天数范围，而不是在结构校验阶段失败。"""
    res = await resolve(context(), modify(IntentEditOp(op="day_load", day=8, level="lighter")))
    assert res.route == "reply"
    assert res.reply == "行程只有 3 天，请指定第 1～3 天。"


@pytest.mark.asyncio
async def test_question_routes_to_qa_even_when_confirmed() -> None:
    """资料问题进入问答，已确认的行程同样可以提问。"""
    res = await resolve(context(status="confirmed"), TurnIntent(kind="question", question="灵隐寺要预约吗？"))
    assert res.route == "qa"
    assert res.question == "灵隐寺要预约吗？"


@pytest.mark.asyncio
async def test_question_without_text() -> None:
    """问题为空时请用户说具体些。"""
    res = await resolve(context(), TurnIntent(kind="question"))
    assert res.route == "reply"
    assert res.reply == "能具体说说想了解什么吗？"


@pytest.mark.asyncio
async def test_confirmed_rejects_modification() -> None:
    """已确认的行程不能修改，并清除待回答问题。"""
    res = await resolve(
        context(status="confirmed", pending=options_pending()),
        modify(IntentEditOp(op="remove", item_ids=["D1-1"])),
    )
    assert res.route == "reply"
    assert res.reply == CONFIRMED_REPLY
    assert res.pending is None
    assert res.ops == []


# ---------------------------------------------------------------------------
# 整轮解析：收集阶段
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collecting_missing_fields_asks_question() -> None:
    """收集阶段缺少日期和天数时追问，并给出日期示例。"""
    amap = FakeAmap(cities={"杭州": [HANGZHOU]})
    res = await resolve(context(status="collecting"), TurnIntent(kind="plan_request", city="杭州"), amap)
    assert res.route == "reply"
    assert res.requirements.city == HANGZHOU
    assert res.reply == "还需要知道出发日期、游玩天数，才能开始规划。（例如“2026-10-22 出发”）"
    assert res.pending is not None and res.pending.kind == "missing_info"
    assert amap.city_calls == ["杭州"]


@pytest.mark.asyncio
async def test_collecting_missing_city_only() -> None:
    """只缺城市时不给日期示例。"""
    intent = TurnIntent(kind="plan_request", start_date=date(2026, 10, 20), days=2)
    res = await resolve(context(status="collecting"), intent)
    assert res.reply == "还需要知道目的地城市，才能开始规划。"


@pytest.mark.asyncio
async def test_collecting_complete_goes_to_plan() -> None:
    """信息齐全时进入首次规划，不启用稳定性奖励。"""
    amap = FakeAmap(cities={"杭州": [HANGZHOU]})
    intent = TurnIntent(
        kind="plan_request", city="杭州", start_date=date(2026, 10, 20), days=2, pace="relaxed",
        preferences=[IntentPreference(tag="museum", weight=2)], walk_only=True, max_walk_minutes=30,
    )
    res = await resolve(context(status="collecting"), intent, amap)
    assert res.route == "plan"
    assert res.stability is False
    assert res.ops == []
    req = res.requirements
    assert (req.city, req.start_date, req.days, req.pace) == (HANGZHOU, date(2026, 10, 20), 2, "relaxed")
    assert req.preferences == {"museum": 2}
    assert req.transport.walk_only is True and req.transport.max_walk_minutes == 30


@pytest.mark.asyncio
async def test_days_over_limit_rejected() -> None:
    """天数超过 7 天时拒绝，需求保持不变。"""
    ctx = context(status="collecting", requirements=Requirements(city=HANGZHOU))
    res = await resolve(ctx, TurnIntent(kind="plan_request", days=8))
    assert res.route == "reply"
    assert "1～7" in res.reply
    assert res.requirements.days is None


@pytest.mark.asyncio
async def test_past_start_date_rejected() -> None:
    """出发日期早于今天时拒绝；当天出发可以。"""
    ctx = context(status="collecting", requirements=Requirements(city=HANGZHOU, days=2))
    res = await resolve(ctx, TurnIntent(kind="plan_request", start_date=date(2026, 10, 14)))
    assert res.route == "reply"
    assert res.reply == "出发日期 2026-10-14 早于今天，请换一个日期。"
    assert res.requirements.start_date is None
    res = await resolve(ctx, TurnIntent(kind="plan_request", start_date=TODAY))
    assert res.route == "plan"


@pytest.mark.asyncio
async def test_city_not_found() -> None:
    """城市查不到时请用户确认名称。"""
    res = await resolve(context(status="collecting"), TurnIntent(kind="plan_request", city="火星"))
    assert res.route == "reply"
    assert "没有找到城市“火星”" in res.reply


@pytest.mark.asyncio
async def test_city_ambiguous_offers_options() -> None:
    """一个名称对应多个城市时给出带需求的选项。"""
    other = CityInfo(adcode="330200", name="宁波", lng=121.55, lat=29.87)
    amap = FakeAmap(cities={"浙": [HANGZHOU, other]})
    res = await resolve(context(status="collecting"), TurnIntent(kind="plan_request", city="浙", days=2), amap)
    assert res.route == "reply"
    assert res.reply == "“浙”对应多个城市，请回复序号：1. 杭州；2. 宁波"
    assert res.pending is not None and res.pending.kind == "ambiguous"
    assert [o.requirements.city for o in res.pending.options] == [HANGZHOU, other]
    # 城市未确定时本轮的其他字段不生效
    assert res.requirements.days is None


@pytest.mark.asyncio
async def test_switch_city_in_collecting_clears_poi_refs() -> None:
    """收集阶段更换城市时清空与原城市绑定的必去和不去景点。"""
    req = Requirements(
        city=HANGZHOU, must_visit=[PoiRef(poi_id="lingyin", name="灵隐寺")], duration_overrides={"lingyin": 100},
    )
    amap = FakeAmap(cities={"苏州": [SUZHOU]})
    res = await resolve(context(status="collecting", requirements=req), TurnIntent(kind="switch_city", city="苏州"), amap)
    assert res.requirements.city == SUZHOU
    assert res.requirements.must_visit == []
    assert res.requirements.duration_overrides == {}
    assert "已更换城市，原来指定的必去和不去景点已清空。" in res.reply


@pytest.mark.asyncio
async def test_switch_city_without_name() -> None:
    """换城市却没说城市时追问。"""
    res = await resolve(context(status="collecting"), TurnIntent(kind="switch_city"))
    assert res.reply == "想换到哪个城市呢？"


@pytest.mark.asyncio
async def test_collecting_mentions_without_city_noted() -> None:
    """没有城市时指定必去景点会提示先给城市。"""
    intent = TurnIntent(kind="plan_request", must_visit=[IntentPoiMention(name="灵隐寺")])
    res = await resolve(context(status="collecting"), intent)
    assert res.reply.startswith("请先告诉我目的地城市，之后再指定必去或不去的景点。\n还需要知道")


@pytest.mark.asyncio
async def test_collecting_must_visit_recorded() -> None:
    """收集阶段核实必去景点并记入需求，指定天超出天数时不限定天。"""
    amap = FakeAmap({"灵隐寺": [amap_poi("lingyin")], "宋城": [amap_poi("songcheng")]})
    ctx = context(status="collecting", requirements=Requirements(city=HANGZHOU, start_date=START_DATE, days=2))
    intent = TurnIntent(
        kind="plan_request",
        must_visit=[IntentPoiMention(name="灵隐寺", day=2), IntentPoiMention(name="宋城", day=3)],
        avoid=["不存在的景点"],
    )
    res = await resolve(ctx, intent, amap)
    assert res.route == "plan"
    assert res.requirements.must_visit == [
        PoiRef(poi_id="lingyin", name="灵隐寺", day=2), PoiRef(poi_id="songcheng", name="宋城", day=None),
    ]
    assert res.notes == ["没有在杭州找到“不存在的景点”，已忽略。"]


@pytest.mark.asyncio
async def test_collecting_ops_noted() -> None:
    """还没有草稿时具体景点的修改只给说明。"""
    ctx = context(status="collecting", requirements=Requirements(city=HANGZHOU, start_date=START_DATE, days=2))
    res = await resolve(ctx, modify(IntentEditOp(op="remove", item_ids=["D1-1"])))
    assert res.route == "plan"
    assert res.ops == []
    assert res.notes == ["还没有生成草稿，具体景点的调整请在草稿生成后再提。"]


# ---------------------------------------------------------------------------
# 整轮解析：草稿阶段的城市与全局变化
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_city_locked_in_drafting() -> None:
    """草稿生成后不能更换城市，也不查询高德。"""
    amap = FakeAmap(cities={"苏州": [SUZHOU]})
    res = await resolve(context(), TurnIntent(kind="switch_city", city="苏州"), amap)
    assert res.route == "reply"
    assert res.reply == "草稿生成后城市已锁定为杭州，不能更换。如需规划其他城市，请新建一个行程。"
    assert amap.city_calls == []
    assert res.requirements.city == HANGZHOU


@pytest.mark.asyncio
async def test_same_city_in_drafting_is_fine() -> None:
    """草稿阶段重复提到当前城市不算更换。"""
    res = await resolve(context(), modify(city="杭州"))
    assert res.route == "reply"
    assert res.reply.startswith("没有识别到需要调整的地方")


@pytest.mark.asyncio
async def test_global_pace_change_replans_full() -> None:
    """节奏变化且没有具体修改时整体重排，不带操作。"""
    res = await resolve(context(), modify(pace="packed"))
    assert res.route == "plan"
    assert res.requirements.pace == "packed"
    assert res.ops == []
    assert res.force_full is False


@pytest.mark.asyncio
async def test_global_change_with_ops_forces_full() -> None:
    """全局变化同时有具体修改时带操作并允许全量重排。"""
    res = await resolve(context(), modify(IntentEditOp(op="remove", item_ids=["D1-2"]), pace="relaxed"))
    assert res.route == "plan"
    assert res.force_full is True
    assert res.ops == [RemoveItemsOp(item_ids=["D1-2"])]


@pytest.mark.asyncio
async def test_days_change_drops_ops_with_note() -> None:
    """天数变化时整体重排，其他具体调整给出说明后丢弃。"""
    res = await resolve(context(), modify(IntentEditOp(op="move", item_ids=["D1-1"], to_day=2), days=2))
    assert res.route == "plan"
    assert res.ops == []
    assert res.stability is True
    assert res.requirements.days == 2
    assert "天数变化后会整体重新编排，其他具体调整请在新草稿上再提一次。" in res.notes


@pytest.mark.asyncio
async def test_days_change_trims_day_loads_and_must_visit_days() -> None:
    """天数缩短时删除超出天数的松紧调整，必去景点超出的指定天改为不限定。"""
    req = draft_requirements(
        day_loads={1: "lighter", 3: "fuller"}, must_visit=[PoiRef(poi_id="lingyin", name="灵隐寺", day=3)],
    )
    res = await resolve(context(requirements=req), modify(days=2))
    assert res.requirements.day_loads == {1: "lighter"}
    assert res.requirements.must_visit == [PoiRef(poi_id="lingyin", name="灵隐寺", day=None)]


@pytest.mark.asyncio
async def test_days_change_with_remove_only_no_note() -> None:
    """天数变化时删除操作已记入不去列表，不需要额外说明。"""
    res = await resolve(context(), modify(IntentEditOp(op="remove", item_ids=["D1-2"]), days=2))
    assert res.notes == []
    assert [r.poi_id for r in res.requirements.avoid] == ["baidi"]


@pytest.mark.asyncio
async def test_unhandled_noted() -> None:
    """无法处理的原话片段作为说明回复。"""
    res = await resolve(context(), modify(unhandled=["要坐游船"]))
    assert res.route == "reply"
    assert res.reply == "这些要求暂时没法处理：要坐游船。"


# ---------------------------------------------------------------------------
# 整轮解析：修改操作转换
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_op_updates_avoid_and_must_visit() -> None:
    """删除转为删除操作，被删景点加入不去列表并移出必去列表。"""
    req = draft_requirements(must_visit=[PoiRef(poi_id="baidi", name="白堤")])
    res = await resolve(context(requirements=req), modify(IntentEditOp(op="remove", item_ids=["D1-2"])))
    assert res.route == "plan"
    assert res.ops == [RemoveItemsOp(item_ids=["D1-2"])]
    assert res.force_full is False
    assert res.requirements.must_visit == []
    assert res.requirements.avoid == [PoiRef(poi_id="baidi", name="白堤")]


@pytest.mark.asyncio
async def test_remove_by_selector() -> None:
    """按天和标签选择删除对象。"""
    res = await resolve(context(), modify(IntentEditOp(op="remove", day=2, tags=["temple"])))
    assert res.ops == [RemoveItemsOp(item_ids=["D2-2"])]


@pytest.mark.asyncio
async def test_remove_unknown_reference() -> None:
    """删除不存在的引用时直接回复错误。"""
    res = await resolve(context(), modify(IntentEditOp(op="remove", item_ids=["D9-9"])))
    assert res.route == "reply"
    assert res.reply == "草稿里没有 D9-9，请对照当前草稿再说一次。"
    assert res.requirements == draft_requirements()


@pytest.mark.asyncio
async def test_replace_with_tags() -> None:
    """按标签替换转为替换操作，被替换景点加入不去列表。"""
    res = await resolve(
        context(), modify(IntentEditOp(op="replace", day=2, tags=["temple"], target_tags=["museum"], target_count=2)),
    )
    assert res.ops == [ReplaceItemsOp(item_ids=["D2-2"], target=ReplaceTarget(tags=["museum"], count=2))]
    assert res.requirements.avoid == [PoiRef(poi_id="jingci", name="净慈寺")]


@pytest.mark.asyncio
async def test_replace_with_named_poi() -> None:
    """指定替换成的景点时核实名称，并从不去列表中移除该景点。"""
    tea = amap_poi("teamuseum")
    req = draft_requirements(avoid=[PoiRef(poi_id="teamuseum", name="中国茶叶博物馆")])
    amap = FakeAmap({"茶叶博物馆": [tea]})
    res = await resolve(
        context(requirements=req), modify(IntentEditOp(op="replace", item_ids=["D2-2"], poi_name="茶叶博物馆")), amap,
    )
    assert res.ops == [ReplaceItemsOp(
        item_ids=["D2-2"], target=ReplaceTarget(poi=PoiRef(poi_id="teamuseum", name="中国茶叶博物馆")),
    )]
    assert res.requirements.avoid == [PoiRef(poi_id="jingci", name="净慈寺")]


@pytest.mark.asyncio
async def test_replace_with_poi_already_in_plan() -> None:
    """替换成已在行程中的景点时提示改用移动。"""
    amap = FakeAmap({"灵隐寺": [amap_poi("lingyin")]})
    res = await resolve(context(), modify(IntentEditOp(op="replace", item_ids=["D2-2"], poi_name="灵隐寺")), amap)
    assert res.route == "reply"
    assert res.reply == "灵隐寺 已经在行程里了，可以说“把它移到第几天”。"


@pytest.mark.asyncio
async def test_replace_with_unknown_poi() -> None:
    """替换成的景点查不到时直接回复。"""
    res = await resolve(context(), modify(IntentEditOp(op="replace", item_ids=["D2-2"], poi_name="火星基地")))
    assert res.reply == "没有在杭州找到“火星基地”。"


@pytest.mark.asyncio
async def test_ambiguous_poi_asks_full_name() -> None:
    """景点名称有歧义时追问完整名称，待回答问题没有选项。"""
    amap = FakeAmap({"老街": [named("a", "清河坊历史街区"), named("b", "南宋御街")]})
    res = await resolve(context(), modify(IntentEditOp(op="add", poi_name="老街")), amap)
    assert res.route == "reply"
    assert res.reply == "你说的“老街”是指 清河坊历史街区、南宋御街 中的哪一个？请说出完整名称。"
    assert res.pending is not None and res.pending.kind == "ambiguous" and res.pending.options == []


@pytest.mark.asyncio
async def test_add_with_day() -> None:
    """指定天新增时转为新增操作并记入必去。"""
    amap = FakeAmap({"河坊街": [amap_poi("hefang")]})
    req = draft_requirements(avoid=[PoiRef(poi_id="hefang", name="河坊街")])
    res = await resolve(context(requirements=req), modify(IntentEditOp(op="add", poi_name="河坊街", day=1)), amap)
    ref = PoiRef(poi_id="hefang", name="河坊街", day=1)
    assert res.route == "plan"
    assert res.ops == [AddPoiOp(poi=ref)]
    assert res.requirements.must_visit == [ref]
    assert res.requirements.avoid == []


@pytest.mark.asyncio
async def test_add_without_day_uses_nearest_day() -> None:
    """未指定天时新增到最近的一天。"""
    amap = FakeAmap({"西溪湿地": [amap_poi("xixi")]})
    res = await resolve(context(), modify(IntentEditOp(op="add", poi_name="西溪湿地")), amap)
    assert res.ops == [AddPoiOp(poi=PoiRef(poi_id="xixi", name="西溪湿地", day=3))]


@pytest.mark.asyncio
async def test_add_existing_poi_noted() -> None:
    """新增已在行程中的景点时只给说明。"""
    amap = FakeAmap({"雷峰塔": [amap_poi("leifeng")]})
    res = await resolve(context(), modify(IntentEditOp(op="add", poi_name="雷峰塔")), amap)
    assert res.route == "reply"
    assert res.reply == "雷峰塔 已经在行程里了。"


@pytest.mark.asyncio
async def test_add_day_out_of_range() -> None:
    """新增到超出草稿天数的天时报错。"""
    amap = FakeAmap({"河坊街": [amap_poi("hefang")]})
    res = await resolve(context(), modify(IntentEditOp(op="add", poi_name="河坊街", day=5)), amap)
    assert res.reply == "行程只有 3 天，请指定第 1～3 天。"


@pytest.mark.asyncio
async def test_add_by_tags_becomes_fuller_day() -> None:
    """新增某类景点但没有名称时，指定了天就把当天调得更充实，并给出说明。"""
    res = await resolve(context(), modify(IntentEditOp(op="add", day=2, target_tags=["museum"])))
    assert res.route == "plan"
    assert res.ops == [SetDayLoadOp(day=2, level="fuller")]
    assert res.notes == ["已把第2天调得充实一点；想新增指定景点可以直接说出名称。"]


@pytest.mark.asyncio
async def test_add_by_tags_without_day_is_noop() -> None:
    """新增某类景点且没有指定天时不改动草稿，直接回复说明。"""
    res = await resolve(context(), modify(IntentEditOp(op="add", target_tags=["museum"])))
    assert res.route == "reply"
    assert res.requirements.day_loads == {}
    assert res.reply == "新增某一类景点需要说出具体名称，或者说“把第几天调得充实一点”。"


@pytest.mark.asyncio
async def test_add_without_name_or_tags() -> None:
    """新增却没有景点名称和标签时追问。"""
    res = await resolve(context(), modify(IntentEditOp(op="add")))
    assert res.reply == "想新增哪个景点呢？"


@pytest.mark.asyncio
async def test_move_by_ids_updates_must_visit_day() -> None:
    """点名移动时忽略选择器的天，必去景点的指定天随之更新。"""
    req = draft_requirements(must_visit=[PoiRef(poi_id="duanqiao", name="断桥残雪", day=1)])
    res = await resolve(
        context(requirements=req), modify(IntentEditOp(op="move", item_ids=["D1-1"], day=3, to_day=3)),
    )
    assert res.ops == [MoveItemsOp(item_ids=["D1-1"], to_day=3)]
    assert res.requirements.must_visit == [PoiRef(poi_id="duanqiao", name="断桥残雪", day=3)]


@pytest.mark.asyncio
async def test_move_by_selector() -> None:
    """按天和时段选择移动对象。"""
    res = await resolve(context(), modify(IntentEditOp(op="move", day=1, part="morning", to_day=2)))
    assert res.ops == [MoveItemsOp(item_ids=["D1-1", "D1-2"], to_day=2)]


@pytest.mark.asyncio
async def test_move_without_target_day() -> None:
    """移动缺少目标天时报错。"""
    res = await resolve(context(), modify(IntentEditOp(op="move", item_ids=["D1-1"])))
    assert res.reply == "行程只有 3 天，请指定第 1～3 天。"


@pytest.mark.asyncio
async def test_swap_across_days() -> None:
    """交换不同天的两个景点。"""
    res = await resolve(context(), modify(IntentEditOp(op="swap", item_ids=["D1-2", "D3-2"])))
    assert res.ops == [SwapItemsOp(a="D1-2", b="D3-2")]


@pytest.mark.asyncio
async def test_swap_same_day_noted() -> None:
    """同一天内交换只给说明。"""
    res = await resolve(context(), modify(IntentEditOp(op="swap", item_ids=["D1-1", "D1-2"])))
    assert res.route == "reply"
    assert res.reply == "同一天内的游览顺序会按路线自动优化，无需手动交换。"


@pytest.mark.asyncio
async def test_swap_needs_two_items() -> None:
    """交换的景点不是恰好两个时报错。"""
    res = await resolve(context(), modify(IntentEditOp(op="swap", item_ids=["D1-1", "D2-1", "D3-1"])))
    assert res.reply.startswith("交换需要恰好两个景点")


@pytest.mark.asyncio
async def test_set_duration() -> None:
    """调整时长转为时长操作并记入时长覆盖。"""
    res = await resolve(context(), modify(IntentEditOp(op="set_duration", item_ids=["D3-1"], minutes=120)))
    assert res.ops == [SetDurationOp(item_id="D3-1", minutes=120)]
    assert res.requirements.duration_overrides == {"lingyin": 120}


@pytest.mark.asyncio
async def test_set_duration_without_minutes() -> None:
    """调整时长缺少分钟数时追问。"""
    res = await resolve(context(), modify(IntentEditOp(op="set_duration", item_ids=["D3-1"])))
    assert res.reply.startswith("想调整成玩多久呢？")


@pytest.mark.asyncio
async def test_day_load() -> None:
    """调整松紧转为松紧操作并记入需求。"""
    res = await resolve(context(), modify(IntentEditOp(op="day_load", day=2, level="lighter")))
    assert res.ops == [SetDayLoadOp(day=2, level="lighter")]
    assert res.requirements.day_loads == {2: "lighter"}


@pytest.mark.asyncio
async def test_day_load_without_level() -> None:
    """调整松紧缺少方向时追问。"""
    res = await resolve(context(), modify(IntentEditOp(op="day_load", day=2)))
    assert res.reply == "想让这一天轻松一点还是充实一点？"


@pytest.mark.asyncio
async def test_drafting_must_visit_mentions() -> None:
    """草稿阶段提到必去景点：已在别的天则移动，已在同一天给说明，不在草稿则新增到最近的天。"""
    amap = FakeAmap({
        "灵隐寺": [amap_poi("lingyin")], "雷峰塔": [amap_poi("leifeng")], "河坊街": [amap_poi("hefang")],
    })
    intent = modify(must_visit=[
        IntentPoiMention(name="灵隐寺", day=1), IntentPoiMention(name="雷峰塔"), IntentPoiMention(name="河坊街"),
    ])
    res = await resolve(context(), intent, amap)
    assert res.route == "plan"
    assert res.ops == [
        MoveItemsOp(item_ids=["D3-1"], to_day=1),
        AddPoiOp(poi=PoiRef(poi_id="hefang", name="河坊街", day=2)),
    ]
    assert res.notes == ["雷峰塔 已经在第2天的安排里。"]
    assert [r.poi_id for r in res.requirements.must_visit] == ["lingyin", "leifeng", "hefang"]


@pytest.mark.asyncio
async def test_drafting_avoid_mention_removes_item() -> None:
    """草稿阶段提到不去的景点：已在草稿中则删除，并移出必去列表。"""
    amap = FakeAmap({"净慈寺": [amap_poi("jingci")]})
    req = draft_requirements(must_visit=[PoiRef(poi_id="jingci", name="净慈寺")])
    res = await resolve(context(requirements=req), modify(avoid=["净慈寺"]), amap)
    assert res.ops == [RemoveItemsOp(item_ids=["D2-2"])]
    assert res.requirements.must_visit == []
    assert res.requirements.avoid == [PoiRef(poi_id="jingci", name="净慈寺")]


# ---------------------------------------------------------------------------
# 整轮解析：撤销与选项
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_undo_restores_days_and_date() -> None:
    """撤销时天数和出发日期随上一版草稿恢复。"""
    previous = draft_plan({1: [("duanqiao", 0)], 2: [("leifeng", 0)]})
    res = await resolve(context(previous=previous), modify(IntentEditOp(op="undo")))
    assert res.route == "undo"
    assert res.requirements.days == 2
    assert res.requirements.start_date == previous.start_date


@pytest.mark.asyncio
async def test_undo_without_previous() -> None:
    """没有上一版草稿时不能撤销。"""
    res = await resolve(context(), modify(IntentEditOp(op="undo")))
    assert res.route == "reply"
    assert res.reply == "没有可以撤销的上一版草稿。"


@pytest.mark.asyncio
async def test_answer_pending_out_of_range() -> None:
    """选项序号超出范围时提示有效范围并保留问题。"""
    pending = options_pending()
    res = await resolve(context(pending=pending), TurnIntent(kind="answer_pending", pending_option=3))
    assert res.route == "reply"
    assert res.reply == "请回复 1～2 之间的序号。"
    assert res.pending == pending


@pytest.mark.asyncio
async def test_answer_pending_without_pending() -> None:
    """没有待选择的问题时提示直接说想法。"""
    res = await resolve(context(), TurnIntent(kind="answer_pending", pending_option=1))
    assert res.reply == "现在没有需要选择的选项，请直接告诉我你的想法。"


@pytest.mark.asyncio
async def test_answer_pending_collecting_uses_option_requirements() -> None:
    """收集阶段选择选项后使用选项中的需求，信息齐全则开始规划。"""
    chosen = Requirements(city=HANGZHOU, start_date=START_DATE, days=2)
    pending = PendingQuestion(kind="ambiguous", text="哪个城市？", options=[PendingOption(label="杭州", requirements=chosen)])
    res = await resolve(context(status="collecting", pending=pending), TurnIntent(kind="answer_pending", pending_option=1))
    assert res.route == "plan"
    assert res.requirements == chosen
    assert res.stability is False


@pytest.mark.asyncio
async def test_answer_pending_drafting_with_ops() -> None:
    """草稿阶段选择带操作的选项时按选项执行，并带上全量重排标记。"""
    ops = [RemoveItemsOp(item_ids=["D1-2"])]
    pending = PendingQuestion(kind="infeasible", text="选一个", options=[
        PendingOption(label="允许调整其他天的安排", ops=ops, force_full=True),
    ])
    res = await resolve(context(pending=pending), TurnIntent(kind="answer_pending", pending_option=1))
    assert res.route == "plan"
    assert res.ops == ops
    assert res.force_full is True
    assert res.requirements == draft_requirements()


@pytest.mark.asyncio
async def test_answer_pending_drafting_without_ops() -> None:
    """草稿阶段选择不带操作的选项时按新需求全量重排。"""
    relaxed = draft_requirements(pace="relaxed")
    pending = PendingQuestion(kind="suggestion", text="选一个", options=[PendingOption(label="轻松", requirements=relaxed)])
    res = await resolve(context(pending=pending), TurnIntent(kind="answer_pending", pending_option=1))
    assert res.route == "plan"
    assert res.ops == []
    assert res.force_full is True
    assert res.requirements.pace == "relaxed"


@pytest.mark.asyncio
async def test_add_by_tags_records_day_load() -> None:
    """按类别新增转为“更充实”时，需求中的当天松紧应同步记为 fuller。"""
    res = await resolve(context(), modify(IntentEditOp(op="add", day=2, target_tags=["museum"])))
    assert res.requirements.day_loads == {2: "fuller"}
