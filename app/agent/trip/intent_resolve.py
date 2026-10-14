"""把理解阶段的意图转换为已校验的需求变更和修改操作。

模型只负责“听懂”，这里由程序负责：状态规则（城市锁定、确认后只读）、景点名称核实、
选择器展开为具体草稿引用、操作合法性检查，以及决定本轮去向。
"""

import re
from dataclasses import dataclass, field
from datetime import date

from haversine import haversine

from app.agent.trip.planning.tags import TAG_LABELS, item_tags, tags_match
from app.schemas.trip_intent_schema import IntentEditOp, TurnIntent
from app.schemas.trip_plan_schema import (
    MAX_TRIP_DAYS,
    AddPoiOp,
    AmapPoi,
    CityInfo,
    EditOp,
    MoveItemsOp,
    Plan,
    PlanItem,
    PoiRef,
    RemoveItemsOp,
    ReplaceItemsOp,
    ReplaceTarget,
    Requirements,
    SetDayLoadOp,
    SetDurationOp,
    SwapItemsOp,
)
from app.schemas.trip_schema import PendingOption, PendingQuestion, TripContext

# 下午的起点：12:00（相对 9:00 的 180 分钟）。
AFTERNOON_START_MIN = 180
_MISSING_LABELS = {"city": "目的地城市", "start_date": "出发日期", "days": "游玩天数"}
OUT_OF_SCOPE_REPLY = "我只负责景点和游玩路线规划，暂不推荐住宿和餐饮。有景点或行程上的需求可以随时告诉我。"
CONFIRMED_REPLY = "这份行程已经确认，不能再修改。如需新的安排，请新建一个行程。"


class ResolveError(Exception):
    """用户请求无法执行，消息直接回复给用户。"""


class AmbiguousPoi(Exception):
    """景点名称对应多个结果，需要用户说清楚。"""

    def __init__(self, name: str, choices: list[str]) -> None:
        """记录原名称和候选名称。"""
        super().__init__(name)
        self.name = name
        self.choices = choices


@dataclass
class Resolution:
    """本轮解析结果。

    Attributes:
        route: reply 直接回复；plan 进入规划；qa 资料问答；undo 恢复上一版草稿。
        requirements: 处理后的需求。
        ops: 待执行的修改操作。
        force_full: 是否允许全量重排。
        stability: 全量重排时是否保留原景点奖励。
        reply: 直接回复的文字。
        pending: 新的待回答问题，空表示清除。
        question: 交给问答 Agent 的问题。
        notes: 附加说明。
    """

    route: str
    requirements: Requirements
    ops: list[EditOp] = field(default_factory=list)
    force_full: bool = False
    stability: bool = True
    reply: str | None = None
    pending: PendingQuestion | None = None
    question: str | None = None
    notes: list[str] = field(default_factory=list)


_OPTION_PATTERN = re.compile(r"^\s*(?:选|选择)?\s*(?:第)?\s*([1-9一二三四五六七八九])\s*(?:个|项|条)?\s*[。.!！]?\s*$")
_CN_DIGITS = {c: i for i, c in enumerate("一二三四五六七八九", start=1)}
_UNDO_WORDS = {"撤销", "撤回", "撤销上一步", "恢复上一版", "回到上一版", "撤销修改"}
_CONFIRM_WORDS = {"确认", "确认行程", "确定", "就这样", "确认草稿"}


def fast_path_intent(context: TripContext, message: str) -> TurnIntent | None:
    """无需模型即可确定的输入：选项序号、撤销、口头确认。

    Args:
        context: 会话快照。
        message: 用户消息。

    Returns:
        确定的意图；无法用规则确定时为空。
    """
    text = message.strip()
    if context.pending is not None and context.pending.options:
        matched = _OPTION_PATTERN.match(text)
        if matched:
            raw = matched.group(1)
            return TurnIntent(kind="answer_pending", pending_option=_CN_DIGITS.get(raw) or int(raw))
    if text in _UNDO_WORDS and context.draft is not None:
        return TurnIntent(kind="modify", ops=[IntentEditOp(op="undo")])
    if text in _CONFIRM_WORDS and context.draft is not None and context.status == "drafting":
        return TurnIntent(kind="chat", chat_reply="满意的话请点击页面上的“确认行程”按钮，确认后行程将不能再修改。")
    return None


