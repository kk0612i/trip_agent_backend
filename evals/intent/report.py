"""汇总运行记录为指标，生成 Markdown 报告和两次运行的对比。

调用异常（status=error，如网络超时）不计入准确率分母，单独统计错误率；
解析失败（parse_failed）属于模型质量问题，计为失败。
"""

import json
from collections import Counter, defaultdict
from statistics import median

from evals.intent.dataset import CATEGORIES, FIELDS, EvalCase
from evals.intent.runner import RunRecord


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def summarize(cases: list[EvalCase], records: list[RunRecord], meta: dict) -> dict:
    """计算全部指标。

    Args:
        cases: 参与本次运行的用例。
        records: 运行记录。
        meta: 运行元信息（模型、温度、提示词指纹等），原样写入。

    Returns:
        可写入 JSON 的指标字典。
    """
    by_case: dict[str, list[RunRecord]] = defaultdict(list)
    for record in records:
        by_case[record.case_id].append(record)
    graded = [r for r in records if r.status != "error"]

    # 用例级：pass^k 为所有重复都通过，pass@k 为至少一次通过。
    case_rates: dict[str, float | None] = {}
    for case in cases:
        runs = [r for r in by_case[case.id] if r.status != "error"]
        case_rates[case.id] = _rate(sum(r.passed for r in runs), len(runs))
    scored = [v for v in case_rates.values() if v is not None]

    overall = {
        "cases": len(cases),
        "runs": len(records),
        "run_pass_rate": _rate(sum(r.passed for r in graded), len(graded)),
        "case_pass_all": _rate(sum(v == 1 for v in scored), len(scored)),
        "case_pass_any": _rate(sum(v > 0 for v in scored), len(scored)),
        "parse_failed_rate": _rate(sum(r.status == "parse_failed" for r in records), len(records)),
        "error_rate": _rate(sum(r.status == "error" for r in records), len(records)),
        "fast_path_rate": _rate(sum(r.fast_path for r in records), len(records)),
    }

    case_by_id = {case.id: case for case in cases}
    confusion: dict[str, Counter] = defaultdict(Counter)
    kind_hits = strict_hits = 0
    for record in graded:
        expect = case_by_id[record.case_id].expect
        predicted = record.intent["kind"] if record.intent else "<解析失败>"
        confusion[expect.kinds[0]][predicted] += 1
        strict_hits += predicted in expect.kinds
        kind_hits += bool(((record.grade or {}).get("fields") or {}).get("kind", {}).get("passed"))
    kind = {
        # 按下游处理分支等价判定，与通过率口径一致。
        "accuracy": _rate(kind_hits, len(graded)),
        # 要求与标注的类型完全相同，用于观察模型对类型定义的理解。
        "strict_accuracy": _rate(strict_hits, len(graded)),
        "confusion": {k: dict(v) for k, v in sorted(confusion.items())},
    }

    # 字段级：分“期望有值”（是否抽对）和“期望为空”（是否多填）两种情况。
    fields: dict[str, dict] = {}
    for name in FIELDS:
        set_total = set_ok = empty_total = empty_ok = 0
        for record in graded:
            result = record.grade["fields"].get(name)
            if result is None:
                continue
            if result["expected_set"]:
                set_total += 1
                set_ok += result["passed"]
            else:
                empty_total += 1
                empty_ok += result["passed"]
        fields[name] = {
            "set_n": set_total, "set_acc": _rate(set_ok, set_total),
            "empty_n": empty_total, "empty_acc": _rate(empty_ok, empty_total),
        }

    def prf(prefix: str) -> dict:
        tp = sum(r.grade[f"{prefix}_tp"] for r in graded)
        fp = sum(r.grade[f"{prefix}_fp"] for r in graded)
        fn = sum(r.grade[f"{prefix}_fn"] for r in graded)
        precision, recall = _rate(tp, tp + fp), _rate(tp, tp + fn)
        f1 = round(2 * precision * recall / (precision + recall), 4) if precision and recall else None
        return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}

    def group_stats(key_fn) -> dict:
        groups: dict[str, list[str]] = defaultdict(list)
        for case in cases:
            for key in key_fn(case):
                groups[key].append(case.id)
        stats = {}
        for key, ids in sorted(groups.items()):
            runs = [r for i in ids for r in by_case[i] if r.status != "error"]
            stats[key] = {
                "cases": len(ids),
                "runs": len(runs),
                "pass_rate": _rate(sum(r.passed for r in runs), len(runs)),
            }
        return stats

    llm_runs = [r for r in records if r.status != "error" and not r.fast_path]
    latencies = [r.latency_ms for r in llm_runs]
    performance = {
        "llm_runs": len(llm_runs),
        "latency_p50_ms": median(latencies) if latencies else None,
        "latency_p95_ms": _percentile(latencies, 0.95),
        "latency_max_ms": max(latencies) if latencies else None,
        "avg_input_tokens": round(sum(r.input_tokens for r in llm_runs) / len(llm_runs), 1) if llm_runs else None,
        "avg_output_tokens": round(sum(r.output_tokens for r in llm_runs) / len(llm_runs), 1) if llm_runs else None,
        "total_tokens": sum(r.input_tokens + r.output_tokens for r in records),
    }

    consistency = None
    repeats = max((r.repeat for r in records), default=0) + 1
    if repeats > 1:
        kind_same = intent_same = total = 0
        for runs in by_case.values():
            ok_runs = [r for r in runs if r.status != "error"]
            if len(ok_runs) < 2:
                continue
            total += 1
            kinds = {r.intent["kind"] if r.intent else None for r in ok_runs}
            dumps = {json.dumps(r.intent, sort_keys=True, ensure_ascii=False) for r in ok_runs}
            kind_same += len(kinds) == 1
            intent_same += len(dumps) == 1
        consistency = {
            "cases": total,
            "kind_consistent": _rate(kind_same, total),
            "intent_identical": _rate(intent_same, total),
        }

    return {
        "meta": {**meta, "repeat": repeats},
        "overall": overall,
        "kind": kind,
        "fields": fields,
        "ops": prf("op"),
        "preferences": prf("pref"),
        "by_category": group_stats(lambda c: [c.category]),
        "by_kind": group_stats(lambda c: [c.expect.kinds[0]]),
        "by_tag": group_stats(lambda c: c.tags),
        "performance": performance,
        "consistency": consistency,
        "cases": case_rates,
    }


