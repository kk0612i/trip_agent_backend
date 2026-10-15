"""把模型输出的 TurnIntent 与标注逐字段比对，全部用代码判分。"""

import re
import unicodedata
from dataclasses import asdict, dataclass, field

from app.agent.trip.intent_resolve import ResolveError, select_items
from app.schemas.trip_intent_schema import IntentEditOp, TurnIntent
from app.schemas.trip_plan_schema import Plan
from evals.intent.dataset import ANY, FIELDS, Expected, OpSpec

_PUNCT = re.compile(r"[\s\-_·•,，.。、!！?？\"'“”‘’()（）\[\]【】]")
_CITY_SUFFIX = re.compile(r"(市|特别行政区)$")


@dataclass
class FieldResult:
    """单个字段的判分结果。

    Attributes:
        passed: 是否正确。
        expected_set: 标注是否期望该字段有值；为假时判的是“不该多填”。
        detail: 失败原因。
    """

    passed: bool
    expected_set: bool
    detail: str = ""


@dataclass
class CaseGrade:
    """一次运行的判分结果。

    Attributes:
        passed: 所有判分字段都正确。
        fields: 字段名到结果，ignore 的字段不出现。
        op_tp / op_fp / op_fn: 修改操作级的命中、多出、遗漏条数。
        pref_tp / pref_fp / pref_fn: 偏好标签级的命中、多出、遗漏个数。
    """

    passed: bool
    fields: dict[str, FieldResult] = field(default_factory=dict)
    op_tp: int = 0
    op_fp: int = 0
    op_fn: int = 0
    pref_tp: int = 0
    pref_fp: int = 0
    pref_fn: int = 0

    def failed_fields(self) -> dict[str, str]:
        """失败字段及原因。"""
        return {name: r.detail for name, r in self.fields.items() if not r.passed}

    def to_dict(self) -> dict:
        """转为可写入 JSON 的字典。"""
        return asdict(self)


def normalize_name(text: str) -> str:
    """景点名归一化：全半角统一、去空白和标点、小写。"""
    return _PUNCT.sub("", unicodedata.normalize("NFKC", text)).lower()


def names_match(expected: str, actual: str | None) -> bool:
    """景点名宽松匹配：归一化后相等或互相包含（“灵隐寺”与“灵隐寺景区”）。"""
    if actual is None:
        return False
    a, b = normalize_name(expected), normalize_name(actual)
    return bool(a) and bool(b) and (a == b or a in b or b in a)


def city_match(expected: str | None, actual: str | None) -> bool:
    """城市名匹配，忽略“市”后缀。"""
    if expected is None or actual is None:
        return expected is None and not actual
    strip = lambda s: _CITY_SUFFIX.sub("", normalize_name(s))  # noqa: E731
    return strip(expected) == strip(actual)


def match_tag_groups(expected: list[str], actual: list[str]) -> tuple[int, int, int]:
    """标签集合匹配，期望标签可写 a|b 备选，每个实际标签只能命中一组。

    Returns:
        （命中, 多出, 遗漏）。
    """
    remaining = list(dict.fromkeys(actual))
    tp = 0
    # 备选少的组先匹配，避免宽泛的组抢占唯一可行的标签。
    for group in sorted(expected, key=lambda g: g.count("|")):
        options = group.split("|")
        hit = next((tag for tag in remaining if tag in options), None)
        if hit is not None:
            remaining.remove(hit)
            tp += 1
    return tp, len(remaining), len(expected) - tp


def _tags_equal(expected: list[str] | str, actual: list[str]) -> bool:
    if expected == ANY:
        return True
    _, fp, fn = match_tag_groups(list(expected), actual)
    return fp == 0 and fn == 0


# 这些操作的 item_ids / day / part / tags 共同决定“选中哪些景点”，点名和选择器选中同一批即等价。
_SELECTING_OPS = ("remove", "replace", "move", "swap", "set_duration")


def _selected(op: IntentEditOp, draft: Plan) -> set[str] | None:
    """按下游规则展开选中的草稿引用，无法展开时为空。"""
    try:
        # move 点名时下游不看 day，与 _convert_op 一致。
        target = op.model_copy(update={"day": None}) if op.op == "move" and op.item_ids else op
        return set(select_items(draft, target))
    except ResolveError:
        return None


