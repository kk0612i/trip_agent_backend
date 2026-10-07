# 配置与日志

配置字段集中在 `app/core/config.py`。默认读取后端项目根目录的 `.env`，不依赖启动时的工作目录；优先级为显式构造参数、系统环境变量、`.env`、字段默认值。`.env.example` 列出全部环境变量。

```python
from app.core.config import get_settings

settings = get_settings()
key = settings.deepseek_api_key.get_secret_value()
mysql_url = settings.mysql_url
```

`get_settings()` 使用 `@lru_cache(maxsize=1)` 延迟加载，返回不可变配置对象。更新 `.env` 后重启进程；测试或明确需要重载时使用 `get_settings.cache_clear()`。API Key 和密码是 `SecretStr`，仅在创建对应客户端时解包，不要输出整个配置对象。

本次初始化允许密钥和数据库密码留空，方便启动项目骨架。接入高德、DeepSeek、数据库之前填写真实配置并在对应客户端初始化时校验。当前未创建客户端、数据库连接、业务表或 Agent 工作流；超时、重试、连接池和 Agent 上限目前是供后续实现使用的配置，需要在调用处传入并执行。

`settings.mysql_url` 返回 SQLAlchemy `URL` 对象，适用于未来的 `create_async_engine()`。其驱动为 `mysql+aiomysql`，构造时自动处理密码中的 `@`、`:` 等字符；默认字符串表示隐藏密码。现有 `scripts/trip_agents.sql` 需自行选择数据库后执行，应用启动不会自动建表。

日志统一由 `app/core/log.py` 的 `setup_logging(settings)` 初始化。FastAPI 生命周期和直接启动入口负责初始化，业务模块仅导入 `loguru.logger`。标准 `logging` 和 Uvicorn 日志转发到同一链路，重复初始化不会累积 Handler 或 Sink。

```python
from loguru import logger

# logger.contextualize 使用 ContextVar，退出上下文后自动恢复。
with logger.contextualize(request_id="请求关联标识", entry_point="api"):
    logger.bind(event="trip_created", status="success", duration_ms=12.5).info("行程创建完成")
```

开发环境 `LOG_FORMAT=auto` 输出易读文本，`json` 可主动启用 JSON。生产环境始终输出 JSON。日志默认输出到 stderr；`LOG_FILE_ENABLED=true` 时额外写入 `LOG_DIR/backend-YYYY-MM-DD.log`，例如 `backend-2026-10-07.log`，文件始终使用 JSON。相对日志目录以项目根目录为基准。

当前采用单 worker，文件名日期由 Loguru 按运行主机的本地时间在文件创建时填充。文件轮转和保留分别使用 `LOG_ROTATION`、`LOG_RETENTION`，默认仍按 10 MB 轮转，日期文件名本身不会触发每日轮转。同一天重复创建同名文件时会追加写入；轮转遇到同名文件时 Loguru 自动添加时间后缀以避免覆盖。容器部署建议使用默认控制台输出并由部署平台收集日志。`LOG_ENQUEUE=true` 使用队列写入，应用退出时等待日志完成。

每条输出包含时间、级别、模块、进程号、事件名、request_id、run_id、entry_point。可附加 status、duration_ms、attempt、error_type、method、route、status_code、node、tool、call_id。其余附加字段不输出；异常堆栈不采集局部变量，已配置的高德 Key、DeepSeek Key 和 MySQL 密码会在消息及异常中脱敏。调用方仍需避免将认证头、提示词、消息历史、原始工具结果等敏感内容放进消息。

当前日志覆盖应用启动与关闭；`run_id` 关联同一次生命周期。当前没有业务路由，未来 API/任务入口应创建请求或执行标识并进入日志上下文；Uvicorn 默认访问日志不自动生成 request_id。意外异常由最外层业务或 API 边界调用一次 `logger.exception`，恢复和重试使用 WARNING，正常关键事件使用 INFO。
