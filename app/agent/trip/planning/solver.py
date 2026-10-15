"""行程 CP-SAT 求解器：选点、分天、排序、定时和午餐安排一次求解。

模型只覆盖 ``SolveInput.days`` 中的天；局部修改时固定天不进入模型，
它们已占用的细分体验数量通过 ``fixed_subtype_counts`` 计入全程重复惩罚。

求解分两阶段：
1. 最大化收益目标，确定入选、分天和顺序；
2. 固定入选、分天和弧，最小化开始时间之和，使行程从 9:00 起尽量紧凑；
   午餐位置不影响目标值，在本阶段可重新选择。

规模较大时（候选数超过 ``_PRUNE_MIN_POIS``），第一阶段先只保留每个景点交通最近的
``_ARC_NEIGHBORS`` 条出边（对称补全，必选/替换/保留景点之间的边全部保留）求解；
裁剪后的解对完整模型同样可行。若裁剪模型无解，再用完整交通边在剩余时间内重解，
因此裁剪只可能损失少量最优性，不会改变可行性判断。

本模块为同步纯计算，调用方应放到线程中执行。
"""

import time
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from app.agent.trip.planning.rules import (
    LUNCH_EARLIEST_START,
    LUNCH_INSIDE_MIN_DURATION,
    LUNCH_LATEST_START,
    LUNCH_MINUTES,
    LUNCH_WINDOW_END,
    rain_units,
)
from app.agent.trip.planning.tags import tags_match
from app.schemas.trip_plan_schema import (
    DAY_WINDOW_MINUTES,
    SolvedDay,
    SolvedVisit,
    SolveInput,
    SolveOutput,
)

# 固定随机种子，保证相同输入在相同机器上尽量得到相同结果
_RANDOM_SEED = 20240601
_NUM_WORKERS = 8
# 第二阶段限时为第一阶段的 1/3，且不少于 1 秒
_PHASE2_RATIO = 1 / 3
_PHASE2_MIN_SECONDS = 1.0
# 候选数超过该值时启用近邻裁剪
_PRUNE_MIN_POIS = 20
# 近邻裁剪时每个景点保留的最近出边数
_ARC_NEIGHBORS = 10

# (起点下标, 终点下标, 交通分钟数)
_Edge = tuple[int, int, int]


@dataclass
class _Arc:
    """一天内两个景点之间的一条候选弧。"""

    src: int
    dst: int
    day_k: int
    minutes: int
    lit: cp_model.IntVar


@dataclass
class _Built:
    """构建完成的模型及其变量引用。"""

    model: cp_model.CpModel
    # (景点下标, 天下标) -> 是否在该天入选
    x: dict[tuple[int, int], cp_model.IntVar] = field(default_factory=dict)
    # 景点开始与占用结束（含园内午餐）
    s: list[cp_model.IntVar] = field(default_factory=list)
    e: list[cp_model.IntVar] = field(default_factory=list)
    # (景点下标, 天下标) -> 结束后午餐 / 园内午餐
    la: dict[tuple[int, int], cp_model.IntVar] = field(default_factory=dict)
    li: dict[tuple[int, int], cp_model.IntVar] = field(default_factory=dict)
    # 景点之间的弧，参与交通时间统计
    arcs: list[_Arc] = field(default_factory=list)
    # 与虚拟起点相关的弧及空天自环，第二阶段同样需要固定
    depot_lits: list[cp_model.IntVar] = field(default_factory=list)


def _infeasible() -> SolveOutput:
    """返回无可行解的输出。"""
    return SolveOutput(status="infeasible")


def _precheck(inp: SolveInput, idx: dict[str, int], day_index: dict[int, int]) -> bool:
    """建模前检查明显不可行的输入。

    Returns:
        输入可以建模时为 True；必选项或替换约束无法满足时为 False。
    """
    for ref in inp.required:
        if ref.poi_id not in idx:
            return False
    for slot in inp.replace_slots:
        if slot.day not in day_index:
            return False
        if slot.poi_id is not None:
            if slot.poi_id not in idx:
                return False
            continue
        matched = [
            pid for pid in set(inp.new_poi_ids) if pid in idx and tags_match(slot.tags, inp.pois[idx[pid]].tags)
        ]
        if len(matched) < slot.count:
            return False
    return True