def _spec_selected(spec: OpSpec, draft: Plan) -> set[str] | None:
    """展开期望操作选中的引用；含通配或 a|b 备选时无法确定，返回空。"""
    if spec.item_ids != ANY and spec.item_ids:
        return set(spec.item_ids)
    if ANY in (spec.day, spec.part, spec.tags) or any("|" in tag for tag in spec.tags):
        return None
    op = IntentEditOp(op=spec.op, day=spec.day, part=spec.part, tags=spec.tags)
    return _selected(op, draft)


def _same_selection(spec: OpSpec, op: IntentEditOp, draft: Plan | None) -> bool:
    """点名写法与选择器写法是否在当前草稿上选中同一批景点。"""
    if draft is None or spec.op not in _SELECTING_OPS:
        return False
    expected = _spec_selected(spec, draft)
    return expected is not None and expected == _selected(op, draft)


def op_matches(spec: OpSpec, op: IntentEditOp, draft: Plan | None = None) -> bool:
    """一条期望操作是否与模型输出的操作一致（未写字段需为默认值，"*" 不比较）。

    Args:
        spec: 期望操作。
        op: 模型输出的操作。
        draft: 场景草稿；给出时，选中景点相同的点名与选择器写法视为一致。

    Returns:
        是否一致。
    """
    if spec.op != op.op:
        return False
    selection_fields = ["item_ids", "tags", "day", "part"]
    if _same_selection(spec, op, draft):
        selection_fields = []
    if "item_ids" in selection_fields and spec.item_ids != ANY and set(spec.item_ids) != set(op.item_ids):
        return False
    if "tags" in selection_fields and not _tags_equal(spec.tags, op.tags):
        return False
    if not _tags_equal(spec.target_tags, op.target_tags):
        return False
    if spec.poi_name != ANY:
        if spec.poi_name is None:
            if op.poi_name:
                return False
        elif not names_match(spec.poi_name, op.poi_name):
            return False
    for name in ("day", "part", "target_count", "to_day", "minutes", "level"):
        if name in ("day", "part") and name not in selection_fields:
            continue
        want = getattr(spec, name)
        if want != ANY and want != getattr(op, name):
            return False
    return True


# 点名景点的 remove/move 拆成多条与合并成一条，下游结果相同（select_items 在 item_ids 非空时不看 day）。
_MERGEABLE_OPS = ("remove", "move")


def _is_named(item_ids: list[str] | str, part: object, tags: list[str] | str) -> bool:
    return item_ids != ANY and bool(item_ids) and part in (None, ANY) and tags in ([], ANY)


def _merge_specs(specs: list[OpSpec]) -> list[OpSpec]:
    """合并可等价合并的期望操作（同类型、同目标天的点名 remove/move）。"""
    merged: list[OpSpec] = []
    groups: dict[tuple, OpSpec] = {}
    for spec in specs:
        others_default = spec.poi_name is None and spec.target_tags == [] and spec.minutes is None
        if spec.op in _MERGEABLE_OPS and others_default and spec.day in (None, ANY) and _is_named(
            spec.item_ids, spec.part, spec.tags,
        ):
            key = (spec.op, spec.to_day)
            if key in groups:
                group = groups[key]
                group.item_ids = [*group.item_ids, *spec.item_ids]
                if spec.day == ANY:
                    group.day = ANY
                continue
            groups[key] = spec.model_copy(deep=True)
            merged.append(groups[key])
        else:
            merged.append(spec)
    return merged


def _merge_ops(ops: list[IntentEditOp]) -> list[IntentEditOp]:
    """合并模型输出中可等价合并的操作，day 不一致时置空。"""
    merged: list[IntentEditOp] = []
    groups: dict[tuple, IntentEditOp] = {}
    for op in ops:
        others_default = not op.poi_name and not op.target_tags and op.minutes is None
        if op.op in _MERGEABLE_OPS and others_default and _is_named(op.item_ids, op.part, op.tags):
            key = (op.op, op.to_day)
            if key in groups:
                group = groups[key]
                day = group.day if group.day == op.day else None
                groups[key] = group.model_copy(update={"item_ids": [*group.item_ids, *op.item_ids], "day": day})
                merged[merged.index(group)] = groups[key]
                continue
            groups[key] = op
        merged.append(op)
    return merged


