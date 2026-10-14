"""把草稿和本轮结果渲染为回复文字。"""

from app.prompt.trip_understand_prompt import minute_label
from app.schemas.trip_plan_schema import Plan, PlanDay, TravelLeg

_WEEKDAYS = "一二三四五六日"
_MODE_LABELS = {"walk": "步行", "taxi": "打车"}
CONFIRM_HINT = "满意的话请点击“确认行程”，确认后将不能再修改；也可以继续告诉我想怎么调整。"


def _leg_text(leg: TravelLeg) -> str:
    """一段交通的文字。"""
    fee = f"，约 {leg.fee_yuan:.0f} 元" if leg.fee_yuan else ""
    return f"    ↓ {_MODE_LABELS[leg.mode]}约 {leg.minutes} 分钟（直线 {leg.distance_km:.1f} 公里{fee}）"


def day_text(day: PlanDay) -> str:
    """一天的安排文字。"""
    weather = f" · {day.weather.day_weather}" if day.weather else ""
    header = f"第{day.day}天 {day.date.month}月{day.date.day}日 周{_WEEKDAYS[day.date.weekday()]}{weather}"
    lines = [header]
    if not day.items:
        lines.append("  自由活动")
        return "\n".join(lines)
    lunch = day.lunch
    lunch_printed = False
    for item in day.items:
        if lunch is not None and lunch.inside_item_id is None and not lunch_printed and lunch.start_min < item.start_min:
            lines.append(f"  {minute_label(lunch.start_min)}-{minute_label(lunch.end_min)} 午餐（自由安排）")
            lunch_printed = True
        if item.travel_from_prev is not None:
            lines.append(_leg_text(item.travel_from_prev))
        inside = "，含园内午餐" if item.lunch_inside else ""
        lines.append(
            f"  {minute_label(item.start_min)}-{minute_label(item.end_min)} {item.name}"
            f"（游玩约 {item.duration_minutes} 分钟{inside}）",
        )
    if lunch is not None and lunch.inside_item_id is None and not lunch_printed:
        lines.append(f"  {minute_label(lunch.start_min)}-{minute_label(lunch.end_min)} 午餐（自由安排）")
    return "\n".join(lines)


def plan_text(plan: Plan) -> str:
    """整份草稿的文字。"""
    parts = [day_text(day) for day in plan.days]
    if plan.warnings:
        parts.append("提示：\n" + "\n".join(f"- {w}" for w in plan.warnings))
    return "\n\n".join(parts)


def compose_reply(
    *,
    new_plan: Plan | None,
    first_plan: bool,
    change_summary: str | None,
    reply: str | None,
    notes: list[str],
) -> str:
    """组合本轮回复：变化摘要、草稿、说明和操作提示。

    Args:
        new_plan: 本轮生成的新草稿。
        first_plan: 是否首次生成草稿。
        change_summary: 草稿变化摘要。
        reply: 其他分支给出的回复。
        notes: 附加说明。

    Returns:
        回复文字。
    """
    blocks: list[str] = []
    if new_plan is not None:
        if reply:
            blocks.append(reply)
        elif first_plan:
            blocks.append(f"已为你生成{new_plan.city.name} {len(new_plan.days)} 天的景点行程草稿：")
        else:
            blocks.append("已按你的要求调整草稿。")
        if change_summary:
            blocks.append(change_summary)
        blocks.append(plan_text(new_plan))
    elif reply:
        blocks.append(reply)
    unique_notes = [note for note in dict.fromkeys(notes) if note and note not in blocks]
    if unique_notes:
        blocks.append("\n".join(unique_notes))
    if new_plan is not None:
        blocks.append(CONFIRM_HINT)
    return "\n\n".join(blocks) or "好的。"
