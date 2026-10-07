"""用户账户的数据访问。"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


class UserRepository:
    """使用调用方提供的会话访问用户表，不提交事务。"""

    def __init__(self, session: AsyncSession) -> None:
        """保存本工作单元的数据库会话。"""
        self.session = session

    async def get_by_email(self, email: str) -> User | None:
        """查询规范化邮箱对应的账户，未找到时返回 None。"""
        return await self.session.scalar(select(User).where(User.email == email))

    async def get_by_id(self, user_id: int) -> User | None:
        """查询已验证令牌对应的用户，未找到时返回 None。"""
        return await self.session.get(User, user_id)

    async def add(self, email: str, password_hash: str, created_at: datetime) -> User:
        """写入账户并取得数据库生成的标识，由调用方提交事务。"""
        user = User(email=email, password_hash=password_hash, created_at=created_at)
        self.session.add(user)
        await self.session.flush()
        return user
