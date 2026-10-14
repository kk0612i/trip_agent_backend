"""映射行程对话消息表。"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.trip import TRIP_ID_TYPE


class TripTurn(Base):
    """保存一轮对话中的用户消息或助手回复。"""

    __tablename__ = "trip_turns"
    __table_args__ = (
        UniqueConstraint("trip_id", "turn_id", "role", name="uk_trip_turns_turn_role"),
        Index("idx_trip_turns_trip_id_id", "trip_id", "id"),
    )

    id: Mapped[int] = mapped_column(TRIP_ID_TYPE, primary_key=True, autoincrement=True)
    trip_id: Mapped[int] = mapped_column(
        TRIP_ID_TYPE, ForeignKey("trips.id", name="fk_trip_turns_trip_id"), nullable=False,
    )
    turn_id: Mapped[str] = mapped_column(
        String(64, collation="utf8mb4_bin").with_variant(String(64), "sqlite"), nullable=False,
    )
    # user / assistant
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    draft_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"),
    )
