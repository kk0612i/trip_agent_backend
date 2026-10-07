"""应用配置及其缓存访问入口。"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """从项目根目录的 .env 和系统环境变量读取配置。

    系统环境变量优先于 .env；实例不可变，避免共享缓存被请求修改。
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    app_name: str = Field(default="Trip Agents", min_length=1, description="应用名称")
    app_env: Literal["development", "test", "production"] = Field(
        default="development", description="应用运行环境"
    )
    app_debug: bool = Field(default=False, description="应用调试模式")
    app_host: str = Field(default="127.0.0.1", min_length=1, description="服务监听地址")
    app_port: int = Field(default=8000, ge=1, le=65535, description="服务监听端口")

    amap_api_key: SecretStr = Field(default=SecretStr(""), description="高德地图 API 密钥")
    amap_base_url: HttpUrl = Field(
        default="https://restapi.amap.com/v3", description="高德地图 API 地址"
    )
    amap_timeout_seconds: float = Field(default=10, gt=0, description="高德请求超时秒数")

    deepseek_api_key: SecretStr = Field(default=SecretStr(""), description="DeepSeek API 密钥")
    deepseek_base_url: HttpUrl = Field(
        default="https://api.deepseek.com", description="DeepSeek API 地址"
    )
    deepseek_model: str = Field(default="deepseek-chat", min_length=1, description="DeepSeek 模型名称")
    deepseek_temperature: float = Field(default=0.7, ge=0, le=2, description="模型采样温度")
    deepseek_max_tokens: int = Field(default=4096, gt=0, description="模型最大输出 token 数")
    deepseek_timeout_seconds: float = Field(default=60, gt=0, description="模型请求超时秒数")
    deepseek_max_retries: int = Field(default=2, ge=0, description="模型请求最大重试次数")

    mysql_host: str = Field(default="127.0.0.1", min_length=1, description="MySQL 主机地址")
    mysql_port: int = Field(default=3306, ge=1, le=65535, description="MySQL 服务端口")
    mysql_user: str = Field(default="trip_agents", min_length=1, description="MySQL 用户名")
    mysql_password: SecretStr = Field(default=SecretStr(""), description="MySQL 用户密码")
    mysql_database: str = Field(default="trip_agents", min_length=1, description="MySQL 数据库名称")
    mysql_charset: str = Field(default="utf8mb4", min_length=1, description="MySQL 连接字符集")
    mysql_pool_size: int = Field(default=5, gt=0, description="数据库连接池大小")
    mysql_max_overflow: int = Field(default=10, ge=0, description="连接池额外连接上限")
    mysql_pool_timeout_seconds: float = Field(default=30, gt=0, description="连接池等待超时秒数")
    mysql_pool_recycle_seconds: int = Field(default=1800, gt=0, description="连接回收间隔秒数")
    mysql_connect_timeout_seconds: int = Field(default=10, gt=0, description="数据库连接超时秒数")
    mysql_echo: bool = Field(default=False, description="SQL 调试日志开关")

    auth_jwt_secret: SecretStr = Field(default=SecretStr(""), description="访问令牌签名密钥")
    auth_jwt_issuer: str = Field(default="trip-agents", min_length=1, description="访问令牌签发方")
    auth_jwt_audience: str = Field(default="trip-agents-api", min_length=1, description="访问令牌接收方")
    auth_token_ttl_seconds: int = Field(
        default=1800, gt=0, le=86400, description="访问令牌有效秒数",
    )

    log_level: Literal["TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", description="日志输出级别"
    )
    log_format: Literal["auto", "text", "json"] = Field(default="auto", description="控制台日志格式")
    log_file_enabled: bool = Field(default=False, description="文件日志开关")
    log_dir: Path = Field(default=Path("logs"), description="日志文件目录")
    log_rotation: str = Field(default="10 MB", min_length=1, description="日志文件轮转阈值")
    log_retention: str = Field(default="7 days", min_length=1, description="日志文件保留期限")
    log_enqueue: bool = Field(default=True, description="日志队列写入开关")

    agent_timeout_seconds: float = Field(default=120, gt=0, description="单轮 Agent 执行期限秒数")
    agent_recursion_limit: int = Field(default=30, gt=0, description="单轮图执行步骤上限")
    agent_max_tool_calls: int = Field(default=10, gt=0, description="单轮工具调用次数上限")

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        """将日志级别统一为大写。"""
        return value.upper() if isinstance(value, str) else value

    @property
    def mysql_url(self) -> URL:
        """构造异步 MySQL URL，正确处理密码中的特殊字符。"""
        return URL.create(
            drivername="mysql+aiomysql",
            username=self.mysql_user,
            password=self.mysql_password.get_secret_value(),
            host=self.mysql_host,
            port=self.mysql_port,
            database=self.mysql_database,
            query={"charset": self.mysql_charset},
        )

    @property
    def log_directory(self) -> Path:
        """返回日志绝对目录，相对路径以项目根目录为基准。"""
        return self.log_dir if self.log_dir.is_absolute() else PROJECT_ROOT / self.log_dir


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """延迟加载并缓存配置，进程内复用同一实例。

    Returns:
        已校验的应用配置。
    """
    return Settings()
