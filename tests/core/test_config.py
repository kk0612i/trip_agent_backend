"""配置加载、校验与缓存验证。"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import PROJECT_ROOT, Settings, get_settings


def test_dotenv_and_environment_priority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """验证 UTF-8 文件读取、字段转换和系统环境变量优先级。"""
    env_file = tmp_path / ".env"
    env_file.write_text(
        'APP_NAME="旅行助手"\nAPP_PORT=9000\nLOG_LEVEL=debug\nDEEPSEEK_API_KEY=test-key\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_PORT", "9001")
    settings = Settings(_env_file=env_file)
    assert settings.app_name == "旅行助手"
    assert settings.app_port == 9001
    assert settings.log_level == "DEBUG"
    assert settings.deepseek_api_key.get_secret_value() == "test-key"
    assert "test-key" not in repr(settings)


def test_cached_settings_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证缓存复用和显式清理后的配置重载。"""
    first = get_settings()
    monkeypatch.setenv("APP_PORT", "9002")
    assert get_settings() is first
    get_settings.cache_clear()
    assert get_settings().app_port == 9002


@pytest.mark.parametrize("field,value", [
    ("app_port", 0), ("mysql_port", 65536), ("deepseek_temperature", 3),
    ("deepseek_timeout_seconds", 0), ("deepseek_max_retries", -1),
    ("agent_recursion_limit", 0), ("log_level", "UNKNOWN"),
])
def test_invalid_settings(field: str, value: object) -> None:
    """验证端口、超时、步骤和日志级别的非法值被拒绝。"""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_secret_url_and_project_relative_paths() -> None:
    """验证特殊密码不损坏 URL，路径不依赖启动目录。"""
    password = "p@ss:/?#中文"
    settings = Settings(_env_file=None, mysql_password=password)
    assert settings.mysql_url.password == password
    assert settings.mysql_url.drivername == "mysql+aiomysql"
    assert password not in str(settings.mysql_url)
    assert settings.log_directory == PROJECT_ROOT / "logs"


def test_default_dotenv_from_another_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """验证切换工作目录后仍按配置中的绝对路径加载 .env。"""
    env_file = tmp_path / ".env"
    env_file.write_text("APP_PORT=9010\n", encoding="utf-8")
    monkeypatch.setitem(Settings.model_config, "env_file", env_file)
    monkeypatch.chdir(PROJECT_ROOT.parent)
    assert get_settings().app_port == 9010
