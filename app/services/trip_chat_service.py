"""行程对话轮次的业务协调：租约锁、图执行与结果持久化。

一轮对话包含较长的模型和外部调用，因此不复用请求会话，而是由会话工厂为每个短事务
分别创建 Session；图执行期间不持有数据库连接和事务。
"""

import asyncio
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Protocol
from uuid import uuid4

from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import TripBusy, TripNotFound, TripTurnFailed
from app.models.trip import Trip
from app.models.trip_turn import TripTurn
from app.repository.trip_plan_version_repository import TripPlanVersionRepository
from app.repository.trip_repository import TripRepository
from app.repository.trip_turn_repository import TripTurnRepository
from app.schemas.auth_schema import AuthenticatedUser
from app.schemas.trip_plan_schema import Plan, Requirements
from app.schemas.trip_schema import (
    MessageRequest, MessageResponse, PendingQuestion, TripContext, TurnOutcome, TurnRecord,
)
from app.services.trip_service import load_plan, to_db_time, to_pending_view, utc_now

# 北京时间固定为 UTC+8；Windows 默认没有 tzdata，不使用 ZoneInfo。
BEIJING_TZ = timezone(timedelta(hours=8))

# 提供给图的最近对话条数。
RECENT_TURN_LIMIT = 6

# 图执行期间行程已被确认或被其他轮次更新时，替换本轮回复的固定提示。
DISCARDED_REPLY = "行程在处理期间已被确认或更新，本次修改未生效，请查看最新行程。"

# 同一轮次重复写入时可能触发的唯一键：MySQL 键名及 SQLite 冲突列。
_TURN_UNIQUE_KEYS = {
    "uk_trip_turns_turn_role": "trip_turns.trip_id, trip_turns.turn_id, trip_turns.role",
    "uk_trip_plan_versions_source_turn": "trip_plan_versions.trip_id, trip_plan_versions.source_turn_id",
}


class TurnRunner(Protocol):
    """执行一轮对话的图调用协议，由 Agent 实现。"""

    async def run(self, context: TripContext, message: str) -> TurnOutcome:
        """根据会话快照和用户消息执行一轮规划。

        Args:
            context: 本轮开始时从数据库读取的会话快照。
            message: 用户消息原文。

        Returns:
            需要由 Service 持久化的本轮结果。
        """
        ...


def _is_turn_unique_conflict(exc: IntegrityError) -> bool:
    """只识别同一轮次重复写入引起的唯一键冲突。"""
    arguments = getattr(exc.orig, "args", ())
    if len(arguments) >= 2 and arguments[0] == 1062:
        key_match = re.search(r"for key ['`]([^'`]+)['`]\s*$", str(arguments[1]))
        return key_match is not None and key_match.group(1).rsplit(".", 1)[-1] in _TURN_UNIQUE_KEYS
    # SQLite 替身使用相同唯一性契约，但错误消息以列名标识冲突。
    return any(arguments == (f"UNIQUE constraint failed: {columns}",) for columns in _TURN_UNIQUE_KEYS.values())


def _elapsed_ms(started_at: float) -> float:
    """返回自开始以来的毫秒数。"""
    return round((perf_counter() - started_at) * 1000, 2)


