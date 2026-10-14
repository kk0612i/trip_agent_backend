"""把求解结果组装成草稿，并生成与原草稿相比的变化摘要。"""

from datetime import date, timedelta

from app.agent.trip.planning.rules import LUNCH_EARLIEST_START, LUNCH_MINUTES
from app.agent.trip.planning.scope import REFILL_MIN_FREED_MINUTES
from app.schemas.trip_plan_schema import (
    Candidate,
    DayWeather,
    LunchBreak,
    Plan,
    PlanDay,
    PlanItem,
    PlanScope,
    Requirements,
    SolveOutput,
    TravelLeg,
)

# 园内午餐在展示上统一安排在 12:00-13:00，处于景点停留时段和午餐窗口之内。
INSIDE_LUNCH_START = 180


def day_date(start_date: date, day: int) -> date:
    """返回第几天对应的日期。"""
    return start_date + timedelta(days=day - 1)


def build_plan(
    *,
    requirements: Requirements,
    baseline: Plan | None,
    scope: PlanScope,
    output: SolveOutput,
    candidates: dict[str, Candidate],
    travel: dict[str, TravelLeg],
    weather: dict[date, DayWeather],
) -> Plan:
    """合并固定天和求解结果得到新草稿。

    Args:
        requirements: 本轮需求，需包含城市、出发日期和天数。
        baseline: 原草稿，局部修改时提供固定天。
        scope: 本次规划范围。
        output: 求解结果，状态必须为 ok。
        candidates: 候选景点，按 POI 标识索引。
        travel: 交通矩阵。
        weather: 按日期的天气预报。

    Returns:
        新草稿。
    """
    assert requirements.city is not None and requirements.start_date is not None and requirements.days
    solved = {day.day: day for day in output.days}
    baseline_days = {day.day: day for day in baseline.days} if baseline else {}
    days: list[PlanDay] = []
    for number in range(1, requirements.days + 1):
        current = day_date(requirements.start_date, number)
        if number in scope.fixed_days and number in baseline_days:
            days.append(baseline_days[number].model_copy(update={"weather": weather.get(current)}))
            continue
        items: list[PlanItem] = []
        lunch: LunchBreak | None = None
        visits = solved[number].visits if number in solved else []
        previous: str | None = None
        for index, visit in enumerate(visits, start=1):
            candidate = candidates[visit.poi_id]
            profile = candidate.profile
            item_id = f"D{number}-{index}"
            items.append(PlanItem(
                item_id=item_id,
                poi_id=visit.poi_id,
                name=candidate.poi.name,
                lng=candidate.poi.lng,
                lat=candidate.poi.lat,
                subtype=profile.subtype,
                main_category=profile.main_category,
                indoor=profile.indoor,
                start_min=visit.start_min,
                end_min=visit.end_min,
                duration_minutes=candidate.duration_minutes,
                lunch_inside=visit.lunch_inside,
                travel_from_prev=travel.get(f"{previous}|{visit.poi_id}") if previous else None,
                rating=candidate.poi.rating,
                address=candidate.poi.address,
                photos=candidate.poi.photos,
            ))
            if visit.lunch_inside:
                lunch = LunchBreak(
                    start_min=max(INSIDE_LUNCH_START, LUNCH_EARLIEST_START),
                    end_min=INSIDE_LUNCH_START + LUNCH_MINUTES,
                    inside_item_id=item_id,
                )
            elif visit.lunch_after:
                lunch = LunchBreak(start_min=visit.end_min, end_min=visit.end_min + LUNCH_MINUTES)
            previous = visit.poi_id
        days.append(PlanDay(day=number, date=current, weather=weather.get(current), items=items, lunch=lunch))

    return Plan(
        city=requirements.city,
        start_date=requirements.start_date,
        days=days,
        objective=output.objective,
        warnings=plan_warnings(days, weather_known=bool(weather)),
    )


