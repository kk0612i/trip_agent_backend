"""对话理解提示词：把一条用户消息解析为结构化意图。"""

from datetime import date, timedelta

from app.agent.trip.planning.tags import INDOOR_LABELS, TAG_LABELS
from app.schemas.trip_plan_schema import MAX_TRIP_DAYS, SUBTYPE_MAIN_CATEGORY, Plan, Requirements
from app.schemas.trip_schema import PendingQuestion, TurnRecord

_WEEKDAYS = "一二三四五六日"


def minute_label(minutes: int) -> str:
    """把相对 9:00 的分钟数转为 HH:MM。"""
    total = 9 * 60 + minutes
    return f"{total // 60:02d}:{total % 60:02d}"


def _date_table(today: date) -> str:
    """生成今天起 21 天的日期表，供模型换算“下周五”“后天”等说法。"""
    rows = []
    for offset in range(21):
        current = today + timedelta(days=offset)
        prefix = {0: "今天", 1: "明天", 2: "后天"}.get(offset, "")
        rows.append(f"{current.isoformat()} 周{_WEEKDAYS[current.weekday()]} {prefix}".rstrip())
    return "\n".join(rows)


def _requirements_text(requirements: Requirements) -> str:
    """当前需求的简要描述。"""
    prefs = "、".join(
        f"{TAG_LABELS.get(tag, tag)}({tag}){'++' if weight >= 2 else '+'}"
        for tag, weight in requirements.preferences.items()
    ) or "无"
    must = "、".join(ref.name for ref in requirements.must_visit) or "无"
    avoid = "、".join(ref.name for ref in requirements.avoid) or "无"
    transport = "只步行" if requirements.transport.walk_only else "步行+打车"
    return (
        f"城市：{requirements.city.name if requirements.city else '未定'}；"
        f"出发日期：{requirements.start_date.isoformat() if requirements.start_date else '未定'}；"
        f"天数：{requirements.days or '未定'}；节奏：{requirements.pace}；偏好：{prefs}；"
        f"必去：{must}；不去：{avoid}；交通：{transport}，单段步行上限 {requirements.transport.max_walk_minutes} 分钟"
    )


def draft_text(plan: Plan) -> str:
    """草稿的逐项列表，带引用编号、时间和标签，供模型把自然语言映射到具体景点。"""
    lines = []
    for day in plan.days:
        weather = f"，{day.weather.day_weather}" if day.weather else ""
        lines.append(f"第{day.day}天（{day.date.isoformat()}{weather}）：")
        for item in day.items:
            lines.append(
                f"  {item.item_id} {minute_label(item.start_min)}-{minute_label(item.end_min)} {item.name} "
                f"[{item.subtype}/{item.main_category}/{item.indoor}]",
            )
        if not day.items:
            lines.append("  （空）")
    return "\n".join(lines)