def _collect_edges(inp: SolveInput, idx: dict[str, int]) -> list[_Edge]:
    """从交通矩阵取出两端都在候选中的有向边。"""
    edges: list[_Edge] = []
    for key, leg in inp.travel.items():
        a, sep, b = key.partition("|")
        if not sep or a == b or a not in idx or b not in idx:
            continue
        edges.append((idx[a], idx[b], leg.minutes))
    return edges


def _prune_edges(inp: SolveInput, idx: dict[str, int], edges: list[_Edge]) -> list[_Edge] | None:
    """按交通近邻裁剪弧，缩小大规模实例的搜索空间。

    每个景点保留耗时最短的若干条出边，再补齐反向边；必选、替换指定和保留奖励
    景点之间的边全部保留，避免它们因裁剪而无法同天。

    Returns:
        裁剪后的边；候选数不超过阈值或裁剪无效果时返回 ``None``。
    """
    if len(inp.pois) <= _PRUNE_MIN_POIS:
        return None
    anchors = {idx[r.poi_id] for r in inp.required if r.poi_id in idx}
    anchors |= {idx[s.poi_id] for s in inp.replace_slots if s.poi_id in idx}
    anchors |= {idx[pid] for pid in inp.keep_bonus if pid in idx}

    by_src: dict[int, list[_Edge]] = {}
    for edge in edges:
        by_src.setdefault(edge[0], []).append(edge)
    keep: set[tuple[int, int]] = set()
    for src, out in by_src.items():
        out.sort(key=lambda e: (e[2], e[1]))
        for _, dst, _ in out[:_ARC_NEIGHBORS]:
            keep.add((src, dst))
            keep.add((dst, src))
    pruned = [e for e in edges if (e[0], e[1]) in keep or (e[0] in anchors and e[1] in anchors)]
    if len(pruned) == len(edges):
        return None
    return pruned


