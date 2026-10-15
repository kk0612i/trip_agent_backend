"""执行评估：对每条用例调用生产环境的 understand_node，记录输出、耗时、token 和判分。

直接复用 understand_node（含规则快速路径和解析失败处理），评估的就是线上真实路径，
不复制提示词或调用逻辑。understand_node 只读取 runtime.context.llm，因此用轻量对象代替 Runtime。
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace

from langchain_core.callbacks import get_usage_metadata_callback
from langchain_core.language_models import BaseChatModel

from app.agent.trip.intent_resolve import fast_path_intent
from app.agent.trip.trip_nodes import understand_node
from app.schemas.trip_intent_schema import TurnIntent
from evals.intent.dataset import EvalCase
from evals.intent.grading import grade_intent
from evals.intent.scenarios import build_context


@dataclass
class RunRecord:
    """一条用例的一次运行。

    Attributes:
        case_id: 用例标识。
        repeat: 第几次重复，从 0 开始。
        status: ok=得到意图；parse_failed=模型输出无法解析；error=调用异常（网络、超时等）。
        fast_path: 是否由规则快速路径处理（不调用模型）。
        latency_ms: 耗时毫秒。
        input_tokens / output_tokens: 本次模型调用的 token 数。
        intent: 模型输出的意图（JSON 字典）。
        grade: 判分结果（JSON 字典），error 时为空。
        error: 异常说明。
    """

    case_id: str
    repeat: int
    status: str
    fast_path: bool
    latency_ms: float
    input_tokens: int = 0
    output_tokens: int = 0
    intent: dict | None = None
    grade: dict | None = None
    error: str | None = None

    @property
    def passed(self) -> bool:
        """本次运行是否全部字段正确。"""
        return bool(self.grade and self.grade["passed"])


async def run_once(case: EvalCase, repeat: int, llm: BaseChatModel) -> RunRecord:
    """执行一条用例一次。

    Args:
        case: 用例。
        repeat: 重复序号。
        llm: 结构化抽取模型，与生产相同配置。

    Returns:
        运行记录；调用异常不抛出，记为 error。
    """
    context = build_context(case.scenario, case.recent_turns)
    fast_path = fast_path_intent(context, case.message) is not None
    runtime = SimpleNamespace(context=SimpleNamespace(llm=llm))
    started = perf_counter()
    try:
        # 每个任务单独统计 token：回调通过 contextvar 生效，asyncio 任务之间互不干扰。
        with get_usage_metadata_callback() as usage_cb:
            update = await understand_node({"context": context, "message": case.message}, runtime)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 - 评估需要记录所有调用失败而不是中断整批
        return RunRecord(
            case_id=case.id, repeat=repeat, status="error", fast_path=fast_path,
            latency_ms=round((perf_counter() - started) * 1000, 1), error=f"{type(exc).__name__}: {exc}",
        )
    latency = round((perf_counter() - started) * 1000, 1)
    intent: TurnIntent | None = update.get("intent")
    usage = list(usage_cb.usage_metadata.values())
    return RunRecord(
        case_id=case.id,
        repeat=repeat,
        status="ok" if intent is not None else "parse_failed",
        fast_path=fast_path,
        latency_ms=latency,
        input_tokens=sum(u.get("input_tokens", 0) for u in usage),
        output_tokens=sum(u.get("output_tokens", 0) for u in usage),
        intent=intent.model_dump(mode="json") if intent is not None else None,
        grade=grade_intent(case.expect, intent, case.ignore, context.draft).to_dict(),
    )


async def run_cases(
    cases: list[EvalCase],
    llm: BaseChatModel,
    *,
    repeat: int = 1,
    concurrency: int = 4,
    on_done: Callable[["RunRecord"], None] | None = None,
) -> list[RunRecord]:
    """并发执行全部用例。

    Args:
        cases: 用例列表。
        llm: 结构化抽取模型。
        repeat: 每条用例重复次数，用于衡量一致性。
        concurrency: 同时进行的模型调用数上限。
        on_done: 每完成一次运行时的回调，用于显示进度。

    Returns:
        按用例和重复序号排序的运行记录。
    """
    semaphore = asyncio.Semaphore(concurrency)

    async def guarded(case: EvalCase, index: int) -> RunRecord:
        async with semaphore:
            record = await run_once(case, index, llm)
        if on_done is not None:
            on_done(record)
        return record

    order = {case.id: pos for pos, case in enumerate(cases)}
    records = await asyncio.gather(*(guarded(c, i) for c in cases for i in range(repeat)))
    return sorted(records, key=lambda r: (order[r.case_id], r.repeat))


def regrade(cases: list[EvalCase], records: list[RunRecord]) -> list[RunRecord]:
    """用当前标注重新判分已有的模型输出，不调用模型；用于修正标注后快速复核。

    已删除的用例对应的记录会被丢弃。
    """
    by_id = {case.id: case for case in cases}
    result = []
    for record in records:
        case = by_id.get(record.case_id)
        if case is None:
            continue
        if record.status != "error":
            intent = TurnIntent.model_validate(record.intent) if record.intent is not None else None
            draft = build_context(case.scenario, case.recent_turns).draft
            record.grade = grade_intent(case.expect, intent, case.ignore, draft).to_dict()
        result.append(record)
    return result


def save_records(records: list[RunRecord], path: Path) -> None:
    """按行写入运行记录（JSON Lines）。"""
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")


def load_records(path: Path) -> list[RunRecord]:
    """读取运行记录。"""
    with path.open(encoding="utf-8") as fh:
        return [RunRecord(**json.loads(line)) for line in fh if line.strip()]
