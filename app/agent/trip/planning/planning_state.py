"""规划子图状态。所有字段为覆盖更新，节点返回局部更新。"""

from datetime import date
from typing import Literal, TypedDict

from app.schemas.trip_plan_schema import (
    AmapPoi,
    DayWeather,
    EditOp,
    Plan,
    PlanScope,
    PoiProfile,
    Requirements,
    SolveOutput,
)
from app.schemas.trip_schema import PendingQuestion


class PlanningState(TypedDict, total=False):
    """规划子图状态。

    Attributes:
        requirements: 已应用本轮修改的需求（输入）。
        baseline: 当前草稿，首次规划为空（输入）。
        ops: 修改操作，为空表示按需求规划（输入）。
        force_full: 是否全量重排（输入）。
        stability: 全量重排时是否保留原景点奖励（输入）。
        scope: 规划范围。
        widen_level: 局部求解放宽级别。
        search_round: 已执行的候选搜索轮次，从 0 开始。
        searched: 已执行过的搜索关键词，避免重复请求。
        pois: 已收集的景点。
        template_hits: 每个景点命中的搜索关键词数。
        must_hits: 命中“必去景点”模板的景点。
        profiles: 景点属性。
        candidate_ids: enrich 节点确定的本次求解候选。
        weather: 按日期的天气预报。
        solve_output: 最近一次求解结果。
        plan: 通过校验的新草稿。
        outcome: 子图结果：ok 成功，failed 失败并给出问题。
        failure_reply: 失败时回复给用户的文字。
        pending: 失败时给用户的选项。
        notes: 附加说明。
    """

    requirements: Requirements
    baseline: Plan | None
    ops: list[EditOp]
    force_full: bool
    stability: bool
    scope: PlanScope
    widen_level: int
    search_round: int
    searched: list[str]
    pois: dict[str, AmapPoi]
    template_hits: dict[str, int]
    must_hits: list[str]
    profiles: dict[str, PoiProfile]
    candidate_ids: list[str]
    weather: dict[date, DayWeather]
    solve_output: SolveOutput | None
    plan: Plan | None
    outcome: Literal["ok", "failed"]
    failure_reply: str | None
    pending: PendingQuestion | None
    notes: list[str]
