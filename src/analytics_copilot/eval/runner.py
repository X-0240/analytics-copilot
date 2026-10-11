"""评测 runner 骨架：跑一批任务，统计当前**机械可算**的部分。

M0 只算两件不依赖判定规则的事：
  1. 端到端完成率（跑通/澄清/报错各多少）；
  2. 追溯链完整率雏形：结论是否带证据、证据是否带 SQL 与结果行。
其余指标（支持率、误判率、边界降级）需要先冻结判定规则，留给 M3。
"""

import argparse
import json
import sys
import uuid
from pathlib import Path

from analytics_copilot import agent


def run_task(task):
    """跑单条任务（in-process，不走 HTTP，评测不需要网络）。"""
    session = {"clarify_rounds": 0, "history": []}
    try:
        outcome = agent.analyze(task["question"], session)
    except agent.ClarifyNeeded:
        return {"task_id": task["task_id"], "status": "need_clarification"}
    except Exception as exc:
        return {
            "task_id": task["task_id"],
            "status": "error",
            "error": type(exc).__name__,
        }
    conclusion = outcome["conclusion"]
    with_evidence = [
        item
        for item in conclusion.evidence
        if item.get("sql") and item.get("row_count")
    ]
    return {
        "task_id": task["task_id"],
        "status": "answered",
        "claim": conclusion.claim,
        "confidence": conclusion.confidence,
        "caveats": len(conclusion.caveats),
        "evidence_steps": len(conclusion.evidence),
        "steps_with_sql_and_rows": len(with_evidence),
        "trace_complete": len(with_evidence) > 0 and len(with_evidence) == len(
            conclusion.evidence
        ),
    }


def summarize(rows):
    """汇总：完成率与追溯链完整率（雏形）。"""
    total = len(rows)
    answered = [row for row in rows if row["status"] == "answered"]
    complete = [row for row in answered if row.get("trace_complete")]
    return {
        "tasks": total,
        "answered": len(answered),
        "need_clarification": len(
            [row for row in rows if row["status"] == "need_clarification"]
        ),
        "errors": len([row for row in rows if row["status"] == "error"]),
        "answer_rate": round(len(answered) / total, 4) if total else 0.0,
        "trace_complete_rate": round(len(complete) / total, 4) if total else 0.0,
        "todo_metrics": [
            "结论支持率",
            "误判率(holdout)",
            "边界降级正确率",
            "无验证层/有验证层对照",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(prog="eval.runner")
    parser.add_argument("--tasks", required=True)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    payload = json.loads(Path(args.tasks).read_text(encoding="utf-8"))
    rows = [run_task(task) for task in payload["tasks"]]
    summary = summarize(rows)
    summary["run_id"] = uuid.uuid4().hex[:8]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "results.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
            encoding="utf-8",
        )
        (out_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
