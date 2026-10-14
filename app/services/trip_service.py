"""行程会话的查询、创建与确认业务及事务边界。"""

from collections.abc import Callable
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DraftVersionConflict, TripNotConfirmable, TripNotFound
from app.models.trip import Trip
from app.models.trip_turn import TripTurn
from app.repository.trip_plan_version_repository import TripPlanVersionRepository
from app.repository.trip_repository import TripRepository
from app.repository.trip_turn_repository import TripTurnRepository
from app.schemas.auth_schema import AuthenticatedUser
from app.schemas.trip_plan_schema import Plan, Requirements
from app.schemas.trip_schema import (
    ConfirmRequest, ConfirmResponse, PendingQuestion, PendingQuestionView, TripDetail, TripSummary, TurnView,
)

# 行程列表单次返回的最大条数。
TRIP_LIST_LIMIT = 50


def utc_now() -> datetime:
    """返回带 UTC 时区的当前时间，作为默认时钟。"""
    return datetime.now(UTC)


def to_db_time(moment: datetime) -> datetime:
    """转换为数据库保存使用的不带时区 UTC 时间，精确到秒。"""
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    return moment.replace(microsecond=0)


def to_aware(moment: datetime) -> datetime:
    """将数据库读出的不带时区 UTC 时间标记为 UTC。"""
    return moment.replace(tzinfo=UTC)


def to_pending_view(pending_question: dict[str, Any] | None) -> PendingQuestionView | None:
    """将保存的待回答问题转换为对外展示结构，选项只保留文字。"""
    if pending_question is None:
        return None
    pending = PendingQuestion.model_validate(pending_question)
    return PendingQuestionView(
        kind=pending.kind, text=pending.text, options=[option.label for option in pending.options],
    )


def to_summary(trip: Trip) -> TripSummary:
    """将行程实体转换为列表项。"""
    return TripSummary(
        trip_id=trip.id, status=trip.status, city_name=trip.city_name,
        draft_version=trip.current_draft_version, confirmed_version=trip.confirmed_version,
        created_at=to_aware(trip.created_at), updated_at=to_aware(trip.updated_at),
    )


def to_detail(trip: Trip, plan: Plan | None) -> TripDetail:
    """将行程实体及当前草稿内容转换为详情。"""
    return TripDetail(
        **to_summary(trip).model_dump(),
        requirements=Requirements.model_validate(trip.requirements),
        plan=plan,
        pending_question=to_pending_view(trip.pending_question),
    )


def to_turn_view(turn: TripTurn) -> TurnView:
    """将对话消息实体转换为对外结构。"""
    return TurnView(
        turn_id=turn.turn_id, role=turn.role, content=turn.content,
        draft_version=turn.draft_version, created_at=to_aware(turn.created_at),
    )


async def load_plan(
    plan_version_repo: TripPlanVersionRepository, trip_id: int, version: int | None,
) -> Plan | None:
    """读取指定草稿版本的内容，版本为空或不存在时返回 None。"""
    if version is None:
        return None
    plan_version = await plan_version_repo.get(trip_id, version)
    return Plan.model_validate(plan_version.plan) if plan_version is not None else None


