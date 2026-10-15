"""意图评估判分器：字段默认值、备选标签、通配、操作匹配和名称宽松匹配。"""

from datetime import date

from app.schemas.trip_intent_schema import IntentEditOp, IntentPoiMention, IntentPreference, TurnIntent
from evals.intent.dataset import Expected
from evals.intent.grading import city_match, effective_kind, grade_intent, match_tag_groups, names_match
from evals.intent.scenarios import build_context


def expected(**data: object) -> Expected:
    """构造标注。"""
    return Expected.model_validate(data)


def test_exact_match_passes() -> None:
    """完全一致时通过，城市忽略“市”后缀。"""
    expect = expected(kind="plan_request", city="杭州", start_date="2026-10-23", days=3, preferences={"museum": 1})
    intent = TurnIntent(
        kind="plan_request", city="杭州市", start_date=date(2026, 10, 23), days=3,
        preferences=[IntentPreference(tag="museum")],
    )
    grade = grade_intent(expect, intent)
    assert grade.passed, grade.failed_fields()
    assert grade.fields["days"].expected_set and not grade.fields["pace"].expected_set


def test_unlisted_field_must_stay_empty() -> None:
    """标注未写的字段被模型多填时判错，用来发现沿用旧需求。"""
    grade = grade_intent(expected(kind="plan_request", days=4), TurnIntent(kind="plan_request", days=4, city="杭州"))
    assert not grade.passed
    assert set(grade.failed_fields()) == {"city"}
    assert grade.fields["city"].expected_set is False


def test_ignore_skips_field() -> None:
    """ignore 中的字段不判分。"""
    grade = grade_intent(
        expected(kind="plan_request", days=4), TurnIntent(kind="plan_request", days=4, city="杭州"), ["city"],
    )
    assert grade.passed and "city" not in grade.fields


def test_kind_alternatives() -> None:
    """kind 列表中任一都算对。"""
    expect = expected(kind=["out_of_scope", "chat"], chat_reply=True)
    assert grade_intent(expect, TurnIntent(kind="chat", chat_reply="好的")).passed
    assert not grade_intent(expect, TurnIntent(kind="question", question="x")).passed


def test_kind_downstream_equivalence() -> None:
    """下游走同一分支的意图类型视为等价；switch_city 没有城市、answer_pending 带选项时不等价。"""
    assert effective_kind("plan_request") == effective_kind("modify") == "apply"
    assert effective_kind("switch_city", TurnIntent(kind="switch_city", city="苏州")) == "apply"
    assert effective_kind("switch_city", TurnIntent(kind="switch_city")) == "switch_city"
    assert effective_kind("answer_pending", TurnIntent(kind="answer_pending")) == "apply"
    assert effective_kind("answer_pending", TurnIntent(kind="answer_pending", pending_option=1)) == "answer_pending"
    assert effective_kind("cancel") == "cancel"
    expect = expected(kind="plan_request", days=4)
    assert grade_intent(expect, TurnIntent(kind="modify", days=4)).passed
    assert not grade_intent(expected(kind="modify"), TurnIntent(kind="cancel")).passed


def test_named_and_selector_ops_equivalent_on_draft() -> None:
    """点名写法和选择器写法在场景草稿上选中同一批景点时等价，选中不同则判错。"""
    draft = build_context("drafting_hz3d").draft
    expect = expected(kind="modify", ops=[
        {"op": "replace", "day": 1, "tags": ["outdoor"], "target_tags": ["museum", "art_gallery"]},
    ])
    named = TurnIntent(kind="modify", ops=[IntentEditOp(
        op="replace", item_ids=["D1-1", "D1-2", "D1-4"], target_tags=["museum", "art_gallery"],
    )])
    assert grade_intent(expect, named, draft=draft).passed
    assert not grade_intent(expect, named).passed
    partial = TurnIntent(kind="modify", ops=[IntentEditOp(
        op="replace", item_ids=["D1-1", "D1-2"], target_tags=["museum", "art_gallery"],
    )])
    assert not grade_intent(expect, partial, draft=draft).passed


