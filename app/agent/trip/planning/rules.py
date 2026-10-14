"""行程规划共享规则：午餐时段、节奏上限、每日松紧调整和雨天惩罚单位。

时间统一使用“相对 9:00 的分钟数”：0 表示 9:00，600 表示 19:00。
求解器、图节点和独立验算都依赖这里的常量，修改时需同步考虑三方。
"""

from app.schemas.trip_plan_schema import DAY_WINDOW_MINUTES, MAX_ITEMS_PER_DAY, Requirements

# 午餐最早开始时间 11:30
LUNCH_EARLIEST_START = 150
# 午餐最晚开始时间 12:30
LUNCH_LATEST_START = 210
# 午餐占用分钟数
LUNCH_MINUTES = 60
# 午餐时段结束 13:30
LUNCH_WINDOW_END = 270
# 游玩时长不少于该值的景点允许园内用餐
LUNCH_INSIDE_MIN_DURATION = 180
# 单段交通超过该分钟数时不生成边
MAX_LEG_MINUTES = 120

# 各节奏每天最多景点数，最高不超过 MAX_ITEMS_PER_DAY（5）
PACE_MAX_ITEMS: dict[str, int] = {"relaxed": 3, "normal": 4, "packed": MAX_ITEMS_PER_DAY}
# 各节奏每天最晚结束时间
PACE_END_LIMIT: dict[str, int] = {"relaxed": 510, "normal": 600, "packed": 600}

# “更轻松”时最晚结束提前的分钟数及下限
_LIGHTER_END_REDUCE = 90
_LIGHTER_END_FLOOR = 360


def day_limits(requirements: Requirements, day: int) -> tuple[int, int]:
    """计算某天的最晚结束时间和景点数上限。

    以节奏为基准，再按 ``requirements.day_loads`` 中该天的松紧调整：
    ``lighter`` 最晚结束提前 90 分钟（不早于 360）、景点数减 1（不少于 1）；
    ``fuller`` 最晚结束放宽到 600、景点数加 1（不超过 5）。

    Args:
        requirements: 用户已确认的规划需求。
        day: 第几天，从 1 开始。

    Returns:
        ``(end_limit_min, max_items)``。
    """
    end_limit = PACE_END_LIMIT[requirements.pace]
    max_items = PACE_MAX_ITEMS[requirements.pace]
    load = requirements.day_loads.get(day)
    if load == "lighter":
        end_limit = max(end_limit - _LIGHTER_END_REDUCE, _LIGHTER_END_FLOOR)
        max_items = max(max_items - 1, 1)
    elif load == "fuller":
        end_limit = DAY_WINDOW_MINUTES
        max_items = min(max_items + 1, MAX_ITEMS_PER_DAY)
    return end_limit, max_items


def rain_units(rain_level: int, indoor: str, duration_minutes: int) -> int:
    """计算一次访问的雨天户外惩罚单位。

    时长分档：不超过 90 分钟为 1，不超过 180 分钟为 2，更长为 3。
    室内为 0；户外为 ``rain_level × 分档``；半室内为 ``rain_level × max(1, 分档 // 2)``。

    Args:
        rain_level: 降雨等级，0 无雨至 3 大雨。
        indoor: 室内外属性，取值 ``indoor``、``outdoor`` 或 ``mixed``。
        duration_minutes: 游玩分钟数。

    Returns:
        非负整数惩罚单位。
    """
    if rain_level <= 0 or indoor == "indoor":
        return 0
    if duration_minutes <= 90:
        bucket = 1
    elif duration_minutes <= 180:
        bucket = 2
    else:
        bucket = 3
    if indoor == "outdoor":
        return rain_level * bucket
    # 半室内只按一半时长档计
    return rain_level * max(1, bucket // 2)
