"""根据修改操作计算规划范围，实现“只动受影响的天”的局部修改。

局部求解失败时按级别逐步放宽：
- 0 级：受影响天中未被操作的原景点全部保留（固定在原天），只在允许补位的天加入新景点；
- 1 级：原景点改为软保留（保留有奖励，但允许被替换或删去）；
- 2 级：再把相邻的天纳入求解，相邻天的原景点同样软保留。
仍失败时由调用方询问用户是否允许全量重排；用户同意后使用 3 级：所有天参与求解，原景点软保留。
"""

from app.schemas.trip_plan_schema import (
    AddPoiOp,
    EditOp,
    MoveItemsOp,
    Plan,
    PlanScope,
    PoiRef,
    RemoveItemsOp,
    ReplaceItemsOp,
    ReplaceSlot,
    Requirements,
    SetDayLoadOp,
    SetDurationOp,
    SwapItemsOp,
)

# 删除或移出后当天空出至少这么多分钟才补入新景点。
REFILL_MIN_FREED_MINUTES = 60
MAX_WIDEN_LEVEL = 2


def full_scope(requirements: Requirements, baseline: Plan | None, *, stability: bool) -> PlanScope:
    """全量规划范围：所有天都参与求解。

    Args:
        requirements: 当前需求，必须已包含天数。
        baseline: 原草稿；启用稳定性时用于给原景点保留奖励。
        stability: 是否启用稳定性奖励。

    Returns:
        全量规划范围。
    """
    days = list(range(1, (requirements.days or 1) + 1))
    keep_bonus: dict[str, int] = {}
    if stability and baseline is not None:
        keep_bonus = {item.poi_id: day.day for day in baseline.days if day.day in days for item in day.items}
    return PlanScope(
        mode="full",
        affected_days=days,
        excluded_poi_ids=[ref.poi_id for ref in requirements.avoid],
        required=_must_visit_refs(requirements, days),
        keep_bonus=keep_bonus,
        allow_refill_days=days,
        stability=stability,
    )