def test_negative_preference_weight() -> None:
    """不喜欢标为 -1，误标成正权重判错。"""
    expect = expected(kind="plan_request", preferences={"temple|religion": -1})
    assert grade_intent(expect, TurnIntent(kind="plan_request", preferences=[
        IntentPreference(tag="religion", weight=-1),
    ])).passed
    assert not grade_intent(expect, TurnIntent(kind="plan_request", preferences=[
        IntentPreference(tag="temple", weight=1),
    ])).passed


def test_preference_alternatives_and_weight() -> None:
    """偏好标签支持 a|b 备选，权重不同判错。"""
    expect = expected(kind="plan_request", preferences={"history|historic_site": 2, "nature": 1})
    ok = TurnIntent(kind="plan_request", preferences=[
        IntentPreference(tag="historic_site", weight=2), IntentPreference(tag="nature"),
    ])
    grade = grade_intent(expect, ok)
    assert grade.passed and (grade.pref_tp, grade.pref_fp, grade.pref_fn) == (2, 0, 0)
    wrong_weight = TurnIntent(kind="plan_request", preferences=[
        IntentPreference(tag="history"), IntentPreference(tag="nature"),
    ])
    assert "权重" in grade_intent(expect, wrong_weight).failed_fields()["preferences"]


def test_preference_whole_group_alternatives() -> None:
    """偏好可写多组整体备选，取最佳一组判分；不能跨组混搭。"""
    expect = expected(kind="plan_request", preferences=[{"mountain": 1, "lake_river": 1}, {"nature": 1}])
    fine = TurnIntent(kind="plan_request", preferences=[
        IntentPreference(tag="mountain"), IntentPreference(tag="lake_river"),
    ])
    coarse = TurnIntent(kind="plan_request", preferences=[IntentPreference(tag="nature")])
    assert grade_intent(expect, fine).passed
    grade = grade_intent(expect, coarse)
    assert grade.passed and (grade.pref_tp, grade.pref_fp, grade.pref_fn) == (1, 0, 0)
    mixed = TurnIntent(kind="plan_request", preferences=[
        IntentPreference(tag="mountain"), IntentPreference(tag="nature"),
    ])
    assert not grade_intent(expect, mixed).passed
    failed = grade_intent(expect, None)
    assert failed.pref_fn == 2


def test_tag_group_matching_prefers_narrow_groups() -> None:
    """窄的组先匹配，避免宽组抢走唯一可行标签。"""
    assert match_tag_groups(["temple|museum", "temple"], ["temple", "museum"]) == (2, 0, 0)
    assert match_tag_groups(["temple"], ["temple", "outdoor"]) == (1, 1, 0)


def test_ops_order_insensitive_with_wildcard() -> None:
    """操作顺序无关，"*" 字段不比较，未写字段需为默认。"""
    expect = expected(kind="modify", ops=[
        {"op": "remove", "item_ids": ["D2-1"], "day": "*"},
        {"op": "day_load", "day": 3, "level": "lighter"},
    ])
    intent = TurnIntent(kind="modify", ops=[
        IntentEditOp(op="day_load", day=3, level="lighter"),
        IntentEditOp(op="remove", item_ids=["D2-1"], day=2),
    ])
    grade = grade_intent(expect, intent)
    assert grade.passed and (grade.op_tp, grade.op_fp, grade.op_fn) == (2, 0, 0)

    extra_field = TurnIntent(kind="modify", ops=[
        IntentEditOp(op="day_load", day=3, level="lighter", part="morning"),
        IntentEditOp(op="remove", item_ids=["D2-1"]),
    ])
    grade = grade_intent(expect, extra_field)
    assert not grade.passed and (grade.op_tp, grade.op_fp, grade.op_fn) == (1, 1, 1)


