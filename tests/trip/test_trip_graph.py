"""根图端到端测试：用假模型、假高德客户端和 SQLite 景点属性缓存执行完整的一轮对话。"""

from datetime import date
from typing import Any

import pytest
from langchain_core.exceptions import OutputParserException
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.trip.intent_resolve import CONFIRMED_REPLY
from app.agent.trip.plan_render import CONFIRM_HINT
from app.agent.trip.planning.validate import validate_plan
from app.agent.trip.trip_graph import TripTurnRunner, UnavailableTurnRunner
from app.agent.trip.trip_nodes import QA_FAILED_REPLY, UNDERSTAND_FAILED_REPLY, UNDO_REPLY
from app.agent.trip.trip_state import TripDeps
from app.schemas.poi_enrich_schema import PoiEnrichResult
from app.schemas.trip_intent_schema import IntentEditOp, TurnIntent
from app.schemas.trip_plan_schema import MAX_ITEMS_PER_DAY, AmapPoi, CityInfo, Plan, PoiRef, Requirements
from app.schemas.trip_schema import PendingOption, PendingQuestion, TripContext, TurnOutcome
from tests.trip.draft_data import amap_poi, draft_plan, draft_requirements
from tests.trip.planning_data import HANGZHOU, START_DATE

TODAY = date(2026, 10, 15)
# 西湖周边 10 个景点，评分各不相同以保证候选顺序确定。
SPOT_RATINGS = {
    "duanqiao": 4.8, "baidi": 4.6, "zjmuseum": 4.7, "yuewang": 4.6, "leifeng": 4.8,
    "jingci": 4.5, "huagang": 4.6, "lingyin": 4.9, "hefang": 4.5, "teamuseum": 4.7,
}


class FakeStructured:
    """结构化输出的替身，按 schema 分派给假模型。"""

    def __init__(self, owner: "FakeLLM", schema: type) -> None:
        """记录所属假模型和输出结构。"""
        self.owner = owner
        self.schema = schema

    async def ainvoke(self, messages: list[Any]) -> Any:
        """意图理解按预置顺序返回或抛出；景点属性补全一律失败，走规则兜底。"""
        self.owner.calls.append(self.schema.__name__)
        if self.schema is TurnIntent:
            assert self.owner.intents, "意图理解不应调用模型"
            result = self.owner.intents.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        if self.schema is PoiEnrichResult:
            raise RuntimeError("景点属性模型不可用")
        raise AssertionError(f"未预期的结构化输出：{self.schema}")


class FakeLLM:
    """只支持 with_structured_output 的假聊天模型。"""

    def __init__(self, *intents: TurnIntent | Exception) -> None:
        """保存按顺序返回的意图。"""
        self.intents = list(intents)
        self.calls: list[str] = []

    def with_structured_output(self, schema: type, **kwargs: Any) -> FakeStructured:
        """返回结构化输出替身。"""
        return FakeStructured(self, schema)


class FakeQAModel(BaseChatModel):
    """问答 Agent 使用的假模型：不调用工具，直接给出固定回答或抛出异常。"""

    answer: str = ""
    fail: bool = False

    @property
    def _llm_type(self) -> str:
        """模型类型标识。"""
        return "fake-trip-qa"

    def _generate(self, messages: list[Any], stop: list[str] | None = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        """返回固定回答。"""
        if self.fail:
            raise RuntimeError("问答模型不可用")
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.answer))])

    def bind_tools(self, tools: Any, **kwargs: Any) -> "FakeQAModel":
        """忽略工具绑定。"""
        return self


class FakeAmap:
    """高德客户端替身：任何关键词的第 1 页都返回同一批杭州景点，没有天气预报。"""

    def __init__(self) -> None:
        """准备杭州景点。"""
        self.pois: list[AmapPoi] = [amap_poi(pid, rating=rating) for pid, rating in SPOT_RATINGS.items()]
        self.searches: list[tuple[str, int]] = []

    async def search_pois(
        self, city_adcode: str, keywords: str, *, types: str | None = None, page: int = 1, page_size: int = 25,
    ) -> list[AmapPoi]:
        """第 1 页返回全部景点，其余页为空。"""
        self.searches.append((keywords, page))
        return list(self.pois) if page == 1 and city_adcode == HANGZHOU.adcode else []

    async def weather_forecast(self, city_adcode: str) -> list:
        """没有天气预报。"""
        return []

    async def poi_detail(self, poi_id: str) -> AmapPoi | None:
        """按标识查找景点。"""
        return next((poi for poi in self.pois if poi.poi_id == poi_id), None)

    async def find_pois_by_name(self, city_adcode: str, name: str, *, limit: int = 5) -> list[AmapPoi]:
        """按完整名称查找景点。"""
        return [poi for poi in self.pois if poi.name == name.strip()][:limit]

    async def resolve_city(self, name: str) -> list[CityInfo]:
        """只认识杭州。"""
        return [HANGZHOU] if name.strip() in {"杭州", "杭州市"} else []


