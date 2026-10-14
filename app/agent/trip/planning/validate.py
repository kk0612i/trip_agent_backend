"""行程独立验算：不依赖求解器，从坐标重新估算交通，逐项核对行程是否合规。

验算与求解器共享 ``rules`` 中的常量和 ``travel`` 中的交通规则，
但不调用求解器，用于兜底发现求解或组装环节的错误。
"""

import re

from app.agent.trip.planning.rules import (
    LUNCH_EARLIEST_START,
    LUNCH_INSIDE_MIN_DURATION,
    LUNCH_LATEST_START,
    LUNCH_MINUTES,
    LUNCH_WINDOW_END,
    day_limits,
)
from app.agent.trip.planning.travel import estimate_leg
from app.schemas.trip_plan_schema import (
    SUBTYPE_MAIN_CATEGORY,
    Plan,
    PlanDay,
    PlanScope,
    Requirements,
)

_ITEM_ID_PATTERN = re.compile(r"^D(\d+)-(\d+)$")


def _check_identity(plan: Plan, errors: list[str]) -> None:
    """检查天序号、跨天重复景点和草稿引用编号。"""
    seen_days: set[int] = set()
    poi_day: dict[str, int] = {}
    for day in plan.days:
        if day.day in seen_days:
            errors.append(f"第{day.day}天在行程中重复出现")
        seen_days.add(day.day)
        for pos, item in enumerate(day.items, start=1):
            if item.poi_id in poi_day:
                errors.append(
                    f"景点 {item.name}（{item.poi_id}）重复安排：第{poi_day[item.poi_id]}天和第{day.day}天"
                )
            else:
                poi_day[item.poi_id] = day.day
            expected = f"D{day.day}-{pos}"
            if item.item_id != expected or not _ITEM_ID_PATTERN.match(item.item_id):
                errors.append(f"第{day.day}天第{pos}个景点编号应为 {expected}，实际为 {item.item_id}")


def _check_items(day: PlanDay, requirements: Requirements, errors: list[str]) -> None:
    """检查单天景点的时间范围、时长和数量上限。"""
    end_limit, max_items = day_limits(requirements, day.day)
    if len(day.items) > max_items:
        errors.append(f"第{day.day}天安排了 {len(day.items)} 个景点，超过上限 {max_items}")
    for item in day.items:
        tag = f"第{day.day}天 {item.item_id} {item.name}"
        if not (0 <= item.start_min < item.end_min <= end_limit):
            errors.append(
                f"{tag} 时间 [{item.start_min}, {item.end_min}] 不在 [0, {end_limit}] 内或结束不晚于开始"
            )
        expected = item.duration_minutes + (LUNCH_MINUTES if item.lunch_inside else 0)
        if item.end_min - item.start_min != expected:
            errors.append(
                f"{tag} 占用 {item.end_min - item.start_min} 分钟，应为 {expected} 分钟（游玩时长加园内午餐）"
            )
        if SUBTYPE_MAIN_CATEGORY.get(item.subtype) != item.main_category:
            errors.append(f"{tag} 主体验类 {item.main_category} 与细分体验 {item.subtype} 不匹配")


def _lunch_between(day: PlanDay, prev_end: int, next_start: int) -> bool:
    """判断景点外午餐是否位于两个相邻景点之间。"""
    lunch = day.lunch
    if lunch is None or lunch.inside_item_id is not None:
        return False
    return lunch.start_min >= prev_end and lunch.end_min <= next_start


