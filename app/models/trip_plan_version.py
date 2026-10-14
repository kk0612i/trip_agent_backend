"""映射行程草稿版本表。"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.trip import TRIP_ID_TYPE


class TripPlanVersion(Base):
    """保存只追加的草稿版本及生成时的需求快照。"""

    __tablename__ = "trip_plan_versions"
    __table_args__ = (
        UniqueConstraint("trip_id", "source_turn_id", name="uk_trip_plan_versions_source_turn"),
    )

    trip_id: Mapped[int] = mapped_column(
        TRIP_ID_TYPE, ForeignKey("trips.id", name="fk_trip_plan_versions_trip_id"),
        primary_key=True, autoincrement=False,
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    plan: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    requirements: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    source_turn_id: Mapped[str] = mapped_column(
        String(64, collation="utf8mb4_bin").with_variant(String(64), "sqlite"), nullable=False,
    )
    change_summary: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"),
    )
