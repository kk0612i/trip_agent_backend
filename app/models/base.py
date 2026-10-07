"""应用 ORM 模型共用的声明式基类。"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """提供全部表映射共用的元数据。"""
