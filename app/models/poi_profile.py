"""映射景点属性全局缓存表。"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class PoiProfile(Base):
    """按高德 POI 标识缓存景点细分类型、室内外属性和建议游玩时长。"""

    __tablename__ = "poi_profiles"

    poi_id: Mapped[str] = mapped_column(
        String(64, collation="utf8mb4_bin").with_variant(String(64), "sqlite"), primary_key=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    subtype: Mapped[str] = mapped_column(String(32), nullable=False)
    indoor: Mapped[str] = mapped_column(String(16), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"),
    )
