"""行程对话根图：understand → resolve →（plan → render | qa | render）。

不使用检查点和人工中断：每条用户消息是一次完整的图执行。会话状态（需求、草稿版本、
待回答问题）以 MySQL 为唯一事实来源，由 Service 读取快照传入、按图的结果条件写回；
待回答问题作为数据保存，用户下一条消息作为普通输入处理，因此不需要 interrupt/resume。
"""

from time import perf_counter

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from loguru import logger

from app.agent.trip.trip_edges import route_after_resolve
from app.agent.trip.trip_nodes import plan_node, qa_node, render_node, resolve_node, understand_node
from app.agent.trip.trip_state import TripDeps, TripState
from app.schemas.trip_schema import TripContext, TurnOutcome

ROOT_RECURSION_LIMIT = 20


def build_trip_graph() -> CompiledStateGraph:
    """构建并编译根图，不使用检查点。"""
    graph = StateGraph(TripState, context_schema=TripDeps)
    graph.add_node("understand", understand_node)
    graph.add_node("resolve", resolve_node)
    graph.add_node("plan", plan_node)
    graph.add_node("qa", qa_node)
    graph.add_node("render", render_node)
    graph.add_edge(START, "understand")
    graph.add_edge("understand", "resolve")
    graph.add_conditional_edges("resolve", route_after_resolve, ["plan", "qa", "render"])
    graph.add_edge("plan", "render")
    graph.add_edge("qa", END)
    graph.add_edge("render", END)
    return graph.compile(name="trip_dialog")


class UnavailableTurnRunner:
    """未配置模型或高德密钥时使用，每轮直接失败，由 Service 返回服务不可用。"""

    async def run(self, context: TripContext, message: str) -> TurnOutcome:
        """拒绝执行。

        Raises:
            RuntimeError: 始终抛出。
        """
        raise RuntimeError("行程对话依赖未配置")


class TripTurnRunner:
    """用根图执行一轮对话，实现 Service 的 TurnRunner 协议。"""

    def __init__(self, deps: TripDeps, *, recursion_limit: int = ROOT_RECURSION_LIMIT) -> None:
        """保存运行依赖并编译图。

        Args:
            deps: 进程内复用的运行依赖。
            recursion_limit: 根图单轮执行步骤上限；规划子图使用独立上限。
        """
        self.deps = deps
        self.recursion_limit = recursion_limit
        self.graph = build_trip_graph()

    async def run(self, context: TripContext, message: str) -> TurnOutcome:
        """执行一轮对话。

        Args:
            context: 本轮开始时的会话快照。
            message: 用户消息原文。

        Returns:
            需要持久化的本轮结果。
        """
        started = perf_counter()
        state = await self.graph.ainvoke(
            {"context": context, "message": message},
            context=self.deps,
            config={"recursion_limit": self.recursion_limit},
        )
        route = state.get("route") or "reply"
        new_plan = state.get("new_plan")
        logger.bind(
            event="trip_graph_completed", route=route, plan_changed=new_plan is not None,
            duration_ms=round((perf_counter() - started) * 1000, 2),
        ).info("对话图执行完成")
        return TurnOutcome(
            route=route,
            reply=state.get("reply") or "好的。",
            requirements=state.get("requirements") or context.requirements,
            pending=state.get("pending"),
            new_plan=new_plan,
            change_summary=state.get("change_summary"),
        )