async def match_poi(amap: object, city: CityInfo, name: str) -> AmapPoi | None:
    """在城市内按名称核实景点。

    Args:
        amap: 高德客户端。
        city: 城市。
        name: 用户说的景点名称。

    Returns:
        匹配的景点；查不到为空。

    Raises:
        AmbiguousPoi: 结果和名称不直接对应且有多个候选。
    """
    results = [poi for poi in await amap.find_pois_by_name(city.adcode, name, limit=5) if poi.city_adcode == city.adcode]
    if not results:
        return None
    key = name.strip()
    for poi in results:
        if poi.name == key:
            return poi
    first = results[0]
    if key in first.name or first.name in key:
        return first
    choices = list(dict.fromkeys(poi.name for poi in results))[:3]
    if len(choices) == 1:
        return first
    raise AmbiguousPoi(name, choices)


def _item_index(plan: Plan) -> dict[str, tuple[int, PlanItem]]:
    """草稿引用到（天，景点）。"""
    return {item.item_id: (day.day, item) for day in plan.days for item in day.items}


def select_items(plan: Plan, op: IntentEditOp) -> list[str]:
    """把点名引用或选择器展开为草稿引用列表。

    Raises:
        ResolveError: 引用不存在、选择器为空或没有匹配项。
    """
    index = _item_index(plan)
    if op.item_ids:
        unknown = [item_id for item_id in op.item_ids if item_id not in index]
        if unknown:
            raise ResolveError(f"草稿里没有 {'、'.join(unknown)}，请对照当前草稿再说一次。")
        return list(dict.fromkeys(op.item_ids))
    if op.day is None and op.part is None and not op.tags:
        raise ResolveError("没听清要调整哪个景点，可以说景点名称，或者“第几天上午/下午的某类景点”。")
    selected = []
    for item_id, (day, item) in index.items():
        if op.day is not None and day != op.day:
            continue
        if op.part == "morning" and item.start_min >= AFTERNOON_START_MIN:
            continue
        if op.part == "afternoon" and item.start_min < AFTERNOON_START_MIN:
            continue
        if op.tags and not tags_match(list(op.tags), item_tags(item.subtype, item.main_category, item.indoor)):
            continue
        selected.append(item_id)
    if not selected:
        where = f"第{op.day}天" if op.day else "行程中"
        part = {"morning": "上午", "afternoon": "下午"}.get(op.part or "", "")
        kinds = "、".join(TAG_LABELS.get(tag, tag) for tag in op.tags)
        raise ResolveError(f"{where}{part}没有找到{kinds or '符合条件'}的景点。")
    return selected


def nearest_day(plan: Plan, poi: AmapPoi) -> int:
    """为未指定天的新增景点选择距离当天景点重心最近、且不太满的一天。"""
    best_day, best_score = 1, float("inf")
    for day in plan.days:
        if not day.items:
            score = 50.0
        else:
            lat = sum(item.lat for item in day.items) / len(day.items)
            lng = sum(item.lng for item in day.items) / len(day.items)
            score = haversine((lat, lng), (poi.lat, poi.lng)) + 2 * len(day.items)
        if score < best_score:
            best_day, best_score = day.day, score
    return best_day


def _add_ref(refs: list[PoiRef], ref: PoiRef) -> list[PoiRef]:
    """按 POI 去重追加，后加入的覆盖指定天。"""
    return [r for r in refs if r.poi_id != ref.poi_id] + [ref]


def _drop_ref(refs: list[PoiRef], poi_id: str) -> list[PoiRef]:
    """移除指定 POI。"""
    return [r for r in refs if r.poi_id != poi_id]