def get_trip_understand_system_prompt(
    *,
    today: date,
    status: str,
    requirements: Requirements,
    draft: Plan | None,
    pending: PendingQuestion | None,
    recent_turns: list[TurnRecord],
) -> str:
    """返回对话理解的系统提示词。

    Args:
        today: 当天日期。
        status: 行程状态。
        requirements: 当前需求。
        draft: 当前草稿，没有时为空。
        pending: 待回答的问题。
        recent_turns: 最近几轮对话。

    Returns:
        系统提示词。
    """
    subtypes = "、".join(f"{name}={TAG_LABELS[name]}" for name in SUBTYPE_MAIN_CATEGORY)
    mains = "、".join(
        f"{name}={TAG_LABELS[name]}"
        for name in ("history", "culture_art", "religion", "nature", "leisure", "entertainment", "urban")
    )
    indoor = "、".join(f"{key}={label}" for key, label in INDOOR_LABELS.items() if key != "mixed")
    pending_text = "无"
    if pending is not None:
        options = "；".join(f"{i}. {o.label}" for i, o in enumerate(pending.options, start=1))
        pending_text = f"{pending.text}" + (f"（选项：{options}）" if options else "（自由回答）")
    history = "\n".join(f"{t.role}: {t.content[:200]}" for t in recent_turns) or "无"
    draft_block = draft_text(draft) if draft is not None else "尚未生成"

    return f"""你是单城市旅游景点规划助手的“理解”模块。你的唯一任务是把用户最新一条消息解析为结构化结果，不要回答问题、不要规划行程。
本助手只推荐景点，不推荐住宿和餐饮；每天游玩时间为 09:00-19:00；行程只支持 1～{MAX_TRIP_DAYS} 天，每天最多 5 个景点。

## 当前会话
- 行程状态：{status}（collecting=收集需求，可换城市；drafting=已有草稿，城市已锁定，可修改；confirmed=已确认，只读）
- 当前需求：{_requirements_text(requirements)}
- 待回答的问题：{pending_text}
- 最近对话：
{history}

## 当前草稿（引用编号形如 D1-2 表示第 1 天第 2 个景点）
{draft_block}

## 日期表（用于换算相对日期，start_date 必须输出 YYYY-MM-DD）
{_date_table(today)}

## 标签词表（偏好、选择器、替换目标只能使用这些英文值）
- 主体验类：{mains}
- 细分体验类：{subtypes}
- 选择器额外可用：{indoor}

## kind 判定
- plan_request：提供或修改出行需求（城市、日期、天数、节奏、偏好、必去、不去、交通），或要求开始规划/重新规划。
- modify：针对当前草稿中具体景点或某天的修改（删除、替换、新增、移动、交换、改时长、某天轻松/充实、撤销）。
- answer_pending：用户在回答“待回答的问题”中的选项，填 pending_option（序号从 1 开始）。
- switch_city：要求改去另一个城市。
- question：咨询景点资料、门票、开放时间、交通、天气等需要查询的问题，原样提炼到 question。
- chat：寒暄或与规划无关但无害的闲聊，在 chat_reply 里给一句简短中文回复。
- out_of_scope：要求推荐酒店住宿、餐厅美食等本助手不提供的内容。
一条消息同时包含需求变化和修改时，kind 取 modify，并同时填写需求字段和 ops。

## 字段规则
- 只填写用户本条消息明确表达的内容；未提到的字段保持为空或空列表，不要沿用当前需求重复填写。
- preferences：喜欢的类型，weight=2 表示“特别喜欢/一定要”；用户说“只想/换成只看”时 clear_preferences=true。
- must_visit / avoid：景点名称原文；“第二天去灵隐寺”在首次规划时写入 must_visit 并给 day。
- 交通：“只想走路/不打车”→ walk_only=true；“可以打车”→ walk_only=false；“每段最多走 20 分钟”→ max_walk_minutes=20。
- 节奏：轻松/慢一点 → relaxed；正常 → normal；紧凑/多去几个 → packed（作用于整个行程）。

## ops 规则（仅在已有草稿时使用）
- 用户点名的景点必须换成草稿里的引用编号写入 item_ids，例如“删掉雷峰塔”→ remove，item_ids=["D1-3"]。
- 用户用条件描述一批景点时使用选择器：day / part（morning=上午，afternoon=下午）/ tags，item_ids 留空。
  例如“把第二天下午的寺庙都删了”→ remove，day=2，part="afternoon"，tags=["temple"]。
  “把户外的都换成室内的”→ replace，tags=["outdoor"]，target_tags=["indoor"]。
- replace：替换成指定景点时填 poi_name；替换成某类景点时填 target_tags；“换成两个博物馆”→ target_count=2。
- add：新增景点填 poi_name，指定天填 day。
- move：把景点移到另一天，填 item_ids 和 to_day。
- swap：交换两个景点所在的天，item_ids 恰好两个。
- set_duration：修改游玩时长，item_ids 一个，minutes 为分钟数（“多玩一小时”需按当前时长换算）。
- day_load：某天“轻松点/少安排点”→ level=lighter；“多安排点”→ level=fuller，填 day。
- undo：“撤销/恢复上一版”。
- 多个独立修改分别写成多条 ops。
- 无法映射到以上任何字段的要求，把原话片段写入 unhandled，不要编造。"""
