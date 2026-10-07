"""映射已有的用户账户表。"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.mysql import BIGINT
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class User(Base):
    """保存公开账户信息及 Argon2id 密码哈希。"""

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uk_users_email"),)

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(BIGINT(unsigned=True), "mysql").with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    email: Mapped[str] = mapped_column(
        String(254, collation="utf8mb4_bin").with_variant(String(254), "sqlite"),
        nullable=False,
    )
    password_hash: Mapped[str] = mapped_column(
        String(255, collation="utf8mb4_bin").with_variant(String(255), "sqlite"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP"),
    )
