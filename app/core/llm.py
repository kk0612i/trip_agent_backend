"""大语言模型对象创建。"""

from langchain_core.language_models import BaseChatModel
from langchain_deepseek import ChatDeepSeek

from app.core.config import Settings


def create_chat_model(settings: Settings, *, temperature: float | None = None) -> BaseChatModel:
    """按配置创建可在进程内复用的 DeepSeek 聊天模型。

    结构化输出依赖强制工具调用，DeepSeek 思考模式不支持该调用方式，因此默认显式关闭思考模式。

    Args:
        settings: 应用配置。
        temperature: 覆盖配置中的采样温度；结构化抽取通常使用较低温度。

    Returns:
        已设置超时和重试次数的聊天模型。
    """
    thinking = "enabled" if settings.deepseek_thinking_enabled else "disabled"
    return ChatDeepSeek(
        model=settings.deepseek_model,
        api_key=settings.deepseek_api_key,
        api_base=str(settings.deepseek_base_url).rstrip("/"),
        temperature=settings.deepseek_temperature if temperature is None else temperature,
        max_tokens=settings.deepseek_max_tokens,
        timeout=settings.deepseek_timeout_seconds,
        max_retries=settings.deepseek_max_retries,
        extra_body={"thinking": {"type": thinking}},
    )
