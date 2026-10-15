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
        f"{TAG_LABELS.get(tag, tag)}({tag}){'++' if weight >= 2 else '+' if weight > 0 else '-'}"
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
    """草稿的逐项列表，带引用编号、时间、游玩时长和标签，供模型把自然语言映射到具体景点。"""
    lines = []
    for day in plan.days:
        weather = f"，{day.weather.day_weather}" if day.weather else ""
        lines.append(f"第{day.day}天（{day.date.isoformat()}{weather}）：")
        for item in day.items:
            # 时间段可能含园内午餐，单独给出游玩分钟数，避免模型换算“多玩一小时”时出错。
            lines.append(
                f"  {item.item_id} {minute_label(item.start_min)}-{minute_label(item.end_min)}"
                f"（游玩 {item.duration_minutes} 分钟） {item.name} "
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

    # 固定不变的说明和词表放在前面，每轮变化的会话内容放在最后，便于模型服务按前缀命中缓存。
    return f"""你是单城市旅游景点规划助手的“理解”模块。你的唯一任务是把用户最新一条消息解析为结构化结果，不要回答问题、不要规划行程。
本助手只推荐景点，不推荐住宿和餐饮；每天游玩时间为 09:00-19:00；行程只支持 1～{MAX_TRIP_DAYS} 天，每天最多 5 个景点。

## kind 判定
- plan_request：提供或修改出行需求（城市、日期、天数、节奏、偏好、必去、不去、交通），或要求开始规划/重新规划。
- modify：针对当前草稿中具体景点或某天的修改（删除、替换、新增、移动、交换、改时长、某天轻松/充实、撤销）。
- answer_pending：用户在回答带选项的“待回答的问题”，填 pending_option（序号从 1 开始）。用户可能报序号，也可能说出选项内容（如“那就打车吧”对应选项“允许打车”）。问题没有选项时不要使用。
- cancel：用户放弃待回答的问题或刚提出、尚未执行的调整，例如“算了，不加了”“先不改了”“那就不弄了”。不填其他字段。
- switch_city：要求改去另一个城市。
- question：咨询景点资料、门票、开放时间、交通、天气等需要查询的问题，原样提炼到 question。
- chat：寒暄或与规划无关但无害的闲聊，在 chat_reply 里给一句简短中文回复。有待回答的问题时，“确定”“好的”这类没说选哪项的回复也用 chat，在 chat_reply 里请用户回复序号或说明选择。
- out_of_scope：要求推荐酒店住宿、餐厅美食等本助手不提供的内容。

一条消息包含多种内容时：
- 需求变化 + 修改：kind 取 modify，同时填写需求字段和 ops。
- 提问 + 修改（“雷峰塔门票多少？顺便把它挪到第三天”）：kind 取 modify 并生成 ops，问题写入 question；修改优先，否则修改会丢失。
- 出行需求 + 住宿餐饮等范围外请求：kind 取 plan_request 并填写需求，范围外部分写入 unhandled。
- 条件式修改（“要预约的话就换成别的”）依赖查询结果才能决定：kind 取 question，不生成 ops。
- 待回答的问题是自由回答（没有选项）时，按回答内容判断：补充日期、天数等需求→ plan_request；回答景点的完整名称→ modify，沿用上一轮用户提出的操作和天。例如上一轮“第一天加一个西湖博物馆”、系统追问是哪一个，本轮“南山路那个”→ add，poi_name 写问题中对应的完整名称，day=1。
- 用户拒绝所有选项并提出新要求时，按新要求判断 kind。

## 字段规则
- 只填写用户本条消息明确表达的内容；未提到的字段保持为空或空列表，不要沿用当前需求重复填写。
- preferences：用户喜欢或不喜欢的景点类型。
  - weight 默认 1；只有“特别/非常/超爱/最喜欢/一定要”这类强调才用 2。“只想/换成只看”表示 clear_preferences=true，不代表权重加大；“就行/即可”、同时说了不喜欢的类型，都不是强调。
  - 不喜欢或想少安排某类景点（“不太喜欢寺庙”“寺庙少排点”）→ 该类标签 weight=-1。“少排点”只降低优先级，不生成删除 ops；明确说“都不要了/删掉”时才对草稿生成 remove。
  - 用户说出细分类型时只填细分标签，不要再追加它所属的主类；只说大类（“自然风光”“人文的”）时填主类；用举例说明大类（“人文的，像博物馆、故居这些”）时只填例子对应的细分标签。
  - 对应不到词表的要求（如“适合拍照”）写入 unhandled，不要用相近标签凑。
- must_visit / avoid：景点名称原文；“第二天去灵隐寺”在首次规划时写入 must_visit 并给 day。avoid 只放具体景点名称，对某一类景点的排斥用 preferences 的 weight=-1；“无所谓/都行”既不是必去也不是不去，不要填写。
- 交通：“只想走路/不打车”→ walk_only=true；“可以打车”→ walk_only=false；“每段最多走 20 分钟”→ max_walk_minutes=20；“不想走太多路/少走点路”且没给分钟数 → max_walk_minutes=20。
- 节奏：轻松/慢一点 → relaxed；正常 → normal；紧凑/多去几个 → packed（作用于整个行程）。

## ops 规则（仅在已有草稿时使用）
- 尚未生成草稿时不输出 ops：“删掉/不去某景点”→ kind=plan_request，景点名写入 avoid（不要写入 unhandled）；针对某天、某类景点的修改写入 unhandled。
- 用户点名的景点必须换成草稿里的引用编号写入 item_ids，例如草稿中 D1-3 是黄鹤楼时，“删掉黄鹤楼”→ remove，item_ids=["D1-3"]（编号以当前草稿为准）。
- 用户用条件描述一批景点时使用选择器：day / part（morning=上午，afternoon=下午）/ tags，item_ids 留空。
  例如“把第二天下午的寺庙都删了”→ remove，day=2，part="afternoon"，tags=["temple"]。
  “把户外的都换成室内的”→ replace，tags=["outdoor"]，target_tags=["indoor"]。
  tags、target_tags 中的多个类型表示“其中任一”，与 indoor/outdoor 同时出现时表示两者都要满足。
- replace：替换成指定景点时填 poi_name；替换成某类景点时填 target_tags，如“换成博物馆、美术馆这类的”→ target_tags=["museum", "art_gallery"]；“换成两个博物馆”→ target_count=2。
- add：新增景点填 poi_name，指定天填 day。
- move：把景点移到另一天，填 item_ids 和 to_day。
- swap：交换两个景点所在的天，item_ids 恰好两个。
- set_duration：修改游玩时长，item_ids 一个，minutes 为调整后的总分钟数，按草稿中显示的当前游玩时长换算（当前 180 分钟，“再加一个半小时”→ 270）。
- day_load：某天“轻松点/少安排点”→ level=lighter；“多安排点”→ level=fuller，填 day。
- undo：只用于用户明确要求“撤销/恢复上一版”；放弃尚未执行的调整用 kind=cancel。
- 多个独立修改分别写成多条 ops。
- 无法映射到以上任何字段的要求，把原话片段写入 unhandled，不要编造。

## 标签词表（偏好、选择器、替换目标只能使用这些英文值）
- 主体验类：{mains}
- 细分体验类：{subtypes}
- 选择器额外可用：{indoor}

## 日期表（用于换算相对日期，start_date 必须输出 YYYY-MM-DD）
{_date_table(today)}

## 当前会话
- 行程状态：{status}（collecting=收集需求，可换城市；drafting=已有草稿，城市已锁定，可修改；confirmed=已确认，只读）
- 当前需求：{_requirements_text(requirements)}
- 待回答的问题：{pending_text}
- 最近对话：
{history}

## 当前草稿（引用编号形如 D1-2 表示第 1 天第 2 个景点）
{draft_block}"""
