"""规划子图节点：确定范围、收集候选、补全属性、求解、扩大搜索、放宽范围、诊断和校验。"""

import asyncio
from time import perf_counter

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.runtime import Runtime
from loguru import logger

from app.agent.trip.planning.assemble import build_plan, day_date
from app.agent.trip.planning.planning_state import PlanningState
from app.agent.trip.planning.rules import day_limits
from app.agent.trip.planning.scope import full_scope, partial_scope
from app.agent.trip.planning.scoring import build_candidate, rating_fallback, to_solver_poi
from app.agent.trip.planning.solver import solve
from app.agent.trip.planning.tags import (
    TAG_LABELS,
    TAG_SEARCH_KEYWORDS,
    item_tags,
    main_category_of,
    rule_profile,
    tags_match,
)
from app.agent.trip.planning.travel import build_travel_matrix
from app.agent.trip.planning.validate import validate_plan
from app.agent.trip.trip_state import TripDeps
from app.prompt.poi_enrich_prompt import get_poi_enrich_system_prompt
from app.repository.poi_profile_repository import PoiProfileRepository
from app.schemas.poi_enrich_schema import PoiEnrichResult
from app.schemas.trip_plan_schema import (
    MAX_CANDIDATES,
    MIN_CANDIDATE_RATING,
    AmapPoi,
    Candidate,
    ObjectiveWeights,
    PlanItem,
    PoiProfile,
    SolveDay,
    SolveInput,
)
from app.schemas.trip_schema import PendingOption, PendingQuestion

# 只搜索风景名胜、博物馆、展览馆、美术馆，排除住宿和餐饮。
SCENIC_TYPES = "110000|140100|140200|140400"
BASE_TEMPLATES = ["热门景点", "必去景点", "旅游景点"]
EXPAND_TEMPLATES = ["景点推荐", "好玩的地方", "公园", "博物馆"]
MUST_TEMPLATE = "必去景点"
MAX_SEARCH_ROUNDS = 2
# 名称包含这些词的 POI 是景区附属设施，不作为景点。
_NAME_BLACKLIST = (
    "停车场", "售票", "游客中心", "服务中心", "出入口", "检票", "厕所", "卫生间", "公交站", "地铁站", "码头",
    "索道", "暂停开放", "暂停营业",
)
_AREA_SUFFIXES = ("风景名胜区", "风景区", "景区")
# 父景区在候选中拥有至少这么多子景点时，视为大范围统称，改为保留子景点。
NESTED_AREA_MIN_CHILDREN = 3
# 每个替换约束预留的同类候选数，避免同类景点因热度不足被候选上限挤掉。
SLOT_RESERVE_PER_SLOT = 5
_ENRICH_BATCH_SIZE = 25
_ENRICH_CONCURRENCY = 3
_WEIGHTS = ObjectiveWeights()


def scope_node(state: PlanningState) -> dict:
    """确定规划范围并初始化收集状态。"""
    requirements = state["requirements"]
    baseline = state.get("baseline")
    ops = state.get("ops") or []
    if baseline is None or not ops:
        scope = full_scope(requirements, baseline, stability=baseline is not None and state.get("stability", True))
    else:
        # 允许全量重排时，等价于所有天都受影响且原景点软保留。
        scope = partial_scope(requirements, baseline, ops, level=3 if state.get("force_full") else 0)
    return {
        "scope": scope,
        "widen_level": 0,
        "search_round": 0,
        "searched": [],
        "pois": {},
        "template_hits": {},
        "must_hits": [],
        "profiles": {},
        "weather": {},
        "solve_output": None,
        "notes": [],
    }


def search_queries(state: PlanningState) -> list[tuple[str, int]]:
    """按搜索轮次生成 (关键词, 页码)，跳过已搜索过的组合。"""
    requirements = state["requirements"]
    scope = state["scope"]
    city = requirements.city.name if requirements.city else ""
    # 只为喜欢的标签追加搜索；不喜欢（负权重）的标签不搜索。
    preferred = [
        tag for tag, weight in sorted(requirements.preferences.items(), key=lambda kv: -kv[1]) if weight > 0
    ]
    slot_tags = [tag for slot in scope.replace_slots for tag in slot.tags]
    extra = list(dict.fromkeys(
        TAG_SEARCH_KEYWORDS[tag] for tag in [*slot_tags, *preferred] if tag in TAG_SEARCH_KEYWORDS
    ))
    round_ = state.get("search_round", 0)
    if round_ == 0:
        plan = [(t, 1) for t in BASE_TEMPLATES] + [(t, 1) for t in extra[:4]]
    elif round_ == 1:
        plan = [(t, 1) for t in EXPAND_TEMPLATES] + [(t, 2) for t in BASE_TEMPLATES] + [(t, 1) for t in extra[4:8]]
    else:
        plan = [(t, 3) for t in BASE_TEMPLATES] + [(t, 2) for t in extra[:4]]
    searched = set(state.get("searched") or [])
    return [(f"{city}{template}", page) for template, page in plan if f"{city}{template}#{page}" not in searched]


