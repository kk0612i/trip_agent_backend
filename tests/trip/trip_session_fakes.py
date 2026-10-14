"""行程会话测试的假图执行器和数据构造器。"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date, timedelta

from app.schemas.trip_plan_schema import CityInfo, Plan, PlanDay, Requirements
from app.schemas.trip_schema import PendingOption, PendingQuestion, TripContext, TurnOutcome

TEST_JWT_SECRET = "trip-session-test-signing-key-with-at-least-64-bytes-for-hs256-1234"

HANGZHOU = CityInfo(adcode="330100", name="杭州", lng=120.1551, lat=30.2741)
SUZHOU = CityInfo(adcode="320500", name="苏州", lng=120.5853, lat=31.2989)
START_DATE = date(2026, 10, 20)

TurnHandler = Callable[[TripContext, str], Awaitable[TurnOutcome]]


def make_requirements(city: CityInfo | None = HANGZHOU, days: int | None = 2, **extra: object) -> Requirements:
    """构造需求，默认杭州两日游。"""
    return Requirements(city=city, start_date=START_DATE if days else None, days=days, **extra)


def make_plan(city: CityInfo = HANGZHOU, days: int = 2, warning: str | None = None) -> Plan:
    """构造不含景点的合法草稿，warning 用于区分不同版本。"""
    return Plan(
        city=city, start_date=START_DATE,
        days=[PlanDay(day=index + 1, date=START_DATE + timedelta(days=index)) for index in range(days)],
        warnings=[warning] if warning else [],
    )


def make_pending(text: str = "想去几天？") -> PendingQuestion:
    """构造一个带选项的待回答问题。"""
    return PendingQuestion(
        kind="missing_info", text=text,
        options=[PendingOption(label="两天"), PendingOption(label="三天")],
    )


async def default_handler(context: TripContext, message: str) -> TurnOutcome:
    """按消息关键字模拟图：“规划”生成草稿，“修改”追加版本，其余只回复。"""
    if message.startswith("规划"):
        requirements = make_requirements()
        return TurnOutcome(
            route="plan", reply="已为你生成杭州两日游草稿", requirements=requirements,
            new_plan=make_plan(warning="v1"), change_summary="首次生成",
        )
    if message.startswith("修改"):
        if context.draft is None:
            return TurnOutcome(route="edit", reply="还没有草稿", requirements=context.requirements)
        requirements = context.requirements.model_copy(update={"pace": "relaxed"})
        return TurnOutcome(
            route="edit", reply="已调整为轻松节奏", requirements=requirements,
            new_plan=make_plan(warning=f"v{(context.draft_version or 0) + 1}"),
            change_summary="节奏改为轻松",
        )
    if message.startswith("提问"):
        return TurnOutcome(
            route="ask", reply="请问想去几天？", requirements=make_requirements(days=None),
            pending=make_pending(),
        )
    return TurnOutcome(
        route="chat", reply="好的", requirements=context.requirements.model_copy(update={"pace": "packed"}),
    )


class FakeTurnRunner:
    """记录调用并按可替换的处理函数返回结果的图执行器替身。"""

    def __init__(self) -> None:
        """使用默认处理函数初始化。"""
        self.handler: TurnHandler = default_handler
        self.calls: list[tuple[TripContext, str]] = []

    async def run(self, context: TripContext, message: str) -> TurnOutcome:
        """记录本次调用并执行当前处理函数。"""
        self.calls.append((context, message))
        return await self.handler(context, message)


class BlockingHandler:
    """在收到放行信号前阻塞的处理函数，用于制造并发窗口。"""

    def __init__(self, inner: TurnHandler = default_handler) -> None:
        """保存放行后实际执行的处理函数。"""
        self.inner = inner
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, context: TripContext, message: str) -> TurnOutcome:
        """通知已进入执行，等待放行后返回内部处理结果。"""
        self.entered.set()
        await self.release.wait()
        return await self.inner(context, message)
