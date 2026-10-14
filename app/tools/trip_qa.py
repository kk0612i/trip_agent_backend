"""资料问答 Agent 的工具。

工具按轮次用闭包绑定当前城市和草稿，模型无法越权查询其他会话的数据。
"""

from langchain_core.tools import BaseTool, tool
from loguru import logger

from app.prompt.trip_understand_prompt import draft_text
from app.schemas.trip_plan_schema import CityInfo, Plan

# 单个工具返回给模型的最大字符数，避免网页摘要撑爆上下文。
_MAX_TOOL_TEXT = 3000


def _clip(text: str) -> str:
    """截断过长的工具结果。"""
    return text if len(text) <= _MAX_TOOL_TEXT else text[:_MAX_TOOL_TEXT] + "……（已截断）"


def build_trip_qa_tools(*, amap: object, bocha: object | None, city: CityInfo | None, draft: Plan | None) -> list[BaseTool]:
    """构造本轮问答可用的工具。

    Args:
        amap: 高德客户端。
        bocha: 博查客户端，未配置时不提供联网搜索。
        city: 当前城市，未确定时地点查询会提示先确定城市。
        draft: 当前草稿或已确认行程。

    Returns:
        工具列表。
    """

    @tool
    async def lookup_place(name: str) -> str:
        """在当前城市按名称查询景点或地点的地址、类型和高德评分。

        Args:
            name: 景点或地点名称。
        """
        if city is None:
            return "还没有确定目的地城市，无法查询地点。"
        try:
            pois = await amap.find_pois_by_name(city.adcode, name, limit=3)
        except Exception as exc:  # 工具失败返回文字，由模型决定如何回答
            logger.bind(event="trip_qa_tool_failed", tool="lookup_place", error_type=type(exc).__name__).warning(
                "问答地点查询失败",
            )
            return "地点查询暂时失败。"
        if not pois:
            return f"在{city.name}没有找到“{name}”。"
        return "\n".join(
            f"- {poi.name}｜类型：{poi.amap_type or '未知'}｜地址：{poi.address or '未知'}"
            f"｜评分：{poi.rating if poi.rating is not None else '暂无'}"
            for poi in pois
        )

    @tool
    def get_current_plan() -> str:
        """读取当前行程草稿的逐日安排，用于回答关于行程本身的问题。"""
        if draft is None:
            return "还没有生成行程草稿。"
        return draft_text(draft)

    tools: list[BaseTool] = [lookup_place, get_current_plan]
    if bocha is not None:

        @tool
        async def web_search(query: str) -> str:
            """联网搜索景点的门票、开放时间、预约方式、游玩攻略等资料。

            Args:
                query: 搜索词，应包含城市名和景点名，例如“杭州 灵隐寺 门票 预约”。
            """
            try:
                results = await bocha.web_search(query, count=5)
            except Exception as exc:  # 工具失败返回文字，由模型决定如何回答
                logger.bind(event="trip_qa_tool_failed", tool="web_search", error_type=type(exc).__name__).warning(
                    "问答联网搜索失败",
                )
                return "联网搜索暂时失败。"
            if not results:
                return "没有搜索到相关资料。"
            return _clip("\n".join(
                f"[{i}] {r.title}（{r.site_name or r.url}{'，' + r.published_at if r.published_at else ''}）\n{r.snippet}"
                for i, r in enumerate(results, start=1)
            ))

        tools.append(web_search)
    return tools
