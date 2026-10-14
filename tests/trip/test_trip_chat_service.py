"""直接验证行程对话 Service 的租约、超时、取消和并发写入行为。"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import TripBusy, TripNotFound, TripTurnFailed
from app.models.trip import Trip
from app.repository.poi_profile_repository import PoiProfileRepository
from app.repository.trip_plan_version_repository import TripPlanVersionRepository
from app.repository.trip_repository import TripRepository
from app.repository.trip_turn_repository import TripTurnRepository
from app.schemas.auth_schema import AuthenticatedUser
from app.schemas.trip_plan_schema import PoiProfile
from app.schemas.trip_schema import MessageRequest, TripContext, TurnOutcome
from app.services.trip_chat_service import TripChatService
from app.services.trip_service import TripService
from tests.trip.trip_session_fakes import BlockingHandler, FakeTurnRunner

UserFactory = Callable[[str], Awaitable[AuthenticatedUser]]


class FakeClock:
    """可手动推进的 UTC 时钟。"""

    def __init__(self, now: datetime) -> None:
        """设置初始时间。"""
        self.now = now

    def __call__(self) -> datetime:
        """返回当前时间。"""
        return self.now


def build_chat_service(
    session_factory: async_sessionmaker[AsyncSession], runner: FakeTurnRunner, *,
    clock: FakeClock | None = None, timeout_seconds: float = 5, lock_seconds: int = 60,
) -> TripChatService:
    """构造带测试限制的对话 Service。"""
    return TripChatService(
        session_factory, runner, lock_seconds=lock_seconds, timeout_seconds=timeout_seconds, clock=clock,
    )


async def create_trip(session_factory: async_sessionmaker[AsyncSession], user: AuthenticatedUser) -> int:
    """通过 TripService 创建行程。"""
    async with session_factory() as session:
        service = TripService(
            session, TripRepository(session), TripPlanVersionRepository(session), TripTurnRepository(session),
        )
        return (await service.create_trip(user)).trip_id


async def load_trip(session_factory: async_sessionmaker[AsyncSession], trip_id: int) -> Trip:
    """读取数据库中的行程行。"""
    async with session_factory() as session:
        return await session.get(Trip, trip_id)


def message(turn_id: str, content: str = "规划") -> MessageRequest:
    """构造消息请求。"""
    return MessageRequest(turn_id=turn_id, content=content)


@pytest.mark.asyncio
async def test_expired_lock_can_be_taken_over(
    session_factory: async_sessionmaker[AsyncSession], make_user: UserFactory,
) -> None:
    """租约过期后新轮次可接管；原轮次完成时发现租约已被接管，返回 TripBusy 且不写入。"""
    user = await make_user("lock@example.com")
    trip_id = await create_trip(session_factory, user)
    clock = FakeClock(datetime(2026, 10, 14, 1, 0, tzinfo=UTC))
    runner = FakeTurnRunner()
    blocking = BlockingHandler()
    runner.handler = blocking
    service = build_chat_service(session_factory, runner, clock=clock, lock_seconds=60)
    stale = asyncio.create_task(service.handle_message(user, trip_id, message("turn-stale-1")))
    await asyncio.wait_for(blocking.entered.wait(), 5)

    with pytest.raises(TripBusy):
        await service.handle_message(user, trip_id, message("turn-early-1"))
    clock.now += timedelta(seconds=61)
    takeover_runner = FakeTurnRunner()
    takeover = build_chat_service(session_factory, takeover_runner, clock=clock)
    result = await takeover.handle_message(user, trip_id, message("turn-takeover-1"))
    assert result.draft_version == 1

    blocking.release.set()
    with pytest.raises(TripBusy):
        await stale
    trip = await load_trip(session_factory, trip_id)
    assert trip.current_draft_version == 1 and trip.lock_token is None
    async with session_factory() as session:
        turns = await TripTurnRepository(session).list_by_trip(trip_id)
    assert {turn.turn_id for turn in turns} == {"turn-takeover-1"}


@pytest.mark.asyncio
async def test_timeout_raises_turn_failed_and_releases_lock(
    session_factory: async_sessionmaker[AsyncSession], make_user: UserFactory,
) -> None:
    """图执行超时转为 TripTurnFailed，并释放租约。"""
    user = await make_user("timeout@example.com")
    trip_id = await create_trip(session_factory, user)
    runner = FakeTurnRunner()
    runner.handler = BlockingHandler()
    service = build_chat_service(session_factory, runner, timeout_seconds=0.05)

    with pytest.raises(TripTurnFailed):
        await service.handle_message(user, trip_id, message("turn-timeout-1"))
    assert (await load_trip(session_factory, trip_id)).lock_token is None


@pytest.mark.asyncio
async def test_cancel_releases_lock(
    session_factory: async_sessionmaker[AsyncSession], make_user: UserFactory,
) -> None:
    """请求被取消时释放租约并继续抛出取消。"""
    user = await make_user("cancel@example.com")
    trip_id = await create_trip(session_factory, user)
    runner = FakeTurnRunner()
    blocking = BlockingHandler()
    runner.handler = blocking
    service = build_chat_service(session_factory, runner)
    task = asyncio.create_task(service.handle_message(user, trip_id, message("turn-cancel-1")))
    await asyncio.wait_for(blocking.entered.wait(), 5)
    assert (await load_trip(session_factory, trip_id)).lock_token is not None

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await load_trip(session_factory, trip_id)).lock_token is None


@pytest.mark.asyncio
async def test_concurrent_retry_unique_conflict_returns_saved_reply(
    session_factory: async_sessionmaker[AsyncSession], make_user: UserFactory,
) -> None:
    """同一 turn_id 已由另一次请求保存时，写入冲突回滚并按幂等返回已保存的回复。"""
    user = await make_user("retry@example.com")
    trip_id = await create_trip(session_factory, user)
    runner = FakeTurnRunner()

    async def save_first(context: TripContext, content: str) -> TurnOutcome:
        # 模拟同一轮次的另一次请求在本次执行期间完成并提交。
        async with session_factory() as session, session.begin():
            repo = TripTurnRepository(session)
            now = datetime(2026, 10, 14)
            await repo.add(trip_id, "turn-retry-1", "user", content, None, now)
            await repo.add(trip_id, "turn-retry-1", "assistant", "先保存的回复", None, now)
        return TurnOutcome(route="chat", reply="后到的回复", requirements=context.requirements)

    runner.handler = save_first
    service = build_chat_service(session_factory, runner)
    result = await service.handle_message(user, trip_id, message("turn-retry-1", "你好"))

    assert result.reply == "先保存的回复"
    trip = await load_trip(session_factory, trip_id)
    assert trip.lock_token is None and trip.requirements["pace"] == "normal"


@pytest.mark.asyncio
async def test_context_uses_beijing_date_and_recent_six_turns(
    session_factory: async_sessionmaker[AsyncSession], make_user: UserFactory,
) -> None:
    """today 按北京时间计算，最近对话最多 6 条且按时间升序。"""
    user = await make_user("context@example.com")
    trip_id = await create_trip(session_factory, user)
    runner = FakeTurnRunner()
    # UTC 16:30 已是北京时间次日 00:30。
    clock = FakeClock(datetime(2026, 10, 14, 16, 30, tzinfo=UTC))
    service = build_chat_service(session_factory, runner, clock=clock)
    for index in range(5):
        await service.handle_message(user, trip_id, message(f"turn-ctx-{index}", f"消息{index}"))

    # 第 5 轮开始时已保存 8 条消息，只取最近 6 条。
    context = runner.calls[-1][0]
    assert context.today == date(2026, 10, 15)
    assert [(turn.role, turn.content) for turn in context.recent_turns] == [
        ("user", "消息1"), ("assistant", "好的"), ("user", "消息2"), ("assistant", "好的"),
        ("user", "消息3"), ("assistant", "好的"),
    ]


@pytest.mark.asyncio
async def test_missing_or_foreign_trip_not_found(
    session_factory: async_sessionmaker[AsyncSession], make_user: UserFactory,
) -> None:
    """对话 Service 同样对不存在与不属于自己的行程抛 TripNotFound。"""
    owner = await make_user("owner2@example.com")
    other = await make_user("other2@example.com")
    trip_id = await create_trip(session_factory, owner)
    service = build_chat_service(session_factory, FakeTurnRunner())

    with pytest.raises(TripNotFound):
        await service.handle_message(other, trip_id, message("turn-nf-0001"))
    with pytest.raises(TripNotFound):
        await service.handle_message(owner, trip_id + 100, message("turn-nf-0002"))


@pytest.mark.asyncio
async def test_poi_profile_insert_ignore_and_get_many(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """重复写入同一景点保留先写入的记录，批量查询返回 Schema 对象。"""
    first = PoiProfile(poi_id="B000A1", subtype="museum", indoor="indoor", duration_minutes=120, source="llm")
    changed = first.model_copy(update={"duration_minutes": 60, "source": "rule"})
    other = PoiProfile(poi_id="B000A2", subtype="lake_river", indoor="outdoor", duration_minutes=90, source="rule")
    async with session_factory() as session, session.begin():
        repo = PoiProfileRepository(session)
        await repo.insert_ignore([(first, "浙江省博物馆")])
        await repo.insert_ignore([(changed, "浙江省博物馆"), (other, "西湖")])
        await repo.insert_ignore([])

    async with session_factory() as session, session.begin():
        profiles = await PoiProfileRepository(session).get_many(["B000A1", "B000A2", "missing"])
        assert await PoiProfileRepository(session).get_many([]) == {}
    assert profiles == {"B000A1": first, "B000A2": other}


@pytest.mark.asyncio
async def test_repository_lock_and_confirm_conditions(
    session_factory: async_sessionmaker[AsyncSession], make_user: UserFactory,
) -> None:
    """租约与确认使用条件更新，条件不满足时返回 False。"""
    user = await make_user("repo@example.com")
    trip_id = await create_trip(session_factory, user)
    now = datetime(2026, 10, 14, 1, 0)
    async with session_factory() as session, session.begin():
        repo = TripRepository(session)
        assert await repo.acquire_lock(trip_id, "token-a", now, now + timedelta(seconds=30))
        assert not await repo.acquire_lock(trip_id, "token-b", now, now + timedelta(seconds=30))
        assert not await repo.release_lock(trip_id, "token-b")
        assert await repo.acquire_lock(trip_id, "token-b", now + timedelta(seconds=31), now + timedelta(seconds=90))
        assert await repo.release_lock(trip_id, "token-b")
        assert not await repo.confirm(trip_id, 1, now)
    trip = await load_trip(session_factory, trip_id)
    assert trip.status == "collecting" and trip.lock_token is None
