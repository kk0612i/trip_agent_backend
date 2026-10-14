# 配置与日志

配置字段集中在 `app/core/config.py`。默认读取后端项目根目录的 `.env`，不依赖启动时的工作目录；优先级为显式构造参数、系统环境变量、`.env`、字段默认值。`.env.example` 列出全部环境变量。

```python
from app.core.config import get_settings

settings = get_settings()
key = settings.deepseek_api_key.get_secret_value()
mysql_url = settings.mysql_url
```

`get_settings()` 使用 `@lru_cache(maxsize=1)` 延迟加载，返回不可变配置对象。更新 `.env` 后重启进程；测试或明确需要重载时使用 `get_settings.cache_clear()`。API Key 和密码是 `SecretStr`，仅在创建对应客户端时解包，不要输出整个配置对象。

认证启动需要配置至少 32 字节的随机 `AUTH_JWT_SECRET`，MySQL 连接沿用 `MYSQL_*` 配置。生命周期装配数据库引擎、会话工厂、密码哈希器和令牌编解码器，首次查询时建立数据库连接。地图和模型密钥可以留空，此时应用正常启动但行程对话返回服务不可用；博查密钥为空时问答不使用网页搜索。`AGENT_TIMEOUT_SECONDS` 限制单轮对话总时长，`AGENT_RECURSION_LIMIT` 限制根图步骤数，`AGENT_MAX_TOOL_CALLS` 限制问答 Agent 的工具调用次数，`TRIP_SOLVER_TIME_LIMIT_SECONDS` 限制单次 CP-SAT 求解时长，`TRIP_TURN_LOCK_SECONDS` 为单轮对话占用行程的租约时长。高德请求受 `AMAP_MAX_CONCURRENCY` 并发限制，查询结果按 `AMAP_CACHE_TTL_SECONDS` 在进程内缓存。行程 Agent 见 [行程 Agent 文档](trip_agent.md)。认证配置与接口见 [认证文档](authentication.md)。

`settings.mysql_url` 返回 SQLAlchemy `URL` 对象，由 `app/core/db.py` 的 `create_async_engine()` 使用。其驱动为 `mysql+aiomysql`，构造时自动处理密码中的 `@`、`:` 等字符；默认字符串表示隐藏密码。连接配置 UTC 时区、连接池与连接超时，并隐藏 SQL 参数。现有 `scripts/trip_agents.sql` 需自行选择数据库后执行，应用启动不会自动建表。

日志统一由 `app/core/log.py` 的 `setup_logging(settings)` 初始化。FastAPI 生命周期和直接启动入口负责初始化，业务模块仅导入 `loguru.logger`。标准 `logging` 和 Uvicorn 日志转发到同一链路，重复初始化不会累积 Handler 或 Sink。

```python
from loguru import logger

# logger.contextualize 使用 ContextVar，退出上下文后自动恢复。
with logger.contextualize(request_id="请求关联标识", entry_point="api"):
    logger.bind(event="trip_created", status="success", duration_ms=12.5).info("行程创建完成")
```

开发环境 `LOG_FORMAT=auto` 输出易读文本，`json` 可主动启用 JSON。生产环境始终输出 JSON。日志默认输出到 stderr；`LOG_FILE_ENABLED=true` 时额外写入 `LOG_DIR/backend-YYYY-MM-DD.log`，例如 `backend-2026-10-07.log`，文件始终使用 JSON。相对日志目录以项目根目录为基准。

当前采用单 worker，文件名日期由 Loguru 按运行主机的本地时间在文件创建时填充。文件轮转和保留分别使用 `LOG_ROTATION`、`LOG_RETENTION`，默认仍按 10 MB 轮转，日期文件名本身不会触发每日轮转。同一天重复创建同名文件时会追加写入；轮转遇到同名文件时 Loguru 自动添加时间后缀以避免覆盖。容器部署建议使用默认控制台输出并由部署平台收集日志。`LOG_ENQUEUE=true` 使用队列写入，应用退出时等待日志完成。

每条输出包含时间、级别、模块、进程号、事件名、request_id、run_id、entry_point。可附加 status、duration_ms、attempt、error_type、method、route、status_code、node、tool、call_id。其余附加字段不输出；异常堆栈不采集局部变量，已配置的高德 Key、博查 Key、DeepSeek Key、MySQL 密码和 JWT 签名密钥会在消息及异常中脱敏。API 意外异常只输出调用位置和异常类型，不输出异常值、源码行或异常链中的原始详情。调用方仍需避免将认证头、提示词、消息历史、原始工具结果等敏感内容放进消息。

日志覆盖应用启动与关闭及认证路径。`run_id` 关联同一次生命周期，HTTP 入口为每次请求生成 `request_id`，通过 ContextVar 传播并以 `X-Request-ID` 返回；请求结束后恢复上下文。`http_request_completed` 记录方法、路由模板、状态码及单调时钟耗时；`auth_registered`、`auth_logged_in`、`auth_token_validated`、`auth_logged_out` 记录业务结果，写入成功事件位于提交之后。预期拒绝使用 WARNING，意外失败只在 API 边界记录一次 ERROR 堆栈。默认 Uvicorn 访问日志由独立的 Uvicorn 调用产生，不携带应用请求上下文，排障优先按应用事件的 request_id 查询。