def _build(
    inp: SolveInput,
    idx: dict[str, int],
    day_index: dict[int, int],
    edges: list[_Edge],
) -> _Built:
    """构建第一阶段的 CP-SAT 模型。

    Args:
        inp: 求解输入。
        idx: POI 标识到下标。
        day_index: 天序号到天下标。
        edges: 允许使用的景点间有向边。

    Returns:
        模型及变量引用。
    """
    m = cp_model.CpModel()
    built = _Built(model=m)
    pois = inp.pois
    days = inp.days
    w = inp.weights
    n = len(pois)
    n_days = len(days)
    x, la, li = built.x, built.la, built.li

    # ---------------- 选点与分天 ----------------
    for i, p in enumerate(pois):
        for k, d in enumerate(days):
            var = m.new_bool_var(f"x_{i}_{k}")
            x[i, k] = var
            if p.duration_minutes > d.end_limit_min:
                m.add(var == 0)
        m.add(sum(x[i, k] for k in range(n_days)) <= 1)
    for k, d in enumerate(days):
        m.add(sum(x[i, k] for i in range(n)) <= d.max_items)

    # ---------------- 时间与午餐变量 ----------------
    for i, p in enumerate(pois):
        s_var = m.new_int_var(0, DAY_WINDOW_MINUTES, f"s_{i}")
        e_var = m.new_int_var(0, DAY_WINDOW_MINUTES + p.duration_minutes + LUNCH_MINUTES, f"e_{i}")
        built.s.append(s_var)
        built.e.append(e_var)
        inside_terms = []
        for k in range(n_days):
            if not inp.lunch_enabled:
                continue
            la_var = m.new_bool_var(f"la_{i}_{k}")
            la[i, k] = la_var
            m.add_implication(la_var, x[i, k])
            m.add(e_var >= LUNCH_EARLIEST_START).only_enforce_if(la_var)
            m.add(e_var <= LUNCH_LATEST_START).only_enforce_if(la_var)
            if p.duration_minutes >= LUNCH_INSIDE_MIN_DURATION:
                li_var = m.new_bool_var(f"li_{i}_{k}")
                li[i, k] = li_var
                inside_terms.append(li_var)
                m.add_implication(li_var, x[i, k])
                m.add(s_var <= LUNCH_EARLIEST_START).only_enforce_if(li_var)
                m.add(e_var >= LUNCH_WINDOW_END).only_enforce_if(li_var)
        m.add(e_var == s_var + p.duration_minutes + LUNCH_MINUTES * sum(inside_terms))
        for k, d in enumerate(days):
            m.add(e_var <= d.end_limit_min).only_enforce_if(x[i, k])

    # 每天至多一次午餐；上午开始且午后结束的天必须有午餐。
    # am/pm 为每天两个布尔量，逐点蕴含：入选且 s ≤ 149 ⇒ am；入选且 e ≥ 271 ⇒ pm。
    if inp.lunch_enabled:
        for k in range(n_days):
            lunch_terms = [la[i, k] for i in range(n)] + [li[i, k] for i in range(n) if (i, k) in li]
            lunch_sum = sum(lunch_terms)
            m.add(lunch_sum <= 1)
            am = m.new_bool_var(f"am_{k}")
            pm = m.new_bool_var(f"pm_{k}")
            for i in range(n):
                m.add(built.s[i] >= LUNCH_EARLIEST_START).only_enforce_if([x[i, k], am.Not()])
                m.add(built.e[i] <= LUNCH_WINDOW_END).only_enforce_if([x[i, k], pm.Not()])
            m.add(lunch_sum >= am + pm - 1)

    # ---------------- 每天一条开放路径 ----------------
    for k, d in enumerate(days):
        circuit: list[tuple[int, int, cp_model.IntVar]] = []
        # 节点 0 为虚拟起点，自环为真表示当天没有景点
        empty = m.new_bool_var(f"empty_{k}")
        built.depot_lits.append(empty)
        circuit.append((0, 0, empty))
        m.add(sum(x[i, k] for i in range(n)) == 0).only_enforce_if(empty)
        for i in range(n):
            node = i + 1
            circuit.append((node, node, x[i, k].Not()))
            first = m.new_bool_var(f"first_{i}_{k}")
            last = m.new_bool_var(f"last_{i}_{k}")
            built.depot_lits.extend([first, last])
            circuit.append((0, node, first))
            circuit.append((node, 0, last))
        for i, j, minutes in edges:
            # 两个景点加交通已超过当天时限的弧不可能被选，直接不建
            if pois[i].duration_minutes + minutes + pois[j].duration_minutes > d.end_limit_min:
                continue
            lit = m.new_bool_var(f"arc_{i}_{j}_{k}")
            circuit.append((i + 1, j + 1, lit))
            built.arcs.append(_Arc(src=i, dst=j, day_k=k, minutes=minutes, lit=lit))
            if inp.lunch_enabled:
                m.add(built.s[j] >= built.e[i] + minutes + LUNCH_MINUTES * la[i, k]).only_enforce_if(lit)
            else:
                m.add(built.s[j] >= built.e[i] + minutes).only_enforce_if(lit)
        m.add_circuit(circuit)

    # ---------------- 必选与替换约束 ----------------
    for ref in inp.required:
        i = idx[ref.poi_id]
        if ref.day is not None and ref.day in day_index:
            m.add(x[i, day_index[ref.day]] == 1)
        else:
            m.add(sum(x[i, k] for k in range(n_days)) == 1)

    new_ids = [pid for pid in dict.fromkeys(inp.new_poi_ids) if pid in idx]
    for slot in inp.replace_slots:
        k = day_index[slot.day]
        if slot.poi_id is not None:
            m.add(x[idx[slot.poi_id], k] == 1)
            continue
        matched = [idx[pid] for pid in new_ids if tags_match(slot.tags, pois[idx[pid]].tags)]
        m.add(sum(x[i, k] for i in matched) >= slot.count)

    # ---------------- 目标 ----------------
    scale = w.score_scale
    divisor = w.travel_divisor
    value_expr = sum(p.value * x[i, k] for i, p in enumerate(pois) for k in range(n_days))

    # 每日同主体验类超出免罚数量的部分
    day_cat_over = []
    categories = sorted({p.main_category for p in pois})
    for k in range(n_days):
        for c in categories:
            members = [i for i, p in enumerate(pois) if p.main_category == c]
            upper = len(members) - w.day_category_free
            if upper <= 0:
                continue
            over = m.new_int_var(0, upper, f"dcat_{k}_{c}")
            m.add(over >= sum(x[i, k] for i in members) - w.day_category_free)
            day_cat_over.append(over)

    # 全程同细分体验超出免罚数量的部分，含固定天已占用数量
    subtype_over = []
    subtypes = sorted({p.subtype for p in pois} | set(inp.fixed_subtype_counts))
    for t in subtypes:
        members = [i for i, p in enumerate(pois) if p.subtype == t]
        fixed = inp.fixed_subtype_counts.get(t, 0)
        upper = fixed + len(members) - w.subtype_free
        if upper <= 0:
            continue
        over = m.new_int_var(0, upper, f"sub_{t}")
        m.add(over >= fixed + sum(x[i, k] for i in members for k in range(n_days)) - w.subtype_free)
        subtype_over.append(over)

    rain_expr = sum(
        rain_units(d.rain_level, p.indoor, p.duration_minutes) * x[i, k]
        for i, p in enumerate(pois)
        for k, d in enumerate(days)
    )
    keep_lits = [
        x[idx[pid], day_index[day]]
        for pid, day in inp.keep_bonus.items()
        if pid in idx and day in day_index
    ]
    travel_expr = sum(arc.minutes * arc.lit for arc in built.arcs)

    penalty_expr = (
        w.day_category_penalty * sum(day_cat_over)
        + w.subtype_penalty * sum(subtype_over)
        + w.rain_penalty * rain_expr
        - w.keep_bonus * sum(keep_lits)
    )
    m.maximize(divisor * value_expr - scale * divisor * penalty_expr - scale * travel_expr)
    return built


