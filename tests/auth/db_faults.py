"""在数据库边界注入故障，保持业务层与路由为真实实现。"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine


@contextmanager
def fail_next_insert(engine: AsyncEngine, table: str, original_error: Exception) -> Iterator[None]:
    """模拟检查之后写入时才出现的数据库唯一约束冲突。"""
    failed = False

    def reject_insert(
        connection: Any, cursor: Any, statement: str, parameters: Any,
        context: Any, executemany: bool,
    ) -> None:
        """在实际数据库执行边界触发一次异常。"""
        nonlocal failed
        if not failed and statement.lstrip().lower().startswith(f"insert into {table}"):
            failed = True
            raise IntegrityError(statement, parameters, original_error, hide_parameters=True)

    event.listen(engine.sync_engine, "before_cursor_execute", reject_insert)
    try:
        yield
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", reject_insert)


@contextmanager
def fail_next_commit(engine: AsyncEngine, table: str) -> Iterator[None]:
    """模拟完成 INSERT 后在事务提交处发生故障。"""
    write_pending = False

    def observe_write(
        connection: Any, cursor: Any, statement: str, parameters: Any,
        context: Any, executemany: bool,
    ) -> None:
        """只对目标表写入后的提交注入异常。"""
        nonlocal write_pending
        if statement.lstrip().lower().startswith(f"insert into {table}"):
            write_pending = True

    def reject_commit(connection: Any) -> None:
        """使第一次目标写入提交失败，异常详情故意包含敏感标记。"""
        nonlocal write_pending
        if write_pending:
            write_pending = False
            raise RuntimeError("private-database-detail private-test-password")

    event.listen(engine.sync_engine, "before_cursor_execute", observe_write)
    event.listen(engine.sync_engine, "commit", reject_commit)
    try:
        yield
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe_write)
        event.remove(engine.sync_engine, "commit", reject_commit)
