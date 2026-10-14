"""根图路由。"""

from typing import Literal

from app.agent.trip.trip_state import TripState


def route_after_resolve(state: TripState) -> Literal["plan", "qa", "render"]:
    """解析后的去向：规划、资料问答或直接渲染回复。"""
    route = state.get("route")
    if route == "plan":
        return "plan"
    if route == "qa":
        return "qa"
    return "render"