def _make_solver(seconds: float) -> cp_model.CpSolver:
    """创建带统一参数的求解器。"""
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = seconds
    solver.parameters.num_search_workers = _NUM_WORKERS
    solver.parameters.random_seed = _RANDOM_SEED
    # 预处理探测在 7 天×45 候选规模下要耗费数秒且收益很小，关闭后首个可行解约 1 秒内出现
    solver.parameters.cp_model_probing_level = 0
    return solver


def _summarize(
    inp: SolveInput,
    idx: dict[str, int],
    day_index: dict[int, int],
    chosen: dict[int, int],
    travel_minutes: int,
    objective_value: int,
) -> dict[str, int]:
    """按入选结果重算目标各项，便于解释和调参。

    Args:
        inp: 求解输入。
        idx: POI 标识到下标。
        day_index: 天序号到天下标。
        chosen: 入选景点下标到天下标。
        travel_minutes: 被选弧的交通分钟数合计。
        objective_value: 第一阶段原始目标值。

    Returns:
        目标各项取值；惩罚和奖励为乘以权重后、未乘放大倍数的值。
    """
    w = inp.weights
    pois = inp.pois
    value_total = sum(pois[i].value for i in chosen)

    day_cat_count: dict[tuple[int, str], int] = {}
    subtype_count: dict[str, int] = dict(inp.fixed_subtype_counts)
    rain_total = 0
    for i, k in chosen.items():
        p = pois[i]
        day_cat_count[k, p.main_category] = day_cat_count.get((k, p.main_category), 0) + 1
        subtype_count[p.subtype] = subtype_count.get(p.subtype, 0) + 1
        rain_total += rain_units(inp.days[k].rain_level, p.indoor, p.duration_minutes)
    day_cat_over = sum(max(c - w.day_category_free, 0) for c in day_cat_count.values())
    subtype_over = sum(max(c - w.subtype_free, 0) for c in subtype_count.values())
    kept = sum(
        1
        for pid, day in inp.keep_bonus.items()
        if pid in idx and day in day_index and chosen.get(idx[pid]) == day_index[day]
    )
    return {
        "visits": len(chosen),
        "value": round(value_total / w.score_scale),
        "travel_minutes": travel_minutes,
        "day_category_penalty": w.day_category_penalty * day_cat_over,
        "subtype_penalty": w.subtype_penalty * subtype_over,
        "rain_penalty": w.rain_penalty * rain_total,
        "keep_bonus": w.keep_bonus * kept,
        "objective": objective_value,
    }