def make_runner(
    session_factory: async_sessionmaker[AsyncSession], llm: FakeLLM, qa: FakeQAModel | None = None,
) -> TripTurnRunner:
    """组装运行依赖，求解时间上限 2 秒。"""
    deps = TripDeps(
        llm=llm, qa_llm=qa or FakeQAModel(), amap=FakeAmap(), bocha=None, session_factory=session_factory,
        solver_time_limit_seconds=2,
    )
    return TripTurnRunner(deps)


def context(
    status: str = "collecting",
    requirements: Requirements | None = None,
    draft: Plan | None = None,
    previous: Plan | None = None,
    pending: PendingQuestion | None = None,
) -> TripContext:
    """构造会话快照。"""
    return TripContext(
        trip_id=1, user_id=1, status=status, requirements=requirements or Requirements(), draft=draft,
        previous_draft=previous, pending=pending, today=TODAY,
    )


def plan_intent() -> TurnIntent:
    """“10 月 20 日去杭州玩两天”的理解结果。"""
    return TurnIntent(kind="plan_request", city="杭州", start_date=START_DATE, days=2)


def assert_good_draft(outcome: TurnOutcome, days: int) -> Plan:
    """新草稿天数正确、每天有景点且不超过上限，并通过独立验算。"""
    plan = outcome.new_plan
    assert plan is not None
    assert len(plan.days) == days
    for day in plan.days:
        assert 1 <= len(day.items) <= MAX_ITEMS_PER_DAY
    assert validate_plan(plan, outcome.requirements) == []
    return plan


