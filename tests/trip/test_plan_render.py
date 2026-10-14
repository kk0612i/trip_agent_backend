"""回复渲染测试：单日文字、整份草稿文字和本轮回复组合。"""

from app.agent.trip.plan_render import CONFIRM_HINT, compose_reply, day_text, plan_text
from app.schemas.trip_plan_schema import DayWeather, LunchBreak, Plan, PlanDay, TravelLeg
from tests.trip.draft_data import draft_plan, plan_item
from tests.trip.planning_data import START_DATE

WALK = TravelLeg(mode="walk", minutes=12, distance_km=0.64)
TAXI = TravelLeg(mode="taxi", minutes=31, distance_km=4.96, fee_yuan=25.4)


def one_day(*items, lunch: LunchBreak | None = None, weather: DayWeather | None = None) -> PlanDay:
    """构造第 1 天（2026-10-20，周二）。"""
    return PlanDay(day=1, date=START_DATE, items=list(items), lunch=lunch, weather=weather)


def test_day_text_empty_day() -> None:
    """没有景点的一天显示自由活动。"""
    assert day_text(one_day()) == "第1天 10月20日 周二\n  自由活动"


def test_day_text_weather_in_header() -> None:
    """有天气预报时标题追加白天天气。"""
    weather = DayWeather(date=START_DATE, day_weather="小雨", rain_level=1)
    assert day_text(one_day(weather=weather)).splitlines()[0] == "第1天 10月20日 周二 · 小雨"


def test_day_text_items_legs_and_lunch_between() -> None:
    """景点按时间列出，交通在景点前，景点外午餐插在午餐之后开始的第一个景点前。"""
    day = one_day(
        plan_item("duanqiao", "D1-1", 0),
        plan_item("baidi", "D1-2", 72).model_copy(update={"travel_from_prev": WALK}),
        plan_item("zjmuseum", "D1-3", 200),
        lunch=LunchBreak(start_min=132, end_min=192),
    )
    assert day_text(day).splitlines() == [
        "第1天 10月20日 周二",
        "  09:00-10:00 断桥残雪（游玩约 60 分钟）",
        "    ↓ 步行约 12 分钟（直线 0.6 公里）",
        "  10:12-11:12 白堤（游玩约 60 分钟）",
        "  11:12-12:12 午餐（自由安排）",
        "  12:20-14:20 浙江省博物馆孤山馆区（游玩约 120 分钟）",
    ]


def test_day_text_lunch_after_last_item() -> None:
    """午餐在最后一个景点之后时放在末尾。"""
    day = one_day(plan_item("duanqiao", "D1-1", 90), lunch=LunchBreak(start_min=150, end_min=210))
    assert day_text(day).splitlines()[-1] == "  11:30-12:30 午餐（自由安排）"


def test_day_text_lunch_inside_item() -> None:
    """园内午餐只在景点后注明，不单独成行；打车段显示费用。"""
    lingyin = plan_item("lingyin", "D1-2", 120).model_copy(
        update={"lunch_inside": True, "end_min": 360, "travel_from_prev": TAXI},
    )
    day = one_day(
        plan_item("leifeng", "D1-1", 0), lingyin,
        lunch=LunchBreak(start_min=180, end_min=240, inside_item_id="D1-2"),
    )
    lines = day_text(day).splitlines()
    assert lines[2] == "    ↓ 打车约 31 分钟（直线 5.0 公里，约 25 元）"
    assert lines[3] == "  11:00-15:00 灵隐寺（游玩约 180 分钟，含园内午餐）"
    assert not any("午餐（自由安排）" in line for line in lines)


def test_plan_text_joins_days_and_warnings() -> None:
    """整份草稿按天用空行分隔，提示信息列在最后。"""
    plan = draft_plan({1: [("duanqiao", 0)], 2: []}).model_copy(update={"warnings": ["注意带伞。", "费用未列出。"]})
    assert plan_text(plan) == (
        "第1天 10月20日 周二\n  09:00-10:00 断桥残雪（游玩约 60 分钟）\n\n"
        "第2天 10月21日 周三\n  自由活动\n\n"
        "提示：\n- 注意带伞。\n- 费用未列出。"
    )


def test_plan_text_without_warnings() -> None:
    """没有提示信息时不输出提示段落。"""
    assert "提示" not in plan_text(draft_plan())


# ---------------------------------------------------------------------------
# 回复组合
# ---------------------------------------------------------------------------


def small_plan() -> Plan:
    """两天的小草稿。"""
    return draft_plan({1: [("duanqiao", 0)], 2: [("leifeng", 0)]})


def test_compose_first_plan() -> None:
    """首次生成草稿时以城市和天数开头，结尾附确认提示。"""
    plan = small_plan()
    reply = compose_reply(new_plan=plan, first_plan=True, change_summary=None, reply=None, notes=[])
    assert reply == "\n\n".join(["已为你生成杭州 2 天的景点行程草稿：", plan_text(plan), CONFIRM_HINT])


def test_compose_modified_plan_with_summary_and_notes() -> None:
    """修改后的草稿依次包含说明、变化摘要、草稿、附加说明和确认提示。"""
    plan = small_plan()
    reply = compose_reply(
        new_plan=plan, first_plan=False, change_summary="第1天：移除 白堤。", reply=None, notes=["说明一", "说明二"],
    )
    assert reply.split("\n\n")[:2] == ["已按你的要求调整草稿。", "第1天：移除 白堤。"]
    assert reply.endswith("说明一\n说明二\n\n" + CONFIRM_HINT)


def test_compose_reply_overrides_header() -> None:
    """带回复文字的新草稿（如撤销）用回复文字作开头。"""
    plan = small_plan()
    reply = compose_reply(new_plan=plan, first_plan=False, change_summary=None, reply="已撤销上一次修改。", notes=[])
    assert reply.startswith("已撤销上一次修改。\n\n第1天")
    assert "已按你的要求调整草稿" not in reply


def test_compose_reply_only() -> None:
    """没有新草稿时只有回复和说明，不附确认提示。"""
    reply = compose_reply(new_plan=None, first_plan=False, change_summary="忽略", reply="好的", notes=["补充"])
    assert reply == "好的\n\n补充"


def test_compose_notes_dedup_and_skip_reply() -> None:
    """说明去重，与回复相同或为空的说明不重复输出。"""
    reply = compose_reply(new_plan=None, first_plan=False, change_summary=None, reply="A", notes=["A", "B", "", "B"])
    assert reply == "A\n\nB"


def test_compose_empty() -> None:
    """什么都没有时回复“好的。”。"""
    assert compose_reply(new_plan=None, first_plan=True, change_summary=None, reply=None, notes=[]) == "好的。"
