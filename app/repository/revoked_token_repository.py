"""已撤销令牌的数据访问。"""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.revoked_token import RevokedToken


class RevokedTokenRepository:
    """在调用方事务内查询和保存撤销状态。"""

    def __init__(self, session: AsyncSession) -> None:
        """保存当前请求专用的会话。"""
        self.session = session

    async def get_by_jti(self, jti: str) -> RevokedToken | None:
        """查询撤销记录，未撤销的令牌返回 None。"""
        return await self.session.get(RevokedToken, jti)

    async def add(self, jti: str, expires_at: datetime) -> None:
        """写入撤销记录，由最外层业务操作提交。"""
        self.session.add(RevokedToken(jti=jti, expires_at=expires_at))
        await self.session.flush()
