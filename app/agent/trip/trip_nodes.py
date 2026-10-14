"""根图节点：理解、解析、规划、问答和回复渲染。"""

from time import perf_counter

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain_core.exceptions import OutputParserException
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.runtime import Runtime
from loguru import logger
from pydantic import ValidationError

from app.agent.trip.intent_resolve import IntentResolver, fast_path_intent
from app.agent.trip.plan_render import compose_reply
from app.agent.trip.planning.assemble import change_summary
from app.agent.trip.planning.planning_graph import build_planning_graph
from app.agent.trip.trip_state import TripDeps, TripState
from app.prompt.trip_qa_prompt import get_trip_qa_system_prompt
from app.prompt.trip_understand_prompt import get_trip_understand_system_prompt
from app.schemas.trip_intent_schema import TurnIntent
from app.tools.trip_qa import build_trip_qa_tools

# 规划子图最长路径约 17 步（两轮扩搜、两级放宽），留出余量。
PLANNING_RECURSION_LIMIT = 40
UNDERSTAND_FAILED_REPLY = "抱歉，没太理解你的意思，可以换个说法吗？例如“下周五去杭州玩 3 天，喜欢博物馆”。"
QA_FAILED_REPLY = "抱歉，暂时没能查到相关资料，可以稍后再问一次。"
UNDO_REPLY = "已撤销上一次修改，恢复到上一版草稿。"

_planning_graph = build_planning_graph()


async def understand_node(state: TripState, runtime: Runtime[TripDeps]) -> dict:
    """把用户消息解析为结构化意图：能用规则确定的不调用模型。

    模型输出无法解析时视为没听懂；网络等调用异常直接抛出，由 Service 按服务不可用处理。
    """
    context = state["context"]
    message = state["message"]
    intent = fast_path_intent(context, message)
    if intent is not None:
        logger.bind(event="trip_intent_fast_path", status="success", kind=intent.kind).info("规则识别意图")
        return {"intent": intent}
    model = runtime.context.llm.with_structured_output(TurnIntent, method="function_calling")
    system = get_trip_understand_system_prompt(
        today=context.today, status=context.status, requirements=context.requirements,
        draft=context.draft, pending=context.pending, recent_turns=context.recent_turns,
    )
    started = perf_counter()
    try:
        intent = await model.ainvoke([SystemMessage(system), HumanMessage(message)])
    except (OutputParserException, ValidationError) as exc:
        logger.bind(event="trip_intent_parse_failed", status="failed", error_type=type(exc).__name__).warning(
            "意图解析结果无效",
        )
        intent = None
    logger.bind(
        event="trip_intent_understood", status="success" if intent else "failed",
        kind=intent.kind if intent else None, ops=len(intent.ops) if intent else 0,
        duration_ms=round((perf_counter() - started) * 1000, 2),
    ).info("意图理解完成")
    return {"intent": intent}


async def resolve_node(state: TripState, runtime: Runtime[TripDeps]) -> dict:
    """按状态规则校验意图，核实景点名称，展开选择器，决定本轮去向。"""
    context = state["context"]
    intent = state.get("intent")
    if intent is None:
        return {
            "route": "reply", "requirements": context.requirements, "reply": UNDERSTAND_FAILED_REPLY,
            "pending": context.pending, "notes": [],
        }
    resolution = await IntentResolver(context, runtime.context.amap).resolve(intent)
    update: dict = {
        "route": resolution.route,
        "requirements": resolution.requirements,
        "ops": resolution.ops,
        "force_full": resolution.force_full,
        "stability": resolution.stability,
        "reply": resolution.reply,
        "pending": resolution.pending,
        "question": resolution.question,
        "notes": resolution.notes,
    }
    if resolution.route == "undo":
        previous = context.previous_draft
        assert previous is not None and context.draft is not None
        update.update({
            "route": "reply", "new_plan": previous, "reply": UNDO_REPLY,
            "change_summary": change_summary(context.draft, previous, None),
        })
    logger.bind(
        event="trip_intent_resolved", route=update["route"], ops=len(resolution.ops),
        force_full=resolution.force_full,
    ).info("意图解析完成")
    return update


async def plan_node(state: TripState, runtime: Runtime[TripDeps]) -> dict:
    """调用规划子图；失败时恢复原需求（已有草稿时），避免需求与草稿不一致。"""
    context = state["context"]
    baseline = context.draft if context.status == "drafting" else None
    result = await _planning_graph.ainvoke(
        {
            "requirements": state["requirements"],
            "baseline": baseline,
            "ops": state.get("ops") or [],
            "force_full": state.get("force_full", False),
            "stability": state.get("stability", True),
        },
        context=runtime.context,
        config={"recursion_limit": PLANNING_RECURSION_LIMIT},
    )
    notes = [*(state.get("notes") or []), *(result.get("notes") or [])]
    if result.get("outcome") == "ok":
        plan = result["plan"]
        return {
            "new_plan": plan,
            "scope": result["scope"],
            "change_summary": change_summary(baseline, plan, result["scope"]),
            "pending": None,
            "notes": notes,
        }
    return {
        "new_plan": None,
        "reply": result.get("failure_reply") or "这次没能排出行程，请换个要求再试。",
        "pending": result.get("pending"),
        "requirements": state["requirements"] if baseline is None else context.requirements,
        "notes": notes,
    }


async def qa_node(state: TripState, runtime: Runtime[TripDeps]) -> dict:
    """资料问答：带工具调用上限的 Agent，只读当前城市和草稿。"""
    context = state["context"]
    deps = runtime.context
    city = context.requirements.city
    tools = build_trip_qa_tools(amap=deps.amap, bocha=deps.bocha, city=city, draft=context.draft)
    agent = create_agent(
        deps.qa_llm,
        tools,
        system_prompt=get_trip_qa_system_prompt(
            today=context.today, city_name=city.name if city else None, web_enabled=deps.bocha is not None,
        ),
        middleware=[
            ToolCallLimitMiddleware(run_limit=deps.max_tool_calls),
            ModelCallLimitMiddleware(run_limit=deps.max_tool_calls + 2, exit_behavior="end"),
        ],
    )
    question = state.get("question") or state["message"]
    started = perf_counter()
    try:
        result = await agent.ainvoke({"messages": [HumanMessage(question)]})
        answer = next(
            (m.text for m in reversed(result["messages"]) if isinstance(m, AIMessage) and m.text.strip()), "",
        )
    except Exception as exc:  # 问答失败不影响会话，给出友好回复
        logger.bind(event="trip_qa_failed", status="failed", error_type=type(exc).__name__).opt(
            exception=exc,
        ).warning("资料问答失败")
        answer = ""
    logger.bind(
        event="trip_qa_answered", status="success" if answer else "failed",
        duration_ms=round((perf_counter() - started) * 1000, 2),
    ).info("资料问答完成")
    return {"reply": answer.strip() or QA_FAILED_REPLY}


def render_node(state: TripState) -> dict:
    """组合最终回复文字。"""
    context = state["context"]
    new_plan = state.get("new_plan")
    reply = compose_reply(
        new_plan=new_plan,
        first_plan=context.draft is None,
        change_summary=state.get("change_summary"),
        reply=state.get("reply") if new_plan is None or state.get("route") == "reply" else None,
        notes=state.get("notes") or [],
    )
    return {"reply": reply}
