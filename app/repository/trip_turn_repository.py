"""行程对话消息的数据访问。"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trip_turn import TripTurn


class TripTurnRepository:
    """使用调用方提供的会话读写对话消息，不提交事务。"""

    def __init__(self, session: AsyncSession) -> None:
        """保存本工作单元的数据库会话。"""
        self.session = session

    async def get(self, trip_id: int, turn_id: str, role: str) -> TripTurn | None:
        """查询某轮次指定发送方的消息，未找到时返回 None。"""
        return await self.session.scalar(
            select(TripTurn).where(
                TripTurn.trip_id == trip_id, TripTurn.turn_id == turn_id, TripTurn.role == role,
            ),
        )

    async def add(
        self,
        trip_id: int,
        turn_id: str,
        role: str,
        content: str,
        draft_version: int | None,
        now: datetime,
    ) -> TripTurn:
        """写入一条消息，唯一键冲突时由数据库异常向上传播。"""
        turn = TripTurn(
            trip_id=trip_id, turn_id=turn_id, role=role, content=content,
            draft_version=draft_version, created_at=now,
        )
        self.session.add(turn)
        await self.session.flush()
        return turn

    async def list_recent(self, trip_id: int, limit: int) -> list[TripTurn]:
        """返回最近若干条消息，按写入顺序升序排列。"""
        result = await self.session.scalars(
            select(TripTurn).where(TripTurn.trip_id == trip_id).order_by(TripTurn.id.desc()).limit(limit),
        )
        return list(reversed(list(result)))

    async def list_by_trip(self, trip_id: int) -> list[TripTurn]:
        """按写入顺序列出行程的全部消息。"""
        result = await self.session.scalars(
            select(TripTurn).where(TripTurn.trip_id == trip_id).order_by(TripTurn.id),
        )
        return list(result)