class IntentResolver:
    """一轮意图解析，持有本轮的会话快照和高德客户端。"""

    def __init__(self, context: TripContext, amap: object) -> None:
        """保存本轮输入。"""
        self.context = context
        self.amap = amap
        self.requirements = context.requirements.model_copy(deep=True)
        self.notes: list[str] = []

    def _reply(self, text: str, *, keep_pending: bool = False) -> Resolution:
        """直接回复，按需保留原待回答问题。"""
        return Resolution(
            route="reply", requirements=self.context.requirements, reply=text,
            pending=self.context.pending if keep_pending else None,
        )

    async def resolve(self, intent: TurnIntent) -> Resolution:
        """解析一条意图。

        Args:
            intent: 理解阶段的结构化结果。

        Returns:
            本轮解析结果。
        """
        status = self.context.status
        if intent.kind == "out_of_scope":
            return self._reply(OUT_OF_SCOPE_REPLY, keep_pending=True)
        if intent.kind == "chat":
            return self._reply(intent.chat_reply or "好的。想去哪里玩、玩几天，随时告诉我。", keep_pending=True)
        if intent.kind == "question":
            if not intent.question:
                return self._reply("能具体说说想了解什么吗？", keep_pending=True)
            return Resolution(
                route="qa", requirements=self.context.requirements, question=intent.question,
                pending=self.context.pending,
            )
        if status == "confirmed":
            return self._reply(CONFIRMED_REPLY)
        try:
            if intent.kind == "answer_pending" and intent.pending_option is not None:
                return self._answer_pending(intent.pending_option)
            return await self._apply(intent)
        except ResolveError as exc:
            return self._reply(str(exc))
        except AmbiguousPoi as exc:
            text = f"你说的“{exc.name}”是指 {'、'.join(exc.choices)} 中的哪一个？请说出完整名称。"
            return Resolution(
                route="reply", requirements=self.context.requirements, reply=text,
                pending=PendingQuestion(kind="ambiguous", text=text),
            )

    def _answer_pending(self, option: int) -> Resolution:
        """执行用户选择的待回答选项。"""
        pending = self.context.pending
        if pending is None or not pending.options:
            raise ResolveError("现在没有需要选择的选项，请直接告诉我你的想法。")
        if option > len(pending.options):
            return self._reply(f"请回复 1～{len(pending.options)} 之间的序号。", keep_pending=True)
        chosen = pending.options[option - 1]
        self.requirements = (chosen.requirements or self.requirements).model_copy(deep=True)
        if self.context.status == "collecting":
            return self._decide_collecting()
        if chosen.ops and self.context.draft is not None:
            return Resolution(
                route="plan", requirements=self.requirements, ops=list(chosen.ops), force_full=chosen.force_full,
            )
        return Resolution(route="plan", requirements=self.requirements, force_full=True)

    async def _apply(self, intent: TurnIntent) -> Resolution:
        """应用需求变化和修改操作，并决定去向。"""
        draft = self.context.draft
        if any(op.op == "undo" for op in intent.ops):
            return self._undo()
        before = self.requirements.model_copy(deep=True)
        city_resolution = await self._apply_city(intent)
        if city_resolution is not None:
            return city_resolution
        self._apply_fields(intent)
        ops: list[EditOp] = []
        await self._apply_mentions(intent, ops)
        if intent.ops:
            if draft is None:
                self.notes.append("还没有生成草稿，具体景点的调整请在草稿生成后再提。")
            else:
                for raw in intent.ops:
                    ops.extend(await self._convert_op(raw, draft))
        if intent.unhandled:
            self.notes.append(f"这些要求暂时没法处理：{'；'.join(intent.unhandled)}。")

        if self.context.status == "collecting" or draft is None:
            return self._decide_collecting()
        global_change = self._global_change(before)
        if global_change:
            days_changed = before.days != self.requirements.days
            if days_changed and any(not isinstance(op, RemoveItemsOp | AddPoiOp) for op in ops):
                self.notes.append("天数变化后会整体重新编排，其他具体调整请在新草稿上再提一次。")
            if days_changed:
                return Resolution(route="plan", requirements=self.requirements, notes=self.notes, stability=True)
            return Resolution(
                route="plan", requirements=self.requirements, ops=ops, force_full=bool(ops), notes=self.notes,
            )
        if ops:
            return Resolution(route="plan", requirements=self.requirements, ops=ops, notes=self.notes)
        text = "\n".join(self.notes) if self.notes else "没有识别到需要调整的地方，可以说说想怎么改，例如“把第二天下午的寺庙换成博物馆”。"
        return Resolution(route="reply", requirements=self.requirements, reply=text)

    def _global_change(self, before: Requirements) -> bool:
        """需求中影响整个行程的字段是否变化。"""
        after = self.requirements
        return (
            before.days != after.days or before.start_date != after.start_date or before.pace != after.pace
            or before.preferences != after.preferences or before.transport != after.transport
        )

    def _undo(self) -> Resolution:
        """撤销到上一个草稿版本，天数和日期随之恢复。"""
        previous = self.context.previous_draft
        if self.context.draft is None or previous is None:
            raise ResolveError("没有可以撤销的上一版草稿。")
        self.requirements.days = len(previous.days)
        self.requirements.start_date = previous.start_date
        return Resolution(route="undo", requirements=self.requirements)

    async def _apply_city(self, intent: TurnIntent) -> Resolution | None:
        """处理城市：收集阶段可设置或更换，草稿生成后锁定。"""
        name = (intent.city or "").strip()
        if not name:
            if intent.kind == "switch_city":
                raise ResolveError("想换到哪个城市呢？")
            return None
        current = self.requirements.city
        if current is not None and (name in current.name or current.name.startswith(name)):
            return None
        if self.context.status != "collecting":
            raise ResolveError(
                f"草稿生成后城市已锁定为{current.name if current else '当前城市'}，不能更换。"
                "如需规划其他城市，请新建一个行程。",
            )
        cities = await self.amap.resolve_city(name)
        if not cities:
            raise ResolveError(f"没有找到城市“{name}”，请确认名称，例如“杭州”“成都”。")
        if len(cities) > 1:
            options = [
                PendingOption(label=city.name, requirements=self._with_city(city)) for city in cities[:5]
            ]
            text = f"“{name}”对应多个城市，请回复序号：" + "；".join(
                f"{i}. {o.label}" for i, o in enumerate(options, start=1)
            )
            return Resolution(
                route="reply", requirements=self.context.requirements, reply=text,
                pending=PendingQuestion(kind="ambiguous", text=text, options=options),
            )
        self.requirements = self._with_city(cities[0])
        return None

    def _with_city(self, city: CityInfo) -> Requirements:
        """设置城市；更换城市时清空与原城市景点绑定的需求。"""
        current = self.requirements
        if current.city is not None and current.city.adcode != city.adcode:
            if current.must_visit or current.avoid:
                self.notes.append("已更换城市，原来指定的必去和不去景点已清空。")
            return current.model_copy(update={
                "city": city, "must_visit": [], "avoid": [], "duration_overrides": {},
            })
        return current.model_copy(update={"city": city})

    def _apply_fields(self, intent: TurnIntent) -> None:
        """应用日期、天数、节奏、偏好和交通。

        Raises:
            ResolveError: 日期早于今天或天数超出上限。
        """
        req = self.requirements
        if intent.start_date is not None:
            if intent.start_date < self.context.today:
                raise ResolveError(f"出发日期 {intent.start_date.isoformat()} 早于今天，请换一个日期。")
            req.start_date = intent.start_date
        if intent.days is not None:
            if intent.days > MAX_TRIP_DAYS:
                raise ResolveError(f"行程只支持 1～{MAX_TRIP_DAYS} 天，可以先规划其中一段。")
            req.days = intent.days
            req.day_loads = {day: level for day, level in req.day_loads.items() if day <= intent.days}
            req.must_visit = [
                ref if ref.day is None or ref.day <= intent.days else ref.model_copy(update={"day": None})
                for ref in req.must_visit
            ]
        if intent.pace is not None:
            req.pace = intent.pace
        if intent.clear_preferences:
            req.preferences = {}
        for pref in intent.preferences:
            req.preferences[pref.tag] = pref.weight
        if intent.walk_only is not None:
            req.transport.walk_only = intent.walk_only
        if intent.max_walk_minutes is not None:
            req.transport.max_walk_minutes = intent.max_walk_minutes

    async def _apply_mentions(self, intent: TurnIntent, ops: list[EditOp]) -> None:
        """核实必去、不去景点；已有草稿时转换为新增或删除操作。"""
        if not intent.must_visit and not intent.avoid:
            return
        city = self.requirements.city
        if city is None:
            self.notes.append("请先告诉我目的地城市，之后再指定必去或不去的景点。")
            return
        draft = self.context.draft if self.context.status == "drafting" else None
        index = {item.poi_id: (day.day, item) for day in draft.days for item in day.items} if draft else {}
        days = self.requirements.days or MAX_TRIP_DAYS
        for mention in intent.must_visit:
            poi = await match_poi(self.amap, city, mention.name)
            if poi is None:
                self.notes.append(f"没有在{city.name}找到“{mention.name}”，已忽略。")
                continue
            day = mention.day if mention.day is not None and mention.day <= days else None
            ref = PoiRef(poi_id=poi.poi_id, name=poi.name, day=day)
            self.requirements.avoid = _drop_ref(self.requirements.avoid, poi.poi_id)
            self.requirements.must_visit = _add_ref(self.requirements.must_visit, ref)
            if draft is None:
                continue
            if poi.poi_id in index:
                current_day, item = index[poi.poi_id]
                if day is not None and day != current_day:
                    ops.append(MoveItemsOp(item_ids=[item.item_id], to_day=day))
                else:
                    self.notes.append(f"{poi.name} 已经在第{current_day}天的安排里。")
            else:
                ops.append(AddPoiOp(poi=ref.model_copy(update={"day": day or nearest_day(draft, poi)})))
        for name in intent.avoid:
            poi = await match_poi(self.amap, city, name)
            if poi is None:
                self.notes.append(f"没有在{city.name}找到“{name}”，已忽略。")
                continue
            self.requirements.must_visit = _drop_ref(self.requirements.must_visit, poi.poi_id)
            self.requirements.avoid = _add_ref(self.requirements.avoid, PoiRef(poi_id=poi.poi_id, name=poi.name))
            if poi.poi_id in index:
                ops.append(RemoveItemsOp(item_ids=[index[poi.poi_id][1].item_id]))

    def _check_day(self, day: int | None, draft: Plan) -> int:
        """校验天数在草稿范围内。"""
        if day is None or not 1 <= day <= len(draft.days):
            raise ResolveError(f"行程只有 {len(draft.days)} 天，请指定第 1～{len(draft.days)} 天。")
        return day

    async def _convert_op(self, raw: IntentEditOp, draft: Plan) -> list[EditOp]:
        """把一条模型输出的修改转换为已校验的操作，并同步更新需求。"""
        index = _item_index(draft)
        req = self.requirements
        if raw.op == "remove":
            ids = select_items(draft, raw)
            for item_id in ids:
                item = index[item_id][1]
                req.must_visit = _drop_ref(req.must_visit, item.poi_id)
                req.avoid = _add_ref(req.avoid, PoiRef(poi_id=item.poi_id, name=item.name))
            return [RemoveItemsOp(item_ids=ids)]
        if raw.op == "replace":
            ids = select_items(draft, raw)
            target = ReplaceTarget(tags=list(raw.target_tags), count=raw.target_count)
            if raw.poi_name:
                assert req.city is not None
                poi = await match_poi(self.amap, req.city, raw.poi_name)
                if poi is None:
                    raise ResolveError(f"没有在{req.city.name}找到“{raw.poi_name}”。")
                if any(item.poi_id == poi.poi_id for _, item in index.values()):
                    raise ResolveError(f"{poi.name} 已经在行程里了，可以说“把它移到第几天”。")
                target = ReplaceTarget(poi=PoiRef(poi_id=poi.poi_id, name=poi.name))
                req.avoid = _drop_ref(req.avoid, poi.poi_id)
            for item_id in ids:
                item = index[item_id][1]
                req.must_visit = _drop_ref(req.must_visit, item.poi_id)
                req.avoid = _add_ref(req.avoid, PoiRef(poi_id=item.poi_id, name=item.name))
            return [ReplaceItemsOp(item_ids=ids, target=target)]
        if raw.op == "add":
            if not raw.poi_name:
                if raw.target_tags or raw.tags:
                    if raw.day is None:
                        self.notes.append("新增某一类景点需要说出具体名称，或者说“把第几天调得充实一点”。")
                        return []
                    # 按类别新增暂不支持，按“当天更充实”处理，并记入需求以便当天上限和结束时间放宽。
                    day = self._check_day(raw.day, draft)
                    req.day_loads[day] = "fuller"
                    self.notes.append(f"已把第{day}天调得充实一点；想新增指定景点可以直接说出名称。")
                    return [SetDayLoadOp(day=day, level="fuller")]
                raise ResolveError("想新增哪个景点呢？")
            assert req.city is not None
            poi = await match_poi(self.amap, req.city, raw.poi_name)
            if poi is None:
                raise ResolveError(f"没有在{req.city.name}找到“{raw.poi_name}”。")
            if any(item.poi_id == poi.poi_id for _, item in index.values()):
                self.notes.append(f"{poi.name} 已经在行程里了。")
                return []
            day = self._check_day(raw.day, draft) if raw.day is not None else nearest_day(draft, poi)
            ref = PoiRef(poi_id=poi.poi_id, name=poi.name, day=day)
            req.avoid = _drop_ref(req.avoid, poi.poi_id)
            req.must_visit = _add_ref(req.must_visit, ref)
            return [AddPoiOp(poi=ref)]
        if raw.op == "move":
            ids = select_items(draft, raw.model_copy(update={"day": raw.day if not raw.item_ids else None}))
            to_day = self._check_day(raw.to_day, draft)
            for item_id in ids:
                poi_id = index[item_id][1].poi_id
                req.must_visit = [r.model_copy(update={"day": to_day}) if r.poi_id == poi_id else r for r in req.must_visit]
            return [MoveItemsOp(item_ids=ids, to_day=to_day)]
        if raw.op == "swap":
            ids = select_items(draft, raw)
            if len(ids) != 2:
                raise ResolveError("交换需要恰好两个景点，例如“把 D1-2 和 D2-3 换一下”。")
            if index[ids[0]][0] == index[ids[1]][0]:
                self.notes.append("同一天内的游览顺序会按路线自动优化，无需手动交换。")
                return []
            return [SwapItemsOp(a=ids[0], b=ids[1])]
        if raw.op == "set_duration":
            ids = select_items(draft, raw)
            if raw.minutes is None:
                raise ResolveError("想调整成玩多久呢？例如“灵隐寺玩两个小时”。")
            item = index[ids[0]][1]
            req.duration_overrides[item.poi_id] = raw.minutes
            return [SetDurationOp(item_id=ids[0], minutes=raw.minutes)]
        if raw.op == "day_load":
            day = self._check_day(raw.day, draft)
            if raw.level is None:
                raise ResolveError("想让这一天轻松一点还是充实一点？")
            req.day_loads[day] = raw.level
            return [SetDayLoadOp(day=day, level=raw.level)]
        return []

    def _decide_collecting(self) -> Resolution:
        """收集阶段：信息齐全就首次规划，否则追问缺失项。"""
        missing = self.requirements.missing_fields()
        if missing:
            labels = "、".join(_MISSING_LABELS[name] for name in missing)
            text = f"还需要知道{labels}，才能开始规划。"
            if "start_date" in missing:
                text += f"（例如“{date.fromordinal(self.context.today.toordinal() + 7).isoformat()} 出发”）"
            if self.notes:
                text = "\n".join([*self.notes, text])
            return Resolution(
                route="reply", requirements=self.requirements, reply=text,
                pending=PendingQuestion(kind="missing_info", text=text),
            )
        return Resolution(route="plan", requirements=self.requirements, notes=self.notes, stability=False)
