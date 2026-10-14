"""规划子图路由。"""

from typing import Literal

from app.agent.trip.planning.planning_nodes import MAX_SEARCH_ROUNDS
from app.agent.trip.planning.planning_state import PlanningState
from app.agent.trip.planning.scope import MAX_WIDEN_LEVEL
from app.agent.trip.planning.tags import item_tags, main_category_of, tags_match

# 允许补位的天少于这么多景点时视为候选不足，尝试扩大搜索。
UNDERFILLED_ITEMS = 2
# 替换约束的同类新候选少于“数量下限 + 该值”时，才认为无解可能源于同类候选不足。
SLOT_SPARE_CANDIDATES = 2


def _slots_short(state: PlanningState) -> bool:
    """判断是否有替换约束的同类新候选过少，多搜一轮可能有帮助。"""
    baseline = state.get("baseline")
    profiles = state.get("profiles") or {}
    old = {item.poi_id for day in baseline.days for item in day.items} if baseline else set()
    new_tags = [
        item_tags(profile.subtype, main_category_of(profile.subtype), profile.indoor)
        for pid in state.get("candidate_ids") or []
        if pid not in old and (profile := profiles.get(pid)) is not None
    ]
    for slot in state["scope"].replace_slots:
        if slot.poi_id is not None:
            continue
        if sum(tags_match(slot.tags, tags) for tags in new_tags) < slot.count + SLOT_SPARE_CANDIDATES:
            return True
    return False


def _underfilled(state: PlanningState) -> bool:
    """判断允许补位的天是否因候选不足而过空。"""
    output = state["solve_output"]
    scope = state["scope"]
    counts = {day.day: len(day.visits) for day in output.days}
    return any(counts.get(day, 0) < UNDERFILLED_ITEMS for day in scope.allow_refill_days)


def route_after_solve(state: PlanningState) -> Literal["expand", "widen", "diagnose", "validate"]:
    """求解后的去向：成功但过空时扩大搜索；失败时依次尝试扩大搜索、放宽范围，最后诊断。"""
    output = state["solve_output"]
    scope = state["scope"]
    can_expand = state.get("search_round", 0) < MAX_SEARCH_ROUNDS
    if output.status == "ok":
        return "expand" if can_expand and _underfilled(state) else "validate"
    # 替换约束可能因同类候选不足而无解，同类候选充足时直接放宽范围；全量无解多因约束冲突，只多搜一轮。
    if can_expand and scope.replace_slots and state.get("widen_level", 0) == 0 and _slots_short(state):
        return "expand"
    if scope.mode == "full" and state.get("search_round", 0) < 1:
        return "expand"
    if scope.mode == "partial" and not state.get("force_full") and state.get("widen_level", 0) < MAX_WIDEN_LEVEL:
        return "widen"
    return "diagnose"
