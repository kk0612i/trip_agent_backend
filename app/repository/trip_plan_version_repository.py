"""行程草稿版本的数据访问。"""

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trip_plan_version import TripPlanVersion


class TripPlanVersionRepository:
    """只追加写入草稿版本，不更新已有版本，不提交事务。"""

    def __init__(self, session: AsyncSession) -> None:
        """保存本工作单元的数据库会话。"""
        self.session = session

    async def get(self, trip_id: int, version: int) -> TripPlanVersion | None:
        """查询指定版本，未找到时返回 None。"""
        return await self.session.get(TripPlanVersion, (trip_id, version))

    async def add(
        self,
        trip_id: int,
        version: int,
        plan: dict[str, Any],
        requirements: dict[str, Any],
        source_turn_id: str,
        change_summary: str | None,
        now: datetime,
    ) -> TripPlanVersion:
        """追加一个草稿版本，由调用方提交事务。

        Args:
            trip_id: 所属行程标识。
            version: 新版本号。
            plan: 已序列化的草稿内容。
            requirements: 已序列化的需求快照。
            source_turn_id: 产生该版本的轮次标识。
            change_summary: 变化摘要。
            now: UTC 创建时间（不带时区）。

        Returns:
            已写入的版本实体。
        """
        plan_version = TripPlanVersion(
            trip_id=trip_id, version=version, plan=plan, requirements=requirements,
            source_turn_id=source_turn_id, change_summary=change_summary, created_at=now,
        )
        self.session.add(plan_version)
        await self.session.flush()
        return plan_version

    async def list_by_trip(self, trip_id: int) -> list[TripPlanVersion]:
        """按版本号升序列出行程的全部草稿版本。"""
        result = await self.session.scalars(
            select(TripPlanVersion).where(TripPlanVersion.trip_id == trip_id).order_by(TripPlanVersion.version),
        )
        return list(result)
