"""景点属性全局缓存的数据访问。"""

from sqlalchemy import select
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.poi_profile import PoiProfile
from app.schemas.trip_plan_schema import PoiProfile as PoiProfileData

# 与表结构 name VARCHAR(255) 一致。
_NAME_MAX_LENGTH = 255


class PoiProfileRepository:
    """按 POI 标识批量读取和写入景点属性，不提交事务。"""

    def __init__(self, session: AsyncSession) -> None:
        """保存本工作单元的数据库会话。"""
        self.session = session

    async def get_many(self, poi_ids: list[str]) -> dict[str, PoiProfileData]:
        """批量查询已缓存的景点属性。

        Args:
            poi_ids: 高德 POI 标识列表，可包含重复值。

        Returns:
            POI 标识到景点属性的映射，未缓存的标识不出现在结果中。
        """
        if not poi_ids:
            return {}
        rows = await self.session.scalars(select(PoiProfile).where(PoiProfile.poi_id.in_(set(poi_ids))))
        return {
            row.poi_id: PoiProfileData(
                poi_id=row.poi_id, subtype=row.subtype, indoor=row.indoor,
                duration_minutes=row.duration_minutes, source=row.source,
            )
            for row in rows
        }

    async def insert_ignore(self, profiles: list[tuple[PoiProfileData, str]]) -> None:
        """批量写入景点属性，已存在的 POI 保留原记录。

        Args:
            profiles: 景点属性与景点名称组成的列表。
        """
        if not profiles:
            return
        values = [
            {
                "poi_id": profile.poi_id, "name": name[:_NAME_MAX_LENGTH], "subtype": profile.subtype,
                "indoor": profile.indoor, "duration_minutes": profile.duration_minutes,
                "source": profile.source,
            }
            for profile, name in profiles
        ]
        dialect = self.session.get_bind().dialect.name
        if dialect == "mysql":
            statement = mysql.insert(PoiProfile).values(values).prefix_with("IGNORE")
        elif dialect == "sqlite":
            statement = sqlite.insert(PoiProfile).values(values).on_conflict_do_nothing()
        else:
            raise NotImplementedError(f"不支持的数据库方言：{dialect}")
        await self.session.execute(statement)