def match_ops(
    expected: list[OpSpec], actual: list[IntentEditOp], draft: Plan | None = None,
) -> tuple[int, int, int, list[str]]:
    """修改操作列表匹配，顺序无关；先合并等价的点名 remove/move。

    Args:
        expected: 期望操作。
        actual: 模型输出的操作。
        draft: 场景草稿，用于判断点名与选择器是否等价。

    Returns:
        （命中, 多出, 遗漏, 失败说明）。
    """
    expected = _merge_specs(expected)
    actual = _merge_ops(actual)
    remaining = list(actual)
    missing: list[str] = []
    tp = 0
    for spec in expected:
        hit = next((op for op in remaining if op_matches(spec, op, draft)), None)
        if hit is None:
            missing.append(spec.model_dump(exclude_defaults=True))
        else:
            remaining.remove(hit)
            tp += 1
    notes = []
    if missing:
        notes.append(f"遗漏或不符 {missing}")
    if remaining:
        notes.append(f"多出 {[op.model_dump(exclude_defaults=True) for op in remaining]}")
    return tp, len(remaining), len(expected) - tp, notes


# IntentResolver 对这几种意图走同一段处理（_apply），互相判错不影响结果。
_APPLY_KINDS = frozenset({"plan_request", "modify", "switch_city"})


def effective_kind(kind: str, intent: TurnIntent | None = None) -> str:
    """把意图类型归并为下游实际走的处理分支。

    plan_request、modify 和带城市的 switch_city 都按需求变化和修改处理；没有选项序号的
    answer_pending 同样走这条分支（resolve 只在有序号时执行选项）。

    Args:
        kind: 意图类型。
        intent: 模型输出；为空时按标注处理，只看类型本身。

    Returns:
        归并后的分支名，需求变化和修改统一为 "apply"。
    """
    if intent is not None:
        if kind == "answer_pending" and intent.pending_option is None:
            return "apply"
        if kind == "switch_city" and not intent.city:
            return kind
    return "apply" if kind in _APPLY_KINDS else kind


def _grade_kind(expect: Expected, intent: TurnIntent) -> FieldResult:
    """按下游等价判 kind：与任一可接受类型走同一处理分支即算对。"""
    accepted = {effective_kind(kind) for kind in expect.kinds}
    passed = effective_kind(intent.kind, intent) in accepted
    return FieldResult(passed, True, "" if passed else f"期望 {expect.kinds}，实际 {intent.kind!r}")


def _scalar(expected: object, actual: object, default: object = None) -> FieldResult:
    passed = expected == actual
    return FieldResult(passed, expected != default, "" if passed else f"期望 {expected!r}，实际 {actual!r}")


def _optional_text(spec: bool | None, actual: str | list[str] | None) -> FieldResult:
    """chat_reply / unhandled：true 应非空，false 应为空。"""
    filled = bool(actual)
    passed = filled == bool(spec)
    return FieldResult(passed, bool(spec), "" if passed else f"期望{'非空' if spec else '为空'}，实际 {actual!r}")


def _grade_preference_option(option: dict[str, int], actual: dict[str, int]) -> tuple[bool, int, int, int, str]:
    """按一组备选答案判分，返回（是否通过, 命中, 多出, 遗漏, 说明）。"""
    tp, fp, fn = match_tag_groups(list(option), list(actual))
    weight_errors = []
    for group, weight in option.items():
        hit = next((tag for tag in group.split("|") if tag in actual), None)
        if hit is not None and actual[hit] != weight:
            weight_errors.append(f"{hit} 权重 {actual[hit]}≠{weight}")
    passed = fp == 0 and fn == 0 and not weight_errors
    detail = "" if passed else f"期望 {option}，实际 {actual}" + (
        f"；{'；'.join(weight_errors)}" if weight_errors else ""
    )
    return passed, tp, fp, fn, detail


def _grade_preferences(expect: Expected, intent: TurnIntent, grade: CaseGrade) -> FieldResult:
    # 与 IntentResolver 合并偏好的方式一致：同一标签以最后一次为准（权重可为 -1）。
    actual: dict[str, int] = {pref.tag: pref.weight for pref in intent.preferences}
    # 多组备选时取通过的那组，都不通过取命中最多、错误最少的一组计入统计。
    results = [_grade_preference_option(option, actual) for option in expect.preference_options]
    best = max(results, key=lambda r: (r[0], r[1] - r[2] - r[3]))
    passed, grade.pref_tp, grade.pref_fp, grade.pref_fn, detail = best
    if not passed and len(results) > 1:
        detail = "；或 ".join(r[4] for r in results)
    return FieldResult(passed, any(expect.preference_options), detail)