def solve(inp: SolveInput) -> SolveOutput:
    """求解行程：选点、分天、排序、定时并安排午餐。

    Args:
        inp: 求解输入，只包含待求解的天。

    Returns:
        求解输出。``status`` 为 ``ok`` 时 ``days`` 与 ``inp.days`` 一一对应，
        每天的访问按开始时间排序；``infeasible`` 或 ``timeout`` 时 ``days`` 为空。

    Raises:
        ValueError: 候选景点或天序号重复。
        RuntimeError: 模型无效，属于建模缺陷。
    """
    ids = [p.poi_id for p in inp.pois]
    if len(set(ids)) != len(ids):
        raise ValueError("候选景点 poi_id 重复")
    idx = {pid: i for i, pid in enumerate(ids)}
    day_index = {d.day: k for k, d in enumerate(inp.days)}
    if len(day_index) != len(inp.days):
        raise ValueError("待求解的天序号重复")
    if not _precheck(inp, idx, day_index):
        return _infeasible()
    if not inp.pois:
        # 没有候选时每天为空，无需建模；目标只剩固定天带来的细分体验惩罚常数
        summary = _summarize(inp, idx, day_index, {}, 0, 0)
        w = inp.weights
        summary["objective"] = -w.score_scale * w.travel_divisor * summary["subtype_penalty"]
        return SolveOutput(status="ok", days=[SolvedDay(day=d.day) for d in inp.days], objective=summary)

    # ---------------- 第一阶段：最大化收益 ----------------
    # 先尝试近邻裁剪模型，无解时退回完整交通边，两次共用同一时限
    full_edges = _collect_edges(inp, idx)
    pruned_edges = _prune_edges(inp, idx, full_edges)
    attempts = [full_edges] if pruned_edges is None else [pruned_edges, full_edges]
    deadline = time.monotonic() + inp.time_limit_seconds
    for n_try, edges in enumerate(attempts):
        remaining = inp.time_limit_seconds if n_try == 0 else deadline - time.monotonic()
        if remaining <= 0:
            return SolveOutput(status="timeout")
        built = _build(inp, idx, day_index, edges)
        model = built.model
        solver = _make_solver(remaining)
        status = solver.solve(model)
        if status == cp_model.MODEL_INVALID:
            raise RuntimeError(f"行程模型无效：{model.validate()}")
        if status == cp_model.INFEASIBLE and n_try < len(attempts) - 1:
            continue
        break
    if status == cp_model.INFEASIBLE:
        return _infeasible()
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return SolveOutput(status="timeout")

    objective_value = round(solver.objective_value)
    x_val = {key: solver.boolean_value(v) for key, v in built.x.items()}
    arc_val = [solver.boolean_value(arc.lit) for arc in built.arcs]
    depot_val = [solver.boolean_value(v) for v in built.depot_lits]
    la_val = {key: solver.boolean_value(v) for key, v in built.la.items()}
    li_val = {key: solver.boolean_value(v) for key, v in built.li.items()}
    starts = [solver.value(v) for v in built.s]
    ends = [solver.value(v) for v in built.e]

    # ---------------- 第二阶段：固定决策，压缩开始时间 ----------------
    # 固定入选、分天和全部弧，它们决定第一阶段的目标值；午餐位置不影响目标，
    # 只作提示保持可调，否则第一阶段若碰巧选择“推迟到 11:30 开始以免午餐”，
    # 第二阶段就无法再把行程提前到 9:00。
    for (key, var) in built.x.items():
        model.add(var == int(x_val[key]))
    for arc, on in zip(built.arcs, arc_val, strict=True):
        model.add(arc.lit == int(on))
    for var, on in zip(built.depot_lits, depot_val, strict=True):
        model.add(var == int(on))
    for key, var in built.la.items():
        model.add_hint(var, int(la_val[key]))
    for key, var in built.li.items():
        model.add_hint(var, int(li_val[key]))
    for var, val in zip(built.s, starts, strict=True):
        model.add_hint(var, val)
    model.minimize(sum(built.s))
    phase2 = _make_solver(max(_PHASE2_MIN_SECONDS, inp.time_limit_seconds * _PHASE2_RATIO))
    status2 = phase2.solve(model)
    if status2 in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        starts = [phase2.value(v) for v in built.s]
        ends = [phase2.value(v) for v in built.e]
        la_val = {key: phase2.boolean_value(v) for key, v in built.la.items()}
        li_val = {key: phase2.boolean_value(v) for key, v in built.li.items()}

    # ---------------- 组装输出 ----------------
    chosen = {i: k for (i, k), on in x_val.items() if on}
    travel_minutes = sum(arc.minutes for arc, on in zip(built.arcs, arc_val, strict=True) if on)

    out_days: list[SolvedDay] = []
    for k, d in enumerate(inp.days):
        visits = [
            SolvedVisit(
                poi_id=inp.pois[i].poi_id,
                start_min=starts[i],
                end_min=ends[i],
                lunch_inside=li_val.get((i, k), False),
                lunch_after=la_val.get((i, k), False),
            )
            for i, kk in chosen.items()
            if kk == k
        ]
        visits.sort(key=lambda v: v.start_min)
        out_days.append(SolvedDay(day=d.day, visits=visits))

    return SolveOutput(
        status="ok",
        days=out_days,
        objective=_summarize(inp, idx, day_index, chosen, travel_minutes, objective_value),
    )
