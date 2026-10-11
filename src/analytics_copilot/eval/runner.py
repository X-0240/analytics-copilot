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
    """跑单条任务（in-process，不走 HTTP，评测不需要网络）。

    会模拟澄清对话：最多 2 轮澄清，之后按冻结稿规则"带显式假设继续"。
    因此最终状态只可能是 answered / refused / error，
    `clarifications_asked` 单独记录"是否问过"，用于判边界案例行为。
    """
    session = {"clarify_rounds": 0, "history": []}
    asked = 0
    outcome = None
    for _ in range(3):
        try:
            outcome = agent.analyze(task["question"], session)
            break
        except agent.ClarifyNeeded:
            asked += 1
            continue
        except agent.Refused as exc:
            return {
                "task_id": task["task_id"],
                "kind": task.get("kind"),
                "status": "refused",
                "clarifications_asked": asked,
                "reason": exc.reason,
            }
        except Exception as exc:
            return {
                "task_id": task["task_id"],
                "kind": task.get("kind"),
                "status": "error",
                "error": type(exc).__name__,
            }
    if outcome is None:
        return {
            "task_id": task["task_id"],
            "kind": task.get("kind"),
            "status": "error",
            "error": "CLARIFY_LOOP",
        }
    conclusion = outcome["conclusion"]
    with_evidence = [
        item
        for item in conclusion.evidence
        if item.get("sql") and item.get("row_count")
    ]
    return {
        "task_id": task["task_id"],
        "kind": task.get("kind"),
        "status": "answered",
        "clarifications_asked": asked,
        "mode": outcome.get("mode", "rule"),
        "claim": conclusion.claim,
        "confidence": conclusion.confidence,
        "caveats": len(conclusion.caveats),
        "evidence_steps": len(conclusion.evidence),
        "steps_with_sql_and_rows": len(with_evidence),
        "trace_complete": len(with_evidence) > 0 and len(with_evidence) == len(
            conclusion.evidence
        ),
        "plan_steps": len((outcome.get("plan") or {}).get("steps", [])),
        "cited_steps": outcome.get("cited_steps", []),
        "cost_cny": outcome.get("cost_cny", 0.0),
    }


def summarize(rows):
    """汇总：完成率、追溯链完整率，以及 M1 验收项完成情况。"""
    total = len(rows)
    answered = [row for row in rows if row["status"] == "answered"]
    complete = [row for row in answered if row.get("trace_complete")]
    return {
        "tasks": total,
        "answered": len(answered),
        "asked_clarification": len(
            [row for row in rows if row.get("clarifications_asked")]
        ),
        "refused": len([row for row in rows if row["status"] == "refused"]),
        "errors": len([row for row in rows if row["status"] == "error"]),
        "answer_rate": round(len(answered) / total, 4) if total else 0.0,
        "trace_complete_rate": round(len(complete) / total, 4) if total else 0.0,
        "plan_present_rate": round(
            len([row for row in answered if row.get("plan_steps")]) / total, 4
        )
        if total
        else 0.0,
        "cost_cny_total": round(
            sum(row.get("cost_cny") or 0.0 for row in rows), 6
        ),
        "acceptance": _acceptance(rows),
        "todo_metrics": [
            "结论支持率",
            "误判率(holdout)",
            "边界降级正确率",
            "无验证层/有验证层对照",
        ],
    }


def _acceptance(rows):
    """M1 验收项：≥8 正常分析、≥4 澄清/拒答、结论含计划与证据链。"""
    normal = [row for row in rows if row.get("kind") == "normal"]
    boundary = [row for row in rows if row.get("kind") in ("clarify", "refuse")]
    normal_ok = [
        row
        for row in normal
        if row["status"] == "answered"
        and row.get("plan_steps")
        and row.get("trace_complete")
    ]
    clarify_ok = [
        row
        for row in boundary
        if row.get("status") == "refused" or row.get("clarifications_asked")
    ]
    return {
        "normal_total": len(normal),
        "normal_ok": len(normal_ok),
        "normal_pass": len(normal_ok) >= 8,
        "boundary_total": len(boundary),
        "boundary_ok": len(clarify_ok),
        "boundary_pass": len(clarify_ok) >= 4,
        "refused_expected": len(
            [row for row in rows if row.get("kind") == "refuse"]
        ),
        "refused_actual": len(
            [row for row in rows if row.get("kind") == "refuse"
             and row["status"] == "refused"]
        ),
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