def _is_scenic(poi: AmapPoi, city_adcode: str) -> bool:
    """过滤跨城市和景区附属设施。"""
    return poi.city_adcode == city_adcode and not any(word in poi.name for word in _NAME_BLACKLIST)


def _clean_name(poi: AmapPoi) -> AmapPoi:
    """去掉“某某风景名胜区-”“某某风景名胜区”这类所属景区前缀，便于阅读。

    不带连字符时只处理“风景名胜区”，避免把“某某景区”这类正常名称截断。
    """
    prefix, sep, rest = poi.name.partition("-")
    if sep and rest and prefix.endswith(_AREA_SUFFIXES):
        return poi.model_copy(update={"name": rest})
    head, sep, rest = poi.name.partition(_AREA_SUFFIXES[0])
    if sep and head and len(rest) >= 2:
        return poi.model_copy(update={"name": rest})
    return poi


def prune_nested(pois: dict[str, AmapPoi]) -> dict[str, AmapPoi]:
    """处理景区与其内部景点的重叠。

    父景区在候选中且子景点不少于 NESTED_AREA_MIN_CHILDREN 个时，父景区是大范围的统称
    （如西湖风景名胜区），保留子景点、去掉父景区；子景点较少时保留父景区、去掉子景点。

    Args:
        pois: 候选景点。

    Returns:
        去重后的候选景点。
    """
    children: dict[str, list[str]] = {}
    for pid, poi in pois.items():
        if poi.parent_id and poi.parent_id in pois and poi.parent_id != pid:
            children.setdefault(poi.parent_id, []).append(pid)
    dropped: set[str] = set()
    for parent, kids in children.items():
        if len(kids) >= NESTED_AREA_MIN_CHILDREN:
            dropped.add(parent)
        else:
            dropped.update(kids)
    return {pid: poi for pid, poi in pois.items() if pid not in dropped}


def _poi_from_item(item: PlanItem, city_adcode: str) -> AmapPoi:
    """用草稿中的景点还原高德景点信息，保留评分、地址和图片。"""
    return AmapPoi(
        poi_id=item.poi_id, name=item.name, lng=item.lng, lat=item.lat, city_adcode=city_adcode,
        address=item.address, rating=item.rating, photos=item.photos,
    )