def _compact_intent(intent: dict | None) -> str:
    """只保留非默认字段，便于在报告中阅读。"""
    if intent is None:
        return "（解析失败）"
    compact = {k: v for k, v in intent.items() if v not in (None, [], False, {})}
    compact["ops"] = [
        {k: v for k, v in op.items() if v not in (None, [])} for op in intent.get("ops", [])
    ] or None
    return json.dumps({k: v for k, v in compact.items() if v is not None}, ensure_ascii=False)


def render_markdown(summary: dict, cases: list[EvalCase], records: list[RunRecord]) -> str:
    """生成 Markdown 报告。"""
    meta, overall = summary["meta"], summary["overall"]
    lines = [
        "# 意图理解评估报告",
        "",
        f"- 时间：{meta.get('started_at')}　模型：`{meta.get('model')}`　温度：{meta.get('temperature')}"
        f"　重复：{meta['repeat']}　提示词指纹：`{meta.get('prompt_fingerprint')}`　提交：`{meta.get('git_commit')}`",
        f"- 用例 {overall['cases']} 条，运行 {overall['runs']} 次；规则快速路径占 {_pct(overall['fast_path_rate'])}",
        "",
        "## 总体",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| 单次运行通过率 | {_pct(overall['run_pass_rate'])} |",
        f"| 用例全部重复通过（pass^k） | {_pct(overall['case_pass_all'])} |",
        f"| 用例至少一次通过（pass@k） | {_pct(overall['case_pass_any'])} |",
        f"| kind 准确率（下游等价 / 严格） | {_pct(summary['kind']['accuracy'])}"
        f" / {_pct(summary['kind'].get('strict_accuracy'))} |",
        f"| ops 精确率 / 召回率 / F1 | {_pct(summary['ops']['precision'])} / {_pct(summary['ops']['recall'])}"
        f" / {_pct(summary['ops']['f1'])} |",
        f"| 偏好标签 精确率 / 召回率 | {_pct(summary['preferences']['precision'])}"
        f" / {_pct(summary['preferences']['recall'])} |",
        f"| 解析失败率 | {_pct(overall['parse_failed_rate'])} |",
        f"| 调用错误率（不计入准确率） | {_pct(overall['error_rate'])} |",
    ]
    perf = summary["performance"]
    lines += [
        f"| 模型调用延迟 P50 / P95 | {perf['latency_p50_ms']} / {perf['latency_p95_ms']} ms |",
        f"| 平均 token（输入 / 输出） | {perf['avg_input_tokens']} / {perf['avg_output_tokens']} |",
    ]
    if summary["consistency"]:
        cons = summary["consistency"]
        lines += [
            f"| 重复运行 kind 一致率 | {_pct(cons['kind_consistent'])} |",
            f"| 重复运行意图完全一致率 | {_pct(cons['intent_identical'])} |",
        ]

    lines += ["", "## 按类别", "", "| 类别 | 说明 | 用例 | 通过率 |", "| --- | --- | --- | --- |"]
    for name, stats in summary["by_category"].items():
        lines.append(f"| {name} | {CATEGORIES.get(name, '')} | {stats['cases']} | {_pct(stats['pass_rate'])} |")

    lines += ["", "## 字段", "", "期望有值时看是否抽对；期望为空时看是否多填。", "",
              "| 字段 | 期望有值 用例 | 抽对率 | 期望为空 用例 | 未多填率 |", "| --- | --- | --- | --- | --- |"]
    for name, stats in summary["fields"].items():
        lines.append(
            f"| {name} | {stats['set_n']} | {_pct(stats['set_acc'])} | {stats['empty_n']} | {_pct(stats['empty_acc'])} |",
        )

    confusion = summary["kind"]["confusion"]
    predicted_kinds = sorted({p for row in confusion.values() for p in row})
    lines += ["", "## kind 混淆矩阵（行=标注，列=模型）", "",
              "| 标注 \\ 模型 | " + " | ".join(predicted_kinds) + " |",
              "| --- | " + " | ".join("---" for _ in predicted_kinds) + " |"]
    for expected, row in confusion.items():
        lines.append(f"| {expected} | " + " | ".join(str(row.get(p, "")) for p in predicted_kinds) + " |")

    if summary["by_tag"]:
        lines += ["", "## 按标签", "", "| 标签 | 用例 | 通过率 |", "| --- | --- | --- |"]
        for name, stats in summary["by_tag"].items():
            lines.append(f"| {name} | {stats['cases']} | {_pct(stats['pass_rate'])} |")

    by_case: dict[str, list[RunRecord]] = defaultdict(list)
    for record in records:
        by_case[record.case_id].append(record)
    failing = [c for c in cases if (summary["cases"].get(c.id) or 0) < 1]
    lines += ["", f"## 失败用例（{len(failing)}）", ""]
    for case in failing:
        runs = by_case[case.id]
        bad = next((r for r in runs if not r.passed), runs[0])
        rate = summary["cases"].get(case.id)
        lines += [f"### {case.id}（{case.category}，通过 {_pct(rate)}）", "",
                  f"- 场景：`{case.scenario}`", f"- 消息：{case.message}", f"- 标注说明：{case.note}"]
        if bad.status == "error":
            lines.append(f"- 调用错误：{bad.error}")
        else:
            for name, detail in (bad.grade or {}).get("fields", {}).items():
                if not detail["passed"]:
                    lines.append(f"- **{name}**：{detail['detail']}")
            lines.append(f"- 模型输出：`{_compact_intent(bad.intent)}`")
        lines.append("")
    return "\n".join(lines)


