"""意图理解评估命令行。

    python -m evals.intent validate                     # 校验评估集，不调用模型
    python -m evals.intent run [--repeat 3] [筛选参数]   # 调用真实 DeepSeek 运行评估
    python -m evals.intent regrade <结果目录>            # 修改标注后用已有输出重新判分
    python -m evals.intent compare <基线目录> <本次目录>  # 对比两次运行
"""

import argparse
import asyncio
import hashlib
import inspect
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from loguru import logger

from evals.intent.dataset import CATEGORIES, DatasetError, EvalCase, load_cases

RESULTS_DIR = Path(__file__).resolve().parents[1] / "results"


def _prompt_fingerprint() -> str:
    """提示词模块源码和输出结构的指纹，用于区分不同版本的提示词。"""
    from app.prompt import trip_understand_prompt
    from app.schemas.trip_intent_schema import TurnIntent

    source = inspect.getsource(trip_understand_prompt) + json.dumps(TurnIntent.model_json_schema(), sort_keys=True)
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:10]


def _git_commit() -> str:
    """当前提交，工作区有改动时加 -dirty。"""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True,
        ).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, check=True).stdout
        return commit + ("-dirty" if dirty.strip() else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _filter(cases: list[EvalCase], args: argparse.Namespace) -> list[EvalCase]:
    """按类别、标签、用例 ID 筛选。"""
    if args.category:
        cases = [c for c in cases if c.category in args.category]
    if args.tag:
        cases = [c for c in cases if set(args.tag) & set(c.tags)]
    if args.case:
        cases = [c for c in cases if c.id in args.case]
    if args.limit:
        cases = cases[: args.limit]
    return cases


def _write_outputs(out_dir: Path, cases: list[EvalCase], records: list, meta: dict) -> dict:
    """写入 results.jsonl、summary.json、report.md 并返回指标。"""
    from evals.intent.report import render_markdown, summarize
    from evals.intent.runner import save_records

    out_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize(cases, records, meta)
    save_records(records, out_dir / "results.jsonl")
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "report.md").write_text(render_markdown(summary, cases, records), encoding="utf-8")
    return summary


def _print_summary(summary: dict, out_dir: Path) -> None:
    from evals.intent.report import _pct

    overall = summary["overall"]
    print(f"\n用例 {overall['cases']}，运行 {overall['runs']}")
    print(f"单次通过率 {_pct(overall['run_pass_rate'])}　pass^k {_pct(overall['case_pass_all'])}"
          f"　kind 准确率 {_pct(summary['kind']['accuracy'])}　ops F1 {_pct(summary['ops']['f1'])}")
    print(f"解析失败 {_pct(overall['parse_failed_rate'])}　调用错误 {_pct(overall['error_rate'])}")
    for name, stats in summary["by_category"].items():
        print(f"  {name:<16} {stats['cases']:>3} 条  {_pct(stats['pass_rate'])}")
    print(f"\n报告：{out_dir / 'report.md'}")


