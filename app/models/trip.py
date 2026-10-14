"""映射行程会话表。"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.mysql import BIGINT
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# MySQL 使用无符号 BIGINT；SQLite 替身使用 INTEGER 才能自增。
TRIP_ID_TYPE = BigInteger().with_variant(BIGINT(unsigned=True), "mysql").with_variant(Integer, "sqlite")


class Trip(Base):
    """保存行程状态、当前需求、待回答问题、草稿版本指针和单轮租约锁。"""

    __tablename__ = "trips"
    __table_args__ = (Index("idx_trips_user_id_id", "user_id", "id"),)

    id: Mapped[int] = mapped_column(TRIP_ID_TYPE, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        TRIP_ID_TYPE, ForeignKey("users.id", name="fk_trips_user_id"), nullable=False,
    )
    # collecting / drafting / confirmed
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    city_adcode: Mapped[str | None] = mapped_column(String(6), nullable=True)
    city_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requirements: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # none_as_null 使 Python None 写入 SQL NULL，而不是 JSON 的 null 字面量。
    pending_question: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    last_change_summary: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    current_draft_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confirmed_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lock_token: Mapped[str | None] = mapped_column(
        String(64, collation="utf8mb4_bin").with_variant(String(64), "sqlite"), nullable=True,
    )
    lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"),
    )