def _check_travel(day: PlanDay, requirements: Requirements, errors: list[str]) -> None:
    """按坐标重算每段交通，核对方式、耗时和衔接时间。"""
    transport = requirements.transport
    for pos, item in enumerate(day.items):
        tag = f"第{day.day}天 {item.item_id} {item.name}"
        if pos == 0:
            if item.travel_from_prev is not None:
                errors.append(f"{tag} 是当天首个景点，不应有来自上一景点的交通")
            continue
        prev = day.items[pos - 1]
        recorded = item.travel_from_prev
        if recorded is None:
            errors.append(f"{tag} 缺少从 {prev.name} 过来的交通")
        elif transport.walk_only and recorded.mode == "taxi":
            errors.append(f"{tag} 只允许步行，但交通方式为打车")
        leg = estimate_leg(prev.lng, prev.lat, item.lng, item.lat, transport)
        if leg is None:
            errors.append(f"{tag} 从 {prev.name} 过来不可达（坐标无效、超出步行上限或耗时过长）")
            continue
        if recorded is not None and (recorded.mode != leg.mode or recorded.minutes != leg.minutes):
            errors.append(
                f"{tag} 交通记录为 {recorded.mode} {recorded.minutes} 分钟，重算应为 {leg.mode} {leg.minutes} 分钟"
            )
        gap = leg.minutes + (LUNCH_MINUTES if _lunch_between(day, prev.end_min, item.start_min) else 0)
        if item.start_min < prev.end_min + gap:
            errors.append(
                f"{tag} 开始于 {item.start_min}，早于上一景点结束 {prev.end_min} 加交通"
                f"{'和午餐' if gap > leg.minutes else ''} {gap} 分钟"
            )


def _check_lunch(day: PlanDay, errors: list[str]) -> None:
    """检查午餐：跨午餐时段必须安排，园内或景点外两种形式各自合规。"""
    items = day.items
    need = any(it.start_min < LUNCH_EARLIEST_START for it in items) and any(
        it.end_min > LUNCH_WINDOW_END for it in items
    )
    inside_items = [it for it in items if it.lunch_inside]
    lunch = day.lunch
    if lunch is None:
        if need:
            errors.append(f"第{day.day}天上午开始且午后结束，但没有安排午餐")
        for it in inside_items:
            errors.append(f"第{day.day}天 {it.item_id} 标记园内午餐，但当天没有午餐安排")
        return

    if lunch.inside_item_id is not None:
        target = next((it for it in items if it.item_id == lunch.inside_item_id), None)
        if target is None:
            errors.append(f"第{day.day}天园内午餐引用的 {lunch.inside_item_id} 不在当天行程中")
        else:
            if not target.lunch_inside:
                errors.append(f"第{day.day}天园内午餐景点 {target.item_id} 未标记 lunch_inside")
            if target.duration_minutes < LUNCH_INSIDE_MIN_DURATION:
                errors.append(
                    f"第{day.day}天 {target.item_id} 游玩 {target.duration_minutes} 分钟，"
                    f"不足 {LUNCH_INSIDE_MIN_DURATION} 分钟，不允许园内午餐"
                )
            if not (target.start_min <= LUNCH_EARLIEST_START and target.end_min >= LUNCH_WINDOW_END):
                errors.append(
                    f"第{day.day}天园内午餐景点 {target.item_id} 时间 [{target.start_min}, {target.end_min}]"
                    f" 未覆盖午餐时段 [{LUNCH_EARLIEST_START}, {LUNCH_WINDOW_END}]"
                )
        for it in inside_items:
            if it.item_id != lunch.inside_item_id:
                errors.append(f"第{day.day}天 {it.item_id} 标记园内午餐，但午餐安排在别处")
        return

    if not (LUNCH_EARLIEST_START <= lunch.start_min <= LUNCH_LATEST_START):
        errors.append(
            f"第{day.day}天午餐开始于 {lunch.start_min}，不在 [{LUNCH_EARLIEST_START}, {LUNCH_LATEST_START}] 内"
        )
    if lunch.end_min != lunch.start_min + LUNCH_MINUTES:
        errors.append(f"第{day.day}天午餐 [{lunch.start_min}, {lunch.end_min}] 时长不是 {LUNCH_MINUTES} 分钟")
    for it in items:
        if lunch.start_min < it.end_min and it.start_min < lunch.end_min:
            errors.append(
                f"第{day.day}天午餐 [{lunch.start_min}, {lunch.end_min}] 与 {it.item_id} {it.name}"
                f" [{it.start_min}, {it.end_min}] 重叠"
            )
    for it in inside_items:
        errors.append(f"第{day.day}天 {it.item_id} 标记园内午餐，但午餐安排在景点外")