async def gather_node(state: PlanningState, runtime: Runtime[TripDeps]) -> dict:
    """并行搜索候选景点和天气，并补齐必选景点和原草稿景点的信息。

    Raises:
        RuntimeError: 首轮搜索全部失败且没有任何候选时抛出，由上层按服务不可用处理。
    """
    amap = runtime.context.amap
    requirements = state["requirements"]
    city = requirements.city
    assert city is not None
    queries = search_queries(state)
    first_round = state.get("search_round", 0) == 0

    async def weather() -> list:
        return await amap.weather_forecast(city.adcode) if first_round else []

    started = perf_counter()
    results = await asyncio.gather(
        *(amap.search_pois(city.adcode, keyword, types=SCENIC_TYPES, page=page) for keyword, page in queries),
        weather(),
        return_exceptions=True,
    )
    search_results, weather_result = results[:-1], results[-1]
    pois = dict(state.get("pois") or {})
    hits = dict(state.get("template_hits") or {})
    must_hits = set(state.get("must_hits") or [])
    failures = 0
    seen_names = {poi.name for poi in pois.values()}
    for (keyword, _), result in zip(queries, search_results, strict=True):
        if isinstance(result, BaseException):
            failures += 1
            logger.bind(event="trip_poi_search_failed", status="failed", error_type=type(result).__name__).warning(
                "景点搜索失败，跳过该关键词",
            )
            continue
        for raw in result:
            if not _is_scenic(raw, city.adcode):
                continue
            poi = _clean_name(raw)
            if poi.poi_id not in pois:
                # 同名景点只保留首次出现的，避免重复入选。
                if poi.name in seen_names:
                    continue
                pois[poi.poi_id] = poi
                seen_names.add(poi.name)
            hits[poi.poi_id] = hits.get(poi.poi_id, 0) + 1
            if MUST_TEMPLATE in keyword:
                must_hits.add(poi.poi_id)
    pois = prune_nested(pois)

    baseline = state.get("baseline")
    if baseline is not None:
        for day in baseline.days:
            for item in day.items:
                pois.setdefault(item.poi_id, _poi_from_item(item, city.adcode))
    missing = [ref for ref in state["scope"].required if ref.poi_id not in pois]
    details = await asyncio.gather(*(amap.poi_detail(ref.poi_id) for ref in missing), return_exceptions=True)
    for ref, detail in zip(missing, details, strict=True):
        if isinstance(detail, AmapPoi):
            pois[detail.poi_id] = detail

    update: dict = {
        "pois": pois,
        "template_hits": hits,
        "must_hits": sorted(must_hits),
        "searched": [*(state.get("searched") or []), *(f"{k}#{p}" for k, p in queries)],
    }
    if first_round:
        if isinstance(weather_result, BaseException):
            logger.bind(event="trip_weather_failed", status="failed", error_type=type(weather_result).__name__).warning(
                "天气预报获取失败，按无雨处理",
            )
            update["weather"] = {}
        else:
            update["weather"] = {day.date: day for day in weather_result}
    if queries and failures == len(queries) and not pois:
        raise RuntimeError("景点搜索全部失败")
    logger.bind(
        event="trip_candidates_gathered", status="success", candidates=len(pois),
        duration_ms=round((perf_counter() - started) * 1000, 2),
    ).info("候选景点收集完成")
    return update


def select_candidate_ids(state: PlanningState) -> list[str]:
    """确定进入求解的候选集，总数不超过 MAX_CANDIDATES（35）。

    规则：
    1. 必选景点（必去、新增、局部修改中固定的原景点）无条件保留，没有评分也保留；
       局部修改时受影响天的原景点同样保留，以便软保留和局部重排。
    2. 其余景点必须有可解析的评分；评分 ≥ MIN_CANDIDATE_RATING 的按热度（命中模板数）、评分降序优先入选。
    3. 高分景点不足时，从剩余有评分的景点中按评分降序补入，直到达到上限。
    4. 有替换约束（如“换成博物馆”）时，每个约束先预留至多 SLOT_RESERVE_PER_SLOT 个
       满足标签的有评分景点（按评分降序），排在第 2、3 条之前。标签优先取已有属性，
       尚未补全属性的按规则推断。
    固定天的景点和用户不想去的景点不进入候选。

    Args:
        state: 规划子图状态。

    Returns:
        候选 POI 标识，保护项在前。
    """
    scope = state["scope"]
    pois = state["pois"]
    hits = state.get("template_hits") or {}
    baseline = state.get("baseline")
    excluded = set(scope.excluded_poi_ids)
    if baseline is not None:
        excluded.update(item.poi_id for day in baseline.days if day.day in scope.fixed_days for item in day.items)
    protected = [ref.poi_id for ref in scope.required if ref.poi_id in pois]
    if scope.mode == "partial":
        protected.extend(pid for pid in scope.keep_bonus if pid in pois and pid not in excluded)
    protected = list(dict.fromkeys(protected))
    taken = set(protected)
    rated = [pid for pid, poi in pois.items() if pid not in taken and pid not in excluded and poi.rating is not None]
    high = sorted(
        (pid for pid in rated if pois[pid].rating >= MIN_CANDIDATE_RATING),
        key=lambda pid: (-hits.get(pid, 0), -pois[pid].rating),
    )
    fill = sorted(
        (pid for pid in rated if pois[pid].rating < MIN_CANDIDATE_RATING),
        key=lambda pid: (-pois[pid].rating, -hits.get(pid, 0)),
    )
    profiles = state.get("profiles") or {}
    reserved: list[str] = []
    for slot in scope.replace_slots:
        if slot.poi_id is not None:
            continue
        matched = sorted(
            (pid for pid in rated if pid not in reserved and tags_match(slot.tags, _tags_of(pois[pid], profiles))),
            key=lambda pid: (-pois[pid].rating, -hits.get(pid, 0)),
        )
        reserved.extend(matched[:SLOT_RESERVE_PER_SLOT])
    rest = [pid for pid in high + fill if pid not in set(reserved)]
    room = max(0, MAX_CANDIDATES - len(protected))
    return [*protected, *[*reserved, *rest][:room]]