def partial_scope(
    requirements: Requirements, baseline: Plan, ops: list[EditOp], *, level: int = 0,
) -> PlanScope:
    """按修改操作计算局部规划范围。

    调用前需求中的时长覆盖、每日松紧、避开列表已按操作更新；新增景点的目标天已确定或为空。

    Args:
        requirements: 已应用本轮修改的需求。
        baseline: 当前草稿。
        ops: 已校验的修改操作，不含撤销。
        level: 放宽级别，见模块说明。

    Returns:
        局部规划范围；新增景点未指定天时退化为全部天参与求解。
    """
    day_count = len(baseline.days)
    all_days = list(range(1, day_count + 1))
    item_day = {item.item_id: day.day for day in baseline.days for item in day.items}
    item_poi = {item.item_id: item.poi_id for day in baseline.days for item in day.items}
    item_minutes = {item.item_id: item.duration_minutes for day in baseline.days for item in day.items}

    affected: set[int] = set()
    excluded: set[str] = {ref.poi_id for ref in requirements.avoid}
    pinned: dict[str, int | None] = {}
    leaving: set[str] = set()
    freed: dict[int, int] = {}
    slots: list[ReplaceSlot] = []
    # 有新景点进入、调整时长或松紧的天，原景点只能软保留，否则容量可能不足。
    soft_days: set[int] = set()
    fuller_days: set[int] = set()

    def free(item_id: str) -> None:
        day = item_day[item_id]
        freed[day] = freed.get(day, 0) + item_minutes[item_id]

    for op in ops:
        if isinstance(op, RemoveItemsOp):
            for item_id in op.item_ids:
                affected.add(item_day[item_id])
                excluded.add(item_poi[item_id])
                leaving.add(item_id)
                free(item_id)
        elif isinstance(op, ReplaceItemsOp):
            slots.extend(_replace_slots(op, item_day))
            for item_id in op.item_ids:
                affected.add(item_day[item_id])
                excluded.add(item_poi[item_id])
                leaving.add(item_id)
            if op.target.poi is not None:
                first_day = item_day[op.item_ids[0]]
                pinned[op.target.poi.poi_id] = first_day
                soft_days.add(first_day)
        elif isinstance(op, AddPoiOp):
            if op.poi.day is None:
                affected.update(all_days)
                soft_days.update(all_days)
            else:
                affected.add(op.poi.day)
                soft_days.add(op.poi.day)
            pinned[op.poi.poi_id] = op.poi.day
        elif isinstance(op, MoveItemsOp):
            for item_id in op.item_ids:
                source = item_day[item_id]
                if source == op.to_day:
                    continue
                affected.update((source, op.to_day))
                pinned[item_poi[item_id]] = op.to_day
                leaving.add(item_id)
                free(item_id)
                soft_days.add(op.to_day)
        elif isinstance(op, SwapItemsOp):
            day_a, day_b = item_day[op.a], item_day[op.b]
            affected.update((day_a, day_b))
            pinned[item_poi[op.a]] = day_b
            pinned[item_poi[op.b]] = day_a
            leaving.update((op.a, op.b))
            soft_days.update((day_a, day_b))
        elif isinstance(op, SetDurationOp):
            day = item_day[op.item_id]
            affected.add(day)
            pinned[item_poi[op.item_id]] = day
            leaving.add(op.item_id)
            soft_days.add(day)
        elif isinstance(op, SetDayLoadOp):
            affected.add(op.day)
            soft_days.add(op.day)
            if op.level == "fuller":
                fuller_days.add(op.day)

    if level >= 3:
        # 用户允许全量重排：所有天参与求解，原景点软保留。
        affected.update(all_days)
        soft_days.update(all_days)
    elif level >= 2:
        for day in list(affected):
            affected.update(d for d in (day - 1, day + 1) if 1 <= d <= day_count)
        soft_days.update(affected)
    elif level >= 1:
        soft_days.update(affected)

    must_visit_ids = {ref.poi_id for ref in requirements.must_visit}
    keep_bonus: dict[str, int] = {}
    staying_count: dict[int, int] = {}
    for day in baseline.days:
        if day.day not in affected:
            continue
        for item in day.items:
            if item.item_id in leaving or item.poi_id in excluded:
                continue
            keep_bonus[item.poi_id] = day.day
            staying_count[day.day] = staying_count.get(day.day, 0) + 1
            # 必去景点无论放宽到哪一级都保留在原天。
            if day.day not in soft_days or item.poi_id in must_visit_ids:
                pinned.setdefault(item.poi_id, day.day)

    refill_days = sorted(
        day for day in affected
        if freed.get(day, 0) >= REFILL_MIN_FREED_MINUTES or day in fuller_days
    )
    incoming = {day: 0 for day in affected}
    for poi_id, day in pinned.items():
        if day is not None and day in incoming and keep_bonus.get(poi_id) != day:
            incoming[day] += 1
    for slot in slots:
        incoming[slot.day] = incoming.get(slot.day, 0) + slot.count
    day_max_items = {
        day: max(1, staying_count.get(day, 0) + incoming.get(day, 0))
        for day in affected if day not in refill_days
    }

    required = [PoiRef(poi_id=poi_id, name=_poi_name(baseline, poi_id, ops), day=day) for poi_id, day in pinned.items()]
    return PlanScope(
        mode="partial",
        affected_days=sorted(affected),
        fixed_days=[day for day in all_days if day not in affected],
        excluded_poi_ids=sorted(excluded),
        required=required,
        replace_slots=slots,
        keep_bonus=keep_bonus,
        allow_refill_days=refill_days,
        day_max_items=day_max_items,
        freed_minutes={day: minutes for day, minutes in freed.items() if day in affected},
        stability=True,
    )


def _replace_slots(op: ReplaceItemsOp, item_day: dict[str, int]) -> list[ReplaceSlot]:
    """把一次替换拆成按天的数量约束；指定景点时由必选约束处理，其余被替换项不再补数量。"""
    if op.target.poi is not None:
        return []
    per_day: dict[int, int] = {}
    for item_id in op.item_ids:
        day = item_day[item_id]
        per_day[day] = per_day.get(day, 0) + 1
    if op.target.count is not None:
        # 用户指定总数时，按出现顺序把差额加到首日或从各天依次扣减。
        delta = op.target.count - len(op.item_ids)
        days = list(per_day)
        if delta > 0:
            per_day[days[0]] += delta
        for day in days:
            if delta >= 0:
                break
            cut = min(per_day[day], -delta)
            per_day[day] -= cut
            delta += cut
    return [ReplaceSlot(day=day, tags=list(op.target.tags), count=count) for day, count in per_day.items() if count > 0]


def _must_visit_refs(requirements: Requirements, days: list[int]) -> list[PoiRef]:
    """返回必去景点约束，指定天超出行程天数时不限定天。"""
    return [
        ref if ref.day is None or ref.day in days else ref.model_copy(update={"day": None})
        for ref in requirements.must_visit
    ]


def _poi_name(baseline: Plan, poi_id: str, ops: list[EditOp]) -> str:
    """查找景点名称，用于诊断和提示。"""
    for day in baseline.days:
        for item in day.items:
            if item.poi_id == poi_id:
                return item.name
    for op in ops:
        if isinstance(op, AddPoiOp) and op.poi.poi_id == poi_id:
            return op.poi.name
        if isinstance(op, ReplaceItemsOp) and op.target.poi is not None and op.target.poi.poi_id == poi_id:
            return op.target.poi.name
    return poi_id
