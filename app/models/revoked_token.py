"""映射已有的令牌撤销表。"""

from datetime import datetime

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class RevokedToken(Base):
    """保存撤销令牌的唯一标识和 UTC 到期时间。"""

    __tablename__ = "revoked_tokens"
    __table_args__ = (Index("idx_revoked_tokens_expires_at", "expires_at"),)

    jti: Mapped[str] = mapped_column(
        String(64, collation="utf8mb4_bin").with_variant(String(64), "sqlite"),
        primary_key=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