def render_compare(base: dict, head: dict) -> str:
    """对比两次运行的 summary.json：总体指标、类别通过率和用例翻转。"""
    def row(label: str, a: float | None, b: float | None) -> str:
        delta = "—" if a is None or b is None else f"{(b - a) * 100:+.1f}"
        return f"| {label} | {_pct(a)} | {_pct(b)} | {delta} |"

    lines = [
        "# 意图评估对比",
        "",
        f"- 基线：{base['meta'].get('started_at')} `{base['meta'].get('model')}` 提示词 `{base['meta'].get('prompt_fingerprint')}`",
        f"- 本次：{head['meta'].get('started_at')} `{head['meta'].get('model')}` 提示词 `{head['meta'].get('prompt_fingerprint')}`",
        "",
        "| 指标 | 基线 | 本次 | 变化（百分点） |",
        "| --- | --- | --- | --- |",
        row("单次运行通过率", base["overall"]["run_pass_rate"], head["overall"]["run_pass_rate"]),
        row("pass^k", base["overall"]["case_pass_all"], head["overall"]["case_pass_all"]),
        row("kind 准确率", base["kind"]["accuracy"], head["kind"]["accuracy"]),
        row("ops F1", base["ops"]["f1"], head["ops"]["f1"]),
        row("解析失败率", base["overall"]["parse_failed_rate"], head["overall"]["parse_failed_rate"]),
    ]
    for name in sorted(set(base["by_category"]) | set(head["by_category"])):
        a = base["by_category"].get(name, {}).get("pass_rate")
        b = head["by_category"].get(name, {}).get("pass_rate")
        lines.append(row(f"类别 {name}", a, b))

    common = set(base["cases"]) & set(head["cases"])
    worse = sorted(i for i in common if (head["cases"][i] or 0) < (base["cases"][i] or 0))
    better = sorted(i for i in common if (head["cases"][i] or 0) > (base["cases"][i] or 0))
    lines += ["", f"## 变差的用例（{len(worse)}）", ""]
    lines += [f"- {i}：{_pct(base['cases'][i])} → {_pct(head['cases'][i])}" for i in worse]
    lines += ["", f"## 变好的用例（{len(better)}）", ""]
    lines += [f"- {i}：{_pct(base['cases'][i])} → {_pct(head['cases'][i])}" for i in better]
    only_base, only_head = set(base["cases"]) - common, set(head["cases"]) - common
    if only_base or only_head:
        lines += ["", f"用例集不同：仅基线 {len(only_base)} 条，仅本次 {len(only_head)} 条，未参与对比。"]
    return "\n".join(lines)