def _tags_of(poi: AmapPoi, profiles: dict[str, PoiProfile]) -> list[str]:
    """景点的匹配标签：优先用已补全的属性，否则按规则推断。"""
    profile = profiles.get(poi.poi_id) or rule_profile(poi)
    return item_tags(profile.subtype, main_category_of(profile.subtype), profile.indoor)


async def _llm_profiles(runtime: Runtime[TripDeps], pois: list[AmapPoi]) -> dict[str, PoiProfile]:
    """分批并行调用模型判断景点属性；某批失败时该批走规则兜底。"""
    model = runtime.context.llm.with_structured_output(PoiEnrichResult, method="function_calling")
    semaphore = asyncio.Semaphore(_ENRICH_CONCURRENCY)
    system = SystemMessage(get_poi_enrich_system_prompt())

    async def run_batch(batch: list[AmapPoi]) -> dict[str, PoiProfile]:
        wanted = {poi.poi_id for poi in batch}
        lines = "\n".join(
            f"- poi_id={poi.poi_id} | 名称={poi.name} | 高德分类={poi.amap_type or '未知'} | 地址={poi.address or '未知'}"
            for poi in batch
        )
        started = perf_counter()
        async with semaphore:
            try:
                result = await model.ainvoke([system, HumanMessage(f"请判断以下景点的属性：\n{lines}")])
            except Exception as exc:  # 模型失败不影响规划，改用规则兜底
                logger.bind(event="trip_poi_enrich_failed", status="failed", error_type=type(exc).__name__).warning(
                    "景点属性模型调用失败，改用规则推断",
                )
                return {}
        logger.bind(
            event="trip_poi_enriched", status="success", count=len(batch),
            duration_ms=round((perf_counter() - started) * 1000, 2),
        ).info("景点属性模型调用完成")
        return {
            item.poi_id: PoiProfile(
                poi_id=item.poi_id, subtype=item.subtype, indoor=item.indoor,
                duration_minutes=item.duration_minutes, source="llm",
            )
            for item in result.items if item.poi_id in wanted
        }

    batches = [pois[i:i + _ENRICH_BATCH_SIZE] for i in range(0, len(pois), _ENRICH_BATCH_SIZE)]
    merged: dict[str, PoiProfile] = {}
    for part in await asyncio.gather(*(run_batch(batch) for batch in batches)):
        merged.update(part)
    return merged


async def enrich_node(state: PlanningState, runtime: Runtime[TripDeps]) -> dict:
    """补全景点属性：先读全局缓存，缺失的批量调用模型，仍缺失的按规则推断；模型结果写回缓存。"""
    profiles = dict(state.get("profiles") or {})
    pois = state["pois"]
    candidate_ids = select_candidate_ids(state)
    wanted = [pid for pid in candidate_ids if pid not in profiles]
    if not wanted:
        return {"candidate_ids": candidate_ids}
    async with runtime.context.session_factory() as session:
        cached = await PoiProfileRepository(session).get_many(wanted)
    profiles.update(cached)
    missing = [pois[pid] for pid in wanted if pid not in cached]
    generated = await _llm_profiles(runtime, missing) if missing else {}
    for poi in missing:
        profiles[poi.poi_id] = generated.get(poi.poi_id) or rule_profile(poi)
    if generated:
        # 写缓存是独立工作单元；失败只影响下次命中率，不影响本轮规划。
        try:
            async with runtime.context.session_factory() as session, session.begin():
                await PoiProfileRepository(session).insert_ignore(
                    [(profile, pois[pid].name) for pid, profile in generated.items()],
                )
        except Exception as exc:
            logger.bind(event="trip_poi_cache_failed", status="failed", error_type=type(exc).__name__).warning(
                "景点属性缓存写入失败",
            )
    return {"profiles": profiles, "candidate_ids": candidate_ids}