def cmd_validate(args: argparse.Namespace) -> int:
    """校验评估集并打印分布。"""
    cases = load_cases()
    print(f"评估集有效：{len(cases)} 条")
    categories = Counter(c.category for c in cases)
    kinds = Counter(c.expect.kinds[0] for c in cases)
    for name in CATEGORIES:
        print(f"  {name:<16} {categories.get(name, 0):>3}")
    print("主标注 kind 分布：" + "，".join(f"{k} {v}" for k, v in kinds.most_common()))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """调用真实模型运行评估。"""
    from app.core.config import get_settings
    from app.core.llm import create_chat_model
    from evals.intent.runner import run_cases

    cases = _filter(load_cases(), args)
    if not cases:
        print("筛选后没有用例", file=sys.stderr)
        return 1
    settings = get_settings()
    if not settings.deepseek_api_key.get_secret_value():
        print("未配置 DEEPSEEK_API_KEY，无法运行评估", file=sys.stderr)
        return 1
    if args.model:
        settings = settings.model_copy(update={"deepseek_model": args.model})
    # 与生产一致：理解模块使用 temperature=0（见 main.py）。
    llm = create_chat_model(settings, temperature=args.temperature)
    total = len(cases) * args.repeat
    done = 0

    def progress(record) -> None:
        nonlocal done
        done += 1
        mark = "✓" if record.passed else ("!" if record.status == "error" else "✗")
        print(f"\r[{done}/{total}] {mark} {record.case_id:<24}", end="", file=sys.stderr, flush=True)

    started = datetime.now()
    meta = {
        "started_at": started.isoformat(timespec="seconds"),
        "model": settings.deepseek_model,
        "temperature": args.temperature,
        "thinking": settings.deepseek_thinking_enabled,
        "prompt_fingerprint": _prompt_fingerprint(),
        "git_commit": _git_commit(),
        "filters": {k: getattr(args, k) for k in ("category", "tag", "case", "limit")},
    }
    records = asyncio.run(run_cases(cases, llm, repeat=args.repeat, concurrency=args.concurrency, on_done=progress))
    out_dir = args.out or RESULTS_DIR / f"intent-{started:%Y%m%d-%H%M%S}"
    summary = _write_outputs(out_dir, cases, records, meta)
    _print_summary(summary, out_dir)
    threshold = args.fail_under
    if threshold is not None and (summary["overall"]["run_pass_rate"] or 0) < threshold:
        print(f"单次通过率低于阈值 {threshold:.0%}", file=sys.stderr)
        return 2
    return 0


def cmd_regrade(args: argparse.Namespace) -> int:
    """用当前标注重新判分已有输出，结果写回原目录。"""
    from evals.intent.runner import load_records, regrade

    cases = load_cases()
    meta = json.loads((args.result_dir / "summary.json").read_text(encoding="utf-8"))["meta"]
    records = regrade(cases, load_records(args.result_dir / "results.jsonl"))
    ran = {r.case_id for r in records}
    cases = [c for c in cases if c.id in ran]
    meta["regraded_at"] = datetime.now().isoformat(timespec="seconds")
    summary = _write_outputs(args.result_dir, cases, records, meta)
    _print_summary(summary, args.result_dir)
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    """对比两次运行。"""
    from evals.intent.report import render_compare

    base = json.loads((args.base / "summary.json").read_text(encoding="utf-8"))
    head = json.loads((args.head / "summary.json").read_text(encoding="utf-8"))
    text = render_compare(base, head)
    (args.head / f"compare-{args.base.name}.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    # understand_node 每次调用都会写 INFO 日志，评估时只保留告警以上。
    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    parser = argparse.ArgumentParser(prog="python -m evals.intent", description="意图理解评估")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate", help="校验评估集")

    run = sub.add_parser("run", help="调用真实模型运行评估")
    run.add_argument("--repeat", type=int, default=1, help="每条用例重复次数，>1 时统计一致性")
    run.add_argument("--concurrency", type=int, default=4, help="并发调用数")
    run.add_argument("--temperature", type=float, default=0, help="采样温度，默认与生产一致为 0")
    run.add_argument("--model", help="覆盖 DEEPSEEK_MODEL")
    run.add_argument("--category", action="append", help="只跑某类别，可重复")
    run.add_argument("--tag", action="append", help="只跑带某标签的用例，可重复")
    run.add_argument("--case", action="append", help="只跑某用例 ID，可重复")
    run.add_argument("--limit", type=int, help="最多跑前 N 条")
    run.add_argument("--out", type=Path, help="结果目录，默认 evals/results/intent-<时间>")
    run.add_argument("--fail-under", type=float, help="单次通过率低于该值（0～1）时退出码为 2，用于 CI")

    regrade_parser = sub.add_parser("regrade", help="修改标注后重新判分")
    regrade_parser.add_argument("result_dir", type=Path)

    compare = sub.add_parser("compare", help="对比两次运行")
    compare.add_argument("base", type=Path)
    compare.add_argument("head", type=Path)

    args = parser.parse_args(argv)
    handlers = {"validate": cmd_validate, "run": cmd_run, "regrade": cmd_regrade, "compare": cmd_compare}
    try:
        return handlers[args.command](args)
    except DatasetError as exc:
        print(f"评估集有错误：\n{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
