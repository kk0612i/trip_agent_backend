"""评估用例的结构、加载与一致性校验。

标注约定（详见 docs/intent_eval.md）：
- expect 中未写出的字段按“应为空/默认值”判分，用来发现模型多填；chat_reply、unhandled 未写出时不判分。
- 标签可写成 "history|historic_site" 表示几种答案都算对。
- 修改操作的字段写 "*" 表示不关心该字段（例如点名景点时模型是否顺带填 day 不影响解析）。
"""

from datetime import date
from pathlib import Path
from typing import Literal, get_args

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.trip_intent_schema import IntentKind
from app.schemas.trip_plan_schema import DayLoad, DayPart, MainCategory, Pace, Subtype
from app.schemas.trip_schema import TurnRecord
from evals.intent.scenarios import SCENARIOS, build_context

CASES_DIR = Path(__file__).parent / "cases"

ANY = "*"
"""修改操作字段的通配值。"""

CATEGORIES: dict[str, str] = {
    "collect": "收集需求：城市、天数、节奏、必去/不去",
    "date": "日期换算：相对日期、节假日说法",
    "preference": "偏好：标签映射、权重、清空重设",
    "transport": "交通：只步行、步行上限",
    "switch_city": "换城市",
    "modify_ref": "修改：点名景点（引用编号）",
    "modify_selector": "修改：按天/时段/标签的条件选择",
    "modify_compound": "修改：一句话多个操作或与需求变化混合",
    "pending": "回答待确认问题",
    "question": "资料问答",
    "chat": "闲聊",
    "out_of_scope": "住宿餐饮等范围外请求",
    "state_boundary": "状态边界：已确认行程、无草稿时的修改等",
    "robustness": "鲁棒性：口语、错别字、提示注入、超范围数值、无法映射的要求",
}

FIELDS = (
    "kind", "city", "start_date", "days", "pace", "preferences", "clear_preferences", "must_visit",
    "avoid", "walk_only", "max_walk_minutes", "ops", "question", "pending_option", "chat_reply", "unhandled",
)
"""参与判分的意图字段。"""

PREFERENCE_TAGS = frozenset(get_args(MainCategory)) | frozenset(get_args(Subtype))
SELECTOR_TAGS = PREFERENCE_TAGS | {"indoor", "outdoor"}
OpName = Literal["remove", "replace", "add", "move", "swap", "set_duration", "day_load", "undo"]
Wildcard = Literal["*"]


class StrictSpec(BaseModel):
    """禁止未知字段，避免标注拼写错误被静默忽略。"""

    model_config = ConfigDict(extra="forbid")


class MentionSpec(StrictSpec):
    """期望的景点提及。"""

    name: str = Field(min_length=1, description="景点名称，判分时宽松匹配")
    day: int | None = Field(default=None, description="指定第几天")


class OpSpec(StrictSpec):
    """期望的一条修改操作；未写出的字段应为默认值，写 "*" 表示不关心。"""

    op: OpName = Field(description="操作类型")
    item_ids: list[str] | Wildcard = Field(default_factory=list, description="草稿引用编号，顺序无关")
    day: int | None | Wildcard = Field(default=None, description="选择器的天或目标天")
    part: DayPart | None | Wildcard = Field(default=None, description="时段")
    tags: list[str] | Wildcard = Field(default_factory=list, description="选择器标签，可写 a|b")
    poi_name: str | None | Wildcard = Field(default=None, description="景点名，宽松匹配")
    target_tags: list[str] | Wildcard = Field(default_factory=list, description="替换目标标签，可写 a|b")
    target_count: int | None | Wildcard = Field(default=None, description="替换后的数量")
    to_day: int | None | Wildcard = Field(default=None, description="移动的目标天")
    minutes: int | None | Wildcard = Field(default=None, description="游玩分钟数")
    level: DayLoad | None | Wildcard = Field(default=None, description="松紧方向")


class Expected(StrictSpec):
    """期望的意图。"""

    kind: IntentKind | list[IntentKind] = Field(description="期望意图；列表表示几种都可接受，第一个为主标注")
    city: str | None = Field(default=None, description="城市名")
    start_date: date | None = Field(default=None, description="出发日期")
    days: int | None = Field(default=None, description="天数")
    pace: Pace | None = Field(default=None, description="节奏")
    preferences: dict[str, Literal[-1, 1, 2]] | list[dict[str, Literal[-1, 1, 2]]] = Field(
        default_factory=dict, description="偏好标签到权重，键可写 a|b；写成列表表示整组备选，任一组完全匹配即可",
    )
    clear_preferences: bool = Field(default=False, description="是否清空原有偏好")
    must_visit: list[MentionSpec | str] = Field(default_factory=list, description="必去景点")
    avoid: list[str] = Field(default_factory=list, description="不去的景点")
    walk_only: bool | None = Field(default=None, description="是否只步行")
    max_walk_minutes: int | None = Field(default=None, description="单段步行上限")
    ops: list[OpSpec] = Field(default_factory=list, description="修改操作，顺序无关")
    question: bool | list[str] | None = Field(
        default=None, description="空=应为空；true=应非空；列表=应包含全部关键词",
    )
    pending_option: int | None = Field(default=None, description="选项序号")
    chat_reply: bool | None = Field(default=None, description="空=不判分；true=应非空；false=应为空")
    unhandled: bool | None = Field(default=None, description="空=不判分；true=应非空；false=应为空")

    @field_validator("must_visit")
    @classmethod
    def _normalize_mentions(cls, value: list[MentionSpec | str]) -> list[MentionSpec | str]:
        """字符串简写转为 MentionSpec。"""
        return [MentionSpec(name=v) if isinstance(v, str) else v for v in value]

    @property
    def preference_options(self) -> list[dict[str, int]]:
        """偏好的备选答案列表；单个字典视为只有一种答案。"""
        return self.preferences if isinstance(self.preferences, list) else [self.preferences]

    @property
    def kinds(self) -> list[str]:
        """可接受的意图列表，第一个为主标注。"""
        return list(self.kind) if isinstance(self.kind, list) else [self.kind]


