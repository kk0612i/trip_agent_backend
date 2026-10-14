"""行程会话的业务传输结构与 HTTP 请求、响应契约。"""

from datetime import date, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.schemas.trip_plan_schema import EditOp, Plan, Requirements

TripStatus = Literal["collecting", "drafting", "confirmed"]
"""行程状态：收集需求（可换城市）→ 草稿（城市锁定、可修改）→ 已确认（只读）。"""


# ---------------------------------------------------------------------------
# 图与 Service 之间的业务结构
# ---------------------------------------------------------------------------


class PendingOption(BaseModel):
    """待确认问题的一个选项，选中后直接按其内容执行。"""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=120, description="展示给用户的选项文字")
    requirements: Requirements | None = Field(default=None, description="选中后使用的需求，空表示沿用当前需求")
    ops: list[EditOp] = Field(default_factory=list, description="选中后执行的修改操作")
    force_full: bool = Field(default=False, description="选中后是否允许全量重排")


class PendingQuestion(BaseModel):
    """等待用户回答的问题。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["missing_info", "ambiguous", "conflict", "infeasible", "suggestion"] = Field(
        description="问题类别",
    )
    text: str = Field(min_length=1, max_length=1000, description="问题文字")
    options: list[PendingOption] = Field(default_factory=list, description="可选项，空表示自由回答")


class TurnRecord(BaseModel):
    """最近的对话记录。"""

    role: Literal["user", "assistant"] = Field(description="发送方")
    content: str = Field(description="消息内容")


class TripContext(BaseModel):
    """一轮对话开始时从数据库读取的会话快照。"""

    model_config = ConfigDict(extra="forbid")

    trip_id: int = Field(gt=0, description="行程标识")
    user_id: int = Field(gt=0, description="已认证用户标识")
    status: TripStatus = Field(description="行程状态")
    requirements: Requirements = Field(default_factory=Requirements, description="当前需求")
    draft: Plan | None = Field(default=None, description="当前草稿")
    draft_version: int | None = Field(default=None, description="当前草稿版本号")
    previous_draft: Plan | None = Field(default=None, description="上一个草稿版本，用于撤销")
    pending: PendingQuestion | None = Field(default=None, description="待回答的问题")
    recent_turns: list[TurnRecord] = Field(default_factory=list, description="最近几轮对话")
    last_change_summary: str | None = Field(default=None, description="上一次草稿变化摘要")
    today: date = Field(description="本轮使用的当天日期")


class TurnOutcome(BaseModel):
    """一轮图执行的结果，由 Service 负责持久化。"""

    model_config = ConfigDict(extra="forbid")

    route: str = Field(description="本轮最终走过的分支，用于日志")
    reply: str = Field(min_length=1, description="回复给用户的文字")
    requirements: Requirements = Field(description="本轮结束后的需求")
    pending: PendingQuestion | None = Field(default=None, description="新的待回答问题，空表示清除")
    new_plan: Plan | None = Field(default=None, description="新的草稿，空表示草稿未变化")
    change_summary: str | None = Field(default=None, description="草稿变化摘要")


# ---------------------------------------------------------------------------
# HTTP 契约
# ---------------------------------------------------------------------------


class PendingQuestionView(BaseModel):
    """对外展示的待回答问题。"""

    kind: str = Field(description="问题类别")
    text: str = Field(description="问题文字")
    options: list[str] = Field(default_factory=list, description="选项文字，按序号从 1 开始")


class TripSummary(BaseModel):
    """行程列表项。"""

    trip_id: int = Field(description="行程标识")
    status: TripStatus = Field(description="行程状态")
    city_name: str | None = Field(default=None, description="城市名称")
    draft_version: int | None = Field(default=None, description="当前草稿版本号")
    confirmed_version: int | None = Field(default=None, description="已确认的版本号")
    created_at: AwareDatetime = Field(description="创建时间")
    updated_at: AwareDatetime = Field(description="更新时间")


class TripDetail(TripSummary):
    """行程详情。"""

    requirements: Requirements = Field(description="当前需求")
    plan: Plan | None = Field(default=None, description="当前草稿或已确认行程")
    pending_question: PendingQuestionView | None = Field(default=None, description="待回答的问题")


class TurnView(BaseModel):
    """一条对话消息。"""

    turn_id: str = Field(description="客户端提交的轮次标识")
    role: Literal["user", "assistant"] = Field(description="发送方")
    content: str = Field(description="消息内容")
    draft_version: int | None = Field(default=None, description="这条回复产生的草稿版本")
    created_at: AwareDatetime = Field(description="创建时间")


class MessageRequest(BaseModel):
    """发送一条用户消息。"""

    model_config = ConfigDict(extra="forbid")

    turn_id: str = Field(
        min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$",
        description="客户端生成的轮次标识，同一轮重试保持不变以保证幂等",
    )
    content: str = Field(min_length=1, max_length=1000, description="消息内容")


class MessageResponse(BaseModel):
    """一轮对话的结果。"""

    turn_id: str = Field(description="轮次标识")
    reply: str = Field(description="助手回复")
    status: TripStatus = Field(description="本轮结束后的行程状态")
    draft_version: int | None = Field(default=None, description="本轮结束后的草稿版本号")
    plan_changed: bool = Field(description="本轮是否产生了新草稿")
    plan: Plan | None = Field(default=None, description="本轮结束后的草稿")
    pending_question: PendingQuestionView | None = Field(default=None, description="待回答的问题")


class ConfirmRequest(BaseModel):
    """确认当前草稿。"""

    model_config = ConfigDict(extra="forbid")

    draft_version: int = Field(gt=0, description="前端当前展示的草稿版本号")


class ConfirmResponse(BaseModel):
    """确认结果。"""

    trip_id: int = Field(description="行程标识")
    status: Literal["confirmed"] = Field(description="行程状态")
    confirmed_version: int = Field(description="已确认的版本号")
    confirmed_at: datetime = Field(description="确认时间")
