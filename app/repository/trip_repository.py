"""行程会话的数据访问。"""

from datetime import datetime
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trip import Trip


class TripRepository:
    """使用调用方提供的会话访问行程表，不提交事务。"""

    def __init__(self, session: AsyncSession) -> None:
        """保存本工作单元的数据库会话。"""
        self.session = session

    async def add(self, user_id: int, status: str, requirements: dict[str, Any], now: datetime) -> Trip:
        """写入新行程并取得数据库生成的标识。

        Args:
            user_id: 所属用户标识。
            status: 初始状态。
            requirements: 已序列化的初始需求。
            now: UTC 创建时间（不带时区）。

        Returns:
            已写入的行程实体。
        """
        trip = Trip(
            user_id=user_id, status=status, requirements=requirements,
            created_at=now, updated_at=now,
        )
        self.session.add(trip)
        await self.session.flush()
        return trip

    async def get_by_id_for_user(self, trip_id: int, user_id: int) -> Trip | None:
        """查询属于指定用户的行程，不存在或不属于该用户时返回 None。"""
        return await self.session.scalar(
            select(Trip).where(Trip.id == trip_id, Trip.user_id == user_id),
        )

    async def get_for_update(self, trip_id: int) -> Trip | None:
        """以加锁读取最新提交的行程行，用于同一事务内的条件写入。"""
        return await self.session.scalar(
            select(Trip).where(Trip.id == trip_id).with_for_update().execution_options(populate_existing=True),
        )

    async def list_by_user(self, user_id: int, limit: int) -> list[Trip]:
        """按标识倒序列出用户的行程。"""
        result = await self.session.scalars(
            select(Trip).where(Trip.user_id == user_id).order_by(Trip.id.desc()).limit(limit),
        )
        return list(result)

    async def acquire_lock(self, trip_id: int, token: str, now: datetime, expires_at: datetime) -> bool:
        """在行程未被占用或租约已过期时写入本轮租约。

        Args:
            trip_id: 行程标识。
            token: 本轮唯一的租约令牌。
            now: 当前 UTC 时间，早于该时间的租约视为过期。
            expires_at: 新租约到期时间。

        Returns:
            成功取得租约时返回 True。
        """
        result = await self.session.execute(
            update(Trip)
            .where(
                Trip.id == trip_id,
                or_(Trip.lock_token.is_(None), Trip.lock_expires_at < now),
            )
            .values(lock_token=token, lock_expires_at=expires_at)
            .execution_options(synchronize_session=False),
        )
        return result.rowcount == 1

    async def release_lock(self, trip_id: int, token: str) -> bool:
        """仅当租约仍属于该令牌时清除租约，返回是否清除。"""
        result = await self.session.execute(
            update(Trip)
            .where(Trip.id == trip_id, Trip.lock_token == token)
            .values(lock_token=None, lock_expires_at=None)
            .execution_options(synchronize_session=False),
        )
        return result.rowcount == 1

    async def confirm(self, trip_id: int, version: int, now: datetime) -> bool:
        """仅当行程处于草稿状态且当前草稿版本一致时标记为已确认。

        Args:
            trip_id: 行程标识。
            version: 要确认的草稿版本号。
            now: UTC 确认时间（不带时区）。

        Returns:
            条件满足并完成更新时返回 True。
        """
        result = await self.session.execute(
            update(Trip)
            .where(
                Trip.id == trip_id,
                Trip.status == "drafting",
                Trip.current_draft_version == version,
            )
            .values(
                status="confirmed", confirmed_version=version, confirmed_at=now,
                pending_question=None, updated_at=now,
            )
            .execution_options(synchronize_session=False),
        )
        return result.rowcount == 1