# ---------------------------------------------------------------------------
# 规划
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_plan_from_model_intent(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """模型理解出完整需求后生成首个草稿，景点属性模型失败时按规则推断。"""
    llm = FakeLLM(plan_intent())
    outcome = await make_runner(session_factory, llm).run(context(), "10月20日去杭州玩两天")
    assert outcome.route == "plan"
    plan = assert_good_draft(outcome, days=2)
    assert plan.city == HANGZHOU
    assert outcome.requirements.city == HANGZHOU and outcome.requirements.days == 2
    assert outcome.pending is None
    assert outcome.change_summary is None
    assert outcome.reply.startswith("已为你生成杭州 2 天的景点行程草稿：")
    assert outcome.reply.endswith(CONFIRM_HINT)
    assert llm.calls[0] == "TurnIntent"
    assert "PoiEnrichResult" in llm.calls
    # 草稿项带有高德评分和地址
    first = plan.days[0].items[0]
    assert first.rating == SPOT_RATINGS[first.poi_id]
    assert first.address


@pytest.mark.asyncio
async def test_first_plan_from_pending_option(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """回复选项序号走规则快速通道，不调用模型理解，直接按选项中的需求规划。"""
    chosen = Requirements(city=HANGZHOU, start_date=START_DATE, days=1, pace="relaxed")
    pending = PendingQuestion(kind="ambiguous", text="请选择城市", options=[PendingOption(label="杭州", requirements=chosen)])
    llm = FakeLLM()
    outcome = await make_runner(session_factory, llm).run(context(pending=pending), "1")
    plan = assert_good_draft(outcome, days=1)
    assert len(plan.days[0].items) <= 3
    assert outcome.requirements.pace == "relaxed"
    assert "TurnIntent" not in llm.calls


@pytest.mark.asyncio
async def test_remove_replans_only_affected_day(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """在首个草稿上删除第 1 天的景点后局部重排：第 2 天不变，被删景点进入不去列表。"""
    first = await make_runner(session_factory, FakeLLM(plan_intent())).run(context(), "去杭州玩两天")
    draft = assert_good_draft(first, days=2)
    removed = draft.days[0].items[0]
    llm = FakeLLM(TurnIntent(kind="modify", ops=[IntentEditOp(op="remove", item_ids=[removed.item_id])]))
    ctx = context(status="drafting", requirements=first.requirements, draft=draft)
    outcome = await make_runner(session_factory, llm).run(ctx, f"不去{removed.name}了")
    plan = assert_good_draft(outcome, days=2)
    assert removed.poi_id not in {item.poi_id for day in plan.days for item in day.items}
    assert plan.days[1].items == draft.days[1].items
    assert PoiRef(poi_id=removed.poi_id, name=removed.name) in outcome.requirements.avoid
    assert outcome.change_summary is not None and f"移除 {removed.name}" in outcome.change_summary
    assert outcome.reply.startswith("已按你的要求调整草稿。")


@pytest.mark.asyncio
async def test_plan_failure_reports_diagnosis(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """必去景点查不到位置时无解，回复诊断原因且不产生草稿。"""
    chosen = Requirements(
        city=HANGZHOU, start_date=START_DATE, days=1, must_visit=[PoiRef(poi_id="ghost", name="幽灵景点")],
    )
    pending = PendingQuestion(kind="missing_info", text="选择", options=[PendingOption(label="开始", requirements=chosen)])
    outcome = await make_runner(session_factory, FakeLLM()).run(context(pending=pending), "1")
    assert outcome.route == "plan"
    assert outcome.new_plan is None
    assert outcome.reply.startswith("这次没能排出行程：没能获取到 幽灵景点 的位置信息。")
    assert outcome.requirements == chosen
    assert outcome.pending is None


# ---------------------------------------------------------------------------
# 撤销、确认与理解失败
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_undo_restores_previous_draft(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """撤销恢复上一版草稿，天数随之恢复，不调用模型。"""
    current = draft_plan()
    previous = draft_plan({1: [("duanqiao", 0)], 2: [("leifeng", 0)]})
    llm = FakeLLM()
    ctx = context(status="drafting", requirements=draft_requirements(), draft=current, previous=previous)
    outcome = await make_runner(session_factory, llm).run(ctx, "撤销")
    assert outcome.route == "reply"
    assert outcome.new_plan == previous
    assert outcome.requirements.days == 2
    assert outcome.reply.startswith(UNDO_REPLY)
    assert outcome.reply.endswith(CONFIRM_HINT)
    assert outcome.change_summary is not None and outcome.change_summary.startswith("行程从 3 天调整为 2 天。")
    assert llm.calls == []


@pytest.mark.asyncio
async def test_confirmed_trip_rejects_modification(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """已确认的行程不能修改。"""
    llm = FakeLLM(TurnIntent(kind="modify", ops=[IntentEditOp(op="remove", item_ids=["D1-1"])]))
    ctx = context(status="confirmed", requirements=draft_requirements(), draft=draft_plan())
    outcome = await make_runner(session_factory, llm).run(ctx, "删掉断桥")
    assert outcome.route == "reply"
    assert outcome.reply == CONFIRMED_REPLY
    assert outcome.new_plan is None


@pytest.mark.asyncio
async def test_understand_parse_failure(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """模型输出无法解析时回复没听懂，并保留原待回答问题。"""
    pending = PendingQuestion(kind="missing_info", text="想去几天？")
    llm = FakeLLM(OutputParserException("bad output"))
    outcome = await make_runner(session_factory, llm).run(context(pending=pending), "啊啊啊")
    assert outcome.route == "reply"
    assert outcome.reply == UNDERSTAND_FAILED_REPLY
    assert outcome.pending == pending


# ---------------------------------------------------------------------------
# 资料问答
# ---------------------------------------------------------------------------


def question_llm() -> FakeLLM:
    """理解为资料问题。"""
    return FakeLLM(TurnIntent(kind="question", question="灵隐寺门票多少钱？"))


@pytest.mark.asyncio
async def test_qa_answer(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """问答 Agent 的回答直接作为回复。"""
    qa = FakeQAModel(answer="  灵隐寺门票约 75 元。 ")
    ctx = context(status="drafting", requirements=draft_requirements(), draft=draft_plan())
    outcome = await make_runner(session_factory, question_llm(), qa).run(ctx, "灵隐寺门票多少钱？")
    assert outcome.route == "qa"
    assert outcome.reply == "灵隐寺门票约 75 元。"
    assert outcome.new_plan is None


@pytest.mark.asyncio
async def test_qa_empty_answer_fails_gracefully(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """问答 Agent 没有给出文字时回复查询失败。"""
    ctx = context(status="drafting", requirements=draft_requirements(), draft=draft_plan())
    outcome = await make_runner(session_factory, question_llm(), FakeQAModel(answer="")).run(ctx, "门票？")
    assert outcome.route == "qa"
    assert outcome.reply == QA_FAILED_REPLY


@pytest.mark.asyncio
async def test_qa_model_error_fails_gracefully(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """问答模型异常不影响会话，回复查询失败并保留需求。"""
    ctx = context(status="drafting", requirements=draft_requirements(), draft=draft_plan())
    outcome = await make_runner(session_factory, question_llm(), FakeQAModel(fail=True)).run(ctx, "门票？")
    assert outcome.route == "qa"
    assert outcome.reply == QA_FAILED_REPLY
    assert outcome.requirements == draft_requirements()


@pytest.mark.asyncio
async def test_unavailable_runner_raises() -> None:
    """未配置依赖时执行器直接失败。"""
    with pytest.raises(RuntimeError, match="行程对话依赖未配置"):
        await UnavailableTurnRunner().run(context(), "你好")