def _poi_days(plan: Plan) -> dict[str, int]:
    """返回行程中每个 POI 所在的天（重复时取首次出现）。"""
    result: dict[str, int] = {}
    for day in plan.days:
        for item in day.items:
            result.setdefault(item.poi_id, day.day)
    return result


def _check_requirements(plan: Plan, requirements: Requirements, errors: list[str]) -> None:
    """检查必去景点全部入选（指定天的在那天），不去的景点没有出现。"""
    located = _poi_days(plan)
    for ref in requirements.must_visit:
        day = located.get(ref.poi_id)
        if day is None:
            errors.append(f"必去景点 {ref.name}（{ref.poi_id}）不在行程中")
        elif ref.day is not None and day != ref.day:
            errors.append(f"必去景点 {ref.name} 指定第{ref.day}天，实际在第{day}天")
    for ref in requirements.avoid:
        if ref.poi_id in located:
            errors.append(f"不去的景点 {ref.name}（{ref.poi_id}）出现在第{located[ref.poi_id]}天")


def _check_scope(plan: Plan, scope: PlanScope, baseline: Plan | None, errors: list[str]) -> None:
    """检查本次规划范围：排除项、必选项和固定天是否与基准一致。"""
    located = _poi_days(plan)
    for poi_id in scope.excluded_poi_ids:
        if poi_id in located:
            errors.append(f"已排除的景点 {poi_id} 出现在第{located[poi_id]}天")
    for ref in scope.required:
        day = located.get(ref.poi_id)
        if day is None:
            errors.append(f"本次必须入选的景点 {ref.name}（{ref.poi_id}）不在行程中")
        elif ref.day is not None and day != ref.day:
            errors.append(f"本次必须入选的景点 {ref.name} 指定第{ref.day}天，实际在第{day}天")
    if not scope.fixed_days:
        return
    if baseline is None:
        errors.append("存在固定天但缺少基准行程，无法核对固定天是否保持不变")
        return
    for fixed in scope.fixed_days:
        current = next((d for d in plan.days if d.day == fixed), None)
        base = next((d for d in baseline.days if d.day == fixed), None)
        if current is None or base is None:
            errors.append(f"固定的第{fixed}天在当前行程或基准行程中缺失")
            continue
        cur_seq = [(it.poi_id, it.start_min, it.end_min) for it in current.items]
        base_seq = [(it.poi_id, it.start_min, it.end_min) for it in base.items]
        if cur_seq != base_seq:
            errors.append(f"固定的第{fixed}天安排与原行程不一致")


def validate_plan(
    plan: Plan,
    requirements: Requirements,
    *,
    baseline: Plan | None = None,
    scope: PlanScope | None = None,
) -> list[str]:
    """独立验算行程是否满足全部硬性规则。

    依次检查：编号与重复、时间与数量上限、交通重算与衔接、午餐、
    必去与不去、以及（提供范围时）排除项、必选项和固定天。

    Args:
        plan: 待验算的行程。
        requirements: 用户已确认的需求，提供节奏、松紧、交通偏好、必去与不去。
        baseline: 修改前的行程，用于核对固定天。
        scope: 本次规划范围；为空时不做范围相关检查。

    Returns:
        中文违规描述列表，为空表示通过。
    """
    errors: list[str] = []
    _check_identity(plan, errors)
    for day in plan.days:
        _check_items(day, requirements, errors)
        _check_travel(day, requirements, errors)
        _check_lunch(day, errors)
    _check_requirements(plan, requirements, errors)
    if scope is not None:
        _check_scope(plan, scope, baseline, errors)
    return errors