class TripService:
    """协调行程的创建、查询与确认，每个请求使用独立会话和 Repository。"""

    def __init__(
        self,
        session: AsyncSession,
        trip_repo: TripRepository,
        trip_plan_version_repo: TripPlanVersionRepository,
        trip_turn_repo: TripTurnRepository,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """接收本次工作单元的依赖，不创建外部连接。

        Args:
            session: 当前请求的数据库会话。
            trip_repo: 行程 Repository。
            trip_plan_version_repo: 草稿版本 Repository。
            trip_turn_repo: 对话消息 Repository。
            clock: 返回当前时间的函数，默认取 UTC 当前时间。
        """
        self.session = session
        self.trip_repo = trip_repo
        self.trip_plan_version_repo = trip_plan_version_repo
        self.trip_turn_repo = trip_turn_repo
        self.clock = clock or utc_now

    async def create_trip(self, user: AuthenticatedUser) -> TripDetail:
        """创建处于需求收集状态的空行程。"""
        now = to_db_time(self.clock())
        async with self.session.begin():
            trip = await self.trip_repo.add(
                user.user_id, "collecting", Requirements().model_dump(mode="json"), now,
            )
            detail = to_detail(trip, None)
        logger.bind(event="trip_created", status="success").info("行程创建完成")
        return detail

    async def list_trips(self, user: AuthenticatedUser) -> list[TripSummary]:
        """按创建倒序列出当前用户最近的行程。"""
        async with self.session.begin():
            trips = await self.trip_repo.list_by_user(user.user_id, TRIP_LIST_LIMIT)
            return [to_summary(trip) for trip in trips]

    async def get_trip(self, user: AuthenticatedUser, trip_id: int) -> TripDetail:
        """返回行程详情及当前草稿内容。

        Raises:
            TripNotFound: 行程不存在或不属于当前用户。
        """
        async with self.session.begin():
            trip = await self._get_owned(user, trip_id)
            plan = await load_plan(self.trip_plan_version_repo, trip.id, trip.current_draft_version)
            return to_detail(trip, plan)

    async def list_turns(self, user: AuthenticatedUser, trip_id: int) -> list[TurnView]:
        """按时间顺序返回行程的全部对话消息。

        Raises:
            TripNotFound: 行程不存在或不属于当前用户。
        """
        async with self.session.begin():
            trip = await self._get_owned(user, trip_id)
            turns = await self.trip_turn_repo.list_by_trip(trip.id)
            return [to_turn_view(turn) for turn in turns]

    async def confirm(self, user: AuthenticatedUser, trip_id: int, req: ConfirmRequest) -> ConfirmResponse:
        """以条件更新确认前端展示的草稿版本，重复确认同一版本时幂等返回。

        Raises:
            TripNotFound: 行程不存在或不属于当前用户。
            TripNotConfirmable: 行程还没有草稿。
            DraftVersionConflict: 提交的版本不是当前草稿版本，或已确认的是其他版本。
        """
        started_at = perf_counter()
        now = to_db_time(self.clock())
        confirmed_now = False
        async with self.session.begin():
            trip: Trip | None = await self._get_owned(user, trip_id)
            if trip.status != "confirmed":
                if trip.current_draft_version is None:
                    raise TripNotConfirmable
                if trip.current_draft_version != req.draft_version:
                    raise DraftVersionConflict
                confirmed_now = await self.trip_repo.confirm(trip.id, req.draft_version, now)
                if not confirmed_now:
                    # 条件更新未命中说明期间有并发写入；加锁读取最新提交的数据再判断。
                    trip = await self.trip_repo.get_for_update(trip_id)
            if confirmed_now:
                response = ConfirmResponse(
                    trip_id=trip_id, status="confirmed",
                    confirmed_version=req.draft_version, confirmed_at=to_aware(now),
                )
            else:
                # 已确认同一版本时按幂等成功返回，其他情况均为版本冲突。
                if (
                    trip is None or trip.status != "confirmed" or trip.confirmed_at is None
                    or trip.confirmed_version != req.draft_version
                ):
                    raise DraftVersionConflict
                response = ConfirmResponse(
                    trip_id=trip_id, status="confirmed",
                    confirmed_version=trip.confirmed_version, confirmed_at=to_aware(trip.confirmed_at),
                )
        logger.bind(
            event="trip_confirmed", status="success" if confirmed_now else "replayed",
            duration_ms=round((perf_counter() - started_at) * 1000, 2),
        ).info("行程确认完成")
        return response

    async def _get_owned(self, user: AuthenticatedUser, trip_id: int) -> Trip:
        """查询当前用户的行程，不存在与无权访问统一视为不存在。"""
        trip = await self.trip_repo.get_by_id_for_user(trip_id, user.user_id)
        if trip is None:
            raise TripNotFound
        return trip
