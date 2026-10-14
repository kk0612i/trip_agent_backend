"""行程对话根图的状态与运行依赖。

图不使用检查点：每条用户消息是一次完整执行，没有人工中断；会话状态以 MySQL 为准，
由 Service 在执行前读取快照、执行后持久化结果，因此状态字段全部为普通覆盖更新，无需 reducer。
"""

from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from langchain_core.language_models import BaseChatModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.schemas.trip_intent_schema import TurnIntent
from app.schemas.trip_plan_schema import EditOp, Plan, PlanScope, Requirements
from app.schemas.trip_schema import PendingQuestion, TripContext

TripRoute = Literal["reply", "plan", "qa", "undo"]


@dataclass(frozen=True)
class TripDeps:
    """图运行时依赖，进程内复用，不保存任何一轮的状态。

    Attributes:
        llm: 结构化抽取使用的聊天模型。
        qa_llm: 资料问答 Agent 使用的聊天模型。
        amap: 高德客户端。
        bocha: 博查客户端，未配置时为空，问答 Agent 不提供联网搜索。
        session_factory: 会话工厂，景点属性缓存读写各自创建会话。
        solver_time_limit_seconds: 单次 CP-SAT 求解秒数上限。
        max_tool_calls: 问答 Agent 单轮工具调用上限。
    """

    llm: BaseChatModel
    qa_llm: BaseChatModel
    amap: Any
    bocha: Any | None
    session_factory: async_sessionmaker[AsyncSession]
    solver_time_limit_seconds: float = 10
    max_tool_calls: int = 6


class TripState(TypedDict, total=False):
    """根图状态。

    Attributes:
        context: 本轮开始时的会话快照（输入）。
        message: 用户消息原文（输入）。
        intent: 理解阶段的结构化结果。
        requirements: 本轮处理后的需求。
        ops: 待执行的修改操作。
        force_full: 是否允许全量重排。
        stability: 全量重排时是否给原景点保留奖励。
        route: 解析后的去向。
        reply: 回复文字。
        notes: 需要附加在回复中的说明。
        pending: 新的待回答问题，空表示清除。
        question: 交给问答 Agent 的问题。
        new_plan: 新草稿。
        scope: 新草稿使用的规划范围，用于生成变化摘要。
        change_summary: 草稿变化摘要。
    """

    context: TripContext
    message: str
    intent: TurnIntent | None
    requirements: Requirements
    ops: list[EditOp]
    force_full: bool
    stability: bool
    route: TripRoute
    reply: str
    notes: list[str]
    pending: PendingQuestion | None
    question: str | None
    new_plan: Plan | None
    scope: PlanScope | None
    change_summary: str | None