def build_candidates(state: PlanningState) -> dict[str, Candidate]:
    """为候选集中已有属性的景点按当前需求打分；评分缺失的必选景点使用候选评分中位数。

    候选集沿用 enrich 节点确定的结果，避免属性补全后标签变化导致前后候选不一致。
    """
    requirements = state["requirements"]
    pois = state["pois"]
    hits = state.get("template_hits") or {}
    must_hits = set(state.get("must_hits") or [])
    profiles = state.get("profiles") or {}
    ids = state.get("candidate_ids")
    selected = [pid for pid in (select_candidate_ids(state) if ids is None else ids) if pid in profiles]
    fallback = rating_fallback([pois[pid] for pid in selected])
    return {
        pid: build_candidate(
            pois[pid], profiles[pid],
            preferences=requirements.preferences,
            duration_overrides=requirements.duration_overrides,
            template_hits=hits.get(pid, 0),
            must_template_hit=pid in must_hits,
            rating_default=fallback,
        )
        for pid in selected
    }


def build_solve_input(state: PlanningState, candidates: dict[str, Candidate], time_limit: float) -> SolveInput:
    """按范围组装求解输入：只含受影响的天，固定天景点不参与且其细分体验计入常数项。"""
    requirements = state["requirements"]
    scope = state["scope"]
    baseline = state.get("baseline")
    weather = state.get("weather") or {}
    assert requirements.start_date is not None
    fixed_pois: set[str] = set()
    fixed_subtypes: dict[str, int] = {}
    if baseline is not None:
        for day in baseline.days:
            if day.day in scope.fixed_days:
                for item in day.items:
                    fixed_pois.add(item.poi_id)
                    fixed_subtypes[item.subtype] = fixed_subtypes.get(item.subtype, 0) + 1
    excluded = set(scope.excluded_poi_ids) | fixed_pois
    pool = [candidate for pid, candidate in candidates.items() if pid not in excluded]
    days = []
    for number in scope.affected_days:
        end_limit, max_items = day_limits(requirements, number)
        # 节奏上限（不超过每天 5 个）是硬上限：固定景点加新增景点超出时本级无解，交给放宽范围处理。
        if number in scope.day_max_items:
            max_items = min(scope.day_max_items[number], max_items)
        forecast = weather.get(day_date(requirements.start_date, number))
        days.append(SolveDay(
            day=number, rain_level=forecast.rain_level if forecast else 0,
            end_limit_min=end_limit, max_items=max(1, max_items),
        ))
    baseline_pois = {item.poi_id for day in baseline.days for item in day.items} if baseline else set()
    city_adcode = requirements.city.adcode if requirements.city else None
    return SolveInput(
        days=days,
        pois=[to_solver_poi(candidate, _WEIGHTS) for candidate in pool],
        travel=build_travel_matrix(
            [(c.poi.poi_id, c.poi.lng, c.poi.lat) for c in pool], requirements.transport, city_adcode=city_adcode,
        ),
        required=scope.required,
        replace_slots=scope.replace_slots,
        new_poi_ids=[c.poi.poi_id for c in pool if c.poi.poi_id not in baseline_pois],
        keep_bonus={pid: day for pid, day in scope.keep_bonus.items() if day in scope.affected_days},
        fixed_subtype_counts=fixed_subtypes,
        weights=_WEIGHTS,
        time_limit_seconds=time_limit,
    )


async def solve_node(state: PlanningState, runtime: Runtime[TripDeps]) -> dict:
    """在线程中执行 CP-SAT 求解，避免阻塞事件循环。"""
    candidates = build_candidates(state)
    inp = build_solve_input(state, candidates, runtime.context.solver_time_limit_seconds)
    started = perf_counter()
    output = await asyncio.to_thread(solve, inp)
    logger.bind(
        event="trip_solved", status=output.status, candidates=len(inp.pois), days=len(inp.days),
        widen_level=state.get("widen_level", 0), duration_ms=round((perf_counter() - started) * 1000, 2),
    ).info("行程求解完成")
    return {"solve_output": output}


def expand_node(state: PlanningState) -> dict:
    """进入下一轮候选搜索。"""
    return {"search_round": state.get("search_round", 0) + 1}


def widen_node(state: PlanningState) -> dict:
    """局部求解失败时放宽一级范围后重新求解。"""
    level = state.get("widen_level", 0) + 1
    scope = partial_scope(state["requirements"], state["baseline"], state.get("ops") or [], level=level)
    return {"widen_level": level, "scope": scope}


