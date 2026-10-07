"""创建可复用的异步数据库引擎和请求会话工厂。"""

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings


def create_db_engine(settings: Settings) -> AsyncEngine:
    """按项目配置创建引擎，首次查询时才建立连接。

    Args:
        settings: 数据库连接和连接池配置。

    Returns:
        由应用生命周期负责关闭的异步引擎。
    """
    return create_async_engine(
        settings.mysql_url,
        pool_size=settings.mysql_pool_size,
        max_overflow=settings.mysql_max_overflow,
        pool_timeout=settings.mysql_pool_timeout_seconds,
        pool_recycle=settings.mysql_pool_recycle_seconds,
        pool_pre_ping=True,
        echo=settings.mysql_echo,
        hide_parameters=True,
        connect_args={
            "connect_timeout": settings.mysql_connect_timeout_seconds,
            "init_command": "SET time_zone = '+00:00'",
        },
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """为每个工作单元创建独立会话，并保留已提交对象的已加载字段。"""
    return async_sessionmaker(engine, expire_on_commit=False)