def plan_warnings(days: list[PlanDay], *, weather_known: bool) -> list[str]:
    """生成草稿提示：降雨日仍有户外景点、超出预报范围、打车费用未知等。"""
    warnings: list[str] = []
    for day in days:
        if day.weather is None:
            continue
        if day.weather.rain_level > 0:
            outdoor = [item.name for item in day.items if item.indoor != "indoor"]
            if outdoor:
                warnings.append(
                    f"第{day.day}天预报{day.weather.day_weather}，{('、'.join(outdoor))}含户外部分，注意带伞。",
                )
    no_forecast = [str(day.day) for day in days if day.weather is None]
    if no_forecast:
        reason = "超出天气预报范围" if weather_known else "暂未获取到天气预报"
        warnings.append(f"第{'、'.join(no_forecast)}天{reason}，出发前请留意天气。")
    if any(item.travel_from_prev and item.travel_from_prev.mode == "taxi" for day in days for item in day.items):
        warnings.append("打车时间按直线距离估算，费用暂无可靠数据未列出。")
    return warnings


def change_summary(baseline: Plan | None, plan: Plan, scope: PlanScope | None) -> str | None:
    """对比原草稿和新草稿，生成按天的变化摘要，并说明删除后的补位情况。

    Args:
        baseline: 原草稿，首次规划为空。
        plan: 新草稿。
        scope: 本次规划范围。

    Returns:
        变化摘要；首次规划或没有变化时为空。
    """
    if baseline is None:
        return None
    old = {item.poi_id: (day.day, item) for day in baseline.days for item in day.items}
    new = {item.poi_id: (day.day, item) for day in plan.days for item in day.items}
    lines: list[str] = []
    if len(baseline.days) != len(plan.days):
        lines.append(f"行程从 {len(baseline.days)} 天调整为 {len(plan.days)} 天。")
    if baseline.start_date != plan.start_date:
        lines.append(f"出发日期改为 {plan.start_date.isoformat()}。")
    for day in plan.days:
        added = [item.name for item in day.items if item.poi_id not in old]
        moved_in = [
            f"{item.name}（原第{old[item.poi_id][0]}天）"
            for item in day.items if item.poi_id in old and old[item.poi_id][0] != day.day
        ]
        removed = [
            item.name for _, (number, item) in old.items()
            if number == day.day and (item.poi_id not in new)
        ]
        retimed = [
            item.name for item in day.items
            if item.poi_id in old and old[item.poi_id][0] == day.day
            and item.duration_minutes != old[item.poi_id][1].duration_minutes
        ]
        parts = []
        if removed:
            parts.append(f"移除 {'、'.join(removed)}")
        if added:
            parts.append(f"新增 {'、'.join(added)}")
        if moved_in:
            parts.append(f"移入 {'、'.join(moved_in)}")
        if retimed:
            parts.append(f"调整时长 {'、'.join(retimed)}")
        if parts:
            lines.append(f"第{day.day}天：{'；'.join(parts)}。")
        if scope is not None and scope.mode == "partial":
            freed = scope.freed_minutes.get(day.day, 0)
            if freed and day.day not in scope.allow_refill_days:
                lines.append(
                    f"第{day.day}天空出约 {freed} 分钟，不足 {REFILL_MIN_FREED_MINUTES} 分钟，未补入新景点。",
                )
            elif freed and day.day in scope.allow_refill_days and not added:
                lines.append(f"第{day.day}天空出约 {freed} 分钟，但没有合适的景点可补，当天会更宽松。")
            elif freed and added:
                lines.append(f"第{day.day}天空出约 {freed} 分钟，已补入 {'、'.join(added)}。")
    lost_days = [day for day in baseline.days if day.day > len(plan.days) and day.items]
    for day in lost_days:
        names = [item.name for item in day.items if item.poi_id not in new]
        if names:
            lines.append(f"原第{day.day}天的 {'、'.join(names)} 已不在行程中。")
    return "\n".join(lines) if lines else "行程内容没有变化，仅重新核算了时间。"