def validate_node(state: PlanningState) -> dict:
    """组装草稿并独立验算；验算不通过说明求解或组装有缺陷，按失败处理。"""
    requirements = state["requirements"]
    scope = state["scope"]
    candidates = build_candidates(state)
    used = {visit.poi_id for day in state["solve_output"].days for visit in day.visits}
    travel = build_travel_matrix(
        [(candidates[pid].poi.poi_id, candidates[pid].poi.lng, candidates[pid].poi.lat) for pid in used],
        requirements.transport,
        city_adcode=requirements.city.adcode if requirements.city else None,
    )
    plan = build_plan(
        requirements=requirements, baseline=state.get("baseline"), scope=scope, output=state["solve_output"],
        candidates=candidates, travel=travel, weather=state.get("weather") or {},
    )
    errors = validate_plan(plan, requirements, baseline=state.get("baseline"), scope=scope)
    if errors:
        logger.bind(event="trip_plan_invalid", status="failed", violations=len(errors)).error(
            "行程验算未通过：{}", "；".join(errors[:5]),
        )
        return {
            "outcome": "failed", "plan": None, "pending": None,
            "failure_reply": "抱歉，这次生成的行程没有通过时间和路线校验，请稍后重试或换个说法。",
        }
    return {"outcome": "ok", "plan": plan}


def diagnose_node(state: PlanningState) -> dict:
    """求解失败时分析原因，给出可直接执行的选项。"""
    requirements = state["requirements"]
    scope = state["scope"]
    output = state.get("solve_output")
    candidates = build_candidates(state)
    reasons: list[str] = []
    options: list[PendingOption] = []

    missing = [ref.name for ref in scope.required if ref.poi_id not in candidates]
    if missing:
        reasons.append(f"没能获取到 {'、'.join(missing)} 的位置信息")
    per_day: dict[int, list[str]] = {}
    for ref in scope.required:
        if ref.day is not None and ref.poi_id in candidates:
            per_day.setdefault(ref.day, []).append(ref.poi_id)
    for day, ids in per_day.items():
        end_limit, max_items = day_limits(requirements, day)
        if len(ids) > max_items:
            names = "、".join(candidates[pid].poi.name for pid in ids)
            reasons.append(f"第{day}天需要保留或新增的景点（{names}）超过当天最多 {max_items} 个的上限")
        total = sum(candidates[pid].duration_minutes for pid in ids)
        if total > end_limit:
            reasons.append(f"第{day}天指定的景点游玩时间合计约 {total} 分钟，超过当天可用时间")
    excluded = set(scope.excluded_poi_ids)
    new_ids = [pid for pid in candidates if pid not in excluded]
    for slot in scope.replace_slots:
        if slot.poi_id is None:
            matched = [pid for pid in new_ids if tags_match(slot.tags, to_solver_poi(candidates[pid], _WEIGHTS).tags)]
            if len(matched) < slot.count:
                label = "、".join(TAG_LABELS.get(tag, tag) for tag in slot.tags) or "合适"
                reasons.append(f"城市内没有找到足够的{label}类景点")
    if output is not None and output.status == "timeout":
        reasons.append("计算超时")
    if requirements.transport.walk_only:
        reasons.append(f"只步行且单段不超过 {requirements.transport.max_walk_minutes} 分钟，景点之间难以连通")
        relaxed = requirements.model_copy(update={"transport": requirements.transport.model_copy(update={"walk_only": False})})
        options.append(PendingOption(label="允许打车", requirements=relaxed, ops=state.get("ops") or []))
    if scope.mode == "partial" and len(scope.affected_days) < (requirements.days or 0):
        options.append(PendingOption(
            label="允许调整其他天的安排", requirements=requirements, ops=state.get("ops") or [], force_full=True,
        ))
    reason_text = "；".join(reasons) if reasons else "当前的条件组合下排不出满足全部要求的行程"
    text = f"这次没能排出行程：{reason_text}。"
    if options:
        text += "可以回复序号选择：" + "；".join(f"{i}. {o.label}" for i, o in enumerate(options, start=1))
    else:
        text += "可以减少必去景点、放宽时长或换一种调整方式再试。"
    pending = PendingQuestion(kind="infeasible", text=text, options=options) if options else None
    logger.bind(event="trip_plan_infeasible", status="failed", reasons=len(reasons)).info("行程无可行解")
    return {"outcome": "failed", "plan": None, "failure_reply": text, "pending": pending}
