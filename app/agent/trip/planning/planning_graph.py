"""规划子图：scope → gather → enrich → solve →（expand 回到 gather | widen 回到 enrich | validate | diagnose）。

放宽范围后受影响的天和保留景点都会变化，需要回到 enrich 重新确定候选并补全新增景点的属性。

候选搜索和天气在 gather 节点内用 asyncio.gather 并行请求，而不是拆成并行分支：
图中存在回环，并行分支的汇合等待会在第二轮卡住。
"""

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agent.trip.planning.planning_edges import route_after_solve
from app.agent.trip.planning.planning_nodes import (
    diagnose_node,
    enrich_node,
    expand_node,
    gather_node,
    scope_node,
    solve_node,
    validate_node,
    widen_node,
)
from app.agent.trip.planning.planning_state import PlanningState
from app.agent.trip.trip_state import TripDeps


def build_planning_graph() -> CompiledStateGraph:
    """构建并编译规划子图，不使用检查点。"""
    graph = StateGraph(PlanningState, context_schema=TripDeps)
    graph.add_node("scope", scope_node)
    graph.add_node("gather", gather_node)
    graph.add_node("enrich", enrich_node)
    graph.add_node("solve", solve_node)
    graph.add_node("expand", expand_node)
    graph.add_node("widen", widen_node)
    graph.add_node("validate", validate_node)
    graph.add_node("diagnose", diagnose_node)
    graph.add_edge(START, "scope")
    graph.add_edge("scope", "gather")
    graph.add_edge("gather", "enrich")
    graph.add_edge("enrich", "solve")
    graph.add_conditional_edges("solve", route_after_solve, ["expand", "widen", "diagnose", "validate"])
    graph.add_edge("expand", "gather")
    graph.add_edge("widen", "enrich")
    graph.add_edge("validate", END)
    graph.add_edge("diagnose", END)
    return graph.compile(name="trip_planning")