def test_named_remove_split_or_merged_is_equivalent() -> None:
    """点名删除拆成两条或合并成一条都算对；replace 不合并。"""
    merged = expected(kind="modify", ops=[{"op": "remove", "item_ids": ["D1-1", "D1-2"], "day": "*"}])
    split = TurnIntent(kind="modify", ops=[
        IntentEditOp(op="remove", item_ids=["D1-1"], day=1), IntentEditOp(op="remove", item_ids=["D1-2"]),
    ])
    grade = grade_intent(merged, split)
    assert grade.passed and (grade.op_tp, grade.op_fp, grade.op_fn) == (1, 0, 0)

    split_spec = expected(kind="modify", ops=[
        {"op": "move", "item_ids": ["D1-1"], "to_day": 3}, {"op": "move", "item_ids": ["D1-2"], "to_day": 3},
    ])
    one = TurnIntent(kind="modify", ops=[IntentEditOp(op="move", item_ids=["D1-2", "D1-1"], to_day=3)])
    assert grade_intent(split_spec, one).passed

    replace_spec = expected(kind="modify", ops=[
        {"op": "replace", "item_ids": ["D1-1", "D1-2"], "target_tags": ["museum"]},
    ])
    replace_split = TurnIntent(kind="modify", ops=[
        IntentEditOp(op="replace", item_ids=["D1-1"], target_tags=["museum"]),
        IntentEditOp(op="replace", item_ids=["D1-2"], target_tags=["museum"]),
    ])
    assert not grade_intent(replace_spec, replace_split).passed


def test_op_poi_name_fuzzy() -> None:
    """景点名互相包含即算匹配。"""
    expect = expected(kind="modify", ops=[{"op": "add", "poi_name": "宋城", "day": 2}])
    intent = TurnIntent(kind="modify", ops=[IntentEditOp(op="add", poi_name="杭州宋城景区", day=2)])
    assert grade_intent(expect, intent).passed


def test_must_visit_day_and_shorthand() -> None:
    """必去景点支持字符串简写，day 需一致。"""
    expect = expected(kind="plan_request", must_visit=["灵隐寺", {"name": "西溪湿地", "day": 2}])
    missing_day = TurnIntent(kind="plan_request", must_visit=[
        IntentPoiMention(name="西溪湿地"), IntentPoiMention(name="灵隐寺"),
    ])
    assert not grade_intent(expect, missing_day).passed
    ok = TurnIntent(kind="plan_request", must_visit=[
        IntentPoiMention(name="西溪湿地", day=2), IntentPoiMention(name="灵隐寺"),
    ])
    assert grade_intent(expect, ok).passed


def test_question_keywords() -> None:
    """question 关键词全部出现才算对；未标注时应为空。"""
    expect = expected(kind="question", question=["灵隐寺", "门票"])
    assert grade_intent(expect, TurnIntent(kind="question", question="灵隐寺门票多少钱")).passed
    assert not grade_intent(expect, TurnIntent(kind="question", question="灵隐寺几点开门")).passed
    grade = grade_intent(expected(kind="modify"), TurnIntent(kind="modify", question="多出来的"))
    assert grade.failed_fields().keys() == {"question"}


def test_optional_text_fields_only_graded_when_labeled() -> None:
    """chat_reply、unhandled 未标注时不判分。"""
    intent = TurnIntent(kind="chat", chat_reply="你好", unhandled=["带老人"])
    assert grade_intent(expected(kind="chat"), intent).passed
    assert not grade_intent(expected(kind="chat", unhandled=False), intent).passed


def test_parse_failure_fails_everything() -> None:
    """解析失败时所有判分字段都判错。"""
    grade = grade_intent(expected(kind="modify", ops=[{"op": "undo"}]), None)
    assert not grade.passed and grade.op_fn == 1
    assert all(not r.passed for r in grade.fields.values())


def test_name_helpers() -> None:
    """名称和城市匹配。"""
    assert names_match("灵隐寺", "灵隐寺景区")
    assert names_match("浙江省博物馆", "浙江省博物馆孤山馆区")
    assert not names_match("雷峰塔", "六和塔")
    assert not names_match("雷峰塔", None)
    assert city_match("杭州", "杭州市") and city_match(None, None)
    assert not city_match("杭州", None) and not city_match(None, "杭州")