def _grade_mentions(expect: Expected, intent: TurnIntent) -> FieldResult:
    remaining = list(intent.must_visit)
    missing = []
    for spec in expect.must_visit:
        hit = next((m for m in remaining if names_match(spec.name, m.name) and m.day == spec.day), None)
        if hit is None:
            missing.append(spec.model_dump(exclude_defaults=True))
        else:
            remaining.remove(hit)
    passed = not missing and not remaining
    detail = "" if passed else (
        f"遗漏或不符 {missing}；多出 {[m.model_dump(exclude_defaults=True) for m in remaining]}"
    )
    return FieldResult(passed, bool(expect.must_visit), detail)


def _grade_avoid(expect: Expected, intent: TurnIntent) -> FieldResult:
    remaining = list(intent.avoid)
    missing = []
    for name in expect.avoid:
        hit = next((a for a in remaining if names_match(name, a)), None)
        if hit is None:
            missing.append(name)
        else:
            remaining.remove(hit)
    passed = not missing and not remaining
    return FieldResult(passed, bool(expect.avoid), "" if passed else f"遗漏 {missing}；多出 {remaining}")


def _grade_question(spec: bool | list[str] | None, actual: str | None) -> FieldResult:
    if spec is None or spec is False:
        passed = not actual
        return FieldResult(passed, False, "" if passed else f"不应填写，实际 {actual!r}")
    if spec is True:
        passed = bool(actual)
        return FieldResult(passed, True, "" if passed else "应提炼问题，实际为空")
    missing = [word for word in spec if not actual or word not in actual]
    return FieldResult(not missing, True, "" if not missing else f"缺少关键词 {missing}，实际 {actual!r}")


def grade_intent(
    expect: Expected, intent: TurnIntent | None, ignore: list[str] | None = None, draft: Plan | None = None,
) -> CaseGrade:
    """逐字段判分。

    Args:
        expect: 标注。
        intent: 模型输出；为空表示解析失败，全部字段判错。
        ignore: 不判分的字段。
        draft: 场景草稿；给出时，选中同一批景点的点名与选择器写法视为一致。

    Returns:
        判分结果。
    """
    skip = set(ignore or [])
    if expect.chat_reply is None:
        skip.add("chat_reply")
    if expect.unhandled is None:
        skip.add("unhandled")
    if intent is None:
        fields = {name: FieldResult(False, True, "意图解析失败") for name in FIELDS if name not in skip}
        return CaseGrade(passed=False, fields=fields, op_fn=len(expect.ops), pref_fn=len(expect.preference_options[0]))

    grade = CaseGrade(passed=False)
    results: dict[str, FieldResult] = {
        "kind": _grade_kind(expect, intent),
        "start_date": _scalar(expect.start_date, intent.start_date),
        "days": _scalar(expect.days, intent.days),
        "pace": _scalar(expect.pace, intent.pace),
        "clear_preferences": _scalar(expect.clear_preferences, intent.clear_preferences, False),
        "walk_only": _scalar(expect.walk_only, intent.walk_only),
        "max_walk_minutes": _scalar(expect.max_walk_minutes, intent.max_walk_minutes),
        "pending_option": _scalar(expect.pending_option, intent.pending_option),
        "preferences": _grade_preferences(expect, intent, grade),
        "must_visit": _grade_mentions(expect, intent),
        "avoid": _grade_avoid(expect, intent),
        "question": _grade_question(expect.question, intent.question),
        "chat_reply": _optional_text(expect.chat_reply, intent.chat_reply),
        "unhandled": _optional_text(expect.unhandled, intent.unhandled),
    }
    city_ok = city_match(expect.city, intent.city)
    results["city"] = FieldResult(city_ok, expect.city is not None, "" if city_ok else (
        f"期望 {expect.city!r}，实际 {intent.city!r}"
    ))
    tp, fp, fn, notes = match_ops(expect.ops, intent.ops, draft)
    grade.op_tp, grade.op_fp, grade.op_fn = tp, fp, fn
    results["ops"] = FieldResult(fp == 0 and fn == 0, bool(expect.ops), "；".join(notes))

    if "ops" in skip:
        grade.op_tp = grade.op_fp = grade.op_fn = 0
    if "preferences" in skip:
        grade.pref_tp = grade.pref_fp = grade.pref_fn = 0
    grade.fields = {name: results[name] for name in FIELDS if name not in skip}
    grade.passed = all(r.passed for r in grade.fields.values())
    return grade
