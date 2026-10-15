"""理解阶段的模型结构化输出。

结构保持扁平，避免模型处理复杂联合类型；由程序在 resolve 阶段转换和校验。
"""

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.trip_plan_schema import DayLoad, DayPart, Pace, PreferenceTag, SelectorTag

IntentKind = Literal[
    "chat", "question", "answer_pending", "plan_request", "modify", "switch_city", "out_of_scope", "cancel",
]

# 模型输出中“第几天”的上限。比行程天数上限宽，超出行程范围由 resolve 给出提示，
# 避免用户说“第 8 天”时整条结构化输出校验失败。
INTENT_DAY_MAX = 30


class IntentPreference(BaseModel):
    """一条偏好。"""

    tag: PreferenceTag = Field(description="偏好标签，必须来自标签词表")
    weight: Literal[-1, 1, 2] = Field(default=1, description="1 表示喜欢，2 表示非常喜欢，-1 表示不喜欢、少安排")


class IntentPoiMention(BaseModel):
    """用户提到的景点名称。"""

    name: str = Field(min_length=1, max_length=60, description="景点名称原文")
    day: int | None = Field(default=None, ge=1, le=INTENT_DAY_MAX, description="指定第几天，未提到为空")


class IntentEditOp(BaseModel):
    """针对当前草稿的一条修改。字段按 op 选择性填写。"""

    op: Literal["remove", "replace", "add", "move", "swap", "set_duration", "day_load", "undo"] = Field(
        description="操作类型",
    )
    item_ids: list[str] = Field(
        default_factory=list, description="明确点名的草稿引用，如 D1-2；用于 remove/replace/move/swap/set_duration",
    )
    day: int | None = Field(
        default=None, ge=1, le=INTENT_DAY_MAX,
        description="选择器的天（remove/replace/move），或 add/day_load 的目标天",
    )
    part: DayPart | None = Field(default=None, description="选择器的时段：上午或下午")
    tags: list[SelectorTag] = Field(default_factory=list, description="选择器标签，如 temple、outdoor")
    poi_name: str | None = Field(default=None, max_length=60, description="add 的景点名，或 replace 指定替换成的景点名")
    target_tags: list[SelectorTag] = Field(default_factory=list, description="replace 时新景点需满足的标签")
    target_count: int | None = Field(default=None, ge=1, le=10, description="replace 后的数量，未提到为空")
    to_day: int | None = Field(default=None, ge=1, le=INTENT_DAY_MAX, description="move 的目标天")
    minutes: int | None = Field(default=None, ge=10, le=600, description="set_duration 的分钟数")
    level: DayLoad | None = Field(default=None, description="day_load 的方向")


class TurnIntent(BaseModel):
    """一轮用户输入的理解结果。"""

    kind: IntentKind = Field(description="本轮主要意图")
    city: str | None = Field(default=None, max_length=30, description="目的地城市名，未提到为空")
    start_date: date | None = Field(default=None, description="出发日期，按提示中的日期表换算")
    days: int | None = Field(default=None, ge=1, le=INTENT_DAY_MAX, description="游玩天数")
    pace: Pace | None = Field(default=None, description="整体节奏")
    preferences: list[IntentPreference] = Field(default_factory=list, description="新增或调整的偏好")
    clear_preferences: bool = Field(default=False, description="是否清空原有偏好后再设置")
    must_visit: list[IntentPoiMention] = Field(default_factory=list, description="首次规划时的必去景点")
    avoid: list[str] = Field(default_factory=list, description="不想去的景点名称")
    walk_only: bool | None = Field(default=None, description="是否只步行，未提到为空")
    max_walk_minutes: int | None = Field(default=None, ge=5, le=180, description="单段步行上限分钟数")
    ops: list[IntentEditOp] = Field(default_factory=list, description="针对当前草稿的修改")
    question: str | None = Field(default=None, max_length=300, description="用户提出的需要查询资料回答的问题")
    pending_option: int | None = Field(default=None, ge=1, le=9, description="回答待确认问题时选择的选项序号")
    chat_reply: str | None = Field(default=None, max_length=300, description="闲聊时的简短回复")
    unhandled: list[str] = Field(default_factory=list, description="无法映射到以上字段的原话片段")
