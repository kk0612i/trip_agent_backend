"""意图评估流程：评估集校验、用假模型跑通运行器、指标汇总、报告、重新判分和对比。"""

import json
from pathlib import Path

import pytest

from app.schemas.trip_intent_schema import IntentEditOp, TurnIntent
from evals.intent.__main__ import main as cli_main
from evals.intent.dataset import DatasetError, EvalCase, load_cases
from evals.intent.report import render_compare, render_markdown, summarize
from evals.intent.runner import load_records, regrade, run_cases, save_records
from evals.intent.scenarios import SCENARIOS, build_context


class FakeStructuredModel:
    """按消息原文返回预设意图；未预设的消息模拟解析失败或网络异常。"""

    def __init__(self, answers: dict[str, TurnIntent | Exception]) -> None:
        self.answers = answers

    def with_structured_output(self, schema: type, *, method: str) -> "FakeStructuredModel":
        assert schema is TurnIntent and method == "function_calling"
        return self

    async def ainvoke(self, messages: list) -> TurnIntent:
        answer = self.answers[messages[-1].content]
        if isinstance(answer, Exception):
            raise answer
        return answer


def case(case_id: str, scenario: str, message: str, **expect: object) -> EvalCase:
    """构造用例。"""
    category = case_id.rsplit("-", 1)[0]
    return EvalCase.model_validate({
        "id": case_id, "category": category, "scenario": scenario, "message": message,
        "expect": expect, "note": "测试", "tags": ["t"],
    })


def write_yaml(path: Path, items: list[dict]) -> Path:
    """写入 YAML 用例文件（JSON 是 YAML 的子集）。"""
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return path


def test_dataset_is_valid() -> None:
    """仓库内的评估集必须能通过校验，场景改动导致标注失效时这里会失败。"""
    cases = load_cases()
    assert len({c.id for c in cases}) == len(cases)


def test_all_scenarios_build() -> None:
    """所有场景都能构造，且每次返回新对象。"""
    for name in SCENARIOS:
        assert build_context(name) is not build_context(name)
    assert build_context("drafting_hz3d").draft.days[1].items[2].name == "花港观鱼"


def test_dataset_validation_errors(tmp_path: Path) -> None:
    """引用不存在的草稿编号、未知标签、无草稿场景期望修改、缺 note 都会报错。"""
    path = write_yaml(tmp_path / "bad.yaml", [
        {"id": "modify_ref-001", "category": "modify_ref", "scenario": "drafting_hz3d", "message": "删掉",
         "expect": {"kind": "modify", "ops": [{"op": "remove", "item_ids": ["D9-1"]}]}, "note": "x"},
        {"id": "preference-001", "category": "preference", "scenario": "collecting_empty", "message": "喜欢",
         "expect": {"kind": "plan_request", "preferences": {"museum|food": 1}}, "note": "x"},
        {"id": "collect-001", "category": "collect", "scenario": "collecting_empty", "message": "删掉",
         "expect": {"kind": "modify", "ops": [{"op": "undo"}]}, "note": ""},
        {"id": "collect-001", "category": "collect", "scenario": "collecting_empty", "message": "重复",
         "expect": {"kind": "chat"}, "note": "x"},
    ])
    with pytest.raises(DatasetError) as exc_info:
        load_cases([path])
    text = str(exc_info.value)
    for fragment in ("D9-1", "'food'", "没有草稿", "缺少 note", "id 重复"):
        assert fragment in text


@pytest.mark.asyncio
async def test_run_summarize_report_regrade(tmp_path: Path) -> None:
    """假模型跑通：通过、判错、解析失败、调用异常、规则快速路径各一条。"""
    cases = [
        case("modify_ref-001", "drafting_hz3d", "雷峰塔不去了", kind="modify",
             ops=[{"op": "remove", "item_ids": ["D2-1"]}]),
        case("collect-001", "collecting_empty", "去杭州", kind="plan_request", city="杭州"),
        case("chat-001", "collecting_empty", "你好", kind="chat"),
        case("question-001", "drafting_hz3d", "灵隐寺门票", kind="question", question=["灵隐寺"]),
        case("modify_ref-002", "drafting_hz3d", "撤销", kind="modify", ops=[{"op": "undo"}]),
    ]
    from langchain_core.exceptions import OutputParserException

    llm = FakeStructuredModel({
        "雷峰塔不去了": TurnIntent(kind="modify", ops=[IntentEditOp(op="remove", item_ids=["D2-1"])]),
        "去杭州": TurnIntent(kind="plan_request", city="杭州", days=3),
        "你好": OutputParserException("bad"),
        "灵隐寺门票": TimeoutError("timeout"),
    })
    records = await run_cases(cases, llm, repeat=2, concurrency=2)  # type: ignore[arg-type]
    assert [r.case_id for r in records][:2] == ["modify_ref-001", "modify_ref-001"]
    status = {r.case_id: r for r in records}
    assert status["modify_ref-001"].passed
    assert not status["collect-001"].passed and "days" in status["collect-001"].grade["fields"]
    assert status["chat-001"].status == "parse_failed"
    assert status["question-001"].status == "error" and status["question-001"].grade is None
    assert status["modify_ref-002"].fast_path and status["modify_ref-002"].passed

    summary = summarize(cases, records, {"model": "fake"})
    overall = summary["overall"]
    assert overall["runs"] == 10
    assert overall["error_rate"] == 0.2
    assert overall["run_pass_rate"] == 0.5  # 8 次有效运行中 4 次通过
    assert summary["cases"]["question-001"] is None
    assert summary["consistency"]["kind_consistent"] == 1.0
    assert summary["fields"]["days"]["empty_acc"] == pytest.approx(0.6667, abs=1e-3)
    assert summary["kind"]["confusion"]["chat"] == {"<解析失败>": 2}
    report = render_markdown(summary, cases, records)
    assert "collect-001" in report and "modify_ref-001（" not in report

    # 修正标注后重新判分：collect-001 改为期望 days=3。
    save_records(records, tmp_path / "results.jsonl")
    fixed = [*cases[:1], case("collect-001", "collecting_empty", "去杭州", kind="plan_request", city="杭州", days=3),
             *cases[2:]]
    regraded = regrade(fixed, load_records(tmp_path / "results.jsonl"))
    head = summarize(fixed, regraded, {"model": "fake"})
    assert head["cases"]["collect-001"] == 1.0
    compare = render_compare(summary, head)
    assert "变好的用例（1）" in compare and "collect-001" in compare


def test_cli_validate(capsys: pytest.CaptureFixture[str]) -> None:
    """validate 子命令输出评估集分布。"""
    assert cli_main(["validate"]) == 0
    assert "评估集有效" in capsys.readouterr().out