class TripChatService:
    """协调一轮行程对话；实例不保存单轮状态，可在请求间安全复用。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        turn_runner: TurnRunner,
        *,
        lock_seconds: int,
        timeout_seconds: float,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """接收会话工厂、图执行器和执行限制，不创建外部连接。

        Args:
            session_factory: 为每个事务创建独立会话的工厂。
            turn_runner: 执行一轮对话的图调用对象。
            lock_seconds: 单轮占用行程的租约秒数，应大于 timeout_seconds。
            timeout_seconds: 单轮图执行期限秒数。
            clock: 返回当前时间（带时区）的函数，默认取 UTC 当前时间。
        """
        self.session_factory = session_factory
        self.turn_runner = turn_runner
        self.lock_seconds = lock_seconds
        self.timeout_seconds = timeout_seconds
        self.clock = clock or utc_now

    async def handle_message(
        self, user: AuthenticatedUser, trip_id: int, req: MessageRequest,
    ) -> MessageResponse:
        """处理一条用户消息：取得租约、执行图、按条件保存结果。

        同一 turn_id 已有回复时直接返回已保存的结果，不再执行图。

        Args:
            user: 已认证用户。
            trip_id: 行程标识。
            req: 用户消息及客户端轮次标识。

        Returns:
            本轮回复及结束后的行程状态。

        Raises:
            TripNotFound: 行程不存在或不属于当前用户。
            TripBusy: 行程正被另一轮对话占用，或本轮租约已被接管。
            TripTurnFailed: 图执行失败或超时。
        """
        started_at = perf_counter()
        token = uuid4().hex

        # 事务 1a：校验归属、幂等重放、取得租约。
        async with self.session_factory() as session:
            async with session.begin():
                trip_repo = TripRepository(session)
                trip = await trip_repo.get_by_id_for_user(trip_id, user.user_id)
                if trip is None:
                    raise TripNotFound
                replay = await self._load_replay(session, trip, req.turn_id)
                if replay is None:
                    now = to_db_time(self.clock())
                    acquired = await trip_repo.acquire_lock(
                        trip_id, token, now, now + timedelta(seconds=self.lock_seconds),
                    )
        if replay is not None:
            self._log_completed("replayed", started_at)
            return replay
        if not acquired:
            logger.bind(event="trip_turn_rejected", status="rejected", error_type="TripBusy").warning(
                "行程正被其他轮次占用",
            )
            raise TripBusy

        # 事务 1b：取得租约后再读取上下文，保证快照包含之前轮次已提交的全部结果。
        try:
            context = await self._load_context(user, trip_id, req.turn_id)
        except BaseException:
            await self._release_lock(trip_id, token)
            raise
        if isinstance(context, MessageResponse):
            await self._release_lock(trip_id, token)
            self._log_completed("replayed", started_at)
            return context

        # 事务外执行图，不占用数据库连接。
        try:
            async with asyncio.timeout(self.timeout_seconds):
                outcome = await self.turn_runner.run(context, req.content)
        except asyncio.CancelledError:
            await self._release_lock(trip_id, token)
            logger.bind(
                event="trip_turn_cancelled", status="cancelled", duration_ms=_elapsed_ms(started_at),
            ).warning("行程对话轮次被取消")
            raise
        except Exception as exc:
            await self._release_lock(trip_id, token)
            logger.bind(
                event="trip_turn_failed", status="failed", error_type=type(exc).__name__,
                duration_ms=_elapsed_ms(started_at), sanitize_exception=True,
            ).opt(exception=exc).error("行程对话轮次执行失败")
            raise TripTurnFailed from None

        # 事务 2：校验租约并按条件保存结果。
        try:
            response, discarded = await self._save_outcome(trip_id, token, context, req, outcome)
        except IntegrityError as exc:
            await self._release_lock(trip_id, token)
            if not _is_turn_unique_conflict(exc):
                raise
            # 同一轮次的另一次请求已先保存结果，按幂等规则返回已保存的回复。
            replay = await self._find_replay(user, trip_id, req.turn_id)
            if replay is None:
                raise TripBusy from None
            self._log_completed("replayed", started_at, route=outcome.route)
            return replay
        except BaseException:
            # 租约释放带令牌条件，已被接管时不会误清他人的租约。
            await self._release_lock(trip_id, token)
            raise
        self._log_completed("discarded" if discarded else "success", started_at, route=outcome.route)
        return response

    async def _load_context(
        self, user: AuthenticatedUser, trip_id: int, turn_id: str,
    ) -> TripContext | MessageResponse:
        """读取本轮会话快照；若该轮次已在取得租约前完成，则返回重放结果。"""
        async with self.session_factory() as session:
            async with session.begin():
                trip = await TripRepository(session).get_for_update(trip_id)
                if trip is None:
                    raise TripNotFound
                replay = await self._load_replay(session, trip, turn_id)
                if replay is not None:
                    return replay
                plan_version_repo = TripPlanVersionRepository(session)
                version = trip.current_draft_version
                draft = await load_plan(plan_version_repo, trip.id, version)
                previous_draft = (
                    await load_plan(plan_version_repo, trip.id, version - 1)
                    if version is not None and version > 1 else None
                )
                turns = await TripTurnRepository(session).list_recent(trip.id, RECENT_TURN_LIMIT)
                now = self.clock()
                if now.tzinfo is None:
                    now = now.replace(tzinfo=timezone.utc)
                context = TripContext(
                    trip_id=trip.id,
                    user_id=user.user_id,
                    status=trip.status,
                    requirements=Requirements.model_validate(trip.requirements),
                    draft=draft,
                    draft_version=version,
                    previous_draft=previous_draft,
                    pending=(
                        PendingQuestion.model_validate(trip.pending_question)
                        if trip.pending_question is not None else None
                    ),
                    recent_turns=[TurnRecord(role=turn.role, content=turn.content) for turn in turns],
                    last_change_summary=trip.last_change_summary,
                    today=now.astimezone(BEIJING_TZ).date(),
                )
        return context

    async def _save_outcome(
        self,
        trip_id: int,
        token: str,
        context: TripContext,
        req: MessageRequest,
        outcome: TurnOutcome,
    ) -> tuple[MessageResponse, bool]:
        """在加锁读取的行程上按条件写入本轮结果、对话记录并释放租约。

        Returns:
            本轮响应，以及新草稿是否因行程已变化而被丢弃。

        Raises:
            TripBusy: 本轮租约已过期并被其他轮次接管。
        """
        now = to_db_time(self.clock())
        discarded = False
        async with self.session_factory() as session:
            async with session.begin():
                trip = await TripRepository(session).get_for_update(trip_id)
                if trip is None or trip.lock_token != token:
                    raise TripBusy
                plan_version_repo = TripPlanVersionRepository(session)
                reply = outcome.reply
                new_version: int | None = None
                plan: Plan | None
                if outcome.new_plan is not None:
                    if (
                        trip.status in ("collecting", "drafting")
                        and trip.current_draft_version == context.draft_version
                    ):
                        new_version = (trip.current_draft_version or 0) + 1
                        await plan_version_repo.add(
                            trip.id, new_version,
                            outcome.new_plan.model_dump(mode="json"),
                            outcome.requirements.model_dump(mode="json"),
                            req.turn_id, outcome.change_summary, now,
                        )
                        trip.status = "drafting"
                        trip.current_draft_version = new_version
                        trip.city_adcode = outcome.new_plan.city.adcode
                        trip.city_name = outcome.new_plan.city.name
                        trip.requirements = outcome.requirements.model_dump(mode="json")
                        trip.pending_question = self._dump_pending(outcome.pending)
                        trip.last_change_summary = outcome.change_summary
                        plan = outcome.new_plan
                    else:
                        # 行程已被确认或已有更新版本，丢弃本轮草稿，不更新需求和待回答问题。
                        discarded = True
                        reply = DISCARDED_REPLY
                        plan = await load_plan(plan_version_repo, trip.id, trip.current_draft_version)
                else:
                    if trip.status != "confirmed":
                        trip.requirements = outcome.requirements.model_dump(mode="json")
                        if trip.status == "collecting":
                            city = outcome.requirements.city
                            trip.city_adcode = city.adcode if city is not None else None
                            trip.city_name = city.name if city is not None else None
                    trip.pending_question = self._dump_pending(outcome.pending)
                    plan = await load_plan(plan_version_repo, trip.id, trip.current_draft_version)
                turn_repo = TripTurnRepository(session)
                await turn_repo.add(trip.id, req.turn_id, "user", req.content, None, now)
                await turn_repo.add(trip.id, req.turn_id, "assistant", reply, new_version, now)
                # 已持有行锁并核对令牌，直接清除租约。
                trip.lock_token = None
                trip.lock_expires_at = None
                trip.updated_at = now
                await session.flush()
                response = MessageResponse(
                    turn_id=req.turn_id, reply=reply, status=trip.status,
                    draft_version=trip.current_draft_version, plan_changed=new_version is not None,
                    plan=plan, pending_question=to_pending_view(trip.pending_question),
                )
        if new_version is not None:
            logger.bind(event="trip_draft_saved", status="success", route=outcome.route).info(
                "行程草稿版本已保存",
            )
        return response, discarded

    async def _find_replay(
        self, user: AuthenticatedUser, trip_id: int, turn_id: str,
    ) -> MessageResponse | None:
        """在新事务中查询某轮次已保存的结果。"""
        async with self.session_factory() as session:
            async with session.begin():
                trip = await TripRepository(session).get_by_id_for_user(trip_id, user.user_id)
                if trip is None:
                    raise TripNotFound
                return await self._load_replay(session, trip, turn_id)

    async def _load_replay(self, session: AsyncSession, trip: Trip, turn_id: str) -> MessageResponse | None:
        """该轮次已有助手回复时，按当时的回复和当前行程状态构造响应。"""
        assistant: TripTurn | None = await TripTurnRepository(session).get(trip.id, turn_id, "assistant")
        if assistant is None:
            return None
        plan = await load_plan(TripPlanVersionRepository(session), trip.id, trip.current_draft_version)
        return MessageResponse(
            turn_id=turn_id, reply=assistant.content, status=trip.status,
            draft_version=trip.current_draft_version, plan_changed=assistant.draft_version is not None,
            plan=plan, pending_question=to_pending_view(trip.pending_question),
        )

    async def _release_lock(self, trip_id: int, token: str) -> None:
        """在独立事务中释放本轮租约；失败只记录日志，租约到期后可被接管。"""

        async def release() -> None:
            async with self.session_factory() as session:
                async with session.begin():
                    await TripRepository(session).release_lock(trip_id, token)

        try:
            # 屏蔽外层取消，保证取消时也尽量完成释放。
            await asyncio.shield(release())
        except Exception as exc:
            logger.bind(
                event="trip_lock_release_failed", status="failed", error_type=type(exc).__name__,
            ).warning("行程租约释放失败，将在到期后自动失效")

    @staticmethod
    def _dump_pending(pending: PendingQuestion | None) -> dict | None:
        """序列化待回答问题，None 表示清除。"""
        return pending.model_dump(mode="json") if pending is not None else None

    @staticmethod
    def _log_completed(status: str, started_at: float, *, route: str | None = None) -> None:
        """记录一轮对话完成事件，不包含消息原文。"""
        fields = {"event": "trip_turn_completed", "status": status, "duration_ms": _elapsed_ms(started_at)}
        if route is not None:
            fields["route"] = route
        logger.bind(**fields).info("行程对话轮次处理完成")