class EvalCase(StrictSpec):
    """一条评估用例：场景 + 用户消息 + 期望意图。"""

    id: str = Field(pattern=r"^[a-z_]+-\d{3}$", description="用例标识，形如 modify_ref-001")
    category: str = Field(description="类别，见 CATEGORIES")
    scenario: str = Field(description="会话场景名，见 scenarios.SCENARIOS")
    message: str = Field(min_length=1, max_length=1000, description="用户消息")
    recent_turns: list[TurnRecord] | None = Field(default=None, description="覆盖场景默认的最近对话")
    expect: Expected = Field(description="期望意图")
    ignore: list[str] = Field(default_factory=list, description="不判分的字段")
    tags: list[str] = Field(default_factory=list, description="自由标签，用于筛选和分组")
    note: str = Field(default="", description="标注说明：为什么是这个答案")


class DatasetError(ValueError):
    """评估集存在错误。"""


def _check_tags(tags: list[str], allowed: frozenset[str] | set[str], where: str, errors: list[str]) -> None:
    """检查标签（含 a|b 备选）都在词表内。"""
    for group in tags:
        for tag in group.split("|"):
            if tag not in allowed:
                errors.append(f"{where}：未知标签 {tag!r}")


def _case_errors(case: EvalCase) -> list[str]:
    """单条用例的一致性问题。"""
    errors: list[str] = []
    where = case.id
    if case.category not in CATEGORIES:
        errors.append(f"{where}：未知类别 {case.category!r}")
    if not case.id.startswith(case.category + "-"):
        errors.append(f"{where}：id 前缀应与类别 {case.category!r} 一致")
    if case.scenario not in SCENARIOS:
        errors.append(f"{where}：未知场景 {case.scenario!r}")
        return errors
    for name in case.ignore:
        if name not in FIELDS:
            errors.append(f"{where}：ignore 中的未知字段 {name!r}")
    context = build_context(case.scenario)
    expect = case.expect
    for option in expect.preference_options:
        _check_tags(list(option), PREFERENCE_TAGS, f"{where}.preferences", errors)
    if isinstance(expect.preferences, list) and len(expect.preferences) < 2:
        errors.append(f"{where}：preferences 写成列表时至少需要两组备选")

    item_ids = {item.item_id for day in context.draft.days for item in day.items} if context.draft else set()
    for index, op in enumerate(expect.ops):
        op_where = f"{where}.ops[{index}]"
        if context.draft is None:
            errors.append(f"{op_where}：场景 {case.scenario} 没有草稿，不应期望修改操作")
        if op.item_ids != ANY:
            for ref in op.item_ids:
                if ref not in item_ids:
                    errors.append(f"{op_where}：草稿中没有引用编号 {ref}")
        for field_name in ("tags", "target_tags"):
            value = getattr(op, field_name)
            if value != ANY:
                _check_tags(value, SELECTOR_TAGS, f"{op_where}.{field_name}", errors)

    if "question" in expect.kinds[:1] and expect.question is None and "question" not in case.ignore:
        errors.append(f"{where}：kind=question 时需标注 question（true 或关键词列表）")
    if "answer_pending" in expect.kinds[:1]:
        if expect.pending_option is None:
            errors.append(f"{where}：kind=answer_pending 时需标注 pending_option")
        if context.pending is None:
            errors.append(f"{where}：场景 {case.scenario} 没有待回答问题")
    if expect.pending_option is not None and context.pending is not None:
        if not 1 <= expect.pending_option <= len(context.pending.options):
            errors.append(f"{where}：pending_option 超出选项范围")
    if not case.note.strip():
        errors.append(f"{where}：缺少 note 标注说明")
    return errors


def load_cases(paths: list[Path] | None = None) -> list[EvalCase]:
    """读取并校验评估集。

    Args:
        paths: YAML 文件列表，缺省为 cases/ 下全部 *.yaml。

    Returns:
        按文件名和文件内顺序排列的用例。

    Raises:
        DatasetError: 格式错误、id 重复或与场景不一致。
    """
    files = sorted(paths if paths is not None else CASES_DIR.glob("*.yaml"))
    cases: list[EvalCase] = []
    errors: list[str] = []
    for path in files:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        if not isinstance(raw, list):
            errors.append(f"{path.name}：顶层应为用例列表")
            continue
        for index, item in enumerate(raw):
            try:
                cases.append(EvalCase.model_validate(item))
            except ValueError as exc:
                ident = item.get("id", f"#{index}") if isinstance(item, dict) else f"#{index}"
                errors.append(f"{path.name} {ident}：{exc}")
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            errors.append(f"{case.id}：id 重复")
        seen.add(case.id)
        errors.extend(_case_errors(case))
    if errors:
        raise DatasetError("\n".join(errors))
    return cases
